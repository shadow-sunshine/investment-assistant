"""固定48页双语融合候选，真实Cross-Encoder精排并复用同模型同原文既有分数。"""
from __future__ import annotations

import gc
import hashlib
import json
import math
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, KNOWLEDGE_DIR
from .cross_encoder_eval import (
    FROZEN_HOLDOUT_SHA, FROZEN_RETRIEVAL_SHA, MODEL_REVISION, FrozenCPUScorer,
    MODEL_ID, MODEL_FILES, verify_inputs, verify_model_files, sha,
)
from .cross_encoder_ranking import candidate_fingerprint, rank_candidates
from .hybrid_v2_eval import assess, evidence_keywords_match, is_target, measure, summarize
from .bilingual_eval import QUADRANT_ORDER
from .multilingual_evidence_eval import _verify_model_snapshot, MODEL_SNAPSHOT as SEM_SNAPSHOT
from .multilingual_evidence_retrieval import MultilingualEvidenceRetriever, MODEL_NAME as SEM_MODEL_NAME
from .cross_encoder_eval import MODEL_REVISION as RERANKER_REVISION

RESULT_DIR = DATA_DIR / "evaluations"
V2_SNAPSHOT = RESULT_DIR / "cross_encoder_candidates_v2.json"
V2_SNAPSHOT_SHA = "6c0426ab5b53c831ed061b5d9b0f1b1b29fe03f75384afe73ed35b44c18b72b5"
V2_SCORE_RESULT = RESULT_DIR / "cross_encoder_v2.json"
V2_SCORE_SHA = "0ef3ef681637d758328be4db57610f34a8e26cdcf2cb3cad85b54a6d897f7db8"
SEM_EVAL = RESULT_DIR / "multilingual_evidence_v3.json"
SEM_EVAL_SHA = "e08de09bdda2cdf29a16eb038610686856088a9429219dc68e5fb05dd58b7ec7"
SNAPSHOT_PATH = RESULT_DIR / "fused_candidates_v3.json"
RESULT_PATH = RESULT_DIR / "fused_rerank_v3.json"
TOP_K = 4
BUDGET = 48


def _identity(source: dict[str, Any]) -> tuple[str, str, str]:
    metadata = source["metadata"]
    return str(metadata["ticker"]), str(metadata["file_name"]), str(metadata["page"])


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".partial")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _verify_sources() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    _, dataset, _ = verify_inputs()
    _verify_model_snapshot()
    if sha(V2_SNAPSHOT) != V2_SNAPSHOT_SHA or sha(V2_SCORE_RESULT) != V2_SCORE_SHA or sha(SEM_EVAL) != SEM_EVAL_SHA:
        raise ValueError("prior_artifact_drift")
    old_snapshot = json.loads(V2_SNAPSHOT.read_text(encoding="utf-8"))
    old_scores = json.loads(V2_SCORE_RESULT.read_text(encoding="utf-8"))
    semantic = json.loads(SEM_EVAL.read_text(encoding="utf-8"))
    if (old_snapshot.get("dataset_sha256") != FROZEN_HOLDOUT_SHA
        or old_scores.get("status") != "ok"
        or old_scores.get("candidate_snapshot_sha256") != V2_SNAPSHOT_SHA
        or old_scores.get("model", {}).get("revision") != RERANKER_REVISION
        or semantic.get("status") != "ok"
        or semantic.get("dataset_sha256") != FROZEN_HOLDOUT_SHA):
        raise ValueError("prior_artifact_binding")
    return dataset, old_snapshot, old_scores, semantic


