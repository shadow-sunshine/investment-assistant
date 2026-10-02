"""R4 多语召回与证据块保真离线评测；结果不切默认线上路径。"""
from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import statistics
import tempfile
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message="fontTools is required")

from .bilingual_eval import QUADRANT_ORDER
from .config import DATA_DIR, KNOWLEDGE_DIR
from .cross_encoder_eval import FROZEN_HOLDOUT_SHA, FROZEN_RETRIEVAL_SHA, sha, verify_inputs
from .hybrid_retrieval import ExperimentalHybridRetrieval
from .hybrid_v2_eval import evidence_keywords_match, is_target, measure, summarize
from .r3_real_chroma_eval import _release_chroma_handles, _retry_rmtree
from .rag import LocalResearchRAG
from .multilingual_evidence_retrieval import MODEL_NAME, MODEL_REVISION, MultilingualEvidenceRetriever

TOP_K = 4
CANDIDATE_BUDGET = 48
RESULT_DIR = DATA_DIR / "evaluations"
RESULT_PATH = RESULT_DIR / "multilingual_evidence_v3.json"
REPORT_PATH = RESULT_DIR / "multilingual_evidence_v3.md"
MODEL_SNAPSHOT = Path.home() / ".cache/huggingface/hub" / f"models--{MODEL_NAME.replace('/', '--')}" / "snapshots" / MODEL_REVISION
MODEL_FILE_SHA256 = {
    "model.safetensors": "eaa086f0ffee582aeb45b36e34cdd1fe2d6de2bef61f8a559a1bbc9bd955917b",
    "tokenizer.json": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
    "modules.json": "8f4b264b80206c830bebbdcae377e137925650a433b689343a63bdc9b3145460",
    "config.json": "6300193cb75e01cf80c96decef7187dfb33094d97cc1490b7ead6ff134476e4e",
    "1_Pooling/config.json": "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23",
}


def _verify_model_snapshot() -> None:
    """对本次已缓存revision固定文件作逐字绑定；摘要为本机实验冻结值，不声称上游签名。"""
    for name, expected in MODEL_FILE_SHA256.items():
        path = MODEL_SNAPSHOT / name
        if not path.is_file() or sha(path) != expected:
            raise ValueError(f"model_snapshot_drift:{name}")


def _page_identity(source: dict[str, Any]) -> tuple[str, str, str]:
    meta = source.get("metadata") or {}
    return str(meta.get("ticker")), str(meta.get("file_name")), str(meta.get("page"))


def _full_page_upper_bound(retriever: MultilingualEvidenceRetriever, case: dict[str, Any]) -> dict[str, Any]:
    spec = retriever.materials[case["ticker"]]
    pages = retriever.pages_by_ticker[case["ticker"]]
    targets = [pages.get(int(page), "") for page in case["target_pages"]]
    return {
        "page_text_has_target": any(any(keyword in text for keyword in case["keywords"]) for text in targets),
        "page_text_strict_verified": any(evidence_keywords_match(text, case["keywords"]) for text in targets),
        "ticker": case["ticker"],
        "file_name": spec["file_name"],
    }


def _build_v2_index(dataset: dict[str, Any]) -> tuple[LocalResearchRAG, ExperimentalHybridRetrieval, tempfile.TemporaryDirectory, Path]:
    env = {key: os.environ.get(key) for key in ("RAG_EMBEDDING_MODE", "RAG_RERANKER_MODE")}
    os.environ["RAG_EMBEDDING_MODE"] = "hash"
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    context = tempfile.TemporaryDirectory(prefix="multilingual_v3_v2_")
    root = Path(context.name).resolve()
    try:
        rag = LocalResearchRAG(path=root)
        indexed = {ticker: rag.index_pdf(KNOWLEDGE_DIR / spec["file_name"])
                   for ticker, spec in dataset["materials"].items()}
        if any(value <= 0 for value in indexed.values()):
            raise ValueError("empty_v2_index")
        rag._r4_original_env = env  # 仅供finally恢复，非检索输入。
        return rag, ExperimentalHybridRetrieval(rag), context, root
    except Exception:
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        gc.collect()
        _release_chroma_handles(root)
        context.cleanup()
        raise


