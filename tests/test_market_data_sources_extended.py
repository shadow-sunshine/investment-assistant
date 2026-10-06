"""数据源适配层扩展契约：结构化行情/财务、审计登记与可选依赖缺失时的行为。"""
from __future__ import annotations

import pytest

from investment_assistant import market_data_sources as mds
from investment_assistant.market_data_sources import (
    AKShareAdapter,
    BaoStockAdapter,
    MarketDataSourceRegistry,
    STATUS_CANDIDATE,
    STATUS_EMPTY,
    STATUS_SUCCESS,
    STATUS_UNAVAILABLE,
    default_registry,
    record_observation,
    source_health_snapshot,
)
from investment_assistant.source_governance import (
    get_default_health_registry,
    get_default_tool_call_ledger,
)


class FakeProfit:
    error_code = "0"
    error_msg = ""
    fields = ["code", "pubDate", "roeAvg"]

    def get_data(self):
        return [["sz.000001", "20251231", "12.5"]]


class FakeBaoExtended:
    def __init__(self, profit: object | None = None) -> None:
        self.profit = profit if profit is not None else FakeProfit()

    def login(self):
        return type("R", (), {"error_code": "0", "error_msg": ""})()

    def logout(self):
        return None

    def query_profit_data(self, **kwargs):
        return self.profit

    def query_history_k_data_plus(self, *args, **kwargs):
        return type("H", (), {
            "error_code": "0", "fields": ["date", "code", "close"],
            "get_data": lambda self: [["2025-01-02", "sz.000001", "10.0"]],
        })()


class FakeAkExtended:
    def stock_hk_hist(self, **kwargs):
        assert kwargs["symbol"] == "09988"
        return [{"日期": "2025-01-02", "收盘": 120.0}]

    def stock_us_hist(self, **kwargs):
        assert kwargs["symbol"] == "BABA"
        return [{"Date": "2025-01-02", "Close": 85.0}]

    def stock_financial_hk_report_em(self, **kwargs):
        return [{"item": "收入", "value": 1}]

    def stock_financial_abstract(self, **kwargs):
        return [{"指标": "营业总收入", "值": 1}]


def test_baostock_market_snapshot_keeps_period_and_provenance():
    result = BaoStockAdapter(client=FakeBaoExtended()).fetch_market_snapshot("000001.SZ")
    assert result.status == STATUS_SUCCESS
    assert result.provenance["start_date"] < result.provenance["end_date"]
    assert result.provenance["endpoint"] == "query_history_k_data_plus"
    assert "not_official_annual_report" in result.warnings[0]
    assert isinstance(result.provenance["elapsed_ms"], int)


def test_baostock_structured_financials_are_candidates_not_evidence():
    result = BaoStockAdapter(client=FakeBaoExtended()).fetch_structured_financials("000001.SZ", year="2025")
    assert result.status == STATUS_CANDIDATE
    assert result.data[0]["roeAvg"] == "12.5"
    assert "candidate_not_official_annual_report" in result.warnings
    assert result.provenance["year"] == "2025"


def test_akshare_multi_market_snapshot_and_financials():
    adapter = AKShareAdapter(client=FakeAkExtended())
    hk = adapter.fetch_market_snapshot("9988.HK")
    assert hk.status == STATUS_SUCCESS and hk.provenance["market"] == "HK"
    assert hk.provenance["endpoint"] == "stock_hk_hist"
    us = adapter.fetch_market_snapshot("BABA")
    assert us.status == STATUS_SUCCESS and us.provenance["endpoint"] == "stock_us_hist"
    assert adapter.fetch_structured_financials("9988.HK").status == STATUS_CANDIDATE
    assert adapter.fetch_structured_financials("000001.SZ").status == STATUS_CANDIDATE


def test_akshare_rejects_unusable_ticker_and_market():
    adapter = AKShareAdapter(client=FakeAkExtended())
    bad = adapter.fetch_market_snapshot("not a ticker")
    assert bad.status == STATUS_UNAVAILABLE and bad.error["error_code"] == "invalid_input"
    unsupported = adapter.fetch_market_snapshot("9988.HK", market="MARS")
    assert unsupported.error["error_code"] == "unsupported_ticker"


