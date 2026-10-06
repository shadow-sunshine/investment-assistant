from pathlib import Path
import hashlib
import json

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from investment_assistant.official_document_ingestion import (
    DocumentIngestError, OfficialDocument, OfficialDocumentStore,
    _records_from_structured, registered_document, search_official_documents,
)
from investment_assistant.official_document_sources import OfficialDocumentDiscovery


def _pdf(path: Path, text: str = "Official filing revenue 100") -> None:
    pdf = canvas.Canvas(str(path))
    pdf.drawString(50, 700, text)
    pdf.showPage()
    pdf.save()


class RecordingParser:
    def __init__(self, payload=None):
        self.payload = payload or {"pages": [{"page_idx": 0, "blocks": [
            {"type": "text", "content": "Revenue increased in the annual filing."},
            {"type": "table", "content": "<table><tr><td>Revenue</td><td>100</td></tr></table>"},
        ]}], "metadata": {"producer": {"version": "test-mineru"}}}
        self.calls = 0

    def parse(self, pdf, output_dir):
        self.calls += 1
        path = output_dir / "structured_content.json"
        path.write_text(json.dumps(self.payload), encoding="utf-8")
        return path


def _doc(tmp_path, source="SEC", ticker="MSFT", path=None):
    file = path or tmp_path / "original.pdf"
    if path is None:
        _pdf(file)
    sha = hashlib.sha256(file.read_bytes()).hexdigest()
    urls = {"SEC": "https://www.sec.gov/Archives/edgar/data/1/123/file.htm",
            "HKEX": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/notice.pdf",
            "CNINFO": "https://static.cninfo.com.cn/finalpage/2026-03-10/notice.PDF"}
    return OfficialDocument(ticker, source, urls[source], "Annual Filing", "annual_report", "2025", None,
                            file, sha, len(PdfReader(str(file)).pages), "generated" if source == "SEC" else "official")


