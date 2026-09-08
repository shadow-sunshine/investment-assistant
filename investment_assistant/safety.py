"""检索、引用质量和报告风险控制。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

PROHIBITED_PATTERNS = {
    "收益承诺": r"(保证|必然|稳赚|无风险).{0,12}(收益|上涨|获利|赚钱)",
    "直接交易指令": r"(立即|马上|现在).{0,8}(买入|卖出|加仓|清仓)",
    "目标价": r"目标价.{0,16}\d",
}
REQUIRED_DISCLAIMER = "仅用于学习和研究，不构成投资建议。"
REQUIRED_FINANCIAL_SECTION = "## 2. 财报与估值快照"
REQUIRED_CONTROLLED_NARRATIVE_SECTION = "## 4. 受控研究叙述"


def _check_fetched_at(snapshot: dict[str, Any], label: str, max_age_hours: int) -> list[str]:
    if not snapshot.get("data_available"):
        return [f"{label}不可用：{snapshot.get('error', '未知原因')}。"]
    try:
        fetched_at = datetime.fromisoformat(snapshot["fetched_at"])
        age_hours = (datetime.now(UTC) - fetched_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            return [f"{label}抓取时间已超过 {max_age_hours} 小时，应重新获取后再解释。"]
    except (KeyError, ValueError):
        return [f"{label}缺少可解析的抓取时间。"]
    return []


def check_data_freshness(market_snapshot: dict[str, Any], max_age_hours: int = 36) -> list[str]:
    return _check_fetched_at(market_snapshot, "市场数据", max_age_hours)


def check_financial_data(financial_snapshot: dict[str, Any], max_age_hours: int = 168) -> list[str]:
    risks = _check_fetched_at(financial_snapshot, "财报与估值数据", max_age_hours)
    if not financial_snapshot.get("data_available"):
        return risks
    missing = financial_snapshot.get("unavailable_fields", [])
    if missing:
        risks.append("财报与估值指标缺失：" + "、".join(missing) + "。")
    for metric, date_key, label in [("revenue", "revenue_period_end", "营收"), ("net_income", "net_income_period_end", "净利润"), ("free_cash_flow", "free_cash_flow_period_end", "自由现金流")]:
        if financial_snapshot.get(metric) is not None and not financial_snapshot.get(date_key):
            risks.append(f"{label}缺少对应报表期末日期，不能作为已核验事实引用。")
    return risks


def assess_risk(market_snapshot: dict[str, Any], financial_snapshot: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    risks = check_data_freshness(market_snapshot)
    risks.extend(check_financial_data(financial_snapshot))
    volatility = market_snapshot.get("annualized_volatility_pct")
    drawdown = market_snapshot.get("max_drawdown_pct")
    if isinstance(volatility, (int, float)) and volatility >= 35:
        risks.append(f"年化波动率为 {volatility}% ，价格波动较高。")
    if isinstance(drawdown, (int, float)) and drawdown <= -25:
        risks.append(f"样本期最大回撤为 {drawdown}% ，历史回撤风险较高。")
    if financial_snapshot.get("net_income") is not None and financial_snapshot["net_income"] < 0:
        risks.append("最近财年净利润为负，盈利质量与持续经营能力需进一步核验。")
    if financial_snapshot.get("free_cash_flow") is not None and financial_snapshot["free_cash_flow"] < 0:
        risks.append("最近财年自由现金流为负，现金流质量需进一步核验。")
    if len(sources) < 2:
        risks.append("可检索证据不足两条，结论仅可作为待验证观察。")
    return risks


def evaluate_retrieval(query: str, sources: list[dict[str, Any]], expected_source_fragments: list[str] | None = None) -> dict[str, Any]:
    """评估来源可追溯性、页码完整性和可选的人工标注召回率。"""
    findings: list[str] = []
    if not sources:
        return {"passed": False, "retrieved_count": 0, "pdf_source_count": 0, "page_citation_coverage": 0.0, "expected_source_recall": None, "findings": ["未检索到证据。"]}
    pdf_sources = [source for source in sources if source.get("metadata", {}).get("source_type") == "pdf"]
    page_covered = [source for source in pdf_sources if source.get("metadata", {}).get("page")]
    if pdf_sources and len(page_covered) != len(pdf_sources):
        findings.append("PDF 检索结果存在缺失页码的引用。")
    if expected_source_fragments:
        corpus = "\n".join(source.get("content", "") for source in sources).lower()
        matched = sum(fragment.lower() in corpus for fragment in expected_source_fragments)
        expected_recall = matched / len(expected_source_fragments)
        if expected_recall < 1:
            findings.append("预期证据未全部召回。")
    else:
        expected_recall = None
    return {"passed": not findings, "query": query, "retrieved_count": len(sources), "pdf_source_count": len(pdf_sources), "page_citation_coverage": round(len(page_covered) / len(pdf_sources), 2) if pdf_sources else 1.0, "expected_source_recall": expected_recall, "findings": findings}



def validate_report(
    report: str,
    sources: list[dict[str, Any]],
    financial_snapshot: dict[str, Any] | None = None,
    ticker: str | None = None,
) -> dict[str, Any]:
    findings: list[str] = []
    cited_numbers = re.findall(r"\[S(\d+)\]", report)
    citation_hits = len(cited_numbers)
    allowed_numbers = {str(source.get("citation", "")).removeprefix("S") for source in sources}
    invalid_citations = sorted(set(cited_numbers) - allowed_numbers)
    banned = [name for name, pattern in PROHIBITED_PATTERNS.items() if re.search(pattern, report)]
    if REQUIRED_DISCLAIMER not in report:
        findings.append("缺少必需免责声明。")
    if sources and citation_hits == 0:
        findings.append("存在检索证据但报告没有引用标记。")
    if invalid_citations:
        findings.append("报告包含不存在的引用编号：" + "、".join(f"S{item}" for item in invalid_citations) + "。")
    normalized_ticker = ticker.upper().strip() if ticker else None
    if normalized_ticker:
        for source in sources:
            metadata = source.get("metadata") or {}
            source_ticker = str(metadata.get("ticker") or "unknown").upper().strip()
            source_type = str(metadata.get("source_type") or "").lower()
            if source_type != "news" and source_ticker != "UNKNOWN" and source_ticker != normalized_ticker:
                findings.append(f"证据-标的不匹配：查 {normalized_ticker} 引用了 {source_ticker} 的资料。")
    if financial_snapshot is not None:
        if REQUIRED_CONTROLLED_NARRATIVE_SECTION not in report:
            findings.append("缺少受控研究叙述章节。")
        if REQUIRED_FINANCIAL_SECTION not in report:
            findings.append("缺少财报与估值独立章节。")
        if not financial_snapshot.get("data_available") and "数据不可用" not in report:
            findings.append("财报与估值不可用时未在报告中显式披露。")
        if financial_snapshot.get("data_available"):
            for field, date_field, label in [("revenue", "revenue_period_end", "营收"), ("net_income", "net_income_period_end", "净利润"), ("free_cash_flow", "free_cash_flow_period_end", "自由现金流")]:
                if financial_snapshot.get(field) is not None and str(financial_snapshot.get(date_field)) not in report:
                    findings.append(f"{label}缺少报表期末日期披露。")
    if banned:
        findings.append("检测到不允许的表述：" + "、".join(banned))
    return {"passed": not findings, "citation_hits": citation_hits, "blocked_patterns": banned, "invalid_citations": [f"S{item}" for item in invalid_citations], "findings": findings}