def test_empty_financial_response_is_empty_not_failure():
    class EmptyProfit:
        error_code = "0"
        fields = ["code"]

        def get_data(self):
            return []

    result = BaoStockAdapter(client=FakeBaoExtended(profit=EmptyProfit())).fetch_structured_financials("000001.SZ")
    assert result.status == STATUS_EMPTY and result.data == []


def test_registry_dispatch_records_health_and_audit():
    health, ledger = get_default_health_registry(), get_default_tool_call_ledger()
    before_ledger = len(ledger.records())
    registry = MarketDataSourceRegistry()
    registry.register(BaoStockAdapter(client=FakeBaoExtended()))
    observations = registry.fetch_market_snapshot("000001.SZ")
    assert len(observations) == 1
    # 真实调用成功后该来源必须是 available，且快照里能看到 baostock。
    assert health.snapshot("baostock")["status"] == "available"
    assert "baostock" in {row["source"] for row in health.snapshot_all()}
    assert len(ledger.records()) == before_ledger + 1
    record = ledger.records()[-1]
    assert record.source == "baostock" and record.operation == "fetch_market_snapshot"
    assert record.ticker == "000001.SZ" and record.result_status == STATUS_SUCCESS


def test_akshare_snapshot_dispatch_records_its_own_operation():
    registry = MarketDataSourceRegistry()
    registry.register(AKShareAdapter(client=FakeAkExtended()))
    before = len(get_default_tool_call_ledger().records())
    (observation,) = registry.fetch_market_snapshot("9988.HK")
    assert observation.provenance["endpoint"] == "stock_hk_hist"
    record = get_default_tool_call_ledger().records()[-1]
    assert len(get_default_tool_call_ledger().records()) == before + 1
    assert record.operation == "fetch_market_snapshot" and record.source == "akshare"


def test_registry_dispatch_converts_adapter_exception_to_observation():
    class Exploding:
        source = "exploding"

        def fetch_market_snapshot(self, ticker, *, market=None):
            raise RuntimeError("boom")

    registry = MarketDataSourceRegistry()
    registry.register(Exploding())
    (observation,) = registry.fetch_market_snapshot("000001.SZ")
    assert observation.status == STATUS_UNAVAILABLE
    assert observation.error["error_code"] == "unknown"
    assert get_default_health_registry().snapshot("exploding")["status"] == "unavailable"


def test_record_observation_registers_failure_with_error_code():
    """用独立来源名，避免与 baostock 的成功用例互相污染全局健康表。"""
    observation = mds._error_observation("probe-timeout-source", "resolve_company", mds.SourceErrorCode.TIMEOUT, "slow")
    record_observation(observation)
    snapshot = get_default_health_registry().snapshot("probe-timeout-source")
    assert snapshot["last_error_code"] == "timeout"
    assert snapshot["status"] == "degraded"


def test_source_health_snapshot_never_fabricates_availability():
    rows = {row["source"]: row for row in source_health_snapshot(default_registry())}
    assert {"baostock", "akshare"} <= set(rows)
    for row in rows.values():
        assert row["configured"] is True
        assert row["status"] in {"unknown", "available", "degraded", "unavailable"}
    assert rows["akshare"]["markets"] == ["A", "HK", "US"]
    assert rows["baostock"]["markets"] == ["A"]


def test_adapters_work_without_optional_packages_installed():
    """未安装 baostock/akshare 时必须返回结构化依赖错误，而不是抛未捕获异常。"""
    import importlib

    for name in ("baostock", "akshare"):
        with pytest.raises(ImportError):
            importlib.import_module(name)
    for adapter, operation in ((BaoStockAdapter(), "resolve_company"), (AKShareAdapter(), "resolve_company")):
        result = getattr(adapter, operation)("某公司")
        assert result.status == STATUS_UNAVAILABLE
        assert result.error["error_code"] == "dependency_error"
