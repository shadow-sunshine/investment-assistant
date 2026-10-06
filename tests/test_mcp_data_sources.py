"""两个数据源 MCP 的离线解析与受控降级回归。"""
from __future__ import annotations

import json
import subprocess

from investment_assistant import mcp_data_sources as mcp
from investment_assistant.chat_session import dispatch_message, new_context
from investment_assistant.market_data_sources import MarketDataSourceRegistry


def test_akshare_columnar_financials_are_ticker_and_year_bound(monkeypatch):
    monkeypatch.setenv("IA_AKSHARE_MCP_PYTHON", "python")
    monkeypatch.setenv("IA_AKSHARE_MCP_URL", "http://127.0.0.1:28888/mcp")
    data = {"SECUCODE": {"0": "09988.HK", "1": "09988.HK", "2": "00001.HK"},
            "REPORT_DATE": {"0": "2025-03-31", "1": "2026-03-31", "2": "2025-03-31"},
            "STD_ITEM_NAME": {"0": "营业额", "1": "营业额", "2": "不相干"},
            "AMOUNT": {"0": 100, "1": 200, "2": 999}}
    payload = json.dumps({"ok": True, "result": json.dumps(data, ensure_ascii=False)}, ensure_ascii=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)
    adapter = mcp.MCPDataSourceAdapter(mcp.SOURCE_AKSHARE_MCP)
    result = adapter.fetch_structured_financials("9988.HK", year="2025")
    assert result.status == "candidate"
    assert result.data == [{"SECUCODE": "09988.HK", "REPORT_DATE": "2025-03-31",
                            "STD_ITEM_NAME": "营业额", "AMOUNT": 100}]
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert adapter.fetch_structured_financials("9988.HK", year="2024").status == "empty"


def test_baostock_markdown_result_does_not_recurse(monkeypatch):
    monkeypatch.setenv("IA_BAOSTOCK_MCP_PYTHON", "python")
    text = "| code | statDate | netProfit |\n|:--|:--|--:|\n| sz.000001 | 2025-12-31 | 123 |"
    monkeypatch.setattr(mcp.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True, "result": {"result": text}}), stderr=""))
    result = mcp.MCPDataSourceAdapter(mcp.SOURCE_BAOSTOCK_MCP).fetch_structured_financials("000001.SZ", year="2025")
    assert result.status == "candidate"
    assert result.data == [{"code": "sz.000001", "statDate": "2025-12-31", "netProfit": "123"}]
    assert mcp._rows("unstructured response") == []


def test_registry_skips_unsupported_market_without_marking_source_failed():
    registry = MarketDataSourceRegistry()
    class AOnly:
        source = "a_only"
        markets = {"A"}
        def fetch_structured_financials(self, ticker, *, year=None):
            raise AssertionError("非 A 股不能调用")
    registry.register(AOnly())
    assert registry.fetch_structured_financials("9988.HK", year="2025") == []


def test_hk_followup_remains_refused_but_shows_unverified_candidates():
    context = {**new_context(), "ticker": "9988.HK", "discovery_ticker": "9988.HK",
               "discovery_query": "阿里巴巴 2025 年收入怎么样"}
    result = dispatch_message(
        context, "收入怎么样", lambda _: {}, lambda _: {}, "analyst",
        company_ask=lambda _: {"status": "refused", "answer": "官方资料未接入。", "error_code": "MATERIAL_NOT_ONBOARDED"},
        candidate_ask=lambda payload: {"ticker": payload["ticker"], "year": payload["year"],
                                       "data_available": True, "verified": False,
                                       "candidates": [{"source": "akshare_mcp", "row": {"AMOUNT": 100}}]},
    )
    answer = result["messages"][-1]["answer"]
    assert answer["status"] == "refused"
    assert answer["candidate_data"]["year"] == "2025"
    assert answer["candidate_data"]["verified"] is False
    assert "未核验" in result["messages"][-1]["text"]
