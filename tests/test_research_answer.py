"""Research Answer v1 的意图、结构化输出和会话编排回归。"""
from __future__ import annotations

from investment_assistant.chat_session import dispatch_message, new_context, route_message
from investment_assistant.research_answer import ResearchAnswerService


def test_concept_answer_is_structured_and_not_financial_fact():
    result = ResearchAnswerService().answer("什么是市盈率", requested_by="test")
    assert result["status"] == "answered"
    assert result["intent"] == "concept"
    assert result["evidence_tier"] == "educational"
    assert result["sections"]["conclusion"]
    assert result["sources"] == []
    assert "不是公司事实" in result["limitations"][0]


def test_unsupported_summary_fails_closed_without_sources():
    result = ResearchAnswerService().answer("腾讯2025年财报摘要", requested_by="test")
    assert result["status"] == "answered"
    assert result["intent"] == "financial_summary"
    assert result["sources"]
    assert result["limitations"]


def test_router_exposes_v1_intents_but_keeps_unsupported_comparison_out():
    assert route_message("腾讯2025年业绩概览", new_context())[0] == "research_answer"
    assert route_message("300750.SZ 与 000001.SZ 2025 年净利润对比", new_context())[0] == "guidance"
    assert route_message("什么是现金流", new_context())[1]["intent"] == "concept"


def test_dispatch_preserves_research_task_context_and_packet():
    calls = []

    def research(payload):
        calls.append(payload)
        return {"status": "refused", "answer": "证据不足", "intent": payload["intent"], "sections": {}}

    state = dispatch_message(new_context(), "腾讯2025年业绩概览", lambda _: {}, lambda _: {}, "actor",
                             research_ask=research)
    assert calls[0]["question"].startswith("腾讯2025年")
    assert state["general_scope"] == "research"
    assert state["messages"][-1]["answer"]["status"] == "refused"
    assert state["messages"][-1]["answer"]["intent_label"] == "研究回答"


def test_unknown_company_summary_goes_to_discovery_before_research_packet():
    calls = []

    def discover(query, market=None):
        calls.append((query, market))
        return {
            "status": "ambiguous",
            "selected_ticker": None,
            "message": "请选择市场",
            "candidates": [
                {"ticker": "9988.HK", "market": "HK", "verified": False},
                {"ticker": "BABA", "market": "US", "verified": False},
            ],
        }

    def forbidden_research(_payload):
        raise AssertionError("未识别主体不能先进入 Research Answer")

    state = dispatch_message(
        new_context(), "阿里巴巴今年年报摘要怎么样", lambda _: {}, lambda _: {}, "actor",
        research_ask=forbidden_research, company_discover=discover,
    )
    assert calls == [("阿里巴巴今年年报摘要怎么样", None)]
    assert state["messages"][-1]["answer"]["status"] == "ambiguous"
    assert state["messages"][-1]["answer"]["intent"] == "discovery"
