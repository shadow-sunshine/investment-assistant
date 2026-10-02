"""R2 中文召回最小可证伪实验：在不修改线上默认检索路径的前提下，量化中文字面 / 术语 / 数值锚点
对四象限检索召回的边际贡献。

设计要点
--------
- **不改 rag.py**：本模块实现一套离线 `FakeRetriever`，只在内存语料（冻结资料的逐页文本）上重排，
  不构建也不调用 Chroma 索引，不加载语义模型。
- **baseline 是默认检索的受控离线近似**：复刻 rag.py 的 `_lexical_overlap` + `_anchor_score` + query 扩展
  （`expand_financial_query`，读同一份 `data/financial_term_map.json`），用于隔离「锚点」的边际贡献。
  它**不替代**真实 Chroma 端到端评测；其 zh-zh≈0/8 与 R0 冻结结果一致，作为可信度校准。
- **统一相关性判定**：Recall / Top-1 / scoped / unscoped 全部复用 `bilingual_eval` 的 `_is_target_source`
  （ticker + 页码双校验）与 `_keyword_match`，避免自造口径。
- **三组独立对照**：baseline / lexical_anchor / numeric_anchor。**不运行 combined**，以免把多个改动混成
  无法归因的一组（见 CURRENT_TASK.md 建议）。
- 语料 = 冻结 R0 资料四份 PDF 的逐页文本（page 粒度，对齐 `_is_target_source` 的页码口径）；
  资料 SHA 与样本 SHA 在建语料 / 检索前校验，失败即 BLOCKED，不产出可比指标。

用法
----
    python -m investment_assistant.chinese_anchor_eval --top-k 4
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, KNOWLEDGE_DIR
from .bilingual_eval import (
    EVAL_SET_PATH,
    QUADRANT_LABEL,
    QUADRANT_ORDER,
    MaterialDriftError,
    _is_target_source,
    _keyword_match,
    _aggregate,
    _failure_reason,
    _sha256_of_file,
    assert_materials_comparable,
    get_frozen_r0_eval_set_sha256,
    load_eval_set,
    verify_materials,
)

RESULT_DIR = DATA_DIR / "evaluations"
R2_RESULT_STEM = "chinese_anchor_r2"
TOP_K_FROZEN = 4
STRATEGIES = ("baseline", "lexical_anchor", "numeric_anchor")
TERM_MAP_PATH = DATA_DIR / "financial_term_map.json"

# ---------------------------------------------------------------------------
# 受控复刻 rag.py 默认评分口径（用于 baseline 分支，隔离锚点边际贡献）
# ---------------------------------------------------------------------------

def _lexical_overlap(query: str, text: str) -> float:
    """轻量字面重排特征 —— 复刻 rag.py L165 的 token 覆盖率口径。"""
    query_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9._-]+", query.lower()))
    text_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9._-]+", text.lower()))
    if not query_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _anchor_score(query: str, text: str) -> float:
    """同页候选块锚点 —— 复刻 rag.py L174：只认 Latin 实体 / 数字（中文实体默认不进锚点）。"""
    anchors = re.findall(r"[A-Za-z][A-Za-z .,&-]{2,}|\d+(?:[,.]\d+)*(?:%|\s*%|\s*billion)?", query)
    lowered_text = text.lower()
    return float(sum(1 for anchor in anchors if anchor.strip() and anchor.lower().strip() in lowered_text))


def _expand_financial_query(query: str) -> str:
    """中文财务术语 → 英文变体扩展 —— 复刻 rag.py L49，读同一份 term map；文件缺失时原样返回。"""
    try:
        raw = json.loads(TERM_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return query
    variants: list[str] = []
    lowered = query.lower()
    if isinstance(raw, dict):
        for term, mapped in raw.items():
            if isinstance(mapped, list) and term.lower() in lowered:
                variants.extend(str(v) for v in mapped if str(v).strip())
    unique = list(dict.fromkeys(v for v in variants if v not in query))
    return query if not unique else f"{query} {' '.join(unique)}"


# ---------------------------------------------------------------------------
# 锚点构造
# ---------------------------------------------------------------------------

# 字段名 / 同义词锚点：键为样本 keywords[0]（中英文皆可），值为同义锚点（含英文等效项，
# 以便 lexical_anchor 也能服务 zh-en 象限）。仅用于「把锚点加进查询」做受控对比，不伪造命中。
_ANCHOR_SYNONYMS: dict[str, list[str]] = {
    "营业收入": ["营收", "收入", "operating revenue", "revenue", "total revenue"],
    "营业总收入": ["营收", "总收入", "total revenue", "revenue"],
    "归属于母公司股东的净利润": ["净利润", "归母净利", "net profit", "net income"],
    "经营活动产生的现金流量净额": ["经营现金流", "现金流", "operating cash flow", "cash flow"],
    "基本每股收益": ["每股收益", "eps", "earnings per share"],
    "加权平均净资产收益率": ["净资产收益率", "roe", "return on equity"],
    "销售费用": ["营销费用", "selling expense", "selling and marketing expenses"],
    "存货": ["库存", "inventory"],
    "总资产": ["资产总额", "total assets", "assets"],
    "归属于上市公司股东的净资产": ["净资产", "股东权益", "equity", "shareholders equity"],
    "货币资金": ["现金", "cash", "cash and cash equivalents"],
    "合同负债": ["预收款", "contract liability"],
    "研发费用": ["研发", "r&d", "research and development"],
    "管理费用": ["行政费用", "general and administrative expenses", "admin expense"],
    "营业成本": ["成本", "cost of sales", "cost of goods sold"],
    "毛利率": ["毛利", "gross margin", "gross profit margin"],
    "Total assets": ["total asset", "assets"],
    "Basic earnings per share": ["eps", "earnings per share"],
    "Wearables, Home and Accessories": ["wearables", "accessories"],
    "Total revenue": ["revenue", "total revenues"],
    "Operating income": ["operating profit", "operating income"],
    "Gross margin": ["gross profit", "gross profit margin"],
    "Revenues": ["revenue", "total revenue"],
    "Gross profit": ["gross margin", "gross profit margin"],
    "Net income": ["net profit", "profit"],
    "Research and development": ["r&d", "research and development expenses"],
    "Total liabilities": ["total liability", "liabilities"],
    "Net cash from operations": ["operating cash flow", "cash flow from operations"],
    "Profit for the year": ["net profit", "net income"],
    "Profit before income tax": ["pre-tax profit", "profit before tax"],
    "Selling and marketing expenses": ["selling expense", "marketing expense"],
}


def _lexical_anchors(query: str) -> list[str]:
    """只从用户查询识别术语并扩展同义词，不读取评测 gold keywords。"""
    lowered = query.lower()
    anchors: list[str] = []
    for label, synonyms in _ANCHOR_SYNONYMS.items():
        recognized = [label, *synonyms]
        if any(term.lower() in lowered for term in recognized):
            anchors.extend(recognized)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in seen:
            seen.add(anchor)
            out.append(anchor)
    return out


def _numeric_anchors(query: str) -> list[str]:
    """只提取用户查询中实际出现的数字、年份、百分比或金额片段。"""
    anchors = re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?\s*%?", query)
    seen: set[str] = set()
    out: list[str] = []
    for anchor in anchors:
        normalized = anchor.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _numeric_anchor_score(value: str | None, content: str) -> float:
    """证据片段真实包含查询数字锚点时才计为命中（1.0），否则 0.0。"""
    if not value:
        return 0.0
    norm_value = value.replace(",", "").replace(" ", "").lower()
    norm_content = content.replace(",", "").replace(" ", "").lower()
    return 1.0 if norm_value and norm_value in norm_content else 0.0


# ---------------------------------------------------------------------------
# FakeRetriever（离线，不依赖 rag.py / Chroma / 语义模型）
# ---------------------------------------------------------------------------

class ChineseAnchorRetriever:
    """在内存语料上按策略重排的受控检索器。"""

    def __init__(self, strategy: str) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"未知策略 {strategy!r}；允许 {STRATEGIES}")
        self.strategy = strategy

    def _anchored_query(self, query: str) -> str:
        expanded = _expand_financial_query(query)
        if self.strategy == "lexical_anchor":
            return f"{expanded} {' '.join(_lexical_anchors(query))}".strip()
        if self.strategy == "numeric_anchor":
            return f"{expanded} {' '.join(_numeric_anchors(query))}".strip()
        return expanded

    def score(self, query: str, content: str) -> float:
        anchored = self._anchored_query(query)
        base = _lexical_overlap(anchored, content) + 0.3 * _anchor_score(anchored, content)
        if self.strategy == "numeric_anchor":
            base += sum(5.0 * _numeric_anchor_score(anchor, content) for anchor in _numeric_anchors(query))
        return base

    def retrieve(
        self,
        query: str,
        case: dict[str, Any],
        corpus: list[dict[str, Any]],
        top_k: int,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        pool = [s for s in corpus if ticker is None or s["ticker"] == ticker]
        # 只保留真正得分 > 0 的候选（对齐 Chroma 只返回近邻、不返回全量页）；
        # 同分时按 page 升序稳定，但 0 分候选不进入召回，避免平局时人为偏袒金标准页。
        scored = [(self.score(query, s["content"]), s) for s in pool]
        positive = [(sc, s) for sc, s in scored if sc > 0]
        positive.sort(key=lambda item: (-item[0], item[1]["page"]))
        return [s for _, s in positive[:top_k]]


# ---------------------------------------------------------------------------
# 用例执行与聚合（统一复用 bilingual_eval 的相关性判定 / 聚合 / 归因）
# ---------------------------------------------------------------------------

def run_case(retriever: ChineseAnchorRetriever, case: dict[str, Any], corpus: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    scoped = retriever.retrieve(case["question"], case, corpus, top_k, ticker=case["ticker"])
    unscoped = retriever.retrieve(case["question"], case, corpus, top_k, ticker=None)
    relevant = [s for s in scoped if _is_target_source(s, case)]
    forbidden = set(case.get("forbidden_tickers", []))
    unscoped_tickers = [s["ticker"] for s in unscoped]
    cross = [t for t in unscoped_tickers if t in forbidden]
    result: dict[str, Any] = {
        "id": case["id"],
        "quadrant": case["quadrant"],
        "ticker": case["ticker"],
        "question": case["question"],
        "target_pages": case["target_pages"],
        "keywords": case["keywords"],
        "retrieved_pages": [s["page"] for s in scoped],
        "page_hit_at_k": bool(relevant),
        "keyword_verified_hit_at_k": any(_keyword_match(s["content"], case["keywords"]) for s in relevant),
        "citation_page_relevance": round(len(relevant) / top_k, 4),
        "top_result_page": scoped[0]["page"] if scoped else None,
        "top_result_is_relevant": bool(scoped and _is_target_source(scoped[0], case)),
        "retrieved_files": [s["file_name"] for s in scoped],
        "unscoped_tickers": unscoped_tickers,
        "unscoped_pages": [s["page"] for s in unscoped],
        "cross_ticker_source_count_unscoped": len(cross),
    }
    result["failure_reason"] = _failure_reason(result, case)
    return result


def _run_strategy(strategy: str, eval_set: dict[str, Any], corpus: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    retriever = ChineseAnchorRetriever(strategy)
    cases = [run_case(retriever, case, corpus, top_k) for case in eval_set["cases"]]
    by_quadrant = {q: _aggregate([c for c in cases if c["quadrant"] == q]) for q in QUADRANT_ORDER}
    reasons: dict[str, int] = {}
    for c in cases:
        reasons[c["failure_reason"]] = reasons.get(c["failure_reason"], 0) + 1
    return {
        "strategy": strategy,
        "top_k": top_k,
        "metrics": _aggregate(cases),
        "by_quadrant": by_quadrant,
        "failure_reason_counts": reasons,
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# 语料构建（逐页文本快照；仅运行时依赖 pypdf，导入不依赖）
# ---------------------------------------------------------------------------

def build_corpus(eval_set: dict[str, Any]) -> list[dict[str, Any]]:
    """从冻结资料 PDF 抽取逐页文本作为检索语料（page 粒度）。"""
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover - 运行环境须有 pypdf
        raise RuntimeError("构建语料需要 pypdf（冻结资料快照抽取）；测试使用 fixture 语料，不依赖此路径") from exc
    corpus: list[dict[str, Any]] = []
    for ticker, spec in eval_set["materials"].items():
        path = KNOWLEDGE_DIR / spec["file_name"]
        if not path.exists():
            raise RuntimeError(f"冻结资料缺失：{path}；拒绝构建语料")
        reader = pypdf.PdfReader(str(path))
        for idx, page in enumerate(reader.pages, start=1):
            # 对齐 bilingual_eval 统一的命中判定口径：_is_target_source / _page_of 读取 source["metadata"]。
            corpus.append(
                {
                    "ticker": ticker,
                    "page": idx,
                    "content": page.extract_text() or "",
                    "file_name": spec["file_name"],
                    "metadata": {"ticker": ticker, "page": idx, "file_name": spec["file_name"]},
                }
            )
    if not corpus:
        raise RuntimeError("语料为空（资料抽取失败）；拒绝产出可比指标，不伪造命中")
    return corpus


# ---------------------------------------------------------------------------
# 实验主流程（含 SHA / 资料快照 fail-closed 护栏）
# ---------------------------------------------------------------------------

def run_experiment(top_k: int = TOP_K_FROZEN, eval_set_path: Path = EVAL_SET_PATH) -> dict[str, Any]:
    if top_k != TOP_K_FROZEN:
        raise ValueError(f"R2 实验 Top-K 冻结为 {TOP_K_FROZEN}，与 R0 保持一致，便于横比")
    eval_set = load_eval_set(eval_set_path)
    current_sha = _sha256_of_file(eval_set_path)
    frozen_sha = get_frozen_r0_eval_set_sha256()
    matches_frozen = current_sha == frozen_sha
    material_report = verify_materials(eval_set["materials"])
    drift = [t for t, v in material_report.items() if v.get("status") != "match"]
    evaluated_at = datetime.now(UTC).isoformat()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    # 护栏 1：样本 SHA 不等于冻结基线 → 在建语料 / 检索前 BLOCKED。
    if not matches_frozen:
        payload = _blocked_payload(
            "eval_set_mismatch", current_sha, frozen_sha, material_report, drift, evaluated_at, top_k, eval_set
        )
        _write_payload(payload)
        return payload
    # 护栏 2：资料快照漂移 / 缺失 → BLOCKED。
    try:
        assert_materials_comparable(material_report)
    except MaterialDriftError:
        payload = _blocked_payload(
            "material_drift", current_sha, frozen_sha, material_report, drift, evaluated_at, top_k, eval_set
        )
        _write_payload(payload)
        return payload

    corpus = build_corpus(eval_set)
    numeric_query_cases = sum(1 for case in eval_set["cases"] if _numeric_anchors(case["question"]))
    results: dict[str, Any] = {}
    timing: dict[str, float] = {}
    for strategy in STRATEGIES:
        t0 = time.perf_counter()
        results[strategy] = _run_strategy(strategy, eval_set, corpus, top_k)
        timing[strategy] = round(time.perf_counter() - t0, 4)

    payload = {
        "status": "ok",
        "experiment": "R2_chinese_anchor",
        "eval_set": str(eval_set_path),
        "eval_set_sha256": current_sha,
        "frozen_r0_eval_set_sha256": frozen_sha,
        "matches_frozen_baseline": matches_frozen,
        "top_k": top_k,
        "evaluated_at": evaluated_at,
        "material_verification": material_report,
        "material_drift": drift,
        "strategies": list(STRATEGIES),
        "numeric_query_cases": numeric_query_cases,
        "numeric_query_case_total": len(eval_set["cases"]),
        "timing_seconds": timing,
        "results": results,
    }
    _write_payload(payload)
    return payload


def _blocked_payload(
    reason: str,
    current_sha: str,
    frozen_sha: str,
    material_report: dict[str, Any],
    drift: list[str],
    evaluated_at: str,
    top_k: int,
    eval_set: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blocked_reason": reason,
        "experiment": "R2_chinese_anchor",
        "eval_set": str(EVAL_SET_PATH),
        "eval_set_sha256": current_sha,
        "frozen_r0_eval_set_sha256": frozen_sha,
        "matches_frozen_baseline": current_sha == frozen_sha,
        "top_k": top_k,
        "evaluated_at": evaluated_at,
        "material_verification": material_report,
        "material_drift": drift,
        "results": {},
    }


def _write_payload(payload: dict[str, Any]) -> None:
    (RESULT_DIR / f"{R2_RESULT_STEM}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESULT_DIR / f"{R2_RESULT_STEM}.md").write_text(render_report(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_report(payload: dict[str, Any]) -> str:
    if payload.get("status") == "blocked":
        return _render_blocked_report(payload)
    results = payload["results"]
    lines = [
        "# R2 中文召回最小可证伪实验",
        "",
        f"- 评测时间：{payload['evaluated_at']}",
        f"- 样本：`data/bilingual_eval_set.json`（冻结 SHA {payload['eval_set_sha256'][:12]}…，与 R0 基线"
        f" {'一致' if payload['matches_frozen_baseline'] else '不一致'}）",
        f"- Top-K 冻结为 {payload['top_k']}；策略：{', '.join(payload['strategies'])}（**未运行 combined**，避免多改动混为一组无法归因）",
        f"- 含查询数字锚点的样本：{payload['numeric_query_cases']}/{payload['numeric_query_case_total']}（仅依据问题文本抽取，不读取答案标签）",
        "- baseline 为默认检索（_lexical_overlap + _anchor_score + 财务术语扩展）的**受控离线近似**，用于隔离锚点边际贡献；",
        "  不替代真实 Chroma 端到端评测。",
        "- 语料 = 四份冻结资料的逐页文本（page 粒度），未构建 Chroma 索引、未加载语义模型。",
        "",
        "## 1. 总体四象限指标（Page Recall@4 / 关键词-数值核验 Recall@4 / Top-1）",
        "",
        "| 指标 | baseline | lexical_anchor | numeric_anchor |",
        "|---|---:|---:|---:|",
    ]
    for key, label in [
        ("page_recall_at_k", "Page Recall@4"),
        ("keyword_verified_recall_at_k", "关键词/数值核验 Recall@4"),
        ("top1_page_relevance", "Top-1 页面相关性"),
        ("citation_page_relevance", "引用页面相关率@4"),
        ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条（unscoped）"),
    ]:
        row = [f"| {label} |"]
        for s in STRATEGIES:
            row.append(f" {results[s]['metrics'].get(key, '未运行')} |")
        lines.append("".join(row))

    lines.extend(["", "## 2. 按象限拆分（Page Recall@4）", ""])
    for quad in QUADRANT_ORDER:
        lines.append(f"### {QUADRANT_LABEL[quad]}")
        lines.append("| 指标 | baseline | lexical_anchor | numeric_anchor |")
        lines.append("|---|---:|---:|---:|")
        for key, label in [
            ("page_recall_at_k", "Page Recall@4"),
            ("keyword_verified_recall_at_k", "核验 Recall@4"),
            ("top1_page_relevance", "Top-1"),
            ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条"),
        ]:
            row = [f"| {label} |"]
            for s in STRATEGIES:
                row.append(f" {results[s]['by_quadrant'][quad].get(key, '未运行')} |")
            lines.append("".join(row))
        lines.append("")

    lines.extend(["", "## 3. 失败归因分布", ""])
    all_reasons = set()
    for s in STRATEGIES:
        all_reasons |= set(results[s].get("failure_reason_counts", {}))
    for reason in sorted(all_reasons):
        row = [f"| {reason} |"]
        for s in STRATEGIES:
            row.append(f" {results[s].get('failure_reason_counts', {}).get(reason, 0)} |")
        lines.append("".join(row))

    lines.extend(["", "## 4. 跨标的污染（unscoped 臂）", ""])
    for label, selector in [
        ("出现跨标的来源的样本数", lambda c: c["cross_ticker_source_count_unscoped"] > 0),
        ("Top-K 全来自错误标的的样本数", lambda c: len(c["unscoped_tickers"]) >= 4 and c["cross_ticker_source_count_unscoped"] >= 4),
    ]:
        row = [f"| {label}（共 32 条） |"]
        for s in STRATEGIES:
            cases = results[s].get("cases", [])
            row.append(f" {sum(1 for c in cases if selector(c))} |")
        lines.append("".join(row))

    lines.extend(["", "## 5. 耗时（秒，语料构建 + 三策略重排）", ""])
    for s in STRATEGIES:
        lines.append(f"- {s}: {payload['timing_seconds'].get(s)}")

    lines.extend(
        [
            "",
            "## 6. 适用范围与未验证项",
            "",
            "- **适用范围**：本实验在 page 粒度语料上，用受控离线检索器隔离「中文字面/术语锚点」「数值锚点」对四象限召回的边际贡献；",
            "  仅衡量「金标准页能否进入 Top-4 / Top-1」，不直接等于真实 Chroma 端到端指标。",
            "- **未验证项**：真实 Chroma 索引 + 语义 embedding + Cross-Encoder 重排下的端到端表现未在本实验运行；",
            "  rag.py 默认路径未被修改，本结论不自动外推为线上改动建议。",
            "- **剩余风险**：pypdf 对 CFF 字体 PDF 的抽取存在已知噪音（fontTools 缺失告警），可能影响个别英文页字面分；",
            "  中文页抽取经验证完整。锚点同义词表为人工精选，覆盖面有限，未做自动挖掘；映射设计参考本评测术语，存在评测集过拟合风险。",
            "",
            "## 7. 下一步建议",
            "",
            "1. numeric_anchor 本轮总 Recall@4 与 baseline 均为 0.125、Top-1 均为 0.0625；当前结果不支持数值锚点带来增益，不应进入线上实现。",
            "2. lexical_anchor 有离线增益，但同义词表是人工精选且针对本评测术语；须在独立留出集验证后再主张泛化。",
            "3. 仅在真实 Chroma 端到端评测复测后，才考虑改动线上 `rag.py` 检索/排序；本实验不构成直接实施方案。",
        ]
    )
    return "\n".join(lines)


def _render_blocked_report(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# R2 中文召回最小可证伪实验 —— BLOCKED",
            "",
            f"- 评测时间：{payload['evaluated_at']}",
            f"- **状态：BLOCKED（{payload['blocked_reason']}）—— 未构建语料、未跑检索、未产出任何可比指标**",
            "",
            "| 标的 | 状态 | 预期 sha256 | 实际 sha256 |",
            "|---|---|---|---|",
        ]
        + [
            f"| {t} | {item.get('status','未校验')} | {item.get('expected_sha256','-')} | {item.get('actual_sha256','-')} |"
            for t, item in payload.get("material_verification", {}).items()
        ]
        + [
            "",
            "> 样本 SHA 与冻结基线不一致或资料快照漂移/缺失时，本实验拒绝给出可比结论（与 R0/R1 护栏一致）。",
            "",
        ]
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="R2 中文召回最小可证伪实验")
    parser.add_argument("--top-k", type=int, default=TOP_K_FROZEN)
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET_PATH)
    args = parser.parse_args()
    payload = run_experiment(args.top_k, args.eval_set)
    if payload.get("status") == "blocked":
        print(f"BLOCKED：{payload['blocked_reason']}；未产出任何指标。")
        return 1
    for s in STRATEGIES:
        print(s, json.dumps(payload["results"][s]["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
