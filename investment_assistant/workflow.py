"""由 LangGraph 编排的可追溯投研工作流。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .llm_generation import generate_controlled_narrative
from .market_data import fetch_financial_snapshot, fetch_market_snapshot, fetch_recent_news
from .news_filter import filter_news_records
from .rag import LocalResearchRAG
from .safety import REQUIRED_DISCLAIMER, assess_risk, evaluate_retrieval, validate_report


class ResearchState(TypedDict, total=False):
    ticker: str
    topic: str
    horizon: str
    report_mode: str
    market_snapshot: dict[str, Any]
    financial_snapshot: dict[str, Any]
    sources: list[dict[str, Any]]
    raw_news: list[dict[str, Any]]
    filtered_news_available: bool
    market_model: dict[str, Any]
    scenarios: list[dict[str, str]]
    llm_result: dict[str, Any]
    report: str
    risk_flags: list[str]
    evaluation: dict[str, Any]
    retrieval_evaluation: dict[str, Any]
    retrieval_status: dict[str, Any]
    local_material_available: bool
    created_at: str


def collect_real_data(state: ResearchState) -> ResearchState:
    snapshot = fetch_market_snapshot(state["ticker"])
    financial_snapshot = fetch_financial_snapshot(state["ticker"])
    raw_news = fetch_recent_news(state["ticker"])
    filtered_news, audited_news = filter_news_records(raw_news, state["ticker"])
    rag = LocalResearchRAG()
    rag.index_local_documents()
    rag.clear_news(state["ticker"])
    rag.index_news(filtered_news)
    return {
        "market_snapshot": snapshot,
        "financial_snapshot": financial_snapshot,
        "raw_news": audited_news,
        "filtered_news_available": bool(filtered_news),
        "created_at": datetime.now(UTC).isoformat(),
    }


def retrieve_evidence(state: ResearchState) -> ResearchState:
    rag = LocalResearchRAG()
    ticker = state["ticker"]
    query = f"{ticker} {state['topic']} {state['horizon']} 营收 净利润 自由现金流 估值 风险 行业 新闻"
    sources = rag.search(query, limit=4, ticker=ticker)
    retrieval_evaluation = evaluate_retrieval(query, sources)
    local_material_available = rag.local_document_count(ticker) > 0
    if not sources:
        retrieval_evaluation["findings"] = [f"该标的无本地研究资料：{ticker}。"]
    return {
        "sources": sources,
        "local_material_available": local_material_available,
        "retrieval_status": rag.retrieval_status(),
        "retrieval_evaluation": retrieval_evaluation,
    }


def model_market(state: ResearchState) -> ResearchState:
    snapshot = state["market_snapshot"]
    if not snapshot.get("data_available"):
        return {"market_model": {"state": "数据不可用", "trend": "无法基于真实行情判断", "evidence": []}}
    period_return = snapshot.get("period_return_pct")
    volatility = snapshot.get("annualized_volatility_pct")
    trend = "样本期上涨" if isinstance(period_return, (int, float)) and period_return > 0 else "样本期回落或持平"
    risk_level = "高" if isinstance(volatility, (int, float)) and volatility >= 35 else "中等或较低"
    return {"market_model": {"state": trend, "period_return_pct": period_return, "annualized_volatility_pct": volatility, "max_drawdown_pct": snapshot.get("max_drawdown_pct"), "risk_level": risk_level, "evidence": ["真实历史行情快照"]}}


def reason_scenarios(state: ResearchState) -> ResearchState:
    model = state["market_model"]
    financials = state["financial_snapshot"]
    citations = [f"[{item['citation']}]" for item in state.get("sources", [])]
    evidence_suffix = " ".join(citations[:2]) if citations else ""
    financial_condition = "财务指标需补充核验" if not financials.get("data_available") else "结合营收、净利润、自由现金流与估值指标的变化"
    scenarios = [
        {"name": "积极情景", "condition": f"{financial_condition}后，基本面数据、行业需求与外部新闻形成正向共振。", "implication": f"仅作为研究假设，不代表收益预期。{evidence_suffix}"},
        {"name": "基准情景", "condition": f"延续当前{model.get('state', '市场状态')}，并接受波动、回撤与估值变化的不确定性。", "implication": f"应持续跟踪价格、财报、自由现金流、估值和监管事件。{evidence_suffix}"},
        {"name": "压力情景", "condition": "营收增速、盈利能力或现金流恶化，且宏观环境、竞争、估值或流动性进一步承压。", "implication": "历史最大回撤不代表未来下限，应预先定义可承受损失和复核频率。"},
    ]
    return {"scenarios": scenarios}


def _format_number(value: Any) -> str:
    return "数据不可用" if value is None else f"{value:,.0f}"


def _financial_section(snapshot: dict[str, Any]) -> str:
    if not snapshot.get("data_available"):
        return f"数据不可用。原因：{snapshot.get('error', '未知原因')}"
    unavailable = "、".join(snapshot.get("unavailable_fields", [])) or "无"
    return f"""- 营收：{_format_number(snapshot.get('revenue'))}；报表期末：{snapshot.get('revenue_period_end') or '数据不可用'}