def prepare() -> str:
    dataset, old_snapshot, _, semantic = _verify_sources()
    retriever = MultilingualEvidenceRetriever(KNOWLEDGE_DIR, dataset["materials"],
                                              model_name=str(SEM_SNAPSHOT), device="cpu",
                                              max_chars=420, overlap_lines=1, pack_units=3)
    rows = []
    semantic_by_id = {row["id"]: row for row in semantic["arms"]["semantic_evidence_pack"]["cases"]}
    for case, v2row in zip(dataset["cases"], old_snapshot["cases"]):
        if case["id"] != v2row["id"] or case["question"] != v2row["question"]:
            raise ValueError("v2_query_binding")
        _, sem_candidates = retriever.search(case["question"], case["ticker"], limit=TOP_K, budget=BUDGET)
        recorded_pages = semantic_by_id[case["id"]]["semantic_candidate_page_list"]
        if [source["metadata"]["page"] for source in sem_candidates] != recorded_pages:
            raise ValueError(f"semantic_page_drift:{case['id']}")
        v2 = v2row["candidates"]
        fused, seen = [], set()
        for arm in (v2[:24], sem_candidates[:24]):
            for source in arm:
                key = _identity(source)
                if key not in seen:
                    seen.add(key)
                    fused.append(source)
        for index in range(24, max(len(v2), len(sem_candidates))):
            for arm in (v2, sem_candidates):
                if len(fused) >= BUDGET:
                    break
                if index < len(arm):
                    source = arm[index]
                    key = _identity(source)
                    if key not in seen:
                        seen.add(key)
                        fused.append(source)
            if len(fused) >= BUDGET:
                break
        if len(fused) != min(BUDGET, len(set(_identity(x) for x in v2+sem_candidates))):
            raise ValueError("candidate_budget_binding")
        rows.append({"id": case["id"], "question": case["question"], "ticker": case["ticker"],
                     "quadrant": case["quadrant"], "fingerprint": candidate_fingerprint(fused),
                     "candidates": fused, "source_counts": {"v2": len(v2), "semantic": len(sem_candidates)}})
        print("FUSED", len(rows), case["id"], len(fused), flush=True)
    _verify_sources()
    payload = {"schema": "fused48_v1", "created_at": datetime.now(UTC).isoformat(),
               "dataset_sha256": FROZEN_HOLDOUT_SHA, "v2_snapshot_sha256": V2_SNAPSHOT_SHA,
               "semantic_eval_sha256": SEM_EVAL_SHA, "candidate_budget": BUDGET, "top_k": TOP_K,
               "strategy": "v2_top24_then_semantic_top24_dedup_fill48_v2_first_exact_source",
               "cases": rows}
    _write_json(SNAPSHOT_PATH, payload)
    return sha(SNAPSHOT_PATH)


class ExactCachedScorer:
    """只复用同一模型、同一问题与同一原始正文的旧分数；其余必须真实推理。"""

    def __init__(self, prior_snapshot: dict[str, Any], prior_scores: dict[str, Any], model: FrozenCPUScorer):
        self.cache: dict[tuple[str, str], float] = {}
        self.model = model
        self.last_stats: dict[str, Any] = {}
        prior_by_id = {row["id"]: row for row in prior_scores["results"]["diagnostics"]}
        for row in prior_snapshot["cases"]:
            diagnostic = prior_by_id[row["id"]]
            if diagnostic["candidate_fingerprint"] != row["fingerprint"]:
                raise ValueError("prior_score_binding")
            scores = diagnostic["ranking"]["candidate_scores"]
            if len(scores) != len(row["candidates"]):
                raise ValueError("prior_score_length")
            for source, score in zip(row["candidates"], scores):
                if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                    raise ValueError("prior_score_nonfinite")
                key = (row["question"], source["content"])
                if key in self.cache and self.cache[key] != float(score):
                    raise ValueError("inconsistent_prior_scores")
                self.cache[key] = float(score)

    def __call__(self, pairs: tuple[tuple[str, str], ...]) -> list[float]:
        fresh = list(dict.fromkeys(pair for pair in pairs if pair not in self.cache))
        if fresh:
            raw = self.model(fresh)
            if len(raw) != len(fresh):
                raise ValueError("fresh_score_length")
            for pair, value in zip(fresh, raw):
                if not math.isfinite(float(value)):
                    raise ValueError("fresh_score_nonfinite")
                self.cache[pair] = float(value)
        self.last_stats = {"total_pairs": len(pairs), "fresh_pairs": len(fresh),
                           "reused_pairs": len(pairs) - len(fresh),
                           "model_stats": dict(self.model.last_stats) if fresh else {}}
        return [self.cache[pair] for pair in pairs]