def _restore_v2_env(rag: LocalResearchRAG) -> None:
    for key, value in getattr(rag, "_r4_original_env", {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def run_experiment() -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    _, dataset, _ = verify_inputs()
    _verify_model_snapshot()
    model_started = time.perf_counter()
    retriever = MultilingualEvidenceRetriever(KNOWLEDGE_DIR, dataset["materials"],
                                             model_name=str(MODEL_SNAPSHOT), device="cpu",
                                             max_chars=420, overlap_lines=1, pack_units=3)
    model_index_seconds = time.perf_counter() - model_started
    rag = hybrid = context = root = None
    try:
        rag, hybrid, context, root = _build_v2_index(dataset)
        semantic_rows: list[dict[str, Any]] = []
        upper_rows: list[dict[str, Any]] = []
        for index, case in enumerate(dataset["cases"], 1):
            started = time.perf_counter()
            sources, candidates = retriever.search(case["question"], case["ticker"], limit=TOP_K, budget=CANDIDATE_BUDGET)
            semantic_seconds = time.perf_counter() - started
            v2_sources, v2_candidates = hybrid.search_with_candidates(case["question"], ticker=case["ticker"], limit=TOP_K)
            semantic_measure = measure(case, sources, candidates, dataset["materials"], semantic_seconds)
            v2_page_ids = {_page_identity(source) for source in v2_candidates[:CANDIDATE_BUDGET]}
            semantic_page_ids = {_page_identity(source) for source in candidates[:CANDIDATE_BUDGET]}
            union_page_ids = v2_page_ids | semantic_page_ids
            target_ids = {(case["ticker"], dataset["materials"][case["ticker"]]["file_name"], str(page))
                          for page in case["target_pages"]}
            full_page = _full_page_upper_bound(retriever, case)
            # 两臂各取前24页，再交替填充到48；不能用96页并集冒充固定预算。
            fused = []
            seen = set()
            for left, right in zip(v2_candidates[:24], candidates[:24]):
                for source in (left, right):
                    key = _page_identity(source)
                    if key not in seen:
                        fused.append(key)
                        seen.add(key)
            for offset in range(24, max(len(v2_candidates), len(candidates))):
                for arm in (v2_candidates, candidates):
                    if len(fused) >= CANDIDATE_BUDGET:
                        break
                    if offset < len(arm):
                        key = _page_identity(arm[offset])
                        if key not in seen:
                            fused.append(key)
                            seen.add(key)
                if len(fused) >= CANDIDATE_BUDGET:
                    break
            if len(fused) > CANDIDATE_BUDGET:
                raise ValueError("fused_budget_violation")
            full_page = _full_page_upper_bound(retriever, case)
            semantic_pack_verified_upper = any(is_target(source, case, dataset["materials"])
                and evidence_keywords_match(source["content"], case["keywords"]) for source in candidates)
            semantic_rows.append({**semantic_measure, "id": case["id"], "quadrant": case["quadrant"],
                                  "semantic_candidate_pages": len(semantic_page_ids),
                                  "semantic_candidate_hit_at_48": bool(target_ids & semantic_page_ids),
                                  "v2_candidate_hit_at_48": bool(target_ids & v2_page_ids),
                                  "union_candidate_hit_at_48": bool(target_ids & union_page_ids),
                                  "fused_candidate_hit_at_48": bool(target_ids & set(fused)),
                                  "semantic_pack_verified_upper": semantic_pack_verified_upper,
                                  "semantic_candidate_page_list": [source["metadata"]["page"] for source in candidates],
                                  "v2_candidate_page_list": [source["metadata"]["page"] for source in v2_candidates[:48]],
                                  "fused_candidate_page_list": [key[2] for key in fused],
                                  "v2_top4_pages": [source["metadata"].get("page") for source in v2_sources],
                                  "semantic_top4_pages": [source["metadata"].get("page") for source in sources],
                                  "full_page_upper_bound": full_page,
                                  "retrieval_ms": semantic_measure["retrieval_ms"]})
            upper_rows.append({"id": case["id"], "quadrant": case["quadrant"],
                               "semantic_candidate_hit_at_48": bool(target_ids & semantic_page_ids),
                               "v2_candidate_hit_at_48": bool(target_ids & v2_page_ids),
                               "union_candidate_hit_at_48": bool(target_ids & union_page_ids),
                               "fused_candidate_hit_at_48": bool(target_ids & set(fused)),
                               "semantic_pack_verified_upper": semantic_pack_verified_upper,
                               "full_page_has_target": full_page["page_text_has_target"],
                               "full_page_strict_verified": full_page["page_text_strict_verified"]})
            print("R4", index, case["id"], len(candidates), flush=True)
        semantic_by_q = {q: summarize([row for row in semantic_rows if row["quadrant"] == q]) for q in QUADRANT_ORDER}
        semantic_metrics = summarize(semantic_rows)
        upper = {
            "full_page_has_target": sum(row["full_page_has_target"] for row in upper_rows),
            "full_page_strict_verified": sum(row["full_page_strict_verified"] for row in upper_rows),
            "v2_candidate_hit_at_48": sum(row["v2_candidate_hit_at_48"] for row in upper_rows),
            "semantic_candidate_hit_at_48": sum(row["semantic_candidate_hit_at_48"] for row in upper_rows),
            "union_candidate_hit_at_48": sum(row["union_candidate_hit_at_48"] for row in upper_rows),
            "fused_candidate_hit_at_48": sum(row["fused_candidate_hit_at_48"] for row in upper_rows),
            "semantic_pack_verified_upper": sum(row["semantic_pack_verified_upper"] for row in upper_rows),
        }
        verify_inputs()
        _verify_model_snapshot()
        result = {
            "status": "ok",
            "evaluated_at": stamp,
            "evaluation_type": "known_set_multilingual_recall_and_evidence_pack",
            "dataset_sha256": FROZEN_HOLDOUT_SHA,
            "retrieval_sha256": FROZEN_RETRIEVAL_SHA,
            "model": {"name": MODEL_NAME, "revision": MODEL_REVISION,
                      "file_sha256": MODEL_FILE_SHA256, "device": "cpu", "local_files_only": True,
                      "max_seq_length": int(getattr(retriever.model, "max_seq_length", 0))},
            "model_index_seconds": round(model_index_seconds, 3),
            "code_sha256": {name: sha(Path(__file__).with_name(name)) for name in
                            ("multilingual_evidence_eval.py", "multilingual_evidence_retrieval.py")},
            "corpus": retriever.corpus_stats(),
            "protocol": {"window_chars": 420, "overlap_lines": 1, "pack_units": 3,
                         "top_k": TOP_K, "candidate_budget": CANDIDATE_BUDGET,
                         "fused_budget_rule": "v2_top24_plus_semantic_top24_dedup_then_interleaved_fill_to48",
                         "v2_and_semantic_same_ticker": True,
                         "no_gold_or_answer_in_query": True},
            "arms": {"semantic_evidence_pack": {"metrics": semantic_metrics,
                                                   "by_quadrant": semantic_by_q,
                                                   "cases": semantic_rows}},
            "upper_bounds": upper,
            "limitations": ["known_64_case_set_not_new_blind_test", "four_documents", "one_Chinese_issuer",
                            "union_candidate_hit_at_48_uses_up_to_96_pages_not_comparable_to_48_budget",
                            "no_cross_encoder_in_this_stage", "semantic_top4_not_online_switch",
                            "full_page_strict_is_an_upper_bound_not_a_claim_entailment_test"],
        }
        return result
    finally:
        _restore_v2_env(rag) if rag is not None else None
        rag = hybrid = None
        gc.collect()
        if root is not None:
            _release_chroma_handles(root)
        if context is not None:
            try:
                context.cleanup()
            except (PermissionError, OSError):
                if root is not None:
                    _retry_rmtree(root)


def render_report(result: dict[str, Any]) -> str:
    arm = result["arms"]["semantic_evidence_pack"]
    lines = ["# R4 多语召回与证据块保真实验", "", "结论：这是已知64题的隔离实验，不切默认线上路径。", "",
             "## 1. 结果", "", "| 象限 | 多语语义候选页@48 | 多语证据包Page Recall@4 | 严格字段/数值 |", "|---|---:|---:|---:|"]
    for q in QUADRANT_ORDER:
        m = arm["by_quadrant"][q]
        rows = [row for row in arm["cases"] if row["quadrant"] == q]
        verified = sum(row["verified_hit_at_4"] for row in rows)
        lines.append(f"| {q} | {sum(row['semantic_candidate_hit_at_48'] for row in rows)}/16 | {m['page_hits']}/16 ({m['page_recall_at_4']:.2%}) | {verified}/16 ({m['verified_recall_at_4']:.2%}) |")
    m = arm["metrics"]
    verified = sum(row["verified_hit_at_4"] for row in arm["cases"])
    lines.append(f"| 总体 | {result['upper_bounds']['semantic_candidate_hit_at_48']}/64 | {m['page_hits']}/64 ({m['page_recall_at_4']:.2%}) | {verified}/64 ({m['verified_recall_at_4']:.2%}) |")
    lines.extend(["", "## 2. 上限诊断", "",
                  f"- 四份资料共 {sum(item['pages'] for item in result['corpus']['tickers'].values())} 页、{result['corpus']['total_units']} 个行边界窗口。",
                  f"- 整页文本存在目标字段/数值：{result['upper_bounds']['full_page_has_target']}/64；整页严格核验：{result['upper_bounds']['full_page_strict_verified']}/64。",
                  f"- V2候选页@48：{result['upper_bounds']['v2_candidate_hit_at_48']}/64；多语语义候选页@48：{result['upper_bounds']['semantic_candidate_hit_at_48']}/64；等预算融合@48：{result['upper_bounds']['fused_candidate_hit_at_48']}/64。",
                  f"- 两臂各48的诊断并集：{result['upper_bounds']['union_candidate_hit_at_48']}/64，最多96页，不可与48预算直接比较；多语证据包候选严格核验上限：{result['upper_bounds']['semantic_pack_verified_upper']}/64。",
                  "- 解释：整页命中而证据包不命中，属于页内窗口选择/表格结构保真；并集仍缺页，才属于召回/表示问题。",
                  "", "## 3. 边界", "",
                  "本阶段没有把语义Top-4直接接入默认RAG，也没有把已看过的64题当成新的盲测。下一步若要继续，应用新的资料/发行人/期间做独立留出；仅在候选上限明显改善后，才对同一候选池重新做Cross-Encoder消融。"])
    return "\n".join(lines) + "\n"


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = run_experiment()
    except Exception as exc:
        blocked = {"status": "BLOCKED", "reason": f"{type(exc).__name__}:{exc}", "results": {},
                   "evaluated_at": datetime.now(UTC).isoformat()}
        (RESULT_DIR / "multilingual_evidence_v3_blocked.json").write_text(
            json.dumps(blocked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("BLOCKED", blocked["reason"], flush=True)
        return 1
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_PATH.write_text(render_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "upper_bounds": result["upper_bounds"],
                      "metrics": result["arms"]["semantic_evidence_pack"]["metrics"]}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