- 净利润：{_format_number(snapshot.get('net_income'))}；报表期末：{snapshot.get('net_income_period_end') or '数据不可用'}
- 自由现金流：{_format_number(snapshot.get('free_cash_flow'))}；报表期末：{snapshot.get('free_cash_flow_period_end') or '数据不可用'}
- 滚动市盈率（PE）：{snapshot.get('trailing_pe') if snapshot.get('trailing_pe') is not None else '数据不可用'}；估值抓取时间：{snapshot.get('valuation_as_of')}
- 市净率（PB）：{snapshot.get('price_to_book') if snapshot.get('price_to_book') is not None else '数据不可用'}；估值抓取时间：{snapshot.get('valuation_as_of')}

数据来源：{snapshot.get('source', '财报与估值请求失败')}；抓取时间：{snapshot.get('fetched_at', '未提供')}；快照字段缺失：{unavailable}。"""


def controlled_generation(state: ResearchState) -> ResearchState:
    if state.get("report_mode") == "rule":
        return {"llm_result": {"used": False, "reason": "用户指定规则版。"}}
    result = generate_controlled_narrative(
        ticker=state["ticker"],
        topic=state["topic"],
        horizon=state["horizon"],
        market_snapshot=state["market_snapshot"],
        financial_snapshot=state["financial_snapshot"],
        sources=state.get("sources", []),
    )
    return {"llm_result": result}


def _rule_narrative(state: ResearchState) -> str:
    model = state["market_model"]
    citations = [f"[{item['citation']}]" for item in state.get("sources", [])]
    evidence = " ".join(citations[:2]) if citations else ""
    return f"**规则版叙述。** 当前样本期状态为{model.get('state')}，风险等级为{model.get('risk_level', '未知')}。现有资料仅支持继续跟踪财报、估值、行业供需和政策变化，不构成交易信号。{evidence}"


def generate_report(state: ResearchState) -> ResearchState:
    snapshot = state["market_snapshot"]
    financial_snapshot = state["financial_snapshot"]
    model = state["market_model"]
    sources = state.get("sources", [])
    llm_result = state.get("llm_result", {})
    source_lines = []
    if not state.get("local_material_available", True):
        source_lines.append(f"- 该标的无本地研究资料：{state['ticker']}；以下证据仅来自实时新闻或其他可用公开来源。")
    if not state.get("filtered_news_available", True):
        source_lines.append("- \u672a\u68c0\u7d22\u5230\u4e0e\u8be5\u6807\u7684\u76f4\u63a5\u76f8\u5173\u7684\u65b0\u95fb\u3002")
    for item in sources:
        metadata = item["metadata"]
        page = metadata.get("page") or "\u4e0d\u9002\u7528"
        if metadata.get("source_type") == "pdf" and metadata.get("page_authority") == "generated":
            page = f"{page}\uff08\u672c\u5730\u8f6c\u6362\u9875\u7801\uff0c\u975e\u5b98\u65b9\u5206\u9875\uff09"
        source_lines.append(
            f"- [{item['citation']}] {metadata.get('title') or metadata.get('file_name') or metadata.get('source', '\u672a\u547d\u540d\u6765\u6e90')}\uff1b"
            f"\u6765\u6e90\uff1a{metadata.get('source', '\u672a\u63d0\u4f9b')}\uff1b\u53d1\u5e03\u65e5\u671f\uff1a{metadata.get('published_at', '\u672a\u63d0\u4f9b')}\uff1b"
            f"\u9875\u7801\uff1a{page}\uff1b\u94fe\u63a5\uff1a{metadata.get('url', '\u672a\u63d0\u4f9b') or '\u672a\u63d0\u4f9b'}"
        )
    if not source_lines:
        source_lines.append(f"- 该标的无本地研究资料：{state['ticker']}；本报告证据仅来自实时新闻或其他可用公开来源。")

    metrics = "数据不可用"
    if snapshot.get("data_available"):
        metrics = f"最新收盘价 {snapshot.get('latest_close')}；样本期收益 {snapshot.get('period_return_pct')}%；近一月收益 {snapshot.get('one_month_return_pct')}%；年化波动率 {snapshot.get('annualized_volatility_pct')}%；最大回撤 {snapshot.get('max_drawdown_pct')}%。"

    scenario_text = "\n".join(f"- **{item['name']}**：条件：{item['condition']} 观察：{item['implication']}" for item in state["scenarios"])
    sources_text = "\n".join(source_lines)
    if llm_result.get("used"):
        narrative = llm_result["narrative"]
        mode_label = f"受控 LLM 版（模型：{llm_result.get('model')}；槽位、引用与数字溯源校验通过）"
    else:
        narrative = _rule_narrative(state)
        mode_label = f"规则版（LLM 未采用：{llm_result.get('reason', '未调用或校验失败')}）"
    report = f"""# {state['ticker']} 投研研究简报