def run(snapshot_sha: str) -> dict[str, Any]:
    dataset, old_snapshot, old_scores, semantic = _verify_sources()
    if sha(SNAPSHOT_PATH) != snapshot_sha:
        raise ValueError("fused_candidate_snapshot_drift")
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if (snapshot.get("schema") != "fused48_v1" or snapshot.get("dataset_sha256") != FROZEN_HOLDOUT_SHA
        or snapshot.get("v2_snapshot_sha256") != V2_SNAPSHOT_SHA
        or snapshot.get("semantic_eval_sha256") != SEM_EVAL_SHA
        or snapshot.get("candidate_budget") != BUDGET or snapshot.get("top_k") != TOP_K
        or len(snapshot.get("cases", [])) != len(dataset["cases"])):
        raise ValueError("fused_snapshot_protocol_drift")
    for case, row in zip(dataset["cases"], snapshot["cases"]):
        if any(case[key] != row.get(key) for key in ("id", "question", "ticker", "quadrant")):
            raise ValueError("fused_query_binding")
        if row.get("fingerprint") != candidate_fingerprint(row["candidates"]):
            raise ValueError("fused_candidate_fingerprint")
        if len(row["candidates"]) > BUDGET:
            raise ValueError("fused_budget_violation")
    model_path = Path.home() / ".cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots" / RERANKER_REVISION
    scorer = ExactCachedScorer(old_snapshot, old_scores, FrozenCPUScorer(model_path))
    arms: dict[str, list[dict[str, Any]]] = {"rules_v2_old_snapshot": [], "cross_encoder_fused48": []}
    diagnostics: list[dict[str, Any]] = []
    old_by_id = {row["id"]: row for row in old_snapshot["cases"]}
    for case, row in zip(dataset["cases"], snapshot["cases"]):
        prior = old_by_id[case["id"]]["candidates"]
        arms["rules_v2_old_snapshot"].append(measure(case, prior[:TOP_K], prior, dataset["materials"], 0))
        start = time.perf_counter()
        ranked = rank_candidates(case["question"], row["candidates"], scorer,
                                 ticker=case["ticker"], material_spec=dataset["materials"][case["ticker"]],
                                 limit=TOP_K, budget=BUDGET)
        elapsed = time.perf_counter()-start
        arms["cross_encoder_fused48"].append(measure(case, ranked["sources"], row["candidates"], dataset["materials"], elapsed))
        diagnostics.append({"id": case["id"], "candidate_fingerprint": row["fingerprint"],
                            "scores": ranked["candidate_scores"], "ranked_indices": ranked["ranked_indices"],
                            "selected_indices": ranked["ranked_indices"][:TOP_K],
                            "scorer": dict(scorer.last_stats), "execution_seconds": round(elapsed, 4)})
        print("RERANKED_FUSED", len(diagnostics), case["id"], scorer.last_stats["fresh_pairs"], flush=True)
    _verify_sources()
    if sha(SNAPSHOT_PATH) != snapshot_sha:
        raise ValueError("fused_candidate_snapshot_changed_during_run")
    verify_model_files(model_path)
    groups = {name: {"metrics": summarize(rows), "by_quadrant": {
        q: summarize([row for row in rows if row["quadrant"] == q]) for q in QUADRANT_ORDER},
        "cases": rows} for name, rows in arms.items()}
    prior_hits = {row["id"]:row["page_hit_at_4"] for row in arms["rules_v2_old_snapshot"]}
    new_hits = {row["id"]:row["page_hit_at_4"] for row in arms["cross_encoder_fused48"]}
    return {"status": "ok", "evaluated_at": datetime.now(UTC).isoformat(),
            "dataset_sha256": FROZEN_HOLDOUT_SHA, "snapshot_sha256": snapshot_sha,
            "v2_snapshot_sha256": V2_SNAPSHOT_SHA, "v2_score_result_sha256": V2_SCORE_SHA,
            "semantic_eval_sha256": SEM_EVAL_SHA,
            "model": {"name": MODEL_ID, "revision": RERANKER_REVISION, "device": "cpu", "cache_exact_prior_scores": True},
            "arms": groups, "diagnostics": diagnostics,
            "gained": [key for key in prior_hits if not prior_hits[key] and new_hits[key]],
            "lost": [key for key in prior_hits if prior_hits[key] and not new_hits[key]],
            "assessment": assess(groups["cross_encoder_fused48"]),
            "limitations": ["known_set_not_blind", "candidate_page_may_lack_claim_evidence",
                            "no_table_column_semantic_validation", "cached_scores_not_live_latency",
                            "no_online_switch"]}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--snapshot-sha")
    args = parser.parse_args()
    try:
        if args.prepare:
            print("FROZEN_FUSED_SHA", prepare(), flush=True)
            return 0
        if not args.snapshot_sha:
            parser.error("精排必须指定独立冻结的--snapshot-sha")
        result = run(args.snapshot_sha)
        _write_json(RESULT_PATH, result)
        for name, arm in result["arms"].items():
            print(name, arm["metrics"], flush=True)
        return 0
    except Exception as exc:
        print("BLOCKED", type(exc).__name__, str(exc), flush=True)
        _write_json(RESULT_DIR / "fused_rerank_v3_blocked.json",
                    {"status":"BLOCKED","reason":f"{type(exc).__name__}:{exc}","results":{}})
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
