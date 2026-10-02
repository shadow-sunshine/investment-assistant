"""V2 评测只验证离线门槛，不自动切线上，且同文件同版本才算命中。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from investment_assistant import hybrid_v2_eval as ev


def test_goal_requires_every_quadrant_not_only_average():
    good = {"page_recall_at_4": .9, "verified_recall_at_4": .9, "top1": .75, "scoped_ticker_pollution": 0}
    result = {"metrics": good, "by_quadrant": {q: dict(good) for q in ev.QUADRANT_ORDER}}
    result["by_quadrant"]["en-zh"]["verified_recall_at_4"] = .5
    gate = ev.assess(result)
    assert not gate["offline_quality_passed"]
    assert gate["decision"] == "no_online_ab"
    assert "en-zh.verified" in gate["failures"][0]


def test_passing_small_sample_never_authorizes_online():
    good = {"page_recall_at_4": .9, "verified_recall_at_4": .9, "top1": .75, "scoped_ticker_pollution": 0}
    gate = ev.assess({"metrics": good, "by_quadrant": {q: good for q in ev.QUADRANT_ORDER}})
    assert gate["offline_quality_passed"]
    assert gate["online_switch"] == "not_authorized"


def test_target_requires_same_file_and_recorded_version():
    materials = {"AAPL": {"file_name": "expected.pdf", "sha256": "good"}}
    case = {"ticker": "AAPL", "target_pages": [4]}
    source = {"metadata": {"ticker": "AAPL", "file_name": "other.pdf", "page": "4"}}
    assert not ev.is_target(source, case, materials)
    source["metadata"].update(file_name="expected.pdf", source_sha256="bad")
    assert not ev.is_target(source, case, materials)
    source["metadata"]["source_sha256"] = "good"
    assert ev.is_target(source, case, materials)
    source["metadata"]["ticker"] = "MSFT"
    assert not ev.is_target(source, case, materials)


def test_code_byte_drift_blocks_without_indexing(tmp_path, monkeypatch):
    source = tmp_path / "policy.py"
    source.write_bytes(b"frozen code")
    monkeypatch.setattr(ev, "RETRIEVAL_SOURCE", source)
    monkeypatch.setattr(ev, "FROZEN_RETRIEVAL_SHA", ev.sha(source))
    source.write_bytes(b"frozen code ")
    monkeypatch.setattr(ev, "LocalResearchRAG", lambda **_: pytest.fail("indexed despite drift"))
    result = ev.run_experiment()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "retrieval_code_drift"
    assert result["results"] == {}


def test_gold_data_byte_drift_blocks_before_indexing(tmp_path, monkeypatch):
    source = tmp_path / "policy.py"
    source.write_bytes(b"frozen code")
    dataset = tmp_path / "holdout.json"
    dataset.write_bytes(b'{"cases": []}')
    monkeypatch.setattr(ev, "RETRIEVAL_SOURCE", source)
    monkeypatch.setattr(ev, "FROZEN_RETRIEVAL_SHA", ev.sha(source))
    monkeypatch.setattr(ev, "NEW_HOLDOUT", dataset)
    monkeypatch.setattr(ev, "FROZEN_HOLDOUT_SHA", ev.sha(dataset))
    dataset.write_bytes(dataset.read_bytes() + b" ")
    monkeypatch.setattr(ev, "LocalResearchRAG", lambda **_: pytest.fail("indexed despite drift"))
    result = ev.run_experiment()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "dataset_drift:new_holdout"


def test_page_hit_is_not_keyword_numeric_proof():
    case = {"id": "c", "quadrant": "en-en", "ticker": "AAPL", "target_pages": [4], "keywords": ["Revenue", "100"]}
    sources = [{"metadata": {"ticker": "AAPL", "file_name": "expected.pdf", "page": "4", "source_id": "s"}, "content": "Revenue details but no target number"}]
    row = ev.measure(case, sources, sources, {"AAPL": {"file_name": "expected.pdf", "sha256": "good"}}, .01)
    assert row["page_hit_at_4"] and not row["verified_hit_at_4"]


def test_wilson_interval_exposes_small_sample_uncertainty():
    lower, upper = ev.wilson(9, 10)
    assert lower < .80 and upper > .90


def test_numeric_anchor_is_complete_token_not_substring():
    assert not ev.evidence_keywords_match("Revenue 1100", ["Revenue", "100"])
    assert ev.evidence_keywords_match("Revenue 100 2025", ["Revenue", "100"])
    assert not ev.evidence_keywords_match("Expense 12,345.67", ["2,345.67"])
