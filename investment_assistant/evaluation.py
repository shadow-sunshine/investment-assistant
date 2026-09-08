"""Apple 10-K 检索评测：同一人工标注集对比 hash 与 semantic 模式。"""

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


def _load_cases() -> list[dict[str, Any]]:
    return json.loads(EVAL_SET_PATH.read_text(encoding="utf-8-sig"))


def _page(value: dict[str, Any]) -> int | None:
    try:
        return int(value.get("metadata", {}).get("page", ""))
    except (TypeError, ValueError):
        return None


def _keyword_match(content: str, keywords: list[str]) -> bool:
    lowered = content.lower()
    return all(keyword.lower() in lowered for keyword in keywords)


def run_mode(mode: str, top_k: int = 4) -> dict[str, Any]:
    if mode not in {"hash", "semantic"}:
        raise ValueError("mode 必须为 hash 或 semantic")
    os.environ["RAG_EMBEDDING_MODE"] = mode
    os.environ.pop("HF_HOME", None)
    os.environ.pop("HF_ENDPOINT", None)
    eval_chroma = DATA_DIR / "evaluation_chroma" / mode
    if eval_chroma.exists():
        shutil.rmtree(eval_chroma)
    rag = LocalResearchRAG(path=eval_chroma)
    status = rag.retrieval_status()
    if mode == "semantic" and status["embedding_mode"] != "semantic":
        return {"mode": mode, "status": status, "error": "语义模型无法从默认缓存离线加载；未执行评测。"}

    pdf_path = KNOWLEDGE_DIR / APPLE_10K_NAME
    if not pdf_path.exists():
        return {"mode": mode, "status": status, "error": f"未找到评测 PDF：{pdf_path}"}
    indexed_chunks = rag.index_pdf(pdf_path)
    cases = _load_cases()
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


def render_comparison(hash_result: dict[str, Any], semantic_result: dict[str, Any]) -> str:
    lines = ["# Apple 2025 Form 10-K 检索模式对比", "", "| 指标 | Hash | Semantic |", "|---|---:|---:|"]
    for key, label in [("recall_at_k", "页面 Recall@4"), ("keyword_verified_recall_at_k", "关键词核验 Recall@4"), ("citation_page_relevance_at_k", "引用页面相关性@4"), ("top1_page_relevance", "Top-1 页面相关性")]:
        left = hash_result.get("metrics", {}).get(key, "失败")
        right = semantic_result.get("metrics", {}).get(key, "失败")
        lines.append(f"| {label} | {left} | {right} |")
    lines.extend(["", "## 失败样本（页面未命中或关键词未核验）", ""])
    for name, result in [("Hash", hash_result), ("Semantic", semantic_result)]:
        failures = [case for case in result.get("cases", []) if not case["keyword_verified_hit_at_k"]]
        lines.append(f"### {name}")
        if not failures:
            lines.append("- 无")
        for case in failures:
            lines.append(f"- {case['id']}：目标页 {case['target_pages']}，召回页 {case['retrieved_pages']}。")
        lines.append("")
    return "\n".join(lines)


def run_comparison(top_k: int = 4) -> dict[str, Any]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    hash_result = run_mode("hash", top_k)
    semantic_result = run_mode("semantic", top_k)
    combined = {"hash": hash_result, "semantic": semantic_result}
    (RESULT_DIR / "apple_10k_retrieval_comparison.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / "apple_10k_retrieval_comparison.md").write_text(render_comparison(hash_result, semantic_result), encoding="utf-8")
    return combined


def main() -> int:
    parser = argparse.ArgumentParser(description="Apple 10-K hash / semantic 检索评测")
    parser.add_argument("--top-k", type=int, default=4)
    args = parser.parse_args()
    result = run_comparison(args.top_k)
    print(render_comparison(result["hash"], result["semantic"]))
    return 0 if "error" not in result["semantic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

