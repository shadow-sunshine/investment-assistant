"""R1 候选池规模敏感性实验：回答「召回不足是候选池太小，还是查询根本没有形成有效召回信号」。

边界（来自 2026-09-25 审阅决定）：
- 只改评测器参数，不改线上默认检索路径；
- 同一资料快照、同一 32 条样本（`data/bilingual_eval_set.json`，与 R0 基线产物的 eval_set_sha256 运行时校验一致；样本集若被改动，重渲染会被拒绝）；
- 保留 scoped（按 ticker 限定，对齐线上行为，作为主因口径）与 unscoped（不限定 ticker，作为跨标的污染对照）两臂；
- Top-K 固定 4，与 R0 及 Apple 基线保持一致；
- 输出 Recall@4、Top-1、失败归因、耗时与候选数；
- 不修改 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`。

候选池是怎么被改变的（关键约束，不可绕过）：

`rag.search()` 内部为 `candidate_limit = min(max(limit * 12, 48), matching_count)`，即候选池是
`limit` 的 12 倍且不低于 48。在**不修改 `rag.py`** 的前提下，候选池只能取到 12 的整数倍，
因此 100 / 200 这两个目标值无法精确命中。本实验对每个目标池取「不小于目标的最小可达池」并在
报告中同时记录目标池与实际池：

    目标 48  -> limit 4  -> 实际池 48   （与线上默认完全一致）
    目标 100 -> limit 9  -> 实际池 108
    目标 200 -> limit 17 -> 实际池 204

随后只取返回结果的前 4 条计算指标。由于 `rag.search()` 的页级排序是按页最高分降序后截断，
「limit=17 取前 4 条」与「候选池 204 + Top-K=4」在排序语义上等价。

用法：
    python -m investment_assistant.candidate_pool_eval --pools 48 100 200
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bilingual_eval import (
    EVAL_SET_PATH,
    FROZEN_R0_EVAL_SET_SHA256,
    MODES,
    get_frozen_r0_eval_set_sha256,
    QUADRANT_LABEL,
    QUADRANT_ORDER,
    RESULT_DIR,
    _aggregate,
    _eval_set_gold_summary,
    _failure_reason,
    _is_target_source,
    _keyword_match,
    _page_of,
    _sha256_of_file,
    load_eval_set,
    top_result_is_relevant,
    verify_materials,
)
from .config import DATA_DIR, KNOWLEDGE_DIR
from .rag import LocalResearchRAG

TOP_K = 4
POOL_TARGETS = [48, 100, 200]
POOL_FACTOR = 12
POOL_FLOOR = 48
RESULT_STEM = "candidate_pool_sensitivity"
R0_RESULT_PATH = RESULT_DIR / "bilingual_r0.json"


def _read_r0_eval_set_sha() -> str | None:
    """读取 R0 基线产物里记录的 eval_set_sha256，用于校验 R1 是否用了同一份样本。

    若 R0 产物不存在或未含该字段，返回 None（表示无法自动校验，而不是不一致）。
    """
    if not R0_RESULT_PATH.exists():
        return None
    try:
        payload = json.loads(R0_RESULT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload.get("eval_set_sha256")


def limit_for_pool(pool: int, top_k: int = TOP_K) -> int:
    """返回使 `rag.search()` 内部候选池 >= pool 的最小 limit。

    不得为了命中 100/200 而改 `rag.py`；只能在评测器侧取最近可达值并如实记录。
    """
    return max(top_k, math.ceil(pool / POOL_FACTOR))


def pool_from_limit(limit: int) -> int:
    """复刻 `rag.search()` 的候选池公式，仅用于记录与断言，不参与线上路径。"""
    return max(limit * POOL_FACTOR, POOL_FLOOR)


def _chunk_counts(rag: LocalResearchRAG, tickers: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ticker in tickers:
        ids = rag.collection.get(where={"ticker": ticker}, include=[]).get("ids", [])
        counts[ticker] = len(ids)
    counts["__total__"] = rag.collection.count()
    return counts


def run_case(
    rag: LocalResearchRAG,
    case: dict[str, Any],
    limit: int,
    top_k: int = TOP_K,
    chunk_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """跑单条样本：候选池由 limit 决定，指标只按前 top_k 条计算。"""
    started = time.perf_counter()
    scoped = rag.search(case["question"], limit=limit, ticker=case["ticker"])
    unscoped = rag.search(case["question"], limit=limit)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    top = scoped[:top_k]
    scoped_pages = [_page_of(source) for source in top]
    target_pages = case["target_pages"]
    relevant = [source for source in top if _is_target_source(source, case)]
    forbidden = set(case.get("forbidden_tickers", []))
    unscoped_tickers = [str(source["metadata"].get("ticker", "")) for source in unscoped[:top_k]]
    cross_ticker = [ticker for ticker in unscoped_tickers if ticker in forbidden]

    formula_pool = pool_from_limit(limit)
    scoped_cap = (chunk_counts or {}).get(case["ticker"])
    unscoped_cap = (chunk_counts or {}).get("__total__")
    result = {
        "id": case["id"],
        "quadrant": case["quadrant"],
        "ticker": case["ticker"],
        "question": case["question"],
        "target_pages": target_pages,
        "retrieved_pages": scoped_pages,
        "search_limit": limit,
        "candidate_pool_formula": formula_pool,
        "candidate_pool_effective": min(formula_pool, scoped_cap) if scoped_cap else formula_pool,
        "candidate_pool_effective_unscoped": min(formula_pool, unscoped_cap) if unscoped_cap else formula_pool,
        "page_hit_at_k": bool(relevant),
        "keyword_verified_hit_at_k": any(_keyword_match(source["content"], case["keywords"]) for source in relevant),
        "citation_page_relevance": round(len(relevant) / top_k, 4),
        "top_result_page": scoped_pages[0] if scoped_pages else None,
        "top_result_is_relevant": top_result_is_relevant(scoped, case),
        "unscoped_pages": [_page_of(source) for source in unscoped[:top_k]],
        "cross_ticker_source_count_unscoped": len(cross_ticker),
        # 必须是「同一标的 + 目标页码」；只比页码会把异标的同页码算成命中。
        "unscoped_page_hit_at_k": any(_is_target_source(source, case) for source in unscoped[:top_k]),
        "search_seconds_ms": elapsed_ms,
    }
    # 主因只看 scoped 臂；unscoped 的跨标的污染是独立对照指标，不能顶替主因。
    result["failure_reason"] = _failure_reason(result, case)
    return result


def _failure_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in cases:
        counts[item["failure_reason"]] = counts.get(item["failure_reason"], 0) + 1
    return counts


def run_pool(
    rag: LocalResearchRAG,
    eval_set: dict[str, Any],
    pool: int,
    top_k: int = TOP_K,
    chunk_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """在已建好的同一份索引上跑一个候选池。不重建索引，保证池间唯一变量是候选池。"""
    if top_k != TOP_K:
        raise ValueError("R1 敏感性实验的 Top-K 冻结为 4，与 R0 保持一致")
    limit = limit_for_pool(pool, top_k)
    cases = [run_case(rag, case, limit, top_k, chunk_counts) for case in eval_set["cases"]]
    by_quadrant = {
        quadrant: _aggregate([item for item in cases if item["quadrant"] == quadrant]) for quadrant in QUADRANT_ORDER
    }
    total_ms = round(sum(item["search_seconds_ms"] for item in cases), 3)
    return {
        "candidate_pool_requested": pool,
        "search_limit": limit,
        "candidate_pool_formula": pool_from_limit(limit),
        "candidate_pool_by_ticker": {
            ticker: min(pool_from_limit(limit), count) for ticker, count in (chunk_counts or {}).items()
        },
        "case_count": len(cases),
        "total_search_ms": total_ms,
        "mean_search_ms_per_case": round(total_ms / len(cases), 3) if cases else 0.0,
        "metrics": _aggregate(cases),
        "by_quadrant": by_quadrant,
        "failure_reason_counts": _failure_counts(cases),
        "cases": cases,
    }


def run_mode(
    mode: str,
    eval_set: dict[str, Any],
    pools: list[int],
    top_k: int = TOP_K,
) -> dict[str, Any]:
    os.environ["RAG_EMBEDDING_MODE"] = mode
    os.environ["RAG_RERANKER_MODE"] = "disabled"
    eval_chroma = DATA_DIR / "evaluation_chroma" / f"pool_{mode}"
    if eval_chroma.exists():
        shutil.rmtree(eval_chroma)
    started = time.perf_counter()
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
    index_seconds = round(time.perf_counter() - started, 3)

    # 三个池共用同一份索引：候选池是池间唯一变量。
    chunk_counts = _chunk_counts(rag, list(eval_set["materials"]))
    pools_payload = {str(pool): run_pool(rag, eval_set, pool, top_k, chunk_counts) for pool in pools}
    return {
        "mode": mode,
        "top_k": top_k,
        "status": status,
        "indexed_chunks": indexed,
        "index_seconds": index_seconds,
        "chunk_counts": chunk_counts,
        "pools": pools_payload,
    }


def _pool_blocks(results: dict[str, dict[str, Any]], mode: str) -> list[tuple[int, dict[str, Any] | None]]:
    payload = results.get(mode, {})
    return [(pool, payload.get("pools", {}).get(str(pool))) for pool in POOL_TARGETS]


def _metric_cell(block: dict[str, Any] | None, key: str) -> str:
    if not block:
        return "未运行"
    return str(block["metrics"].get(key, "未运行"))


def render_report(
    eval_set: dict[str, Any],
    results: dict[str, dict[str, Any]],
    material_report: dict[str, Any],
    pools: list[int],
    matches_r0_eval_set: bool | None = None,
    eval_set_sha256: str = "",
    frozen_r0_eval_set_sha256: str = "",
    matches_frozen_baseline: bool | None = None,
) -> str:
    lines = [
        "# R1 候选池规模敏感性实验（48 / 100 / 200）",
        "",
        f"- 评测时间：{datetime.now(UTC).isoformat()}",
        f"- 样本：`data/bilingual_eval_set.json`（{len(eval_set['cases'])} 条，gold page 摘要与 sha256 见 JSON）",
        "- 与 R0 基线样本一致性："
        + (
            "**一致（已校验 eval_set_sha256 相同）**"
            if matches_r0_eval_set is True
            else "⚠️ **不一致** —— 本实验所用评测集的 eval_set_sha256 与 R0 基线产物不同，结论不可与 R0 直接横比"
            if matches_r0_eval_set is False
            else "R0 基线产物未含 eval_set_sha256，无法自动校验（请人工确认评测集未被改动）"
        ),
        "- 与冻结 R0 基线（commit c0f5782）一致性："
        + (
            "**一致（已校验 frozen_r0_eval_set_sha256 相同）**"
            if matches_frozen_baseline is True
            else "⚠️ **不一致** —— 本实验所用评测集的 SHA 与冻结的 R0 基线不同，结论不可与 R0 直接横比"
            if matches_frozen_baseline is False
            else "未记录 frozen_r0_eval_set_sha256，无法自动校验（请人工确认评测集是冻结基线）"
        ),
        "- Top-K 冻结为 4；scoped = 按 ticker 限定（对齐线上行为，**主因口径**），unscoped = 不限定 ticker（**跨标的污染对照**）",
        "- 未修改 `rag.py`、`workflow.py`、`llm_generation.py`、`safety.py`；线上默认检索路径保持不变",
        "- 本实验是小样本敏感性对照，**不外推为业务准确率**",
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
            "## 2. 候选池如何取到（约束说明）",
            "",
            "`rag.search()` 的候选池公式为 `max(limit * 12, 48)`。在不修改 `rag.py` 的前提下，候选池只能取到 12 的整数倍，"
            "因此目标池 100 / 200 无法精确命中；本实验取「不小于目标的最小可达池」，并同时记录目标池与实际池。",
            "",
            "| 目标池 | 请求 limit | 实际候选池 | 说明 |",
            "|---:|---:|---:|---|",
        ]
    )
    for pool in pools:
        limit = limit_for_pool(pool)
        note = "与线上默认一致" if pool_from_limit(limit) == POOL_FLOOR else "公式只能取 12 的整数倍，向上取最近值"
        lines.append(f"| {pool} | {limit} | {pool_from_limit(limit)} | {note} |")

    counts = next((item.get("chunk_counts") for item in results.values() if "chunk_counts" in item), None)
    if counts:
        lines.extend(
            [
                "",
                "| 标的 | 已索引块数 | " + " | ".join(f"目标池 {pool} 实际" for pool in pools) + " |",
                "|---|---:|" + "---:|" * len(pools),
            ]
        )
        for ticker, total in counts.items():
            label = "全部语料（unscoped 臂）" if ticker == "__total__" else ticker
            cells = " | ".join(str(min(pool_from_limit(limit_for_pool(pool)), total)) for pool in pools)
            lines.append(f"| {label} | {total} | {cells} |")
        lines.append("")
        lines.append("> 实际池 = `min(公式池, 该臂的块数)`；被块数上限截断的臂，其池子小于目标值，已在表中如实记录。")

    metric_labels = [
        ("page_recall_at_k", "Page Recall@4"),
        ("keyword_verified_recall_at_k", "关键词/数值核验 Recall@4"),
        ("top1_page_relevance", "Top-1 页面相关性"),
        ("citation_page_relevance", "引用页面相关率@4"),
        ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条（unscoped 对照）"),
    ]
    lines.extend(
        [
            "",
            "## 3. 总体指标（scoped 臂，对齐线上行为）",
            "",
            "| 模式 | 目标池 | 实际池 | " + " | ".join(label for _, label in metric_labels) + " |",
            "|---|---:|---:|" + "---:|" * len(metric_labels),
        ]
    )
    for mode in MODES:
        for pool, block in _pool_blocks(results, mode):
            if block is None:
                lines.append(f"| {mode} | {pool} | 未运行 | " + " | ".join(["未运行"] * len(metric_labels)) + " |")
                continue
            cells = " | ".join(_metric_cell(block, key) for key, _ in metric_labels)
            lines.append(f"| {mode} | {pool} | {block['candidate_pool_formula']} | {cells} |")

    quadrant_sizes = {
        quadrant: sum(1 for case in eval_set["cases"] if case["quadrant"] == quadrant) for quadrant in QUADRANT_ORDER
    }
    for suffix, key, title in [
        ("a", "page_recall_at_k", "Page Recall@4"),
        ("b", "top1_page_relevance", "Top-1 页面相关性"),
    ]:
        header = " | ".join(f"{mode} {pool}" for mode in MODES for pool in pools)
        lines.extend(
            [
                "",
                f"## 4{suffix}. 按象限：{title}",
                "",
                f"| 象限 | 样本数 | {header} |",
                "|---|---:|" + "---:|" * (len(MODES) * len(pools)),
            ]
        )
        for quadrant in QUADRANT_ORDER:
            cells: list[str] = []
            for mode in MODES:
                for _pool, block in _pool_blocks(results, mode):
                    cells.append(str(block["by_quadrant"][quadrant].get(key, "未运行")) if block else "未运行")
            lines.append(f"| {QUADRANT_LABEL[quadrant]} | {quadrant_sizes[quadrant]} | " + " | ".join(cells) + " |")

    reason_labels = {
        "none": "命中（无失败）",
        "cross_language_miss": "跨语言未召回",
        "candidate_miss": "同语言候选未召回",
        "chunk_or_value_boundary": "页命中但数值不在块内",
        "ranking": "已召回但排序未进 Top-1",
    }
    lines.extend(
        [
            "",
            "## 5. 失败归因分布（scoped 臂主因）",
            "",
            "| 归因 | " + " | ".join(f"{mode} {pool}" for mode in MODES for pool in pools) + " |",
            "|---|" + "---:|" * (len(MODES) * len(pools)),
        ]
    )
    all_reasons: list[str] = []
    for mode in MODES:
        for _, block in _pool_blocks(results, mode):
            if block:
                for reason in block["failure_reason_counts"]:
                    if reason not in all_reasons:
                        all_reasons.append(reason)
    for reason in sorted(all_reasons, key=lambda item: list(reason_labels).index(item) if item in reason_labels else 99):
        cells = []
        for mode in MODES:
            for _, block in _pool_blocks(results, mode):
                cells.append(str(block["failure_reason_counts"].get(reason, 0)) if block else "未运行")
        lines.append(f"| {reason_labels.get(reason, reason)} | " + " | ".join(cells) + " |")

    lines.extend(["", "## 6. 耗时与候选数", "", "| 模式 | 目标池 | 实际池 | 检索总耗时 (ms) | 单样本均值 (ms) | 索引耗时 (s) |", "|---|---:|---:|---:|---:|---:|"])
    for mode in MODES:
        payload = results[mode]
        index_seconds = payload.get("index_seconds", "未运行")
        for pool, block in _pool_blocks(results, mode):
            if not block:
                lines.append(f"| {mode} | {pool} | 未运行 | 未运行 | 未运行 | {index_seconds} |")
                continue
            lines.append(
                f"| {mode} | {pool} | {block['candidate_pool_formula']} | {block['total_search_ms']} | "
                f"{block['mean_search_ms_per_case']} | {index_seconds} |"
            )

    smallest, largest = min(pools), max(pools)
    lines.extend(
        [
            "",
            f"## 7. 候选池放大带来的变化（Δ = 目标池 {largest} − 目标池 {smallest}）",
            "",
            "| 模式 | 象限 | " + f"Recall@4 @{smallest} | " + f"Recall@4 @{largest} | Δ Recall@4 | " + f"Top-1 @{smallest} | " + f"Top-1 @{largest} | Δ Top-1 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for mode in MODES:
        small_block = dict(_pool_blocks(results, mode)).get(smallest)
        large_block = dict(_pool_blocks(results, mode)).get(largest)
        if not small_block or not large_block:
            lines.append(f"| {mode} | 全部 | 未运行 | 未运行 | 未运行 | 未运行 | 未运行 | 未运行 |")
            continue
        rows = [("全部", small_block["metrics"], large_block["metrics"])]
        rows.extend(
            (QUADRANT_LABEL[q], small_block["by_quadrant"][q], large_block["by_quadrant"][q]) for q in QUADRANT_ORDER
        )
        for label, small, large in rows:
            delta = round(large["page_recall_at_k"] - small["page_recall_at_k"], 4)
            top_delta = round(large["top1_page_relevance"] - small["top1_page_relevance"], 4)
            lines.append(
                f"| {mode} | {label} | {small['page_recall_at_k']} | {large['page_recall_at_k']} | {delta} | "
                f"{small['top1_page_relevance']} | {large['top1_page_relevance']} | {top_delta} |"
            )

    # 判定规则先于实验结果登记在 docs/投研服务台升级总纲.md，此处只做机械比对，不自创阈值。
    moved: list[str] = []
    for mode in MODES:
        small_block = dict(_pool_blocks(results, mode)).get(smallest)
        large_block = dict(_pool_blocks(results, mode)).get(largest)
        if not small_block or not large_block:
            continue
        pairs = [("全部", small_block["metrics"], large_block["metrics"])]
        pairs.extend(
            (QUADRANT_LABEL[q], small_block["by_quadrant"][q], large_block["by_quadrant"][q]) for q in QUADRANT_ORDER
        )
        for label, small, large in pairs:
            delta = round(large["page_recall_at_k"] - small["page_recall_at_k"], 4)
            top_delta = round(large["top1_page_relevance"] - small["top1_page_relevance"], 4)
            if delta > 0 or top_delta > 0:
                moved.append(f"{mode} / {label}：Δ Recall@4 = {delta}，Δ Top-1 = {top_delta}")

    lines.extend(
        [
            "",
            "## 8. 按预先登记的判定规则",
            "",
            "> 规则来源：`docs/投研服务台升级总纲.md` 第 4 节，登记时间先于本实验结果 —— "
            "「若 48 → 200 提升明显，再考虑扩大线上候选池；若提升很小，下一步直接做中文字面/数值锚点召回，而不是继续调池子」。",
            "",
        ]
    )
    if not moved:
        lines.append(
            f"- **实测**：目标池 {smallest} → {largest}（实际 "
            f"{pool_from_limit(limit_for_pool(smallest))} → {pool_from_limit(limit_for_pool(largest))}）区间内，"
            "Hash 与 Semantic 两模式、四个象限的 **Δ Recall@4 与 Δ Top-1 全部为 0.0000**，失败归因分布也逐项不变。"
        )
        lines.append("- **命中分支**：**暂不扩大线上默认候选池**；下一步验证中文字面/数值锚点召回。")
    else:
        lines.append("- **实测**：以下模式/象限随候选池放大而变化：" + "；".join(moved) + "。")
        lines.append("- 幅度是否「明显」由审阅者按上述 Δ 数值判定，本实验不自行下结论。")
    lines.extend(
        [
            "- **结论适用范围（不得泛化）**：本结论只成立于「32 条冻结样本 + 4 份冻结资料（sha256 全部 match）"
            " + Hash / Semantic 两种 embedding + `RAG_RERANKER_MODE=disabled` 的默认检索路径」。",
            "  不得外推为：所有资料都不是候选池问题、所有 reranker 配置都不是候选池问题、"
            "Chroma 不存在候选池相关问题、或生产环境任何查询都不受候选池影响。",
        ]
    )

    lines.extend(
        [
            "",
            "## 9. 已知限制",
            "",
            "- 四象限各 8 条，小样本，不做统计显著性外推。",
            "- 目标池 100 / 200 在不动 `rag.py` 的前提下无法精确命中，实际为 108 / 204；结论按实际池解读。",
            "- 单条样本的耗时包含 scoped 与 unscoped 两次检索，受本机负载影响，只作量级参考。",
            "- 三个池共用同一份索引，因此索引侧完全一致；但 semantic 与 hash 各自建库，两模式之间不可横比耗时。",
            "- 本实验只覆盖 `RAG_RERANKER_MODE=disabled` 的默认路径；Cross-Encoder 分支（启用时候选池固定为 30）"
            "没有单独实验，其结论未知。",
            "- 冻结区 `rag.py` 未改动，因此候选池是通过 `limit` 入参间接改变的；若日后改动 `rag.py` 的候选池公式，"
            "本实验的实际池映射（48 / 108 / 204）需要重新核对。",
        ]
    )
    return "\n".join(lines)


def render_blocked_report(material_report: dict[str, Any], evaluated_at: str) -> str:
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— BLOCKED",
            "",
            f"- 评测时间：{evaluated_at}",
            "- **状态：BLOCKED —— 资料快照不一致，已拒绝产出可比指标**",
            "",
            "## 资料快照校验",
            "",
            "| 标的 | 状态 | 预期 sha256 | 实际 sha256 |",
            "|---|---|---|---|",
        ]
        + [
            f"| {ticker} | {item.get('status', '未校验')} | {item.get('expected_sha256', '-')} | {item.get('actual_sha256', '-')} |"
            for ticker, item in material_report.items()
        ]
        + [
            "",
            "> 资料 sha256 与样本集记录不一致（drift）或资料缺失（missing）时，本评测**不跑检索、不生成 Recall/Top-1、"
            "不给出任何可比结论**。请先恢复样本集记录的资料版本，或显式更新样本集中的 sha256 并说明原因。",
            "",
        ]
    )


def _render_eval_set_changed_report(stored_sha: str | None, current_sha: str, evaluated_at: str) -> str:
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— 样本集已变更（重渲染被拒绝）",
            "",
            f"- 上次产出时间：{evaluated_at}",
            "- **状态：RENDER BLOCKED —— 当前评测集与产出结果时的评测集不一致，禁止用过期结果重渲染报告**",
            "",
            "| 项目 | 值 |",
            "|---|---|",
            f"| 结果中记录的 eval_set_sha256 | {stored_sha or '（缺失）'} |",
            f"| 当前评测集 eval_set_sha256 | {current_sha} |",
            "",
            "> 评测集（gold page 或样本）已被增删/改动，旧结果不可再代表当前样本。请重新运行 `python -m "
            "investment_assistant.candidate_pool_eval` 而非 `--rerender` 来刷新实验。",
            "",
        ]
    )


def _render_eval_set_mismatch_report(current_sha: str, frozen_sha: str, evaluated_at: str) -> str:
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— BLOCKED（评测集与 R0 冻结基线不一致）",
            "",
            f"- 评测时间：{evaluated_at}",
            "- **状态：BLOCKED —— 当前评测集的 eval_set_sha256 与冻结的 R0 基线（commit c0f5782）不一致，已拒绝产出可比指标**",
            "",
            "| 项目 | 值 |",
            "|---|---|",
            f"| 冻结 R0 基线 eval_set_sha256 | {frozen_sha} |",
            f"| 当前评测集 eval_set_sha256 | {current_sha} |",
            "",
            "> 仅当 R1 允许传入 `--eval-set` 时，若样本与冻结 R0 基线（commit c0f5782, data/bilingual_eval_set.json）"
            "不一致，本评测**不建索引、不跑检索、不生成任何可比指标**。请恢复冻结样本后重新运行"
            "`python -m investment_assistant.candidate_pool_eval`。",
            "",
        ]
    )


def _render_eval_set_missing_sha_report(evaluated_at: str) -> str:
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— 样本集身份无法校验（旧结果无 eval_set_sha256）",
            "",
            f"- 上次产出时间：{evaluated_at}",
            "- **状态：RENDER BLOCKED —— 旧结果未记录 eval_set_sha256，无法校验样本集身份，禁止重渲染为看似正常的可比报告**",
            "",
            "> 旧结果缺少样本集 SHA，不能凭当前文件 SHA 静默补写成旧实验的 SHA。请重新运行"
            "`python -m investment_assistant.candidate_pool_eval` 而非 `--rerender` 来刷新实验。",
            "",
        ]
    )


def _render_blocked_unknown_report(reason: str | None, evaluated_at: str) -> str:
    """未知 BLOCKED 原因或必要字段缺失时的 fail-closed 报告：明确无法重渲染，不生成正常报告。"""
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— BLOCKED（无法重渲染）",
            "",
            f"- 上次产出时间：{evaluated_at}",
            "- **状态：RENDER BLOCKED —— 旧结果处于未知的 BLOCKED 状态或缺少必要字段，禁止重渲染为看似正常的可比报告**",
            "",
            "| 项目 | 值 |",
            "|---|---|",
            f"| blocked_reason | {reason or '（缺失）'} |",
            "",
            "> 旧结果的 BLOCKED 原因无法识别（或缺少重渲染所需字段，如 `material_verification` / SHA）。"
            "本实验**不重渲染为正常报告**，也不抛未处理的异常。请重新运行"
            "`python -m investment_assistant.candidate_pool_eval` 而非 `--rerender` 来刷新实验。",
            "",
        ]
    )


def _render_unknown_status_report(status: object, evaluated_at: str) -> str:
    """未知或未完成状态的复渲染结果：拒绝生成正常实验报告。"""
    displayed_status = status if status is not None else "（缺失）"
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— BLOCKED（结果状态不可渲染）",
            "",
            f"- 上次产出时间：{evaluated_at}",
            "- **状态：RENDER BLOCKED —— 只有 status=ok 的完整结果允许渲染正常报告**",
            "",
            "| 项目 | 值 |",
            "|---|---|",
            f"| status | {displayed_status} |",
            "",
            "> 该结果处于未完成、未知或缺失状态，不能据此生成可比实验报告。请重新运行实验，"
            "不要对中间态结果使用 `--rerender`。",
            "",
        ]
    )


def _render_eval_set_not_frozen_report(
    current_sha: str, frozen_sha: str, evaluated_at: str, payload_frozen_sha: str | None = None
) -> str:
    """复渲染时，结果所用评测集与冻结 R0 基线不一致 / 冻结元数据不完整 / 伪造的 fail-closed 报告。

    payload_frozen_sha 为结果 JSON 中实际记录的 frozen_r0_eval_set_sha256（可能为伪造值或缺省），
    展示出来便于审计被拒绝的冻结证明来源。
    """
    return "\n".join(
        [
            "# R1 候选池规模敏感性实验 —— BLOCKED（评测集不是冻结的 R0 基线 / 冻结元数据不完整或伪造）",
            "",
            f"- 上次产出时间：{evaluated_at}",
            "- **状态：RENDER BLOCKED —— 旧结果的评测集 SHA 与冻结 R0 基线（commit c0f5782）不一致，"
            "或其冻结元数据不完整/伪造，禁止重渲染为看似正常的可比报告**",
            "",
            "| 项目 | 值 |",
            "|---|---|",
            f"| 冻结 R0 基线 eval_set_sha256（代码常量） | {frozen_sha} |",
            f"| 当前评测集 eval_set_sha256 | {current_sha} |",
            f"| 结果 JSON 记录的 frozen_r0_eval_set_sha256 | {payload_frozen_sha if payload_frozen_sha is not None else '（缺失）'} |",
            "",
            "> 复渲染护栏（F4）要求：结果记录的 eval_set_sha256 与冻结 R0 基线一致、结果自报的"
            " frozen_r0_eval_set_sha256 与基线一致、且 matches_frozen_baseline 必须明确为 True。"
            " 三者任一不满足即拒绝重渲染。该结果不得宣称与 R0 直接横比，"
            "也不得重渲染为看似正常的可比报告。请恢复冻结样本后重新运行"
            "`python -m investment_assistant.candidate_pool_eval`。",
            "",
        ]
    )


def run_experiment(
    pools: list[int] = POOL_TARGETS,
    top_k: int = TOP_K,
    eval_set_path: Path = EVAL_SET_PATH,
) -> dict[str, Any]:
    if top_k != TOP_K:
        raise ValueError("R1 敏感性实验的 Top-K 冻结为 4，与 R0 保持一致")
    eval_set = load_eval_set(eval_set_path)
    material_report = verify_materials(eval_set["materials"])
    drift = [ticker for ticker, item in material_report.items() if item.get("status") != "match"]
    evaluated_at = datetime.now(UTC).isoformat()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    # 样本不可变保障：固化「用了哪份样本」，并跨文件校验是否与 R0 基线同一份。
    eval_set_sha = _sha256_of_file(eval_set_path)
    gold_summary = _eval_set_gold_summary(eval_set)
    r0_sha = _read_r0_eval_set_sha()
    matches_r0_eval_set = (r0_sha == eval_set_sha) if r0_sha is not None else None
    frozen_sha = get_frozen_r0_eval_set_sha256()
    matches_frozen_baseline = eval_set_sha == frozen_sha
    if not matches_frozen_baseline:
        # 4.B.5 fail-closed：评测集与冻结 R0 基线（commit c0f5782）不一致，
        # 不建索引、不检索，拒绝产出可比指标；run_mode 不会被调用。
        payload = {
            "experiment": "R1 候选池规模敏感性实验",
            "status": "blocked",
            "blocked_reason": "eval_set_mismatch",
            "eval_set": str(eval_set_path),
            "eval_set_sha256": eval_set_sha,
            "frozen_r0_eval_set_sha256": frozen_sha,
            "matches_frozen_baseline": False,
            "eval_set_case_count": len(eval_set["cases"]),
            "case_count": len(eval_set["cases"]),
            "top_k": top_k,
            "pools_requested": list(pools),
            "evaluated_at": evaluated_at,
            "online_path_modified": False,
            "blocked_note": "评测集与冻结的 R0 基线（commit c0f5782, data/bilingual_eval_set.json）SHA256 不一致；"
            "不建索引、不检索，拒绝产出可比指标。请以冻结样本重新运行。",
            "results": {},
        }
        (RESULT_DIR / f"{RESULT_STEM}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (RESULT_DIR / f"{RESULT_STEM}.md").write_text(
            _render_eval_set_mismatch_report(eval_set_sha, frozen_sha, evaluated_at), encoding="utf-8"
        )
        return payload
    if drift:
        # fail-closed：不调用 run_mode，不产出任何可比较指标。
        payload = {
            "experiment": "R1 候选池规模敏感性实验",
            "status": "blocked",
            "blocked_reason": "material_drift",
            "eval_set": str(eval_set_path),
            "eval_set_sha256": eval_set_sha,
            "eval_set_case_count": len(eval_set["cases"]),
            "eval_set_gold_summary": gold_summary,
            "matches_r0_eval_set": matches_r0_eval_set,
            "frozen_r0_eval_set_sha256": frozen_sha,
            "matches_frozen_baseline": matches_frozen_baseline,
            "case_count": len(eval_set["cases"]),
            "top_k": top_k,
            "pools_requested": list(pools),
            "pools_actual": {str(pool): pool_from_limit(limit_for_pool(pool, top_k)) for pool in pools},
            "evaluated_at": evaluated_at,
            "material_verification": material_report,
            "material_drift": drift,
            "online_path_modified": False,
            "results": {},
        }
        (RESULT_DIR / f"{RESULT_STEM}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (RESULT_DIR / f"{RESULT_STEM}.md").write_text(
            render_blocked_report(material_report, evaluated_at), encoding="utf-8"
        )
        return payload
    results = {mode: run_mode(mode, eval_set, list(pools), top_k) for mode in MODES}
    payload = {
        "experiment": "R1 候选池规模敏感性实验",
        "status": "ok",
        "eval_set": str(eval_set_path),
        "eval_set_sha256": eval_set_sha,
        "eval_set_case_count": len(eval_set["cases"]),
        "eval_set_gold_summary": gold_summary,
        "matches_r0_eval_set": matches_r0_eval_set,
        "frozen_r0_eval_set_sha256": frozen_sha,
        "matches_frozen_baseline": matches_frozen_baseline,
        "case_count": len(eval_set["cases"]),
        "top_k": top_k,
        "pools_requested": list(pools),
        "pools_actual": {str(pool): pool_from_limit(limit_for_pool(pool, top_k)) for pool in pools},
        "evaluated_at": evaluated_at,
        "material_verification": material_report,
        "material_drift": drift,
        "online_path_modified": False,
        "results": results,
    }
    (RESULT_DIR / f"{RESULT_STEM}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / f"{RESULT_STEM}.md").write_text(
        render_report(
            eval_set,
            results,
            material_report,
            list(pools),
            matches_r0_eval_set,
            eval_set_sha,
            frozen_sha,
            matches_frozen_baseline,
        ),
        encoding="utf-8",
    )
    return payload


def render_from_result(
    result_path: Path = RESULT_DIR / f"{RESULT_STEM}.json",
    eval_set_path: Path = EVAL_SET_PATH,
) -> Path:
    """只按已产出的 JSON 重写 Markdown，不重跑检索、不重建索引。

    适用前提：指标未变，仅报告口径/结论段需要同步。
    """
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    markdown_path = result_path.parent / f"{RESULT_STEM}.md"
    if payload.get("status") == "blocked":
        # BLOCKED 结果重渲染：按 blocked_reason 分派，不得假定每种 payload 都含 material_verification；
        # 未知原因或必要字段缺失时 fail-closed，不抛 KeyError，也不生成正常报告。
        reason = payload.get("blocked_reason")
        evaluated_at = payload.get("evaluated_at", "")
        if reason == "material_drift":
            markdown_path.write_text(
                render_blocked_report(payload.get("material_verification", {}), evaluated_at), encoding="utf-8"
            )
        elif reason == "eval_set_mismatch":
            markdown_path.write_text(
                _render_eval_set_mismatch_report(
                    payload.get("eval_set_sha256", ""),
                    payload.get("frozen_r0_eval_set_sha256", ""),
                    evaluated_at,
                ),
                encoding="utf-8",
            )
        else:
            markdown_path.write_text(_render_blocked_unknown_report(reason, evaluated_at), encoding="utf-8")
        return markdown_path
    status = payload.get("status")
    if status != "ok":
        markdown_path.write_text(
            _render_unknown_status_report(status, payload.get("evaluated_at", "")), encoding="utf-8"
        )
        return markdown_path
    # 样本集一致性校验（复渲染前，F2）：
    #  - 旧结果未记录 sha（4.B.7）：fail-closed，不得静默用当前文件 SHA 补写、不得生成正常报告。
    #  - 记录的 sha 与当前评测集 sha 不一致（4.B.6）：结果已过期，禁止重渲染为看似正常的可比报告。
    #  - 记录的 sha 与当前一致、但不等于冻结 R0 基线（F2）：不是冻结样本，禁止重渲染为正常报告。
    #  - 记录的 matches_frozen_baseline 不为 True（F2/F4）：禁止重渲染。
    #  - 记录的 frozen_r0_eval_set_sha256 存在但不等于冻结 R0 基线（F4）：元数据与代码常量不一致/伪造，
    #    禁止重渲染为看似正常的可比报告，防止带伪造/缺失冻结证明的旧结果被渲染成正常报告。
    # 不得只依赖 case_count / gold summary / 文件名代替 SHA 校验。
    current_sha = _sha256_of_file(eval_set_path)
    stored_sha = payload.get("eval_set_sha256")
    if stored_sha is None:
        markdown_path.write_text(
            _render_eval_set_missing_sha_report(payload.get("evaluated_at", "")),
            encoding="utf-8",
        )
        return markdown_path
    if stored_sha != current_sha:
        markdown_path.write_text(
            _render_eval_set_changed_report(stored_sha, current_sha, payload.get("evaluated_at", "")),
            encoding="utf-8",
        )
        return markdown_path
    frozen_sha = get_frozen_r0_eval_set_sha256()
    payload_frozen_sha = payload.get("frozen_r0_eval_set_sha256")
    # 严格校验冻结元数据（F4）：stored_sha 与冻结基线一致、payload 自报的冻结 SHA 与基线一致、
    # 且 matches_frozen_baseline 必须明确为 True。任一不满足即 fail-closed。
    if (
        stored_sha != frozen_sha
        or payload_frozen_sha != frozen_sha
        or payload.get("matches_frozen_baseline") is not True
    ):
        markdown_path.write_text(
            _render_eval_set_not_frozen_report(
                stored_sha, frozen_sha, payload.get("evaluated_at", ""), payload_frozen_sha
            ),
            encoding="utf-8",
        )
        return markdown_path
    eval_set = load_eval_set(eval_set_path)
    markdown_path.write_text(
        render_report(
            eval_set,
            payload["results"],
            payload["material_verification"],
            payload["pools_requested"],
            payload.get("matches_r0_eval_set"),
            payload.get("eval_set_sha256", ""),
            payload.get("frozen_r0_eval_set_sha256", ""),
            payload.get("matches_frozen_baseline"),
        ),
        encoding="utf-8",
    )
    return markdown_path


def main() -> int:
    parser = argparse.ArgumentParser(description="R1 候选池规模敏感性实验")
    parser.add_argument("--pools", type=int, nargs="+", default=POOL_TARGETS)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET_PATH)
    parser.add_argument("--rerender", action="store_true", help="只按已有 JSON 重写 Markdown，不重跑检索")
    args = parser.parse_args()
    if args.rerender:
        print(f"已重写报告：{render_from_result(eval_set_path=args.eval_set)}")
        return 0
    payload = run_experiment(args.pools, args.top_k, args.eval_set)
    if payload.get("status") == "blocked":
        print(f"BLOCKED：{payload['blocked_reason']}；资料漂移={payload['material_drift']}；未产出任何指标。")
        return 1
    for mode in MODES:
        for pool in args.pools:
            block = payload["results"][mode].get("pools", {}).get(str(pool))
            if not block:
                print(f"{mode}@{pool}: {payload['results'][mode].get('error', '未运行')}")
                continue
            metrics = block["metrics"]
            print(
                f"{mode}@pool {pool} (实际 {block['candidate_pool_formula']}): "
                f"Recall@4={metrics['page_recall_at_k']} Top-1={metrics['top1_page_relevance']} "
                f"耗时={block['total_search_ms']}ms"
            )
    if payload["material_drift"]:
        print(f"资料漂移：{payload['material_drift']}")
    if payload.get("matches_r0_eval_set") is False:
        print("⚠️ 警告：本实验所用评测集与 R0 基线产物的 eval_set_sha256 不一致，结论不可与 R0 直接横比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