@pytest.mark.parametrize("source,ticker", [("SEC", "MSFT"), ("HKEX", "0700.HK"), ("CNINFO", "300750.SZ")])
def test_official_store_indexes_three_levels_and_does_not_promote(source, ticker, tmp_path):
    parser = RecordingParser()
    store = OfficialDocumentStore(tmp_path / "store", parser)
    document = _doc(tmp_path, source, ticker)
    result = store.ingest(document)
    assert (result["page_count"], result["paragraph_count"], result["table_count"]) == (1, 1, 1)
    assert result["state"] == "indexed_unverified"
    records = [json.loads(line) for line in (store.root / result["document_id"] / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {record["level"] for record in records} == {"page", "paragraph", "table"}
    assert all(record["page"] == 1 and record["source_sha256"] == result["source_sha256"] for record in records)
    hits = search_official_documents("Revenue 100", ticker=ticker, store=store)
    assert hits and hits[0]["evidence_level"] == "indexed_official_unverified"
    assert search_official_documents("Revenue", ticker="AAPL", store=store) == []
    assert parser.calls == 1
    assert store.ingest(document)["document_id"] == result["document_id"]
    assert parser.calls == 1


def test_official_store_refuses_partial_page_results_and_source_sha_drift(tmp_path):
    doc = _doc(tmp_path)
    store = OfficialDocumentStore(tmp_path / "store", RecordingParser({"pages": []}))
    with pytest.raises(DocumentIngestError, match="page_coverage"):
        store.ingest(doc)
    assert store.list_documents() == []
    doc.file_path.write_bytes(doc.file_path.read_bytes() + b"drift")
    with pytest.raises(DocumentIngestError, match="sha256_drift"):
        OfficialDocumentStore(tmp_path / "store", RecordingParser()).ingest(doc)


def test_index_byte_drift_fails_closed(tmp_path):
    doc = _doc(tmp_path)
    store = OfficialDocumentStore(tmp_path / "store", RecordingParser())
    result = store.ingest(doc)
    index = store.root / result["document_id"] / "index.jsonl"
    index.write_text(index.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(DocumentIngestError, match="index.*|sha256_drift"):
        search_official_documents("Revenue", ticker="MSFT", store=store)


def test_registration_is_strict_and_source_url_cannot_be_injected(tmp_path):
    data, knowledge = tmp_path / "data", tmp_path / "knowledge"
    data.mkdir(); knowledge.mkdir()
    file = knowledge / "MSFT_annual.pdf"; _pdf(file)
    doc = _doc(tmp_path, path=file)
    entry = {"MSFT": {"ticker": "MSFT", "source": "SEC EDGAR", "source_url": doc.source_url,
                      "file_name": file.name, "filing_form": "10-K", "report_date": "2025-06-30",
                      "validation": {"sha256": doc.source_sha256, "page_count": 1}}}
    (data / "materials_manifest.json").write_text(json.dumps(entry), encoding="utf-8")
    assert registered_document("MSFT", data_dir=data, knowledge_dir=knowledge).source_sha256 == doc.source_sha256
    entry["MSFT"]["source_url"] = "https://not-sec.gov/Archives/edgar/data/1/123/file.htm"
    (data / "materials_manifest.json").write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(DocumentIngestError, match="official_url_not_allowed"):
        registered_document("MSFT", data_dir=data, knowledge_dir=knowledge)


class FakeFetcher:
    def _cninfo_org_id(self, code): return "orgID"
    def _post_json(self, url, source, host, params):
        return {"announcements": [{"secCode": "300750", "adjunctUrl": "finalpage/2026-05-01/notice.PDF",
                                   "announcementTitle": "公司公告", "announcementTime": 123}]}
    def _hkex_jsonp(self, response): return [{"stockId": "7609"}]
    def _find_stock_id(self, payload): return "7609"
    def _looks_like_html(self, response): return False
    def _source_get(self, url, source, host, params=None):
        if "prefix.do" in url: return type("Response", (), {"text": "callback([])"})()
        return type("Response", (), {"json": lambda self: {"result": json.dumps([
            {"FILE_LINK": "/listedco/listconews/sehk/2026/0409/notice.pdf", "TITLE": "Notice", "DATE_TIME": "09/04/2026"}])}})()
    def _sec_cik(self, ticker): return 789019
    def _get_json(self, url, source, host):
        return {"filings": {"recent": {"form": ["10-K", "8-K"],
                                        "accessionNumber": ["0001193125-26-323660", "0001193125-26-323661"],
                                        "primaryDocument": ["msft.htm", "msft8k.htm"],
                                        "filingDate": ["2026-07-29", "2026-08-01"],
                                        "reportDate": ["2026-06-30", "2026-08-01"]}}}


def test_discovery_handles_cninfo_hkex_sec_without_writing_manifests(monkeypatch):
    finder = OfficialDocumentDiscovery(FakeFetcher())
    cn = finder.discover("CNINFO", "300750.SZ", year="2026")
    hk = finder.discover("HKEX", "0700.HK", year="2026", company_name="Tencent")
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "InvestmentAssistant research contact@example.com")
    sec = finder.discover("SEC", "MSFT", year="2026")
    assert cn[0]["filing_form"] == "announcement"
    assert hk[0]["source"] == "HKEX"
    assert {item["filing_form"] for item in sec} == {"10-K", "8-K"}
    for collection in (cn, hk, sec):
        assert all(item["status"] == "discovered_unverified" for item in collection)
        assert all(len(item["candidate_id"]) == 24 for item in collection)


def test_candidate_id_must_exist_after_refetch(tmp_path, monkeypatch):
    finder = OfficialDocumentDiscovery(FakeFetcher())
    with pytest.raises(DocumentIngestError, match="candidate_missing"):
        finder.download_and_ingest(source="CNINFO", ticker="300750.SZ", year="2026", candidate_id="arbitrary")
