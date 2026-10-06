"""市场数据源适配层的离线契约测试。"""
from __future__ import annotations

from investment_assistant.market_data_sources import (
    AKShareAdapter,
    BaoStockAdapter,
    MarketDataSourceRegistry,
    STATUS_CANDIDATE,
    STATUS_SUCCESS,
    STATUS_UNAVAILABLE,
    default_registry,
)


class FakeBaoResult:
    error_code = "0"
    error_msg = ""
    fields = ["code", "code_name"]

    def get_data(self):
        return [["sz.000001", "平安银行"]]


class FakeBaoClient:
    def __init__(self):
        self.logged_in = 0
        self.logged_out = 0

    def login(self):
        self.logged_in += 1
        return FakeBaoResult()

    def logout(self):
        self.logged_out += 1

    def query_stock_basic(self, **kwargs):
        assert kwargs == {"code": "", "code_name": "平安银行"}
        return FakeBaoResult()

    def query_history_k_data_plus(self, *args, **kwargs):
        assert args[0] == "sz.000001"
        return type("History", (), {
            "error_code": "0", "fields": ["date", "code", "close"],
            "get_data": lambda self: [["2025-01-02", "sz.000001", "10.0"]],
        })()


class FakeAkClient:
    def stock_zh_a_spot_em(self):
        return [{"代码": "000001", "名称": "平安银行"}, {"代码": "600519", "名称": "贵州茅台"}]

    def stock_zh_a_hist(self, **kwargs):
        assert kwargs["symbol"] == "000001"
        return [{"日期": "2025-01-02", "收盘": 10.0}]


def test_default_registry_is_lazy_and_does_not_import_optional_packages():
    registry = default_registry()
    assert set(registry.adapters) == {"baostock", "akshare"}
    assert registry.health() == [{"source": "akshare", "status": "configured"},
                                 {"source": "baostock", "status": "configured"}]


def test_baostock_resolve_returns_candidate_with_provenance():
    client = FakeBaoClient()
    result = BaoStockAdapter(client=client).resolve_company("平安银行")
    assert result.status == STATUS_CANDIDATE
    assert result.data == [{"ticker": "sz.000001", "company": "平安银行", "market": "A"}]
    assert result.provenance["endpoint"] == "query_stock_basic"
    assert "verified" not in result.status
    assert client.logged_in == 1 and client.logged_out == 1


def test_baostock_history_is_structured_but_not_annual_report_evidence():
    result = BaoStockAdapter(client=FakeBaoClient()).fetch_history(
        "000001.SZ", start_date="2025-01-01", end_date="2025-01-03"
    )
    assert result.status == STATUS_SUCCESS
    assert result.data[0]["close"] == "10.0"
    assert "not_official_annual_report" in result.warnings[0]


def test_baostock_rejects_non_a_share_without_external_call():
    result = BaoStockAdapter(client=FakeBaoClient()).fetch_history(
        "0700.HK", start_date="2025-01-01", end_date="2025-01-03"
    )
    assert result.status == STATUS_UNAVAILABLE
    assert result.error["error_code"] == "invalid_input"


def test_akshare_resolve_filters_candidates_by_company_name():
    result = AKShareAdapter(client=FakeAkClient()).resolve_company("平安银行", market="A")
    assert result.status == STATUS_CANDIDATE
    assert result.data == [{"ticker": "000001", "company": "平安银行", "market": "A"}]
    assert result.provenance["endpoint"] == "stock_zh_a_spot_em"


def test_akshare_history_preserves_source_role_and_period():
    result = AKShareAdapter(client=FakeAkClient()).fetch_a_history(
        "000001.SZ", start_date="2025-01-01", end_date="2025-01-03"
    )
    assert result.status == STATUS_SUCCESS
    assert result.provenance["start_date"] == "2025-01-01"
    assert result.data[0]["收盘"] == 10.0


def test_malformed_or_empty_external_response_fails_closed():
    class BrokenAk:
        def stock_zh_a_spot_em(self):
            return object()

    result = AKShareAdapter(client=BrokenAk()).resolve_company("腾讯", market="A")
    assert result.status == STATUS_UNAVAILABLE
    assert result.error["error_code"] == "parse_error"

    class EmptyBao:
        def login(self):
            return FakeBaoResult()

        def logout(self):
            pass

        def query_stock_basic(self, **kwargs):
            return []

    result = BaoStockAdapter(client=EmptyBao()).resolve_company("不存在")
    assert result.status == STATUS_UNAVAILABLE
    assert result.error["error_code"] == "empty_response"


def test_registry_uses_injected_adapters_without_network():
    class FakeAdapter:
        source = "fake"

        def resolve_company(self, query, *, market=None):
            from investment_assistant.market_data_sources import SourceObservation
            return SourceObservation(
                source="fake", operation="resolve_company", status=STATUS_CANDIDATE,
                retrieved_at="2026-10-05T00:00:00+00:00", company=query,
                data=[{"ticker": "X", "company": query}],
            )

    registry = MarketDataSourceRegistry()
    registry.register(FakeAdapter())
    results = registry.resolve_company("测试公司")
    assert len(results) == 1 and results[0].status == STATUS_CANDIDATE
