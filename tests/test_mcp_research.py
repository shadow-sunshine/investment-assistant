from investment_assistant.chat_session import dispatch_message, new_context
from investment_assistant.mcp_research import answer_market_snapshot, is_market_query


def test_market_query_classifier_is_explicit():
    assert is_market_query("腾讯最新股价和涨跌幅")
    assert is_market_query("查看最近走势")
    assert not is_market_query("2025年收入")


def test_market_snapshot_formats_third_party_source_and_freshness():
    result = answer_market_snapshot({"data_available": True, "observations": [{
        "source": "akshare_mcp", "retrieved_at": "2026-10-06T10:00:00Z",
        "provenance": {"tool": "stock_hk_spot_em"},
        "data": [{"代码": "0700", "最新价": 512.5, "涨跌幅": 1.2, "日期": "2026-10-06"}],
    }]}, "0700.HK", "腾讯最新行情")
    assert result["status"] == "candidate"
    assert "512.5" in result["answer"] and "akshare_mcp" in result["answer"]
    assert result["verified"] is False
    assert result["sources"][0]["identity"]["retrieved_at"]


def test_mcp_dispatch_uses_market_snapshot_before_financial_candidate():
    calls = []
    def snapshot(payload):
        calls.append(("snapshot", payload))
        return {"data_available": True, "observations": [{"source": "fake_mcp", "retrieved_at": "now",
                 "provenance": {"tool": "spot"}, "data": [{"代码": "0700", "最新价": 500}]}]}
    def candidate(payload):
        calls.append(("financial", payload))
        return {"data_available": True, "candidates": []}
    result = dispatch_message(new_context(), "腾讯最新股价", lambda _: {}, lambda _: {}, "actor",
                              candidate_ask=candidate, market_snapshot_ask=snapshot,
                              company_ask=lambda payload: {"status": "refused", "answer": "no"},
                              answer_mode="mcp")
    assert calls[0][0] == "snapshot"
    assert "500" in result["messages"][-1]["text"]
    assert result["messages"][-1]["answer"]["evidence_level"] == "third_party_market_data"


def test_mcp_failure_is_readable_and_does_not_fallback_official():
    calls = []
    def snapshot(payload):
        calls.append("snapshot")
        return {"data_available": False, "error_codes": ["MCP_SOURCE_UNAVAILABLE"], "observations": []}
    def official(payload):
        calls.append("official")
        return {"status": "answered", "answer": "official"}
    result = dispatch_message(new_context(), "腾讯最新行情", lambda _: {}, lambda _: {}, "actor",
                              market_snapshot_ask=snapshot, company_ask=official, answer_mode="mcp")
    assert calls == ["snapshot"]
    assert "外部行情数据" in result["messages"][-1]["text"] or "暂时没有取得" in result["messages"][-1]["text"]
    assert result["messages"][-1]["answer"]["status"] == "refused"
