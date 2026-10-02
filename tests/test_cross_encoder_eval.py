"""同池评测协议和失败关闭护栏；不下载或加载真实模型。"""
import copy
import hashlib
from pathlib import Path

import pytest

from investment_assistant import cross_encoder_eval as ce
from investment_assistant.cross_encoder_ranking import candidate_fingerprint


@pytest.fixture
def protocol(tmp_path):
    materials = {"AAPL": {"file_name": "apple.pdf", "sha256": "a" * 64}}
    cases, rows = [], []
    for i, q in enumerate(ce.QUADRANT_ORDER):
        c = {"id": str(i), "question": f"revenue question {i}", "ticker": "AAPL",
             "quadrant": q, "target_pages": [5], "keywords": ["Revenue", "123"]}
        sources = [{"content": "Revenue 123", "metadata": {"ticker": "AAPL", "file_name": "apple.pdf",
                    "source_sha256": "a" * 64, "source_type": "pdf", "source_id": f"p{p}", "page": p}}
                   for p in range(1, 7)]
        cases.append(c)
        rows.append({"id": c["id"], "question": c["question"], "ticker": "AAPL", "quadrant": q,
                     "candidates": sources, "fingerprint": candidate_fingerprint(sources),
                     "retrieval_ms": 1.0, "full_union_count": 6})
    dataset = {"materials": materials, "cases": cases}
    snapshot = {"schema": "same_pool_cross_encoder_v1", "dataset_sha256": ce.FROZEN_HOLDOUT_SHA,
                "retrieval_sha256": ce.FROZEN_RETRIEVAL_SHA, "candidate_budget": 48, "top_k": 4,
                "materials": materials, "cases": rows}
    path = tmp_path / "snapshot.json"
    ce.write_json(path, snapshot)
    return dataset, snapshot, path


def test_snapshot_requires_independent_expected_sha(protocol):
    dataset, snapshot, path = protocol
    expected = ce.sha(path)
    assert ce.load_snapshot(path, expected, dataset) == snapshot
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="candidate_snapshot_drift"):
        ce.load_snapshot(path, expected, dataset)


@pytest.mark.parametrize("mutation,error", [
    (lambda s: s.update(top_k=5), "snapshot_protocol_drift"),
    (lambda s: s["cases"].pop(), "snapshot_case_count"),
    (lambda s: s["cases"][0].update(question="gold answer"), "snapshot_query_binding"),
    (lambda s: s["cases"][0]["candidates"][0].update(content="tampered"), "snapshot_candidate_binding"),
    (lambda s: s["cases"][0].update(retrieval_ms=True), "snapshot_invalid_latency"),
    (lambda s: s["cases"][0].update(candidates=[]), "snapshot_candidate_count"),
])
def test_snapshot_semantics_fail_even_if_caller_rehashes(protocol, mutation, error):
    dataset, snapshot, path = protocol
    mutation(snapshot)
    ce.write_json(path, snapshot)
    with pytest.raises(ValueError, match=error):
        ce.load_snapshot(path, ce.sha(path), dataset)


def test_two_arms_share_exact_pool_and_raw_inputs(protocol):
    dataset, snapshot, _ = protocol
    original = copy.deepcopy(snapshot)
    received = []
    def score(pairs):
        received.append(pairs)
        return [0, 1, 2, 3, 4, 5]
    result = ce.contrast(dataset, snapshot, score)
    assert snapshot == original
    assert len(received) == 4
    for case, pairs in zip(dataset["cases"], received):
        assert list(pairs) == [(case["question"], "Revenue 123")] * 6
    arms = result["arms"]
    assert arms["rules_same_pool"]["metrics"]["page_recall_at_4"] == 0
    assert arms["cross_encoder_same_pool"]["metrics"]["page_recall_at_4"] == 1
    for name in arms:
        assert arms[name]["metrics"]["candidate_recall_at_48"] == 1
        assert arms[name]["metrics"]["scoped_ticker_pollution"] == 0
    assert len(result["gained"]) == 4
    assert result["lost"] == []


def test_partial_inference_failure_clears_all_metrics(protocol, monkeypatch):
    dataset, _, path = protocol
    monkeypatch.setattr(ce, "verify_inputs", lambda: ("new_holdout", dataset, ce.FROZEN_HOLDOUT_SHA))
    class FailingScorer:
        def __init__(self, _):
            self.calls = 0
        def __call__(self, pairs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("actual model inference failed")
            return list(range(len(pairs)))
    monkeypatch.setattr(ce, "FrozenCPUScorer", FailingScorer)
    result = ce.run_experiment(path, ce.sha(path), Path("unused"))
    assert result["status"] == "BLOCKED"
    assert result["results"] == {}
    assert "scorer_failed" in result["reason"]


def test_input_drift_blocks_before_model_loading(protocol, monkeypatch):
    _, _, path = protocol
    def fail():
        raise ValueError("frozen_core_drift:rag.py")
    monkeypatch.setattr(ce, "verify_inputs", fail)
    monkeypatch.setattr(ce, "FrozenCPUScorer", lambda p: pytest.fail("must not load model"))
    result = ce.run_experiment(path, ce.sha(path), Path("unused"))
    assert result["status"] == "BLOCKED"
    assert result["results"] == {}


def test_weight_hash_checked_against_pinned_authority(tmp_path, monkeypatch):
    path = tmp_path / "weights.bin"
    path.write_bytes(b"fixed official weight bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(ce, "MODEL_FILES", {path.name: ("sha256", digest)})
    assert ce.verify_model_files(tmp_path)[path.name]["digest"] == digest
    path.write_bytes(b"different current bytes")
    with pytest.raises(ValueError, match="model_file_drift"):
        ce.verify_model_files(tmp_path)


def test_git_blob_hash_uses_size_prefix(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b"{}")
    digest = hashlib.sha1(b"blob 2\0{}").hexdigest()
    monkeypatch.setattr(ce, "MODEL_FILES", {path.name: ("git", digest)})
    assert ce.verify_model_files(tmp_path)[path.name]["digest"] == digest


def test_no_model_means_blocked_not_rule_fallback(protocol, monkeypatch, tmp_path):
    dataset, _, path = protocol
    monkeypatch.setattr(ce, "verify_inputs", lambda: ("new_holdout", dataset, ce.FROZEN_HOLDOUT_SHA))
    result = ce.run_experiment(path, ce.sha(path), tmp_path / "missing")
    assert result["status"] == "BLOCKED"
    assert "model_file_missing" in result["reason"]
    assert result["results"] == {}
