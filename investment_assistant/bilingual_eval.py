"""R0 四象限双语检索评测：在不改动报告生成检索路径的前提下量化跨语言检索质量。

与既有 `evaluation.py` 的区别：
- 覆盖中→中 / 英→英 / 中→英 / 英→中 四个象限，以及 AAPL / MSFT / 600519.SS / 0700.HK 四个标的；
- 同一份资料快照下同时跑「按 ticker 限定」与「不限定 ticker」两组，后者用于量化跨标的污染；
- 按象限拆分指标并给出失败归因标签。

硬约束：
- 不改 `rag.py` 的检索与排序逻辑，也不改 `workflow.py` 的默认检索路径；
- 语义模式若实际未加载成功，直接记录阻塞原因，**不把 Hash fallback 当成 Semantic 成绩**；
- 资料 sha256 与样本集记录不一致时记录资料漂移并拒绝给出可比结论。

用法：
    python -m investment_assistant.bilingual_eval --top-k 4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, KNOWLEDGE_DIR
from .rag import LocalResearchRAG

EVAL_SET_PATH = DATA_DIR / "bilingual_eval_set.json"
RESULT_DIR = DATA_DIR / "evaluations"
QUADRANT_ORDER = ["zh-zh", "en-en", "zh-en", "en-zh"]
QUADRANT_LABEL = {
    "zh-zh": "中→中（中文问 / 中文资料）",
    "en-en": "英→英（英文问 / 英文资料）",
    "zh-en": "中→英（中文问 / 英文资料）",
    "en-zh": "英→中（英文问 / 中文资料）",
}
MODES = ["hash", "semantic"]


def load_eval_set(path: Path = EVAL_SET_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _page_of(source: dict[str, Any]) -> int | None:
    try:
        return int(source["metadata"].get("page", ""))
    except (TypeError, ValueError):
        return None


def _keyword_match(content: str, keywords: list[str]) -> bool:
    lowered = content.lower()
    return all(keyword.lower() in lowered for keyword in keywords)


def verify_materials(materials: dict[str, Any]) -> dict[str, Any]:
    """校验本地资料与样本集记录的 sha256 是否一致；不一致即视为资料漂移。"""
    report: dict[str, Any] = {}
    for ticker, spec in materials.items():
        path = KNOWLEDGE_DIR / spec["file_name"]
        if not path.exists():
            report[ticker] = {"status": "missing", "path": str(path)}
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        report[ticker] = {
            "status": "match" if actual == spec["sha256"] else "drift",
            "expected_sha256": spec["sha256"],
            "actual_sha256": actual,
            "page_authority": spec.get("page_authority"),
        }
    return report


def _failure_reason(case_result: dict[str, Any], case: dict[str, Any]) -> str:
    """按优先级给出单一主因，便于归类统计；不做多标签稀释。"""
    if case_result["cross_ticker_source_count_unscoped"]:
        return "wrong_ticker_source"
    if not case_result["page_hit_at_k"]:
        return "cross_language_miss" if case["question_lang"] != case["doc_lang"] else "candidate_miss"
    if not case_result["keyword_verified_hit_at_k"]:
        return "chunk_or_value_boundary"
    if not case_result["top_result_is_relevant"]:
        return "ranking"
    return "none"


def run_case(rag: LocalResearchRAG, case: dict[str, Any], top_k: int) -> dict[str, Any]:
    scoped = rag.search(case["question"], limit=top_k, ticker=case["ticker"])
    unscoped = rag.search(case["question"], limit=top_k)

    scoped_pages = [_page_of(source) for source in scoped]
    target_pages = case["target_pages"]
    relevant = [source for source in scoped if _page_of(source) in target_pages]
    forbidden = set(case.get("forbidden_tickers", []))
    unscoped_tickers = [str(source["metadata"].get("ticker", "")) for source in unscoped]
    cross_ticker = [ticker for ticker in unscoped_tickers if ticker in forbidden]

    result = {
        "id": case["id"],
        "quadrant": case["quadrant"],
        "ticker": case["ticker"],
        "question": case["question"],
        "target_pages": target_pages,
        "keywords": case["keywords"],
        "retrieved_pages": scoped_pages,
        "page_hit_at_k": bool(relevant),
        "keyword_verified_hit_at_k": any(_keyword_match(source["content"], case["keywords"]) for source in relevant),
        "citation_page_relevance": round(len(relevant) / top_k, 4),
        "top_result_page": scoped_pages[0] if scoped_pages else None,
        "top_result_is_relevant": bool(scoped_pages and scoped_pages[0] in target_pages),
        "retrieved_files": [source["metadata"].get("file_name") for source in scoped],
        "unscoped_tickers": unscoped_tickers,
        "cross_ticker_source_count_unscoped": len(cross_ticker),
        "unscoped_page_hit_at_k": any(_page_of(source) in target_pages for source in unscoped),
    }
    result["failure_reason"] = _failure_reason(result, case)
    return result


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    if not total:
        return {"case_count": 0}
    return {
        "case_count": total,
        "page_recall_at_k": round(sum(item["page_hit_at_k"] for item in cases) / total, 4),
        "keyword_verified_recall_at_k": round(sum(item["keyword_verified_hit_at_k"] for item in cases) / total, 4),
        "top1_page_relevance": round(sum(item["top_result_is_relevant"] for item in cases) / total, 4),
        "citation_page_relevance": round(sum(item["citation_page_relevance"] for item in cases) / total, 4),
        "cross_ticker_sources_per_case_unscoped": round(
            sum(item["cross_ticker_source_count_unscoped"] for item in cases) / total, 4
        ),
    }


def run_mode(mode: str, eval_set: dict[str, Any], top_k: int = 4) -> dict[str, Any]:
    if top_k != 4:
        raise ValueError("R0 evaluation Top-K is frozen at 4 to stay comparable with the Apple baseline")
    os.environ["RAG_EMBEDDING_MODE"] = mode
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    eval_chroma = DATA_DIR / "evaluation_chroma" / f"bilingual_{mode}"
    if eval_chroma.exists():
        shutil.rmtree(eval_chroma)
    rag = LocalResearchRAG(path=eval_chroma)
    status = rag.retrieval_status()
    if mode == "semantic" and status["embedding_mode"] != "semantic":
        return {
            "mode": mode,
            "status": status,
            "error": "语义模型无法从本地缓存加载，Semantic 分支未运行；不将 Hash fallback 记为 Semantic 成绩。",
        }

    indexed: dict[str, int] = {}
    for ticker, spec in eval_set["materials"].items():
        pdf_path = KNOWLEDGE_DIR / spec["file_name"]
        if not pdf_path.exists():
            return {"mode": mode, "status": status, "error": f"资料缺失：{pdf_path}"}
        indexed[ticker] = rag.index_pdf(pdf_path)

    cases = [run_case(rag, case, top_k) for case in eval_set["cases"]]
    by_quadrant = {
        quadrant: _aggregate([item for item in cases if item["quadrant"] == quadrant]) for quadrant in QUADRANT_ORDER
    }
    reasons: dict[str, int] = {}
    for item in cases:
        reasons[item["failure_reason"]] = reasons.get(item["failure_reason"], 0) + 1
    return {
        "mode": mode,
        "top_k": top_k,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "indexed_chunks": indexed,
        "metrics": _aggregate(cases),
        "by_quadrant": by_quadrant,
        "failure_reason_counts": reasons,
        "cases": cases,
    }


def render_report(eval_set: dict[str, Any], results: dict[str, dict[str, Any]], material_report: dict[str, Any]) -> str:
    lines = [
        "# R0 四象限双语检索评测（实验基线）",
        "",
        f"- 评测时间：{datetime.now(UTC).isoformat()}",
        "- 样本：`data/bilingual_eval_set.json`（32 条，gold page 由资料原文关键词共现 + 逐页人工核对确定，先于检索结果固化）",
        "- Top-K 冻结为 4，与既有 Apple 20 条基线保持一致",
        "- 本报告为小样本实验基线，**不外推为业务准确率**",
        "",
        "## 1. 资料快照校验",
        "",
        "| 标的 | 资料 | sha256 校验 | 页码权威性 |",
        "|---|---|---|---|",
    ]
    for ticker, spec in eval_set["materials"].items():
        check = material_report.get(ticker, {})
        lines.append(
            f"| {ticker} | {spec['file_name']} | {check.get('status', '未校验')} | {spec.get('page_authority', '未提供')} |"
        )
    lines.extend(
        [
            "",
            "> MSFT 的页码是本地 HTML→PDF 转换页码（`page_authority=generated`），不是 SEC 官方分页；其首页含 XBRL 标签噪音。",
            "",
            "## 2. 总体指标（按 ticker 限定检索，对齐线上行为）",
            "",
            "| 指标 | Hash | Semantic |",
            "|---|---:|---:|",
        ]
    )
    metric_labels = [
        ("page_recall_at_k", "Page Recall@4"),
        ("keyword_verified_recall_at_k", "关键词/数值核验 Recall@4"),
        ("top1_page_relevance", "Top-1 页面相关性"),
        ("citation_page_relevance", "引用页面相关率@4"),
        ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条（不限定 ticker 对照）"),
    ]
    for key, label in metric_labels:
        hash_value = results["hash"].get("metrics", {}).get(key, "未运行")
        semantic_value = results["semantic"].get("metrics", {}).get(key, results["semantic"].get("error", "未运行"))
        lines.append(f"| {label} | {hash_value} | {semantic_value} |")

    lines.extend(["", "## 3. 按象限拆分（Page Recall@4 / 关键词核验 Recall@4 / Top-1）", ""])
    for mode in MODES:
        payload = results[mode]
        lines.append(f"### {mode}")
        if "error" in payload:
            lines.append(f"- 未运行：{payload['error']}")
            continue
        lines.append("| 象限 | 样本数 | Page Recall@4 | 关键词核验 Recall@4 | Top-1 | 跨标的来源/条 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for quadrant in QUADRANT_ORDER:
            block = payload["by_quadrant"][quadrant]
            lines.append(
                f"| {QUADRANT_LABEL[quadrant]} | {block['case_count']} | {block['page_recall_at_k']} | "
                f"{block['keyword_verified_recall_at_k']} | {block['top1_page_relevance']} | {block['cross_ticker_sources_per_case_unscoped']} |"
            )
        lines.append("")

    lines.extend(["## 4. 失败归因分布", "", "| 归因 | Hash | Semantic |", "|---|---:|---:|"])
    reason_labels = {
        "none": "命中（无失败）",
        "wrong_ticker_source": "跨标的来源污染",
        "cross_language_miss": "跨语言召回失败",
        "candidate_miss": "同语言候选未召回",
        "chunk_or_value_boundary": "页命中但数值不在块内",
        "ranking": "已召回但排序未进 Top-1",
    }
    all_reasons = set(results["hash"].get("failure_reason_counts", {})) | set(
        results["semantic"].get("failure_reason_counts", {})
    )
    for reason in sorted(all_reasons, key=lambda item: list(reason_labels).index(item) if item in reason_labels else 99):
        hash_count = results["hash"].get("failure_reason_counts", {}).get(reason, 0)
        semantic_count = results["semantic"].get("failure_reason_counts", {}).get(reason, "未运行")
        lines.append(f"| {reason_labels.get(reason, reason)} | {hash_count} | {semantic_count} |")

    lines.extend(["", "## 5. 失败样本明细", ""])
    for mode in MODES:
        payload = results[mode]
        if "error" in payload:
            continue
        failures = [item for item in payload["cases"] if item["failure_reason"] != "none"]
        lines.append(f"### {mode}（{len(failures)} 条未完全命中）")
        if not failures:
            lines.append("- 无")
        for item in failures:
            lines.append(
                f"- `{item['id']}` [{item['quadrant']}/{item['ticker']}] 归因={item['failure_reason']}；"
                f"目标页 {item['target_pages']}；召回页 {item['retrieved_pages']}；"
                f"不限定 ticker 时跨标的来源 {item['cross_ticker_source_count_unscoped']} 条"
            )
        lines.append("")

    lines.extend(
        [
            "## 6. 已知限制",
            "",
            "- 四象限各 8 条，属小样本实验基线，不做统计显著性外推。",
            "- 术语映射 `data/financial_term_map.json` 只覆盖 中文→英文，英→中/英→英 象限不触发扩展。",
            "- 既有 Apple 25 条评测（含 5 条 holdout）全部是 中→英 单象限；holdout 的 gold keyword 与术语映射表存在 1:1 重合，其提升幅度不能直接作为泛化证据。",
            "- pypdf 未安装 fontTools，CFF 字体编码解析受限，可能影响部分页面抽取质量，归为「原始资料」类风险。",
        ]
    )
    return "\n".join(lines)


def run_evaluation(top_k: int = 4, eval_set_path: Path = EVAL_SET_PATH) -> dict[str, Any]:
    eval_set = load_eval_set(eval_set_path)
    material_report = verify_materials(eval_set["materials"])
    drift = [ticker for ticker, item in material_report.items() if item.get("status") != "match"]
    results = {mode: run_mode(mode, eval_set, top_k) for mode in MODES}
    payload = {
        "eval_set": str(eval_set_path),
        "top_k": top_k,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "material_verification": material_report,
        "material_drift": drift,
        "results": results,
    }
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "bilingual_r0.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / "bilingual_r0.md").write_text(
        render_report(eval_set, results, material_report), encoding="utf-8"
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="R0 四象限双语检索评测")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET_PATH)
    args = parser.parse_args()
    payload = run_evaluation(args.top_k, args.eval_set)
    print(json.dumps(payload["results"]["hash"].get("metrics", {}), ensure_ascii=False, indent=2))
    print(json.dumps(payload["results"]["semantic"].get("metrics", payload["results"]["semantic"].get("error")), ensure_ascii=False, indent=2))
    if payload["material_drift"]:
        print(f"资料漂移：{payload['material_drift']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
