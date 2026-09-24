"""Official annual-report material acquisition with source-specific routing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from .config import DATA_DIR, KNOWLEDGE_DIR
from .rag import LocalResearchRAG

MANIFEST_PATH = DATA_DIR / "materials_manifest.json"
SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCHIVE_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
SEC_ARCHIVE_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"
SEC_SOURCE = "SEC EDGAR"
CNINFO_SOURCE = "\u5de8\u6f6e\u8d44\u8baf"
HKEX_SOURCE = "\u62ab\u9732\u6613"
CNINFO_TOP_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCEMENT_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_URL = "http://static.cninfo.com.cn/"
HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
HKEX_TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_REFERER = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=EN"
HKEX_COMPANY_NAMES = {"0700.HK": "Tencent Holdings"}
MIN_REQUEST_INTERVAL_SECONDS = 2.0
MAX_RETRIES = 1


class MaterialFetchError(RuntimeError):
    """Raised when an official material cannot be acquired and validated."""


def route_source(ticker: str) -> str:
    """Route a market ticker to its official disclosure system."""
    normalized = ticker.upper().strip()
    if normalized.endswith(".HK"):
        return HKEX_SOURCE
    if normalized.endswith(".SS") or normalized.endswith(".SZ") or re.fullmatch(r"\d{6}", normalized):
        return CNINFO_SOURCE
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", normalized):
        return SEC_SOURCE
    raise MaterialFetchError(f"\u65e0\u6cd5\u8bc6\u522b ticker {normalized} \u7684\u5b98\u65b9\u4fe1\u6e90\u8def\u7531\u3002")


class OfficialMaterialFetcher:
    """Fetch one annual report from the routed official source with explicit pacing and failures."""

    def __init__(
        self,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.clock = clock
        self._last_request_at: dict[str, float] = {}

    def _headers(self, host: str) -> dict[str, str]:
        contact = os.getenv("SEC_EDGAR_USER_AGENT", "InvestmentAssistant contact@example.com")
        return {"User-Agent": contact, "Accept-Encoding": "gzip, deflate", "Host": host}

    def _wait_for_source(self, source: str) -> None:
        previous = self._last_request_at.get(source)
        if previous is not None:
            wait_seconds = MIN_REQUEST_INTERVAL_SECONDS - (self.clock() - previous)
            if wait_seconds > 0:
                self.sleeper(wait_seconds)
        self._last_request_at[source] = self.clock()

    def _get(self, url: str, source: str, host: str) -> requests.Response:
        errors: list[str] = []
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_source(source)
            try:
                response = self.session.get(url, headers=self._headers(host), timeout=30)
                if response.status_code >= 400:
                    raise MaterialFetchError(f"HTTP {response.status_code}")
                return response
            except Exception as exc:
                errors.append(f"\u7b2c {attempt + 1} \u6b21\u8bf7\u6c42\uff1a{type(exc).__name__}: {exc}")
        raise MaterialFetchError(
            f"{source} \u8bf7\u6c42\u5931\u8d25\uff08\u6700\u591a\u91cd\u8bd5 {MAX_RETRIES} \u6b21\uff09\uff1a{'\uFF1B'.join(errors)}"
        )

    def _source_headers(self, source: str, host: str) -> dict[str, str]:
        headers = self._headers(host)
        if source == CNINFO_SOURCE:
            headers["Referer"] = "http://www.cninfo.com.cn/new/index"
        elif source == HKEX_SOURCE:
            headers["Referer"] = HKEX_REFERER
        return headers

    def _looks_like_html(self, response: requests.Response) -> bool:
        content_type = str(getattr(response, "headers", {}).get("Content-Type") or "").lower()
        content = bytes(getattr(response, "content", b"") or b"").lstrip().lower()
        return "text/html" in content_type or content.startswith(b"<!doctype html") or content.startswith(b"<html")

    def _anti_bot_error(self, source: str, detail: str) -> MaterialFetchError:
        return MaterialFetchError(f"{source} \u7591\u4f3c\u88ab\u53cd\u722c\u62e6\u622a\uff1a{detail}")

    def _post(self, url: str, source: str, host: str, data: dict[str, str]) -> requests.Response:
        errors: list[str] = []
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_source(source)
            try:
                response = self.session.post(url, headers=self._source_headers(source, host), data=data, timeout=30)
                if response.status_code >= 400:
                    raise MaterialFetchError(f"HTTP {response.status_code}")
                if self._looks_like_html(response):
                    raise self._anti_bot_error(source, f"POST {url} \u8fd4\u56de HTML \u800c\u975e JSON")
                return response
            except Exception as exc:
                errors.append(f"\u7b2c {attempt + 1} \u6b21\u8bf7\u6c42\uff1a{type(exc).__name__}: {exc}")
        joined_errors = "\uFF1B".join(errors)
        raise MaterialFetchError(f"{source} \u8bf7\u6c42\u5931\u8d25\uff08\u6700\u591a\u91cd\u8bd5 {MAX_RETRIES} \u6b21\uff09\uff1a{joined_errors}")

    def _post_json(self, url: str, source: str, host: str, data: dict[str, str]) -> Any:
        response = self._post(url, source, host, data)
        try:
            return response.json()
        except ValueError as exc:
            raise self._anti_bot_error(source, f"POST {url} \u8fd4\u56de\u7684\u4e0d\u662f JSON\uff1a{type(exc).__name__}: {exc}") from exc

    def _source_get(self, url: str, source: str, host: str, params: dict[str, str] | None = None) -> requests.Response:
        errors: list[str] = []
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_source(source)
            try:
                response = self.session.get(url, headers=self._source_headers(source, host), params=params, timeout=30)
                if response.status_code >= 400:
                    raise MaterialFetchError(f"HTTP {response.status_code}")
                return response
            except Exception as exc:
                errors.append(f"\u7b2c {attempt + 1} \u6b21\u8bf7\u6c42\uff1a{type(exc).__name__}: {exc}")
        joined_errors = "\uFF1B".join(errors)
        raise MaterialFetchError(f"{source} \u8bf7\u6c42\u5931\u8d25\uff08\u6700\u591a\u91cd\u8bd5 {MAX_RETRIES} \u6b21\uff09\uff1a{joined_errors}")

    def _get_official_pdf(self, url: str, source: str, host: str) -> bytes:
        response = self._source_get(url, source, host)
        content = bytes(response.content or b"")
        if self._looks_like_html(response) or not content.startswith(b"%PDF"):
            raise self._anti_bot_error(source, f"PDF \u8bf7\u6c42\u8fd4\u56de HTML \u6216\u975e PDF \u5185\u5bb9\uff1a{url}")
        return content

    def _store_native_pdf(
        self,
        ticker: str,
        source: str,
        source_url: str,
        filename: str,
        filing_date: str | None = None,
        report_date: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        destination = KNOWLEDGE_DIR / filename
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            host = "static.cninfo.com.cn" if source == CNINFO_SOURCE else "www1.hkexnews.hk"
            temporary.write_bytes(self._get_official_pdf(source_url, source, host))
            checks = self._validate_pdf(temporary)
            temporary.replace(destination)
            entry = {
                "ticker": ticker,
                "source": source,
                "source_url": source_url,
                "downloaded_at": datetime.now(UTC).isoformat(),
                "filing_form": "annual_report",
                "filing_date": filing_date,
                "report_date": report_date,
                "material_kind": "official_pdf",
                "page_authority": "official",
                "file_name": destination.name,
                "validation": checks,
                **(extra or {}),
            }
            self._write_manifest(ticker, entry)
            indexed_chunks = LocalResearchRAG().index_local_documents()
            return {**entry, "path": str(destination), "indexed_chunks": indexed_chunks}
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _cninfo_org_id(self, code: str) -> str:
        payload = self._post_json(
            CNINFO_TOP_SEARCH_URL,
            CNINFO_SOURCE,
            "www.cninfo.com.cn",
            {"keyWord": code, "maxNum": "10"},
        )
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = payload.get("keyBoardList") or payload.get("results") or payload.get("data") or []
        else:
            raise self._anti_bot_error(CNINFO_SOURCE, "topSearch \u8fd4\u56de JSON \u7ed3\u6784\u5f02\u5e38")
        for item in candidates:
            if str(item.get("code") or item.get("secCode") or "") == code:
                org_id = item.get("orgId")
                if org_id:
                    return str(org_id)
        for item in candidates:
            if item.get("orgId"):
                return str(item["orgId"])
        raise MaterialFetchError(f"{CNINFO_SOURCE} \u672a\u627e\u5230\u80a1\u7968\u4ee3\u7801 {code} \u7684 orgId\u3002")

    def _cninfo_annual_announcement(self, code: str, org_id: str) -> dict[str, Any]:
        column = "sse" if code.startswith("6") else "szse"
        payload = self._post_json(
            CNINFO_ANNOUNCEMENT_URL,
            CNINFO_SOURCE,
            "www.cninfo.com.cn",
            {
                "stock": f"{code},{org_id}",
                "column": column,
                "category": "category_ndbg_szsh",
                "pageSize": "30",
                "tabName": "fulltext",
            },
        )
        announcements = payload.get("announcements") or []
        for item in announcements:
            if item.get("adjunctUrl"):
                return item
        raise MaterialFetchError(f"{CNINFO_SOURCE} \u672a\u627e\u5230 {code} \u7684\u5e74\u62a5 PDF \u516c\u544a\u3002")

    def fetch_cninfo_annual_pdf(self, ticker: str) -> dict[str, Any]:
        normalized = ticker.upper().strip()
        code = normalized.split(".", maxsplit=1)[0]
        org_id = self._cninfo_org_id(code)
        announcement = self._cninfo_annual_announcement(code, org_id)
        adjunct_url = str(announcement["adjunctUrl"]).lstrip("/")
        source_url = f"{CNINFO_STATIC_URL}{adjunct_url}"
        title = str(announcement.get("announcementTitle") or "")
        year_match = re.search(r"(20\d{2})", title) or re.search(r"(20\d{2})", adjunct_url)
        report_year = year_match.group(1) if year_match else "latest"
        return self._store_native_pdf(
            normalized,
            CNINFO_SOURCE,
            source_url,
            f"{code}_{report_year}_annual_report.pdf",
            filing_date=str(announcement.get("announcementTime") or ""),
            report_date=report_year,
            extra={"org_id": org_id, "announcement_title": title, "column": "sse" if code.startswith("6") else "szse"},
        )

    def _hkex_jsonp(self, response: requests.Response) -> Any:
        if self._looks_like_html(response):
            raise self._anti_bot_error(HKEX_SOURCE, "prefix \u8bf7\u6c42\u8fd4\u56de HTML \u800c\u975e JSONP")
        text = bytes(response.content or b"").decode("utf-8", errors="replace").strip()
        match = re.match(r"^[A-Za-z_$][\w$]*\((.*)\)\s*;?\s*$", text, flags=re.DOTALL)
        if not match:
            raise self._anti_bot_error(HKEX_SOURCE, "prefix \u8fd4\u56de\u4e0d\u662f\u9884\u671f JSONP")
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise self._anti_bot_error(HKEX_SOURCE, f"prefix JSONP \u89e3\u6790\u5931\u8d25\uff1a{exc}") from exc

    def _find_stock_id(self, value: Any) -> str | None:
        if isinstance(value, dict):
            if value.get("stockId"):
                return str(value["stockId"])
            for item in value.values():
                found = self._find_stock_id(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_stock_id(item)
                if found:
                    return found
        return None

    def _hkex_stock_id(self, ticker: str) -> str:
        name = HKEX_COMPANY_NAMES.get(ticker)
        if not name:
            raise MaterialFetchError(f"{HKEX_SOURCE} \u672a\u914d\u7f6e ticker {ticker} \u7684\u516c\u53f8\u540d\u79f0\u67e5\u8be2\u53c2\u6570\u3002")
        response = self._source_get(
            HKEX_PREFIX_URL,
            HKEX_SOURCE,
            "www1.hkexnews.hk",
            {"callback": "callback", "lang": "EN", "type": "A", "name": name, "market": "SEHK"},
        )
        stock_id = self._find_stock_id(self._hkex_jsonp(response))
        if not stock_id:
            raise MaterialFetchError(f"{HKEX_SOURCE} \u672a\u5728 prefix \u7ed3\u679c\u4e2d\u627e\u5230 {ticker} \u7684 stockId\u3002")
        return stock_id

    def _hkex_annual_result(self, stock_id: str) -> dict[str, Any]:
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": "",
            "toDate": "",
            "title": "Annual Report",
            "searchType": "1",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": "100",
            "lang": "EN",
        }
        response = self._source_get(HKEX_TITLE_SEARCH_URL, HKEX_SOURCE, "www1.hkexnews.hk", params)
        if self._looks_like_html(response):
            raise self._anti_bot_error(HKEX_SOURCE, "titleSearch \u8fd4\u56de HTML \u800c\u975e JSON")
        try:
            outer = response.json()
            result = outer.get("result") if isinstance(outer, dict) else None
            records = json.loads(result) if isinstance(result, str) else result
        except (ValueError, json.JSONDecodeError) as exc:
            raise self._anti_bot_error(HKEX_SOURCE, f"titleSearch JSON \u89e3\u6790\u5931\u8d25\uff1a{type(exc).__name__}: {exc}") from exc
        if isinstance(records, dict):
            records = records.get("result") or records.get("data") or []
        for item in records or []:
            if item.get("FILE_LINK"):
                return item
        raise MaterialFetchError(f"{HKEX_SOURCE} \u672a\u627e\u5230 Annual Report PDF\u3002")

    def fetch_hkex_annual_pdf(self, ticker: str) -> dict[str, Any]:
        normalized = ticker.upper().strip()
        stock_id = self._hkex_stock_id(normalized)
        item = self._hkex_annual_result(stock_id)
        file_link = str(item["FILE_LINK"])
        source_url = f"https://www1.hkexnews.hk{file_link}"
        title = str(item.get("TITLE") or item.get("TITLE_EN") or "")
        year_match = re.search(r"(20\d{2})", title) or re.search(r"(20\d{2})", file_link)
        report_year = year_match.group(1) if year_match else "latest"
        code = normalized.split(".", maxsplit=1)[0]
        return self._store_native_pdf(
            normalized,
            HKEX_SOURCE,
            source_url,
            f"{code}HK_annual_report_{report_year}.pdf",
            filing_date=str(item.get("DATE_TIME") or item.get("DateTime") or ""),
            report_date=report_year,
            extra={"stock_id": stock_id, "announcement_title": title},
        )

    def _get_json(self, url: str, source: str, host: str) -> dict[str, Any]:
        response = self._get(url, source, host)
        try:
            return response.json()
        except ValueError as exc:
            raise MaterialFetchError(
                f"{source} \u8fd4\u56de\u7684\u4e0d\u662f\u6709\u6548 JSON\uff1a{url}\uff1b{type(exc).__name__}: {exc}"
            ) from exc

    def _sec_cik(self, ticker: str) -> int:
        records = self._get_json(SEC_TICKER_URL, SEC_SOURCE, "www.sec.gov")
        normalized = ticker.upper().strip()
        for item in records.values():
            if str(item.get("ticker") or "").upper() == normalized:
                return int(item["cik_str"])
        raise MaterialFetchError(f"{SEC_SOURCE} \u672a\u627e\u5230 ticker {normalized} \u7684 CIK\u3002")

    def _latest_10k(self, cik: int) -> dict[str, str]:
        data = self._get_json(SEC_SUBMISSIONS_URL.format(cik=cik), SEC_SOURCE, "data.sec.gov")
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        for index, form in enumerate(forms):
            if form == "10-K":
                values = {
                    "accession": str((recent.get("accessionNumber") or [])[index]),
                    "filing_date": str((recent.get("filingDate") or [])[index]),
                    "report_date": str((recent.get("reportDate") or [])[index]),
                    "primary_document": str((recent.get("primaryDocument") or [])[index]),
                }
                if values["accession"] and values["primary_document"]:
                    return values
        raise MaterialFetchError(f"{SEC_SOURCE} \u672a\u627e\u5230 CIK {cik:010d} \u7684\u6700\u65b0 10-K\u3002")

    def _official_pdf_name(self, cik: int, accession: str) -> str | None:
        accession_compact = accession.replace("-", "")
        index_url = SEC_ARCHIVE_INDEX_URL.format(cik=cik, accession=accession_compact)
        data = self._get_json(index_url, SEC_SOURCE, "www.sec.gov")
        files = ((data.get("directory") or {}).get("item") or [])
        pdfs = [str(item.get("name")) for item in files if str(item.get("name") or "").lower().endswith(".pdf")]
        return pdfs[0] if pdfs else None

    def _html_to_pdf(self, html: bytes, destination: Path, title: str) -> None:
        """Create a searchable local PDF from an official SEC HTML filing when SEC has no PDF rendition."""
        text = BeautifulSoup(html, "lxml").get_text("\n")
        lines = [" ".join(line.split()) for line in text.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            raise MaterialFetchError("\u5b98\u65b9 SEC HTML \u7533\u62a5\u4e0d\u542b\u53ef\u63d0\u53d6\u6587\u672c\uff0c\u65e0\u6cd5\u751f\u6210\u6587\u5b57\u5c42 PDF\u3002")
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=A4, pageCompression=1)
        width, height = A4
        margin = 40
        font_name = "Helvetica"
        font_size = 8
        leading = 11
        y = height - margin
        canvas.setTitle(title)
        canvas.setFont(font_name, font_size)
        for line in lines:
            words = line.split()
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if stringWidth(candidate, font_name, font_size) <= width - margin * 2:
                    current = candidate
                    continue
                canvas.drawString(margin, y, current)
                y -= leading
                if y < margin:
                    canvas.showPage()
                    canvas.setFont(font_name, font_size)
                    y = height - margin
                current = word
            if current:
                canvas.drawString(margin, y, current)
                y -= leading
                if y < margin:
                    canvas.showPage()
                    canvas.setFont(font_name, font_size)
                    y = height - margin
        canvas.save()
        destination.write_bytes(buffer.getvalue())

    def _validate_pdf(self, path: Path) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size <= 0:
            raise MaterialFetchError("\u4e0b\u8f7d\u6587\u4ef6\u4e3a\u7a7a\u6216\u4e0d\u5b58\u5728\u3002")
        try:
            reader = PdfReader(str(path))
            page_count = len(reader.pages)
            if page_count <= 0:
                raise MaterialFetchError("PDF \u9875\u6570\u4e3a 0\u3002")
            text_pages = sum(1 for page in reader.pages if (page.extract_text(extraction_mode="layout") or "").strip())
            if text_pages <= 0:
                raise MaterialFetchError("PDF \u4e0d\u542b\u53ef\u63d0\u53d6\u6587\u5b57\u5c42\u3002")
        except MaterialFetchError:
            raise
        except Exception as exc:
            raise MaterialFetchError(f"PDF \u6821\u9a8c\u5931\u8d25\uff1a{type(exc).__name__}: {exc}") from exc
        return {
            "file_size_bytes": path.stat().st_size,
            "page_count": page_count,
            "text_page_count": text_pages,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _write_manifest(self, ticker: str, entry: dict[str, Any]) -> None:
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig")) if MANIFEST_PATH.exists() else {}
        except json.JSONDecodeError as exc:
            raise MaterialFetchError(f"\u6750\u6599\u6e05\u5355\u683c\u5f0f\u65e0\u6548\uff1a{MANIFEST_PATH}\uff1b{type(exc).__name__}: {exc}") from exc
        manifest[ticker] = entry
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def fetch_sec_10k_pdf(self, ticker: str) -> dict[str, Any]:
        normalized = ticker.upper().strip()
        cik = self._sec_cik(normalized)
        filing = self._latest_10k(cik)
        accession_compact = filing["accession"].replace("-", "")
        official_pdf = self._official_pdf_name(cik, filing["accession"])
        if official_pdf:
            source_url = SEC_ARCHIVE_FILE_URL.format(cik=cik, accession=accession_compact, filename=official_pdf)
            material_kind = "official_pdf"
            file_suffix = official_pdf
        else:
            source_url = SEC_ARCHIVE_FILE_URL.format(cik=cik, accession=accession_compact, filename=filing["primary_document"])
            material_kind = "official_html_converted_to_pdf"
            file_suffix = "official-html.pdf"
        filename = f"{normalized}_SEC_10-K_{filing['report_date']}_{file_suffix}"
        destination = KNOWLEDGE_DIR / filename
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            response = self._get(source_url, SEC_SOURCE, "www.sec.gov")
            if material_kind == "official_pdf":
                temporary.write_bytes(response.content)
            else:
                self._html_to_pdf(response.content, temporary, f"{normalized} SEC 10-K {filing['report_date']}")
            checks = self._validate_pdf(temporary)
            temporary.replace(destination)
            entry = {
                "ticker": normalized,
                "source": SEC_SOURCE,
                "source_url": source_url,
                "downloaded_at": datetime.now(UTC).isoformat(),
                "filing_form": "10-K",
                "filing_date": filing["filing_date"],
                "report_date": filing["report_date"],
                "primary_document": filing["primary_document"],
                "material_kind": material_kind,
                "conversion_note": None if material_kind == "official_pdf" else "SEC archive has no PDF rendition; generated a searchable local PDF directly from the official SEC HTML filing.",
                "file_name": destination.name,
                "validation": checks,
            }
            self._write_manifest(normalized, entry)
            indexed_chunks = LocalResearchRAG().index_local_documents()
            return {**entry, "path": str(destination), "indexed_chunks": indexed_chunks}
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, MaterialFetchError):
                raise
            raise MaterialFetchError(f"{SEC_SOURCE} \u4e0b\u8f7d\u6216\u5165\u5e93\u5931\u8d25\uff1a{type(exc).__name__}: {exc}") from exc


def fetch_materials(ticker: str) -> dict[str, Any]:
    """Fetch one ticker independently from its routed official disclosure source."""
    source = route_source(ticker)
    fetcher = OfficialMaterialFetcher()
    if source == SEC_SOURCE:
        return fetcher.fetch_sec_10k_pdf(ticker)
    if source == CNINFO_SOURCE:
        return fetcher.fetch_cninfo_annual_pdf(ticker)
    if source == HKEX_SOURCE:
        return fetcher.fetch_hkex_annual_pdf(ticker)
    raise MaterialFetchError(f"{source} \u8def\u7531\u4e0d\u53ef\u7528\u3002")
