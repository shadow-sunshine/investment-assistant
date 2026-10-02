"""固定V2前48候选的真实Cross-Encoder离线消融；不修改默认检索。"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bilingual_eval import QUADRANT_ORDER
from .config import KNOWLEDGE_DIR
from .hybrid_retrieval import ExperimentalHybridRetrieval
from .hybrid_v2_eval import (
    FROZEN_HOLDOUT_SHA, FROZEN_RETRIEVAL_SHA, RESULT_DIR, TOP_K,
    assess, load_frozen, measure, sha, summarize,
)
from .r3_real_chroma_eval import _release_chroma_handles, _retry_rmtree
from .rag import LocalResearchRAG

MODEL_ID = "BAAI/bge-reranker-v2-m3"
MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
MODEL_FILES = {
    "config.json": ("git", "9f62673cb00ec41dcec8947b9ed16f6f2eb23ba2"),
    "model.safetensors": ("sha256", "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"),
    "sentencepiece.bpe.model": ("sha256", "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"),
    "special_tokens_map.json": ("git", "b1879d702821e753ffe4245048eee415d54a9385"),
    "tokenizer.json": ("sha256", "69564b696052886ed0ac63fa393e928384e0f8caada38c1f4864a9bfbf379c15"),
    "tokenizer_config.json": ("git", "328a00a9a560aadcf2a3064f917517359eb3cc26"),
}
CORE_FILES = {
    "rag.py": "371de6635ed1a05a4e4f36b34f689cb7146efbd801165df6b8c3b917752014a0",
    "workflow.py": "df0f0b2f73bae5acaf7df4155b4c23cc18dc54b5a70c55ab4ccc57a608e38eb3",
    "llm_generation.py": "8574980f09c937339fbcb514b8cb88546fa2cd0f42b32c9873b963c5e3221d54",
    "safety.py": "b3780f5a6ba2df364b9f84b1330dae964090ed0d6e8610c90b70103424a04d85",
}
FROZEN_MEASURE_SHA = "d6d680de59d5f965c059a57545bb5fc27362a659c5d65bcae9f07495d95a5d68"
CANDIDATE_BUDGET = 48
MAX_LENGTH = 1024
BATCH_SIZE = 4
CPU_THREADS = 8
SNAPSHOT_PATH = RESULT_DIR / "cross_encoder_candidates_v2.json"
RESULT_PATH = RESULT_DIR / "cross_encoder_v2.json"


def verify_inputs() -> tuple[str, dict[str, Any], str]:
    if sha(Path(__file__).with_name("hybrid_v2_eval.py")) != FROZEN_MEASURE_SHA:
        raise ValueError("measurement_protocol_drift")
    for name, expected in CORE_FILES.items():
        if sha(Path(__file__).with_name(name)) != expected:
            raise ValueError(f"frozen_core_drift:{name}")
    return load_frozen("holdout")[0]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 中断只留下.partial，不能把半个结果误当完成产物。
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    partial.replace(path)


def prepare_candidates(path: Path = SNAPSHOT_PATH) -> str:
    from .cross_encoder_ranking import candidate_fingerprint
    _, dataset, data_sha = verify_inputs()
    env = {k: os.environ.get(k) for k in ("RAG_EMBEDDING_MODE", "RAG_RERANKER_MODE")}
    os.environ["RAG_EMBEDDING_MODE"] = "hash"
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    context = tempfile.TemporaryDirectory(prefix="cross_encoder_same_pool_")
    root = Path(context.name).resolve()
    rag = hybrid = None
    try:
        rag = LocalResearchRAG(path=root)
        counts = {t: rag.index_pdf(KNOWLEDGE_DIR / spec["file_name"]) for t, spec in dataset["materials"].items()}
        if any(v <= 0 for v in counts.values()):
            raise ValueError("empty_index")
        hybrid = ExperimentalHybridRetrieval(rag)
        rows = []
        for case in dataset["cases"]:
            started = time.perf_counter()
            sources, candidates = hybrid.search_with_candidates(case["question"], ticker=case["ticker"], limit=TOP_K)
            elapsed = time.perf_counter() - started
            selected = candidates[:CANDIDATE_BUDGET]
            # 原规则Top4必须是同一候选前缀，禁止两臂分开召回。
            if [{k: v for k, v in s.items() if k != "citation"} for s in sources] != selected[:TOP_K]:
                raise ValueError("rule_prefix_mismatch")
            rows.append({"id": case["id"], "question": case["question"], "ticker": case["ticker"],
                         "quadrant": case["quadrant"], "candidates": selected,
                         "fingerprint": candidate_fingerprint(selected),
                         "retrieval_ms": elapsed * 1000, "full_union_count": len(candidates)})
            print("CANDIDATES", len(rows), case["id"], len(selected), flush=True)
        verify_inputs()
        write_json(path, {"schema": "same_pool_cross_encoder_v1", "created_at": datetime.now(UTC).isoformat(),
                         "dataset_sha256": data_sha, "retrieval_sha256": FROZEN_RETRIEVAL_SHA,
                         "candidate_budget": CANDIDATE_BUDGET, "top_k": TOP_K,
                         "materials": dataset["materials"], "indexed_chunks": counts, "cases": rows})
        return sha(path)
    finally:
        rag = hybrid = None
        gc.collect()
        _release_chroma_handles(root)
        try:
            context.cleanup()
        except (PermissionError, OSError):
            if not _retry_rmtree(root):
                print("临时索引仍待释放", root, flush=True)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def load_snapshot(path: Path, expected_sha: str, dataset: dict[str, Any]) -> dict[str, Any]:
    from .cross_encoder_ranking import candidate_fingerprint
    if not expected_sha or sha(path) != expected_sha:
        raise ValueError("candidate_snapshot_drift")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema") != "same_pool_cross_encoder_v1"
        or data.get("dataset_sha256") != FROZEN_HOLDOUT_SHA
        or data.get("retrieval_sha256") != FROZEN_RETRIEVAL_SHA
        or data.get("candidate_budget") != CANDIDATE_BUDGET or data.get("top_k") != TOP_K
        or data.get("materials") != dataset["materials"]):
        raise ValueError("snapshot_protocol_drift")
    rows = data.get("cases", [])
    if len(rows) != len(dataset["cases"]):
        raise ValueError("snapshot_case_count")
    for row, case in zip(rows, dataset["cases"]):
        if any(row.get(k) != case[k] for k in ("id", "question", "ticker", "quadrant")):
            raise ValueError("snapshot_query_binding")
        candidates = row.get("candidates")
        if not isinstance(candidates, list) or not 0 < len(candidates) <= CANDIDATE_BUDGET:
            raise ValueError("snapshot_candidate_count")
        if row.get("fingerprint") != candidate_fingerprint(candidates):
            raise ValueError("snapshot_candidate_binding")
        ms = row.get("retrieval_ms")
        if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 0 <= ms < float("inf"):
            raise ValueError("snapshot_invalid_latency")
    return data


def verify_model_files(model_path: Path) -> dict[str, Any]:
    """期望摘要来自固定revision的官方Git/LFS元数据，不从待测文件反编。"""
    checked = {}
    for name, (mode, expected) in MODEL_FILES.items():
        file = model_path / name
        if not file.is_file():
            raise ValueError(f"model_file_missing:{name}")
        hasher = hashlib.sha256() if mode == "sha256" else hashlib.sha1()
        if mode == "git":
            hasher.update(f"blob {file.stat().st_size}\0".encode())
        with file.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if actual != expected:
            raise ValueError(f"model_file_drift:{name}")
        checked[name] = {"size": file.stat().st_size, "digest_type": mode, "digest": actual}
    return checked


class FrozenCPUScorer:
    def __init__(self, model_path: Path):
        # 先核验再加载；网络、远程代码、自动设备选择和规则降级均不允许。
        self.files = verify_model_files(model_path)
        import torch
        from sentence_transformers import CrossEncoder
        torch.set_num_threads(CPU_THREADS)
        started = time.perf_counter()
        self.model = CrossEncoder(str(model_path), device="cpu", local_files_only=True,
                                  trust_remote_code=False, max_length=MAX_LENGTH,
                                  activation_fn=torch.nn.Identity(),
                                  model_kwargs={"dtype": torch.float32})
        self.load_ms = (time.perf_counter() - started) * 1000
        self.last_stats: dict[str, Any] = {}
        self.versions = {n: importlib.metadata.version(n) for n in
                         ("torch", "sentence-transformers", "transformers", "huggingface-hub")}

    def __call__(self, pairs: list[tuple[str, str]]) -> Any:
        tokenizer = self.model.tokenizer
        lengths = [len(tokenizer(q, d, truncation=False, padding=False)["input_ids"]) for q, d in pairs]
        started = time.perf_counter()
        scores = self.model.predict(pairs, batch_size=BATCH_SIZE, show_progress_bar=False,
                                    convert_to_numpy=True)
        self.last_stats = {"token_lengths": lengths, "truncated_pairs": sum(v > MAX_LENGTH for v in lengths),
                           "max_tokens": max(lengths, default=0), "inference_ms": (time.perf_counter() - started) * 1000}
        return scores.tolist()


def contrast(dataset: dict[str, Any], snapshot: dict[str, Any], scorer: Any) -> dict[str, Any]:
    from .cross_encoder_ranking import rank_candidates
    arms: dict[str, list[dict[str, Any]]] = {"rules_same_pool": [], "cross_encoder_same_pool": []}
    diagnostics = []
    for case, row in zip(dataset["cases"], snapshot["cases"]):
        candidates = row["candidates"]
        base_seconds = row["retrieval_ms"] / 1000
        arms["rules_same_pool"].append(measure(case, candidates[:TOP_K], candidates, dataset["materials"], base_seconds))
        started = time.perf_counter()
        result = rank_candidates(case["question"], candidates, scorer, ticker=case["ticker"],
                                 material_spec=dataset["materials"][case["ticker"]], limit=TOP_K,
                                 budget=CANDIDATE_BUDGET)
        rerank_ms = (time.perf_counter() - started) * 1000
        arms["cross_encoder_same_pool"].append(measure(case, result["sources"], candidates,
                                                      dataset["materials"], base_seconds + rerank_ms / 1000))
        diagnostics.append({"id": case["id"], "candidate_fingerprint": row["fingerprint"],
                            "ranking": result, "rerank_ms": rerank_ms,
                            "tokens": dict(getattr(scorer, "last_stats", {}))})
        print("RERANKED", len(diagnostics), case["id"], round(rerank_ms), flush=True)
    grouped = {name: {"metrics": summarize(rows), "by_quadrant": {
        q: summarize([r for r in rows if r["quadrant"] == q]) for q in QUADRANT_ORDER}, "cases": rows}
               for name, rows in arms.items()}
    base = {r["id"]: r for r in arms["rules_same_pool"]}
    ranked = {r["id"]: r for r in arms["cross_encoder_same_pool"]}
    out = {"arms": grouped, "diagnostics": diagnostics,
           "gained": [k for k in base if not base[k]["page_hit_at_4"] and ranked[k]["page_hit_at_4"]],
           "lost": [k for k in base if base[k]["page_hit_at_4"] and not ranked[k]["page_hit_at_4"]],
           "candidate_misses": [k for k in base if not base[k]["candidate_hit_at_48"]],
           "assessment": assess(grouped["cross_encoder_same_pool"])}
    return out


def run_experiment(path: Path, expected_sha: str, model_path: Path) -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    code_sha = {}
    try:
        code_sha = {n: sha(Path(__file__).with_name(n)) for n in
                    ("cross_encoder_eval.py", "cross_encoder_ranking.py", "hybrid_v2_eval.py")}
        _, dataset, _ = verify_inputs()
        snapshot = load_snapshot(path, expected_sha, dataset)
        scorer = FrozenCPUScorer(model_path)
        result = contrast(dataset, snapshot, scorer)
        verify_inputs()
        load_snapshot(path, expected_sha, dataset)
        verify_model_files(model_path)
        if any(sha(Path(__file__).with_name(n)) != digest for n, digest in code_sha.items()):
            raise ValueError("experiment_code_changed_during_run")
        return {"status": "ok", "evaluated_at": stamp, "evaluation_type": "known_set_same_pool_ablation",
                "dataset_sha256": FROZEN_HOLDOUT_SHA, "candidate_snapshot_sha256": expected_sha,
                "code_sha256": code_sha, "frozen_core_sha256": CORE_FILES,
                "model": {"id": MODEL_ID, "revision": MODEL_REVISION, "files": scorer.files,
                          "device": "cpu", "dtype": "float32", "batch_size": BATCH_SIZE,
                          "max_length": MAX_LENGTH, "threads": CPU_THREADS, "load_ms": scorer.load_ms,
                          "packages": scorer.versions, "offline": True, "fallback": False},
                "candidate_budget": CANDIDATE_BUDGET, "top_k": TOP_K,
                "results": result, "limitations": ["known_set_not_blind", "four_documents",
                 "one_Chinese_issuer", "no_recall_change", "no_table_structure_fix", "no_online_switch",
                 "latency_retrieval_cached_plus_measured_rerank_not_live_end_to_end"]}
    except Exception as error:
        # 半途失败清空成绩：不把已完成的部分题或fallback伪装成正式比较。
        return {"status": "BLOCKED", "reason": f"{type(error).__name__}:{error}",
                "evaluated_at": stamp, "results": {}, "code_sha256": code_sha}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--snapshot-sha")
    parser.add_argument("--model-path", type=Path, default=Path.home() / ".cache/huggingface/hub" /
                        "models--BAAI--bge-reranker-v2-m3/snapshots" / MODEL_REVISION)
    args = parser.parse_args()
    if args.prepare:
        print("FROZEN_SNAPSHOT_SHA", prepare_candidates(args.snapshot), flush=True)
        return 0
    if not args.snapshot_sha:
        parser.error("精排必须传入准备阶段独立保存的 --snapshot-sha")
    result = run_experiment(args.snapshot, args.snapshot_sha, args.model_path)
    write_json(RESULT_PATH, result)
    if result["status"] != "ok":
        print("BLOCKED", result["reason"], flush=True)
        return 1
    for name, arm in result["results"]["arms"].items():
        print(name, json.dumps(arm["metrics"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
