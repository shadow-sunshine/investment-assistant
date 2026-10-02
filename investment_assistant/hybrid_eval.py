"""冻结快照上的隔离离线对照：不写默认索引，不改变线上检索。"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bilingual_eval import EVAL_SET_PATH, QUADRANT_ORDER, _aggregate, _is_target_source, _keyword_match, verify_materials
from .config import DATA_DIR, KNOWLEDGE_DIR
from .hybrid_retrieval import CANDIDATES_PER_BRANCH, ExperimentalHybridRetrieval
from .r3_real_chroma_eval import (
    FROZEN_R3_HOLDOUT_SHA256, FROZEN_TERM_MAP_SHA256, HOLDOUT_PATH, TERM_MAP_PATH,
    _release_chroma_handles, _retry_rmtree,
)
from .rag import LocalResearchRAG, expand_financial_query

# 当前历史 JSON 原始字节的审计锚点；漂移时拒绝比较，绝不从文件自身获取期望值。
FROZEN_R0_SET_SHA256 = "8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5"
FROZEN_R0_RESULT_SHA256 = "3302ab9ad5a95713829e60e3975f1675e457047d512ce7be9b4f435a0987610d"
FROZEN_R3_RESULT_SHA256 = "35e4769671e567333b8f0d08a8b09b082b637b519fa5dfe1bb006cf94a012fc3"
R0_RESULT = DATA_DIR / "evaluations" / "bilingual_r0.json"
R3_RESULT = DATA_DIR / "evaluations" / "r3_real_chroma_v3.json"
OUTPUT = DATA_DIR / "evaluations" / "hybrid_hash_v1.json"
TOP_K = 4
CANDIDATE_N = 48
# V2 使用独立评测入口；原规则已变时禁止以新代码覆盖 V1 历史证据。
FROZEN_V1_RETRIEVAL_SHA256 = "106f188330a6af06a8beee9cfbd91f144b96e68ef3172e185301a01b06ed87b8"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_snapshots() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """所有校验在索引创建之前完成；历史结果和资料同时约束。"""
    expected = {
        EVAL_SET_PATH: FROZEN_R0_SET_SHA256, HOLDOUT_PATH: FROZEN_R3_HOLDOUT_SHA256,
        TERM_MAP_PATH: FROZEN_TERM_MAP_SHA256, R0_RESULT: FROZEN_R0_RESULT_SHA256,
        R3_RESULT: FROZEN_R3_RESULT_SHA256,
    }
    for path, frozen in expected.items():
        if not path.is_file() or _sha(path) != frozen:
            raise ValueError(f"sha_mismatch:{path.name}")
    r0 = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8-sig"))
    r3 = json.loads(HOLDOUT_PATH.read_text(encoding="utf-8-sig"))
    old_r0 = json.loads(R0_RESULT.read_text(encoding="utf-8-sig"))
    old_r3 = json.loads(R3_RESULT.read_text(encoding="utf-8-sig"))
    if (old_r0.get("status") != "ok" or old_r0.get("eval_set_sha256") != FROZEN_R0_SET_SHA256
        or old_r0.get("top_k") != TOP_K or old_r0.get("results", {}).get("hash", {}).get("status", {}).get("embedding_mode") != "hash"
        or old_r3.get("status") != "ok" or old_r3.get("holdout_sha256") != FROZEN_R3_HOLDOUT_SHA256
        or old_r3.get("frozen_r3_holdout_sha256") != FROZEN_R3_HOLDOUT_SHA256
        or old_r3.get("holdout_matches_frozen") is not True
        or old_r3.get("term_map_sha256") != FROZEN_TERM_MAP_SHA256
        or old_r3.get("term_map_matches_frozen") is not True
        or old_r3.get("embedding_mode") != "hash" or old_r3.get("top_k") != TOP_K):
        raise ValueError("historical_payload_invalid")
    if len(r0.get("cases", [])) != 32 or len(r3.get("cases", [])) != 32:
        raise ValueError("case_count_drift")
    if r0["materials"] != r3["materials"]:
        # R0/R3 的 note/report_date 等注记可能不同，只比索引所需的文件和指纹。
        for ticker in set(r0["materials"]) | set(r3["materials"]):
            a = r0["materials"].get(ticker, {})
            b = r3["materials"].get(ticker, {})
            if (a.get("file_name"), a.get("sha256")) != (b.get("file_name"), b.get("sha256")):
                raise ValueError("material_spec_disagreement")
    material = verify_materials(r3["materials"])
    r0_material = verify_materials(r0["materials"])
    for ticker, check in material.items():
        if (check.get("status") != "match" or r0_material.get(ticker, {}).get("status") != "match"
            or old_r0.get("material_verification", {}).get(ticker, {}).get("actual_sha256") != check["actual_sha256"]
            or old_r3.get("material_verification", {}).get(ticker, {}).get("actual_sha256") != check["actual_sha256"]):
            raise ValueError(f"material_drift:{ticker}")
    if set(material) != set(r0["materials"]) or set(material) != set(r3["materials"]):
        raise ValueError("material_set_drift")
    return r0, r3, material


def _baseline_candidates(rag: LocalResearchRAG, question: str, ticker: str) -> list[dict[str, Any]]:
    """原向量分支的 48 个块；只用于诊断候选召回，不改变 baseline Top-4。"""
    count = len(rag.collection.get(where={"ticker": ticker}, include=[])["ids"])
    if not count:
        return []
    response = rag.collection.query(
        query_embeddings=rag.provider.embed([expand_financial_query(question)]),
        n_results=min(CANDIDATES_PER_BRANCH, count), where={"ticker": ticker},
        include=["documents", "metadatas"],
    )
    return [{"content": content, "metadata": meta} for content, meta in zip(response["documents"][0], response["metadatas"][0])]


def _measure(case: dict[str, Any], sources: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    # gold 只在此函数用于评估，检索 API 仅接收 question/ticker/limit。
    target = [item for item in sources if _is_target_source(item, case)]
    number_keys = [key for key in case["keywords"] if any(ch.isdigit() for ch in key)]
    return {
        "id": case["id"], "quadrant": case["quadrant"], "ticker": case["ticker"],
        "page_hit_at_k": bool(target),
        "keyword_verified_hit_at_k": any(_keyword_match(item["content"], case["keywords"]) for item in target),
        "number_check_at_k": None if not number_keys else any(_keyword_match(item["content"], number_keys) for item in target),
        "top_result_is_relevant": bool(sources and _is_target_source(sources[0], case)),
        "citation_page_relevance": round(len(target) / TOP_K, 4),
        "cross_ticker_source_count_scoped": sum(item["metadata"].get("ticker") != case["ticker"] for item in sources),
        "candidate_recall_at_48": any(_is_target_source(item, case) for item in candidates[:CANDIDATE_N]),
        "retrieved_pages": [item["metadata"].get("page") for item in sources],
        "candidate_page_count": len({(item["metadata"].get("source_id"), item["metadata"].get("page")) for item in candidates[:CANDIDATE_N]}),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # 复用冻结的基本指标计算，但只在调用处映射旧评测器要求的 unscoped 字段；结果不输出伪造的 unscoped 统计。
    aggregate_rows = [{**row, "cross_ticker_source_count_unscoped": row["cross_ticker_source_count_scoped"]} for row in rows]
    base = _aggregate(aggregate_rows)
    base.pop("cross_ticker_sources_per_case_unscoped")
    base["candidate_recall_at_48"] = round(sum(row["candidate_recall_at_48"] for row in rows) / len(rows), 4)
    numbers = [row["number_check_at_k"] for row in rows if row["number_check_at_k"] is not None]
    base["number_check_at_k"] = round(sum(numbers) / len(numbers), 4) if numbers else None
    base["number_check_denominator"] = len(numbers)
    base["scoped_ticker_pollution_count"] = sum(row["cross_ticker_source_count_scoped"] for row in rows)
    return base


def run_experiment() -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    try:
        r0, r3, material = validate_snapshots()
        if _sha(Path(__file__).with_name("hybrid_retrieval.py")) != FROZEN_V1_RETRIEVAL_SHA256:
            raise ValueError("retrieval_version_drift: V1 规则已变，请使用 hybrid_v2_eval，保留 V1 历史结果")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"status": "BLOCKED", "reason": str(exc), "evaluated_at": stamp, "results": {}}
    # 环境暂时隔离，结束时原样恢复；不允许 semantic 静默降级被标为 hash/semantic 成绩。
    previous = {key: os.environ.get(key) for key in ("RAG_EMBEDDING_MODE", "RAG_RERANKER_MODE")}
    os.environ["RAG_EMBEDDING_MODE"] = "hash"
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    path: Path | None = None
    try:
        ctx = tempfile.TemporaryDirectory(prefix="hybrid_chroma_")
        directory = ctx.name
        try:
            path = Path(directory)
            rag: LocalResearchRAG | None = None
            try:
                rag = LocalResearchRAG(path=path)
                if rag.retrieval_status()["embedding_mode"] != "hash" or rag.reranker.mode != "disabled":
                    raise ValueError("mode_drift")
                indexed = {ticker: rag.index_pdf(KNOWLEDGE_DIR / spec["file_name"])
                           for ticker, spec in r3["materials"].items()}
                hybrid = ExperimentalHybridRetrieval(rag)
                results: dict[str, Any] = {}
                for label, dataset in (("R0", r0), ("R3_v3", r3)):
                    rows = {"baseline_default": [], "hybrid": []}
                    timing = {"baseline_default": 0.0, "hybrid": 0.0}
                    for case in dataset["cases"]:
                        question, ticker = case["question"], case["ticker"]
                        t0 = time.perf_counter()
                        baseline = rag.search(question, limit=TOP_K, ticker=ticker)
                        raw = _baseline_candidates(rag, question, ticker)
                        timing["baseline_default"] += time.perf_counter() - t0
                        rows["baseline_default"].append(_measure(case, baseline, raw))
                        t0 = time.perf_counter()
                        top, candidates = hybrid.search_with_candidates(question, ticker, TOP_K)
                        timing["hybrid"] += time.perf_counter() - t0
                        rows["hybrid"].append(_measure(case, top, candidates))
                    results[label] = {arm: {
                        "metrics": _summary(items),
                        "by_quadrant": {q: _summary([item for item in items if item["quadrant"] == q]) for q in QUADRANT_ORDER},
                        "cases": items, "retrieval_seconds": round(timing[arm], 4),
                    } for arm, items in rows.items()}
                payload = {
                    "status": "ok", "evaluated_at": stamp, "mode": "hash", "reranker": "disabled",
                    "baseline_definition": "rag.search default expands financial terms; comparable to historical R3 treatment, NOT R3 raw-query baseline",
                    "hybrid_definition": "same query expansion, vector 48 chunks + literal top 48 pages, reciprocal-rank fusion at page level",
                    "candidate_definition": "baseline first 48 vector chunks, hybrid first 48 fused pages; union may have up to 96 pages",
                    "sha256": {str(p): _sha(p) for p in (EVAL_SET_PATH, HOLDOUT_PATH, TERM_MAP_PATH, R0_RESULT, R3_RESULT)},
                    "material_verification": material, "indexed_chunks": indexed, "results": results,
                    "online_switch": "forbidden", "semantic": "not_tested",
                }
                OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return payload
            finally:
                hybrid = None
                rag = None
                gc.collect()
                _release_chroma_handles(path)
        finally:
            try:
                ctx.cleanup()
            except (PermissionError, OSError) as exc:
                if not _retry_rmtree(path):
                    print(f"[warn] 仅临时实验目录清理失败：{path} ({type(exc).__name__})")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description="双语 hybrid 检索离线冻结对照（hash only）")
    parser.parse_args()
    payload = run_experiment()
    if payload["status"] != "ok":
        print(f"BLOCKED: {payload['reason']}")
        return 1
    for label, arms in payload["results"].items():
        for arm, result in arms.items():
            print(label, arm, json.dumps(result["metrics"], ensure_ascii=False), f"retrieval_seconds={result['retrieval_seconds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
