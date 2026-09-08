from pathlib import Path

from pypdf import PdfWriter

from investment_assistant.rag import LocalResearchRAG, infer_ticker_from_filename
from investment_assistant.safety import evaluate_retrieval


class FakePage:
    def extract_text(self, **kwargs):
        assert kwargs["extraction_mode"] == "layout"
        return "本页披露：自由现金流保持为正。"


class FakeReader:
    is_encrypted = False
    pages = [FakePage()]


def test_pdf_index_skips_blank_pages_without_creating_evidence(tmp_path: Path):
    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf_path.open("wb") as file:
        writer.write(file)

    rag = LocalResearchRAG(path=tmp_path / "chroma")
    assert rag.index_pdf(pdf_path) == 0


def test_pdf_index_preserves_page_metadata(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "annual_report.pdf"
    pdf_path.write_bytes(b"placeholder")
    monkeypatch.setattr("investment_assistant.rag.PdfReader", lambda _: FakeReader())
    rag = LocalResearchRAG(path=tmp_path / "chroma")
    assert rag.index_pdf(pdf_path) == 1
    source = rag.search("自由现金流", limit=1)[0]
    assert source["metadata"]["source_type"] == "pdf"
    assert source["metadata"]["page"] == "1"
    assert source["metadata"]["file_name"] == "annual_report.pdf"


def test_retrieval_evaluation_requires_pdf_page_number():
    sources = [{"content": "free cash flow", "metadata": {"source_type": "pdf", "page": ""}}]
    result = evaluate_retrieval("free cash flow", sources)
    assert not result["passed"]
    assert result["page_citation_coverage"] == 0.0


def test_retrieval_evaluation_measures_expected_evidence_recall():
    sources = [{"content": "Revenue increased and free cash flow was positive.", "metadata": {"source_type": "text", "page": ""}}]
    result = evaluate_retrieval("cash flow", sources, ["Revenue", "free cash flow"])
    assert result["passed"]
    assert result["expected_source_recall"] == 1.0


def test_local_file_index_infers_aapl_ticker_and_search_filters_by_ticker(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "Apple_cash_flow_note.md").write_text("Apple free cash flow note", encoding="utf-8")
    (knowledge / "600519_cash_flow_note.md").write_text("Kweichow Moutai cash flow note", encoding="utf-8")

    rag = LocalResearchRAG(path=tmp_path / "chroma")
    rag.index_local_documents(knowledge)

    aapl_sources = rag.search("cash flow", ticker="AAPL")
    assert aapl_sources
    assert all(source["metadata"]["ticker"] == "AAPL" for source in aapl_sources)
    assert rag.search("cash flow", ticker="600519.SS")
    assert rag.search("cash flow", ticker="0700.HK") == []


def test_filename_ticker_inference_uses_known_aliases_and_unknown_without_guessing():
    assert infer_ticker_from_filename(Path("Apple_2025_Form_10-K.pdf")) == "AAPL"
    assert infer_ticker_from_filename(Path("600519_annual_report.pdf")) == "600519.SS"
    assert infer_ticker_from_filename(Path("sector_research.pdf")) == "unknown"
