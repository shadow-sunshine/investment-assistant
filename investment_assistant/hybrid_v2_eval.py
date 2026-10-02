"""检索 V2 固定 Top-4 开发/盲测对照；质量未达标时只输出离线失败证据。"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bilingual_eval import EVAL_SET_PATH, QUADRANT_ORDER, _keyword_match, verify_materials
from .config import DATA_DIR, KNOWLEDGE_DIR
from .hybrid_retrieval import ExperimentalHybridRetrieval
from .hybrid_eval import _baseline_candidates
from .r3_real_chroma_eval import HOLDOUT_PATH, FROZEN_R3_HOLDOUT_SHA256, TERM_MAP_PATH, FROZEN_TERM_MAP_SHA256, _release_chroma_handles, _retry_rmtree
from .rag import LocalResearchRAG

TOP_K = 4
MIN_RECALL = 0.80
MIN_TOP1 = 0.60
FROZEN_R0_SHA = "8a367299a49abe836c21738c792f55e890e9c2b7cf5fcb3f0891442eb657acf5"
FROZEN_RAG_SHA = "371de6635ed1a05a4e4f36b34f689cb7146efbd801165df6b8c3b917752014a0"
# 独立数据作者交付和实现者冻结后，由审阅者写入实际字节摘要；不可从待测文件自身取期望值。
FROZEN_HOLDOUT_SHA = "a4657d3cc87c232884f0bb66c8848be250f16399ef09af1557048d6e18d93eda"
FROZEN_RETRIEVAL_SHA = "b4ee58723eb5b28c7ecb6be0dbcff1a3ab03fd937924893d7ef03c9fc8fb5bff"
NEW_HOLDOUT = DATA_DIR / "retrieval_holdout_v2.json"
RETRIEVAL_SOURCE = Path(__file__).with_name("hybrid_retrieval.py")
RESULT_DIR = DATA_DIR / "evaluations"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen(split: str) -> list[tuple[str, dict[str, Any], str]]:
    if split not in {"dev", "holdout"}:
        raise ValueError("unknown_split")
    if sha(RETRIEVAL_SOURCE) != FROZEN_RETRIEVAL_SHA:
        raise ValueError("retrieval_code_drift")
    if sha(Path(__file__).with_name("rag.py")) != FROZEN_RAG_SHA or sha(TERM_MAP_PATH) != FROZEN_TERM_MAP_SHA256:
        raise ValueError("baseline_dependency_drift")
    files = [("R0_dev", EVAL_SET_PATH, FROZEN_R0_SHA), ("R3_dev", HOLDOUT_PATH, FROZEN_R3_HOLDOUT_SHA256)] if split == "dev" else [("new_holdout", NEW_HOLDOUT, FROZEN_HOLDOUT_SHA)]
    loaded = []
    for label, path, expected in files:
        if not path.is_file() or sha(path) != expected:
            raise ValueError(f"dataset_drift:{label}")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        cases = data.get("cases") or []
        counts = {q: sum(c.get("quadrant") == q for c in cases) for q in QUADRANT_ORDER}
        if len(set(c["id"] for c in cases)) != len(cases) or set(counts.values()) == {0} or len(set(counts.values())) != 1 or counts[QUADRANT_ORDER[0]] < (12 if split == "holdout" else 8):
            raise ValueError(f"invalid_case_coverage:{label}")
        materials = data.get("materials") or {}
        if not materials or any(v.get("status") != "match" for v in verify_materials(materials).values()):
            raise ValueError(f"material_drift:{label}")
        for c in cases:
            if c.get("ticker") not in materials or not c.get("target_pages") or not c.get("keywords"):
                raise ValueError(f"invalid_case:{c.get('id')}")
        loaded.append((label, data, expected))
    first = loaded[0][1]["materials"]
    for _, data, _ in loaded[1:]:
        if {(k, v["file_name"], v["sha256"]) for k, v in first.items()} != {(k, v["file_name"], v["sha256"]) for k, v in data["materials"].items()}:
            raise ValueError("material_spec_drift")
    return loaded


def is_target(source: dict[str, Any], case: dict[str, Any], materials: dict[str, Any]) -> bool:
    """同标的、同文件、同资料版本和目标页；不是仅按页码命中。"""
    m = source.get("metadata") or {}
    spec = materials[case["ticker"]]
    try:
        page = int(m.get("page", ""))
    except (TypeError, ValueError):
        return False
    version = m.get("source_sha256")
    return (m.get("ticker") == case["ticker"] and m.get("file_name") == spec["file_name"]
            and page in case["target_pages"] and (version is None or version == spec["sha256"]))


def evidence_keywords_match(content: str, keywords: list[str]) -> bool:
    """字段按字面核验；纯数字金标须是完整数值，不能用 100 命中 1100。"""
    for keyword in keywords:
        if re.fullmatch(r"[-+]?\d[\d,.]*(?:\s*%)?", keyword.strip()):
            if not re.search(r"(?<![\d,.])" + re.escape(keyword.strip()) + r"(?![\d,.])", content):
                return False
        elif not _keyword_match(content, [keyword]):
            return False
    return True


def measure(case: dict[str, Any], sources: list[dict[str, Any]], candidates: list[dict[str, Any]], materials: dict[str, Any], elapsed: float) -> dict[str, Any]:
    target = [s for s in sources if is_target(s, case, materials)]
    numbers = [s for s in case["keywords"] if any(c.isdigit() for c in s)]
    return {
        "id": case["id"], "quadrant": case["quadrant"], "ticker": case["ticker"],
        "page_hit_at_4": bool(target),
        "verified_hit_at_4": any(evidence_keywords_match(s["content"], case["keywords"]) for s in target),
        "number_hit_at_4": any(evidence_keywords_match(s["content"], numbers) for s in target) if numbers else None,
        "top1_hit": bool(sources and is_target(sources[0], case, materials)),
        "candidate_hit_at_48": any(is_target(s, case, materials) for s in candidates[:48]),
        "scoped_ticker_pollution": sum(s.get("metadata", {}).get("ticker") != case["ticker"] for s in sources),
        "retrieval_ms": round(elapsed * 1000, 3),
        "sources": [{"file_name": s["metadata"].get("file_name"), "page": s["metadata"].get("page"), "source_id": s["metadata"].get("source_id"), "source_sha256": s["metadata"].get("source_sha256"), "content": s["content"]} for s in sources],
    }


def wilson(hits: int, n: int) -> list[float]:
    """小样本报告区间，不把点估计称作稳定业务准确率。"""
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(center - radius, 4), round(center + radius, 4)]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    times = sorted(r["retrieval_ms"] for r in rows)
    hits = sum(r["page_hit_at_4"] for r in rows)
    return {
        "cases": n, "page_hits": hits, "page_recall_at_4": round(hits / n, 4),
        "verified_recall_at_4": round(sum(r["verified_hit_at_4"] for r in rows) / n, 4),
        "top1": round(sum(r["top1_hit"] for r in rows) / n, 4),
        "candidate_recall_at_48": round(sum(r["candidate_hit_at_48"] for r in rows) / n, 4),
        "scoped_ticker_pollution": sum(r["scoped_ticker_pollution"] for r in rows),
        "page_recall_wilson95": wilson(hits, n), "p50_ms": round(statistics.median(times), 3),
        "p95_ms": times[max(0, math.ceil(n * 0.95) - 1)],
    }


def assess(result: dict[str, Any]) -> dict[str, Any]:
    """门槛先于盲测写定，不能只看总体掩盖某个象限失败。"""
    failures = []
    for label, stats in [("overall", result["metrics"]), *result["by_quadrant"].items()]:
        for name, minimum in (("page_recall_at_4", MIN_RECALL), ("verified_recall_at_4", MIN_RECALL), ("top1", MIN_TOP1)):
            if stats[name] < minimum:
                failures.append(f"{label}.{name}={stats[name]}<{minimum}")
        if stats["scoped_ticker_pollution"]:
            failures.append(f"{label}.ticker_pollution")
    return {"offline_quality_passed": not failures, "failures": failures, "online_switch": "not_authorized", "decision": "requires_cross_document_validation" if not failures else "no_online_ab"}


def run_experiment(split: str = "holdout") -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    try:
        datasets = load_frozen(split)
    except (OSError, ValueError, TypeError, KeyError) as e:
        return {"status": "BLOCKED", "reason": str(e), "results": {}, "evaluated_at": stamp}
    env = {k: os.environ.get(k) for k in ("RAG_EMBEDDING_MODE", "RAG_RERANKER_MODE")}
    os.environ["RAG_EMBEDDING_MODE"] = "hash"
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    ctx = tempfile.TemporaryDirectory(prefix="hybrid_v2_chroma_")
    root = Path(ctx.name).resolve()
    rag = hybrid = None
    try:
        rag = LocalResearchRAG(path=root)
        materials = datasets[0][1]["materials"]
        indexed = {t: rag.index_pdf(KNOWLEDGE_DIR / spec["file_name"]) for t, spec in materials.items()}
        if any(count == 0 for count in indexed.values()):
            raise ValueError("empty_index")
        hybrid = ExperimentalHybridRetrieval(rag)
        results = {}
        for label, dataset, data_sha in datasets:
            rows = {"baseline_default": [], "field_hybrid_v2": []}
            for c in dataset["cases"]:
                start = time.perf_counter()
                sources = rag.search(c["question"], ticker=c["ticker"], limit=TOP_K)
                elapsed = time.perf_counter() - start
                # 诊断候选另作一次 query，耗时不冒称最终检索耗时。
                baseline_candidates = _baseline_candidates(rag, c["question"], c["ticker"])
                rows["baseline_default"].append(measure(c, sources, baseline_candidates, dataset["materials"], elapsed))
                start = time.perf_counter()
                sources, candidates = hybrid.search_with_candidates(c["question"], ticker=c["ticker"], limit=TOP_K)
                elapsed = time.perf_counter() - start
                if len(sources) > TOP_K:
                    raise ValueError("top_k_violation")
                rows["field_hybrid_v2"].append(measure(c, sources, candidates, dataset["materials"], elapsed))
            arms = {name: {"metrics": summarize(items), "by_quadrant": {q: summarize([r for r in items if r["quadrant"] == q]) for q in QUADRANT_ORDER}, "cases": items} for name, items in rows.items()}
            b = {r["id"]: r for r in rows["baseline_default"]}
            h = {r["id"]: r for r in rows["field_hybrid_v2"]}
            results[label] = {"dataset_sha256": data_sha, "arms": arms, "gained": [k for k in b if not b[k]["page_hit_at_4"] and h[k]["page_hit_at_4"]], "lost": [k for k in b if b[k]["page_hit_at_4"] and not h[k]["page_hit_at_4"]], "assessment": assess(arms["field_hybrid_v2"])}
        # 检索中途改文件也不保留可比成绩。
        load_frozen(split)
        return {"status": "ok", "split": split, "evaluated_at": stamp, "algorithm_sha256": sha(RETRIEVAL_SOURCE), "evaluator_sha256": sha(Path(__file__)), "baseline_sha256": FROZEN_RAG_SHA, "term_map_sha256": FROZEN_TERM_MAP_SHA256, "material_verification": verify_materials(materials), "index_mode": "hash", "semantic": "not_tested", "top_k": TOP_K, "indexed_chunks": indexed, "results": results, "limitations": ["four_fixed_documents", "Chinese_document_one_issuer", "not_general_investment_QA", "cold_cache_included", "candidate_budget_not_equal", "no_online_switch"]}
    except (OSError, ValueError, TypeError, KeyError) as e:
        return {"status": "BLOCKED", "reason": str(e), "results": {}, "evaluated_at": stamp}
    finally:
        hybrid = rag = None
        gc.collect()
        _release_chroma_handles(root)
        try:
            ctx.cleanup()
        except (PermissionError, OSError):
            # 只清理由本函数创建并解析过的临时目录；不接受外部传入路径。
            if not _retry_rmtree(root):
                print(f"实验临时目录尚未释放：{root}")
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "holdout"], default="holdout")
    args = parser.parse_args()
    result = run_experiment(args.split)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULT_DIR / f"hybrid_v2_{args.split}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["status"] != "ok":
        print("BLOCKED", result["reason"])
        return 1
    for label, entry in result["results"].items():
        for arm, data in entry["arms"].items():
            print(label, arm, json.dumps(data["metrics"], ensure_ascii=False))
        print(label, json.dumps(entry["assessment"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
