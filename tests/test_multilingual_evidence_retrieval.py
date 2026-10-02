from pathlib import Path

import pytest

from investment_assistant.multilingual_evidence_retrieval import (
    EvidenceUnit,
    MultilingualEvidenceRetriever,
    build_pdf_units,
    split_evidence_windows,
)


def test_windows_keep_line_boundaries_and_never_cross_page():
    windows = split_evidence_windows("表头 2025 2024\n收入 100 90\n成本 50 40\n利润 50 50", max_chars=80, overlap_lines=1)
    assert windows
    assert all(start >= 1 and end >= start for start, end, _ in windows)
    assert all("\n" in content or start == end for start, end, content in windows)
    assert any("收入 100 90" in content and "成本 50 40" in content for _, _, content in windows)


def test_build_pdf_units_binds_material_and_metadata():
    root = Path("data/knowledge_base")
    spec = {"file_name": "Apple_2025_Form_10-K.pdf", "sha256": "3eb270b22acb7d8d8e9c32a43dc221dec3345f8e4bca02755fdbc1ee16c823de"}
    units, pages = build_pdf_units(root / spec["file_name"], "AAPL", spec)
    assert len(pages) == 80
    first = next(unit for group in units.values() for unit in group)
    assert first.metadata()["ticker"] == "AAPL"
    assert first.metadata()["source_sha256"] == spec["sha256"]
    assert first.metadata()["page"] == first.page
    assert first.metadata()["line_start"] <= first.metadata()["line_end"]


def test_unknown_model_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="multilingual_model_unavailable"):
        MultilingualEvidenceRetriever(tmp_path, {}, model_name="not-a-local-model")


def test_pack_requires_same_page_and_adjacent_units():
    retriever = object.__new__(MultilingualEvidenceRetriever)
    retriever.pack_units = 3
    units = [EvidenceUnit("AAPL", "a.pdf", "a" * 64, 2, i, i + 1, i + 1, f"line-{i}") for i in range(4)]
    content, evidence = retriever._pack_page(units, {i: float(i) for i in range(4)})
    assert "line-3" in content and "line-2" in content and "line-1" in content
    assert all(item["metadata"]["page"] == 2 for item in evidence)
    assert [item["metadata"]["unit_index"] for item in evidence] == [1, 2, 3]


def test_corpus_stats_shape_without_model():
    retriever = object.__new__(MultilingualEvidenceRetriever)
    retriever.model_name = "test"
    retriever.max_chars = 420
    retriever.overlap_lines = 1
    retriever.pack_units = 3
    retriever.model = type("M", (), {"max_seq_length": 128})()
    retriever.units_by_ticker = {"AAPL": {1: [EvidenceUnit("AAPL", "a.pdf", "a" * 64, 1, 0, 1, 1, "x")]}}
    retriever.unit_list_by_ticker = {"AAPL": retriever.units_by_ticker["AAPL"][1]}
    assert retriever.corpus_stats()["total_units"] == 1
