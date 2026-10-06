"""按官方披露站点发现文档；显式选定候选后才下载入独立索引。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from pypdf import PdfReader

from .fetch_materials import (
    CNINFO_ANNOUNCEMENT_URL, CNINFO_SOURCE, HKEX_PREFIX_URL, HKEX_REFERER,
    HKEX_SOURCE, HKEX_TITLE_SEARCH_URL, SEC_ARCHIVE_FILE_URL,
    SEC_SOURCE, SEC_SUBMISSIONS_URL, OfficialMaterialFetcher,
)
from .official_document_ingestion import (
    DocumentIngestError, OfficialDocument, OfficialDocumentStore,
    _sha256, _source_name, _validate_url,
)

SEC_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "6-K"})
_CNINFO_CODE = re.compile(r"\d{6}\.(?:SS|SZ)")
_HKEX_CODE = re.compile(r"\d{4,5}\.HK")
_SEC_CODE = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")


def _candidate(source: str, ticker: str, title: str, source_url: str,
               filing_form: str, filing_date: str | None, report_date: str | None,
               **metadata: Any) -> dict[str, Any]:
    _validate_url(source, source_url)
    candidate_id = hashlib.sha256(f"{source}|{ticker}|{source_url}".encode()).hexdigest()[:24]
    return {"candidate_id": candidate_id, "source": source, "ticker": ticker,
            "title": title, "source_url": source_url, "filing_form": filing_form,
            "filing_date": filing_date, "report_date": report_date,
            "status": "discovered_unverified", "metadata": metadata}


class OfficialDocumentDiscovery:
    """复用既有源限流/重试接口，不复用自动写入正式 manifest 的抓取方法。"""

    def __init__(self, fetcher: OfficialMaterialFetcher | None = None) -> None:
        self.fetcher = fetcher or OfficialMaterialFetcher()

    def discover(self, source: str, ticker: str, *, year: str, limit: int = 20,
                 company_name: str | None = None, forms: set[str] | None = None) -> list[dict[str, Any]]:
        source = _source_name(source)
        ticker = str(ticker or "").upper().strip()
        if not re.fullmatch(r"20\d{2}", year) or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("discovery_year_or_limit_invalid")
        if source == "CNINFO":
            if not _CNINFO_CODE.fullmatch(ticker):
                raise ValueError("cninfo_ticker_invalid")
            return self._cninfo(ticker, year, limit)
        if source == "HKEX":
            if not _HKEX_CODE.fullmatch(ticker):
                raise ValueError("hkex_ticker_invalid")
            return self._hkex(ticker, year, limit, company_name)
        if not _SEC_CODE.fullmatch(ticker):
            raise ValueError("sec_ticker_invalid")
        if not os.getenv("SEC_EDGAR_USER_AGENT"):
            raise DocumentIngestError("sec_contact_user_agent_required")
        selected = SEC_FORMS if forms is None else set(forms)
        if not selected or not selected <= SEC_FORMS:
            raise ValueError("sec_forms_invalid")
        return self._sec(ticker, year, limit, selected)

    def _cninfo(self, ticker: str, year: str, limit: int) -> list[dict[str, Any]]:
        code = ticker.split(".")[0]
        org_id = self.fetcher._cninfo_org_id(code)
        payload = self.fetcher._post_json(
            CNINFO_ANNOUNCEMENT_URL, CNINFO_SOURCE, "www.cninfo.com.cn",
            {"stock": f"{code},{org_id}", "column": "sse" if ticker.endswith(".SS") else "szse",
             "tabName": "fulltext", "pageNum": "1", "pageSize": str(min(100, limit * 2)),
             "seDate": f"{year}-01-01~{year}-12-31"},
        )
        results = []
        for item in (payload.get("announcements") or []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or str(item.get("secCode") or code) != code:
                continue
            path = str(item.get("adjunctUrl") or "").lstrip("/")
            if not path:
                continue
            title = str(item.get("announcementTitle") or "公告")
            url = f"https://static.cninfo.com.cn/{path}"
            try:
                results.append(_candidate("CNINFO", ticker, title, url,
                                          "annual_report" if "年度报告" in title else "announcement",
                                          str(item.get("announcementTime") or "") or None, year,
                                          org_id=org_id, announcement_id=item.get("announcementId")))
            except DocumentIngestError:
                continue
            if len(results) >= limit:
                break
        return results

    def _hkex_stock_id(self, ticker: str, company_name: str | None) -> str:
        if company_name:
            if not 1 <= len(company_name) <= 100 or any(char in company_name for char in "<>\\/{}"):
                raise ValueError("hkex_company_name_invalid")
            name = company_name
        else:
            from .fetch_materials import HKEX_COMPANY_NAMES
            name = HKEX_COMPANY_NAMES.get(ticker)
        if not name:
            raise DocumentIngestError("hkex_company_name_required")
        response = self.fetcher._source_get(
            HKEX_PREFIX_URL, HKEX_SOURCE, "www1.hkexnews.hk",
            {"callback": "callback", "lang": "EN", "type": "A", "name": name, "market": "SEHK"},
        )
        payload = self.fetcher._hkex_jsonp(response)
        # prefix 结果中的 stockId 只作为候选；后续披露结果仍核对证券代码。
        stock_id = self.fetcher._find_stock_id(payload)
        if not stock_id or not stock_id.isdigit():
            raise DocumentIngestError("hkex_stock_id_unavailable")
        return stock_id

    def _hkex(self, ticker: str, year: str, limit: int, company_name: str | None) -> list[dict[str, Any]]:
        stock_id = self._hkex_stock_id(ticker, company_name)
        response = self.fetcher._source_get(
            HKEX_TITLE_SEARCH_URL, HKEX_SOURCE, "www1.hkexnews.hk",
            {"sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK",
             "stockId": stock_id, "documentType": "-1", "fromDate": "",
             "toDate": "", "title": "", "searchType": "1", "t2Gcode": "-2",
             "t2code": "-2", "rowRange": "100", "lang": "EN"},
        )
        if self.fetcher._looks_like_html(response):
            raise DocumentIngestError("hkex_discovery_html_blocked")
        payload = response.json()
        records = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(records, str):
            records = json.loads(records)
        if isinstance(records, dict):
            records = records.get("result") or records.get("data")
        results = []
        for item in records or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("FILE_LINK") or "")
            title = str(item.get("TITLE") or item.get("TITLE_EN") or "Announcement")
            if not path.startswith(f"/listedco/listconews/sehk/{year}/"):
                continue
            url = urljoin("https://www1.hkexnews.hk", path)
            try:
                results.append(_candidate("HKEX", ticker, title, url,
                                          "annual_report" if "annual report" in title.lower() else "announcement",
                                          str(item.get("DATE_TIME") or item.get("DateTime") or "") or None,
                                          year, stock_id=stock_id))
            except DocumentIngestError:
                continue
            if len(results) >= limit:
                break
        return results

    def _sec(self, ticker: str, year: str, limit: int, forms: set[str]) -> list[dict[str, Any]]:
        cik = self.fetcher._sec_cik(ticker)
        payload = self.fetcher._get_json(SEC_SUBMISSIONS_URL.format(cik=cik), SEC_SOURCE, "data.sec.gov")
        recent = (payload.get("filings") or {}).get("recent") or {}
        results = []
        for idx, form in enumerate(recent.get("form") or []):
            if form not in forms:
                continue
            try:
                accession = str(recent["accessionNumber"][idx]).replace("-", "")
                filename = str(recent["primaryDocument"][idx])
                filing_date = str(recent["filingDate"][idx])
                report_date = str(recent["reportDate"][idx])
            except (IndexError, KeyError):
                continue
            if not filing_date.startswith(year):
                continue
            url = SEC_ARCHIVE_FILE_URL.format(cik=cik, accession=accession, filename=filename)
            try:
                results.append(_candidate("SEC", ticker, f"{ticker} {form} {filing_date}", url,
                                          form, filing_date, report_date, cik=cik,
                                          accession=accession, primary_document=filename))
            except DocumentIngestError:
                continue
            if len(results) >= limit:
                break
        return results

    def download_and_ingest(self, *, source: str, ticker: str, year: str, candidate_id: str,
                            store: OfficialDocumentStore | None = None, company_name: str | None = None) -> dict[str, Any]:
        """提交所选候选 ID，重新发现并核对后下载；不接受任意 URL。"""
        candidates = self.discover(source, ticker, year=year, limit=100, company_name=company_name)
        matches = [item for item in candidates if item["candidate_id"] == candidate_id]
        if len(matches) != 1:
            raise DocumentIngestError("discovered_candidate_missing_or_ambiguous")
        candidate = matches[0]
        official_source = _source_name(candidate["source"])
        url = candidate["source_url"]
        response = self.fetcher._source_get(url, candidate["source"], _host(official_source))
        payload = bytes(response.content or b"")
        if not payload or len(payload) > 30 * 1024 * 1024:
            raise DocumentIngestError("downloaded_document_size_invalid")
        is_pdf = payload.startswith(b"%PDF")
        if not is_pdf and official_source != "SEC":
            raise DocumentIngestError("downloaded_document_not_pdf")
        if is_pdf and not url.lower().endswith(".pdf"):
            raise DocumentIngestError("official_document_format_mismatch")
        with tempfile.TemporaryDirectory(prefix="ia-official-download-") as directory:
            path = Path(directory) / "source.pdf"
            html_sha = None
            if is_pdf:
                path.write_bytes(payload)
            else:
                if b"<html" not in payload[:8192].lower() and b"<!doctype html" not in payload[:8192].lower():
                    raise DocumentIngestError("sec_primary_document_not_html")
                html_sha = hashlib.sha256(payload).hexdigest()
                self.fetcher._html_to_pdf(payload, path, candidate["title"])
            page_count = len(PdfReader(str(path)).pages)
            document = OfficialDocument(
                ticker=candidate["ticker"], source=official_source, source_url=url,
                title=candidate["title"], filing_form=candidate["filing_form"],
                filing_date=candidate["filing_date"], report_date=candidate["report_date"],
                file_path=path, source_sha256=_sha256(path), page_count=page_count,
                page_authority="official" if is_pdf else "generated",
            )
            result = (store or OfficialDocumentStore()).ingest(document)
            return {**result, "discovery_candidate_id": candidate_id,
                    "source_html_sha256": html_sha, "retrieval_status": "indexed_unverified"}


def _host(source: str) -> str:
    return {"CNINFO": "static.cninfo.com.cn", "HKEX": "www1.hkexnews.hk", "SEC": "www.sec.gov"}[source]
