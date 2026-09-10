"""Apple 10-K retrieval evaluation using the same manually labelled set."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, KNOWLEDGE_DIR
from .rag import LocalResearchRAG

EVAL_SET_PATH = DATA_DIR / "apple_10k_eval_set.json"
RESULT_DIR = DATA_DIR / "evaluations"
APPLE_10K_NAME = "Apple_2025_Form_10-K.pdf"


def _load_cases(path: Path = EVAL_SET_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _page(value: dict[str, Any]) -> int | None:
    try:
        return int(value.get("metadata", {}).get("page", ""))
    except (TypeError, ValueError):
        return None


def _keyword_match(content: str, keywords: list[str]) -> bool:
    lowered = content.lower()
    return all(keyword.lower() in lowered for keyword in keywords)


def run_mode(
    mode: str,
    top_k: int = 4,
    eval_set_path: Path = EVAL_SET_PATH,
    reranker_mode: str = "disabled",
) -> dict[str, Any]:
    if mode not in {"hash", "semantic"}:
        raise ValueError("mode must be hash or semantic")
    if top_k != 4:
        raise ValueError("evaluation Top-K is frozen at 4")
    os.environ["RAG_EMBEDDING_MODE"] = mode
    os.environ["RAG_RERANKER_MODE"] = reranker_mode
    os.environ.pop("HF_HOME", None)
    os.environ.pop("HF_ENDPOINT", None)
    eval_chroma = DATA_DIR / "evaluation_chroma" / f"{mode}_{reranker_mode}"
    if eval_chroma.exists():
        shutil.rmtree(eval_chroma)
    rag = LocalResearchRAG(path=eval_chroma)
    status = rag.retrieval_status()
    if mode == "semantic" and status["embedding_mode"] != "semantic":
        return {"mode": mode, "status": status, "error": "Semantic model cannot be loaded from the default local cache; evaluation not run."}

    pdf_path = KNOWLEDGE_DIR / APPLE_10K_NAME
    if not pdf_path.exists():
        return {"mode": mode, "status": status, "error": f"Evaluation PDF not found: {pdf_path}"}
    indexed_chunks = rag.index_pdf(pdf_path)
    cases = _load_cases(eval_set_path)
    results: list[dict[str, Any]] = []
    for case in cases:
        sources = rag.search(case["question"], limit=top_k)
        pages = [_page(source) for source in sources]
        target_pages = case["target_pages"]
        relevant = [source for source in sources if _page(source) in target_pages]
        page_hit = bool(relevant)
        keyword_hit = any(_keyword_match(source["content"], case["keywords"]) for source in relevant)
        results.append({
            "id": case["id"],
            "question": case["question"],
            "target_pages": target_pages,
            "keywords": case["keywords"],
            "retrieved_pages": pages,
            "page_hit_at_k": page_hit,
            "keyword_verified_hit_at_k": keyword_hit,
            "citation_page_relevance": round(len(relevant) / top_k, 4),
            "top_result_page": pages[0] if pages else None,
            "top_result_is_relevant": bool(pages and pages[0] in target_pages),
        })
    total = len(results)
    return {
        "mode": mode,
        "reranker_requested": reranker_mode,
        "top_k": top_k,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "indexed_pdf": str(pdf_path),
        "indexed_chunks": indexed_chunks,
        "case_count": total,
        "metrics": {
            "recall_at_k": round(sum(item["page_hit_at_k"] for item in results) / total, 4),
            "keyword_verified_recall_at_k": round(sum(item["keyword_verified_hit_at_k"] for item in results) / total, 4),
            "citation_page_relevance_at_k": round(sum(item["citation_page_relevance"] for item in results) / total, 4),
            "top1_page_relevance": round(sum(item["top_result_is_relevant"] for item in results) / total, 4),
        },
        "cases": results,
    }


def render_comparison(hash_result: dict[str, Any], semantic_result: dict[str, Any], title: str = "Apple 2025 Form 10-K retrieval comparison") -> str:
    lines = [f"# {title}", "", "| Metric | Hash | Semantic |", "|---|---:|---:|"]
    for key, label in [
        ("recall_at_k", "Page Recall@4"),
        ("keyword_verified_recall_at_k", "Keyword-verified Recall@4"),
        ("citation_page_relevance_at_k", "Citation page relevance@4"),
        ("top1_page_relevance", "Top-1 page relevance"),
    ]:
        lines.append(f"| {label} | {hash_result.get('metrics', {}).get(key, 'failed')} | {semantic_result.get('metrics', {}).get(key, 'failed')} |")
    lines.extend(["", "## Retrieval configuration", ""])
    for name, result in [("Hash", hash_result), ("Semantic", semantic_result)]:
        status = result.get("status", {})
        lines.append(
            f"- {name}: embedding={status.get('embedding_mode')}; reranker requested={result.get('reranker_requested', 'disabled')}; "
            f"reranker actual={status.get('reranker_mode')}; reranker fallback={status.get('reranker_fallback_reason') or 'none'}."
        )
    lines.extend(["", "## Failures (page miss or keyword verification miss)", ""])
    for name, result in [("Hash", hash_result), ("Semantic", semantic_result)]:
        failures = [case for case in result.get("cases", []) if not case["keyword_verified_hit_at_k"]]
        lines.append(f"### {name}")
        lines.extend([f"- {case['id']}: targets {case['target_pages']}; retrieved {case['retrieved_pages']}." for case in failures] or ["- none"])
        lines.append("")
    return "\n".join(lines)


def run_comparison(
    top_k: int = 4,
    eval_set_path: Path = EVAL_SET_PATH,
    reranker_mode: str = "disabled",
    output_stem: str = "apple_10k_retrieval_comparison",
    title: str = "Apple 2025 Form 10-K retrieval comparison",
) -> dict[str, Any]:
    if top_k != 4:
        raise ValueError("evaluation Top-K is frozen at 4")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    hash_result = run_mode("hash", top_k, eval_set_path, reranker_mode)
    semantic_result = run_mode("semantic", top_k, eval_set_path, reranker_mode)
    combined = {"hash": hash_result, "semantic": semantic_result}
    (RESULT_DIR / f"{output_stem}.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / f"{output_stem}.md").write_text(render_comparison(hash_result, semantic_result, title), encoding="utf-8")
    return combined


def main() -> int:
    parser = argparse.ArgumentParser(description="Apple 10-K hash / semantic retrieval evaluation")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--reranker-mode", choices=["disabled", "cross_encoder"], default="disabled")
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET_PATH)
    parser.add_argument("--output-stem", default="apple_10k_retrieval_comparison")
    args = parser.parse_args()
    result = run_comparison(args.top_k, args.eval_set, args.reranker_mode, args.output_stem)
    print(render_comparison(result["hash"], result["semantic"]))
    return 0 if "error" not in result["semantic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