- 研究主题：{state['topic']}
- 观察期限：{state['horizon']}
- 生成时间（UTC）：{state['created_at']}
- 报告模式：{mode_label}

## 1. 真实市场数据快照

{metrics}

数据来源：{snapshot.get('source', '行情请求失败')}；交易日：{snapshot.get('latest_trading_date', '未提供')}；抓取时间：{snapshot.get('fetched_at', '未提供')}。

## 2. 财报与估值快照

{_financial_section(financial_snapshot)}

## 3. 市场建模观察

样本期状态：{model.get('state')}。风险等级：{model.get('risk_level', '未知')}。

## 4. 受控研究叙述

{narrative}

## 5. 情景推理

{scenario_text}

## 6. 研究结论与后续核验

当前结论仅是基于有限价格统计、财报估值快照和已检索资料的研究观察，不应被视为交易信号。后续应核验最新财报、估值、行业供需、政策变化以及来源资料的时效性。

## 7. 证据与来源

{sources_text}

## 8. 风险声明

{REQUIRED_DISCLAIMER} 市场存在本金损失风险；历史价格、财报、估值与新闻内容均不能预测未来表现。请在任何决策前核验原始来源，并结合自身风险承受能力咨询持牌专业人士。
"""
    return {"report": report}


def validate_risks(state: ResearchState) -> ResearchState:
    risks = assess_risk(state["market_snapshot"], state["financial_snapshot"], state.get("sources", []))
    evaluation = validate_report(
        state["report"],
        state.get("sources", []),
        state["financial_snapshot"],
        ticker=state["ticker"],
    )
    retrieval_evaluation = state.get("retrieval_evaluation", {})
    llm_result = state.get("llm_result", {})
    if not retrieval_evaluation.get("passed", True):
        risks.extend(f"检索质量提示：{item}" for item in retrieval_evaluation.get("findings", []))
    if llm_result.get("used") and not evaluation.get("passed"):
        risks.append("受控 LLM 报告未通过 safety 校验，已拒绝输出 LLM 版本并降级为规则版。")
        fallback_state = {**state, "llm_result": {"used": False, "reason": "LLM 完整报告未通过 safety 校验。"}}
        fallback_report = generate_report(fallback_state)["report"]
        fallback_evaluation = validate_report(
            fallback_report,
            state.get("sources", []),
            state["financial_snapshot"],
            ticker=state["ticker"],
        )
        return {"report": fallback_report, "risk_flags": risks, "evaluation": fallback_evaluation, "retrieval_evaluation": retrieval_evaluation, "llm_result": fallback_state["llm_result"]}
    return {"risk_flags": risks, "evaluation": evaluation, "retrieval_evaluation": retrieval_evaluation}


def create_research_workflow():
    builder = StateGraph(ResearchState)
    builder.add_node("collect_real_data", collect_real_data)
    builder.add_node("retrieve_evidence", retrieve_evidence)
    builder.add_node("model_market", model_market)
    builder.add_node("reason_scenarios", reason_scenarios)
    builder.add_node("controlled_generation", controlled_generation)
    builder.add_node("generate_report", generate_report)
    builder.add_node("validate_risks", validate_risks)
    builder.add_edge(START, "collect_real_data")
    builder.add_edge("collect_real_data", "retrieve_evidence")
    builder.add_edge("retrieve_evidence", "model_market")
    builder.add_edge("model_market", "reason_scenarios")
    builder.add_edge("reason_scenarios", "controlled_generation")
    builder.add_edge("controlled_generation", "generate_report")
    builder.add_edge("generate_report", "validate_risks")
    builder.add_edge("validate_risks", END)
    return builder.compile()


def run_research(ticker: str, topic: str, horizon: str, report_mode: str = "auto") -> dict[str, Any]:
    if report_mode not in {"auto", "rule"}:
        raise ValueError("report_mode 必须是 auto 或 rule")
    return create_research_workflow().invoke({"ticker": ticker.upper().strip(), "topic": topic.strip(), "horizon": horizon.strip(), "report_mode": report_mode})


def audit_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)

