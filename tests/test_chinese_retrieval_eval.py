"""中文语料评测的身份边界与防泄漏回归。"""
from __future__ import annotations

import json
from pathlib import Path

from investment_assistant.chinese_retrieval_eval import _is_target, _load_dataset
from investment_assistant.multilingual_evidence_retrieval import EvidenceUnit


ROOT = Path(__file__).resolve().parents[1]


def test_dataset_answers_are_not_in_questions():
    dataset = _load_dataset()
    assert all(case["answer_value"] not in case["question"] for case in dataset["cases"])


def test_target_requires_material_identity_not_page_only():
    dataset = _load_dataset()
    case = dataset["cases"][0]
    spec = dataset["materials"][case["ticker"]]
    spoofed = {
        "metadata": {
            "ticker": "000001.SZ",
            "file_name": spec["file_name"],
            "source_sha256": spec["sha256"],
            "page": case["target_pages"][0],
        }
    }
    assert not _is_target(spoofed, case, dataset)


def test_source_id_binds_ticker_file_and_digest():
    a = EvidenceUnit("300750.SZ", "a.pdf", "a" * 64, 1, 0, 1, 2, "x").metadata()
    b = EvidenceUnit("000001.SZ", "a.pdf", "a" * 64, 1, 0, 1, 2, "x").metadata()
    assert a["source_id"] != b["source_id"]
    assert "300750.SZ" in a["source_id"]
    assert "a" * 64 in a["source_id"]
