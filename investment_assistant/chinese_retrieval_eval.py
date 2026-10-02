"""中文官方年报离线评测：先低成本字面候选，再用本地语义模型重排。"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader

from .config import DATA_DIR, KNOWLEDGE_DIR
from .cross_encoder_eval import sha
from .hybrid_retrieval import _normalize, _intent, FINANCIAL_GLOSSARY
from .hybrid_v2_eval import evidence_keywords_match
from .multilingual_evidence_retrieval import MODEL_NAME, MODEL_REVISION

DATASET_PATH = DATA_DIR / "chinese_retrieval_bootstrap.json"
RESULT_PATH = DATA_DIR / "evaluations" / "chinese_retrieval_bootstrap.json"
REPORT_PATH = DATA_DIR / "evaluations" / "chinese_retrieval_bootstrap.md"
MODEL_SNAPSHOT = Path.home() / ".cache/huggingface/hub" / f"models--{MODEL_NAME.replace('/', '--')}" / "snapshots" / MODEL_REVISION
TOP_K = 4
CANDIDATE_BUDGET = 48
SEMANTIC_BUDGET = 16


def _load_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != "chinese-retrieval-bootstrap-v1" or len(dataset.get("cases", [])) < 20:
        raise ValueError("invalid_dataset")
    if len({case["id"] for case in dataset["cases"]}) != len(dataset["cases"]):
        raise ValueError("duplicate_case")
    for case in dataset["cases"]:
        if case["answer_value"] not in case["keywords"] or case["answer_value"] in case["question"]:
            raise ValueError(f"gold_leak_or_mismatch:{case['id']}")
    return dataset


def _load_pages(dataset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pages_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for ticker, spec in dataset["materials"].items():
        path = KNOWLEDGE_DIR / spec["file_name"]
        manifest = json.loads((DATA_DIR / "materials_manifest.json").read_text(encoding="utf-8"))[ticker]
        if (spec["ticker"] != ticker or sha(path) != spec["sha256"]
                or manifest["validation"]["sha256"] != spec["sha256"]
                or manifest["file_name"] != spec["file_name"]):
            raise ValueError(f"material_identity_drift:{ticker}")
        reader = PdfReader(str(path))
        if len(reader.pages) != spec["page_count"]:
            raise ValueError(f"page_count_drift:{ticker}")
        pages: list[dict[str, Any]] = []
        for page_no, page in enumerate(reader.pages, 1):
            raw = page.extract_text() or ""
            pages.append({"content": raw,
                          "lines": [line.strip() for line in raw.splitlines() if line.strip()],
                          "metadata": {"ticker": ticker, "file_name": spec["file_name"],
                                       "source_sha256": spec["sha256"], "page": page_no}})
        pages_by_ticker[ticker] = pages
        for case in (item for item in dataset["cases"] if item["ticker"] == ticker):
            if not any(evidence_keywords_match(pages[int(no) - 1]["content"], case["keywords"])
                       for no in case["target_pages"]):
                raise ValueError(f"gold_not_on_target_page:{case['id']}")
    return pages_by_ticker


def _query_terms(question: str, company: str) -> tuple[str, list[str]]:
    query = _normalize(question).replace(_normalize(company), "")
    query = re.sub(r"20\d\d年?(?:末|度)?|是(多少|什么)|有多少|的|为多少|[？?，。\s]", "", query)
    intent = _intent(question)
    aliases = [alias for key, terms in FINANCIAL_GLOSSARY if key in intent["fields"]
               for alias in terms if re.search(r"[\u4e00-\u9fff]", alias)]
    # 别名只提供通用词扩展，不包含答案、页码和标的专用规则。
    terms = list(dict.fromkeys([query] + sorted(aliases, key=len, reverse=True)))
    return query, [term for term in terms if len(term) >= 2]


def _page_score(page: dict[str, Any], terms: list[str]) -> tuple[float, str]:
    lines = page["lines"]
    best_score, best_index = 0.0, 0
    for index, line in enumerate(lines):
        normalized = _normalize(line)
        matched = [term for term in terms if _normalize(term) in normalized]
        if not matched:
            continue
        score = max(len(term) for term in matched) + 0.25 * len(matched)
        if re.search(r"\d[\d,.]*(?:%|％)?", normalized):
            score += 1.5
        if score > best_score:
            best_score, best_index = score, index
    excerpt = "\n".join(lines[max(0, best_index - 2):best_index + 3])[:1200]
    return best_score, excerpt


def _is_target(source: dict[str, Any], case: dict[str, Any], dataset: dict[str, Any]) -> bool:
    meta = source.get("metadata") or {}
    spec = dataset["materials"][case["ticker"]]
    return (meta.get("ticker") == case["ticker"] and meta.get("file_name") == spec["file_name"]
            and meta.get("source_sha256") == spec["sha256"]
            and meta.get("page") in case["target_pages"])


def _measure(case: dict[str, Any], sources: list[dict[str, Any]], candidates: list[dict[str, Any]],
             dataset: dict[str, Any], elapsed_ms: float) -> dict[str, Any]:
    target = [source for source in sources if _is_target(source, case, dataset)]
    return {"id": case["id"], "ticker": case["ticker"],
            "candidate_hit_at_48": any(_is_target(source, case, dataset) for source in candidates),
            "page_hit_at_4": bool(target),
            "field_value_cooccurrence_at_4": any(evidence_keywords_match(source["content"], case["keywords"])
                                                 for source in target),
            "top1_hit": bool(sources and _is_target(sources[0], case, dataset)),
            "ticker_pollution": sum(source["metadata"]["ticker"] != case["ticker"] for source in sources),
            "top_pages": [source["metadata"]["page"] for source in sources],
            "retrieval_ms": round(elapsed_ms, 3)}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    return {"cases": n,
            "candidate_recall_at_48": round(sum(row["candidate_hit_at_48"] for row in rows) / n, 4),
            "page_recall_at_4": round(sum(row["page_hit_at_4"] for row in rows) / n, 4),
            "field_value_cooccurrence_at_4": round(sum(row["field_value_cooccurrence_at_4"] for row in rows) / n, 4),
            "top1": round(sum(row["top1_hit"] for row in rows) / n, 4),
            "ticker_pollution": sum(row["ticker_pollution"] for row in rows),
            "median_retrieval_ms": round(sorted(row["retrieval_ms"] for row in rows)[n // 2], 3)}


def run() -> dict[str, Any]:
    dataset = _load_dataset()
    pages = _load_pages(dataset)
    if not MODEL_SNAPSHOT.is_dir():
        raise ValueError("model_snapshot_missing")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(str(MODEL_SNAPSHOT), local_files_only=True, device="cpu")
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(dataset["cases"], 1):
        started = time.perf_counter()
        query, terms = _query_terms(case["question"], dataset["materials"][case["ticker"]]["company"])
        ranked: list[dict[str, Any]] = []
        for page in pages[case["ticker"]]:
            score, excerpt = _page_score(page, terms)
            ranked.append({**page, "lexical_score": score, "excerpt": excerpt})
        ranked.sort(key=lambda page: (-page["lexical_score"], page["metadata"]["page"]))
        candidates = ranked[:CANDIDATE_BUDGET]
        # 语义只在同标的字面召回前16页的查询相关片段上排序，控制CPU与截断风险。
        head = candidates[:SEMANTIC_BUDGET]
        embeddings = np.asarray(model.encode([case["question"]] + [page["excerpt"] for page in head],
                                             batch_size=32, normalize_embeddings=True, convert_to_numpy=True,
                                             show_progress_bar=False), dtype=np.float32)
        if embeddings.shape[0] != len(head) + 1 or not np.isfinite(embeddings).all():
            raise ValueError(f"invalid_embedding:{case['id']}")
        similarities = embeddings[1:] @ embeddings[0]
        ordered = sorted(enumerate(head), key=lambda item: (-(item[1]["lexical_score"] +
                     float(similarities[item[0]]) * 3.0), item[1]["metadata"]["page"]))
        sources = [page for _, page in ordered[:TOP_K]]
        rows.append(_measure(case, sources, candidates, dataset, (time.perf_counter() - started) * 1000))
        print(f"CN {index}/{len(dataset['cases'])} {case['id']}", flush=True)
    result = {"status": "ok", "evaluated_at": datetime.now(UTC).isoformat(),
              "dataset_sha256": sha(DATASET_PATH), "model": {"name": MODEL_NAME, "revision": MODEL_REVISION},
              "protocol": {"candidate_budget": CANDIDATE_BUDGET, "semantic_budget": SEMANTIC_BUDGET,
                           "top_k": TOP_K, "semantic_scope": "lexical_top16_query_relevant_excerpt",
                           "online_path_changed": False},
              "metrics": _summary(rows),
              "by_ticker": {ticker: _summary([r for r in rows if r["ticker"] == ticker]) for ticker in pages},
              "cases": rows,
              "limitations": ["开发集不是独立盲测", "字段数值共现不证明期间/单位/列归属", "语义精排只覆盖字面前16页", "默认线上路径未改变"]}
    return result


def render(result: dict[str, Any]) -> str:
    m = result["metrics"]
    return ("# 中文官方年报召回开发集\n\n"
            f"资料：三家中文上市公司2025年年报，26条中文问答。\n\n"
            f"- Candidate Recall@48: {m['candidate_recall_at_48']:.2%}\n"
            f"- Page Recall@4: {m['page_recall_at_4']:.2%}\n"
            f"- 字段/数值同页共现@4（非严格证据）: {m['field_value_cooccurrence_at_4']:.2%}\n"
            f"- Top-1: {m['top1']:.2%}\n"
            f"- 跨标的污染: {m['ticker_pollution']}\n\n"
            "仅为隔离开发集；同页共现不能证明金额归属、期间、单位和可回答性。不切默认线上路径。\n")


def main() -> int:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run()
    except Exception as exc:
        failure = {"status": "BLOCKED", "reason": f"{type(exc).__name__}:{exc}"}
        RESULT_PATH.with_name("chinese_retrieval_bootstrap_blocked.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("BLOCKED", failure["reason"], flush=True)
        return 1
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_PATH.write_text(render(result), encoding="utf-8")
    print(json.dumps(result["metrics"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
