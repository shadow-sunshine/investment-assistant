import pandas as pd

from investment_assistant import market_data
from investment_assistant.source_governance import (
    HEALTH_AVAILABLE, HEALTH_DEGRADED, RESULT_FAILED, RESULT_SUCCESS,
    get_default_health_registry, get_default_tool_call_ledger,
)


def setup_function():
    get_default_health_registry().reset()
    get_default_tool_call_ledger().reset()


class FakeTicker:
    def history(self, **kwargs):
        return pd.DataFrame(
            {"Close": [10.0, 11.0], "Volume": [100, 120]},
            index=pd.to_datetime(["2026-09-25", "2026-09-26"], utc=True),
        )

    def get_income_stmt(self, **kwargs):
        return pd.DataFrame({pd.Timestamp("2025-12-31"): {"TotalRevenue": 100.0, "NetIncome": 20.0}})

    def get_cash_flow(self, **kwargs):
        return pd.DataFrame({pd.Timestamp("2025-12-31"): {"FreeCashFlow": 15.0}})

    def get_info(self):
        return {"trailingPE": 10.0, "priceToBook": 2.0}

    def get_news(self, **kwargs):
        return []


def _ledger(operation):
    return [item for item in get_default_tool_call_ledger().records() if item.operation == operation]


def test_market_snapshot_success_keeps_payload_and_registers_health_and_audit(monkeypatch):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: FakeTicker())

    result = market_data.fetch_market_snapshot("aapl")

    assert result["ticker"] == "AAPL" and result["data_available"] is True
    assert result["latest_close"] == 11.0
    assert get_default_health_registry().snapshot(market_data.YAHOO_SOURCE)["status"] == HEALTH_AVAILABLE
    record, = _ledger("fetch_market_snapshot")
    assert record.result_status == RESULT_SUCCESS
    assert record.error_code is None


def test_market_snapshot_failure_returns_stable_error_and_records_call(monkeypatch):
    class BrokenTicker:
        def history(self, **kwargs):
            raise ConnectionError("api_key=private-value")

    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: BrokenTicker())

    result = market_data.fetch_market_snapshot("AAPL")

    assert result["data_available"] is False
    assert result["error_code"] == "dependency_error"
    assert "private-value" not in result["error"]
    record, = _ledger("fetch_market_snapshot")
    assert record.result_status == RESULT_FAILED and record.error_code == "dependency_error"


def test_financial_snapshot_failure_is_structured_safe_and_audited(monkeypatch):
    class BrokenTicker:
        def get_income_stmt(self, **kwargs):
            raise TimeoutError("token=private-secret")

    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: BrokenTicker())

    result = market_data.fetch_financial_snapshot("aapl")

    assert result["data_available"] is False
    assert result["error_code"] == "timeout"
    assert "private-secret" not in result["error"]
    assert get_default_health_registry().snapshot(market_data.YAHOO_SOURCE)["status"] == HEALTH_DEGRADED
    record, = _ledger("fetch_financial_snapshot")
    assert record.result_status == RESULT_FAILED and record.error_code == "timeout"


def test_news_empty_success_is_distinct_from_request_failure(monkeypatch):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: FakeTicker())

    empty = market_data.fetch_recent_news("AAPL")

    assert isinstance(empty, list) and empty == []
    assert empty.status == RESULT_SUCCESS and empty.error is None
    assert get_default_health_registry().snapshot(market_data.YAHOO_SOURCE)["status"] == HEALTH_AVAILABLE
    assert _ledger("fetch_recent_news")[-1].result_status == RESULT_SUCCESS

    class BrokenNewsTicker:
        def get_news(self, **kwargs):
            raise TimeoutError("upstream private details")

    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: BrokenNewsTicker())
    failed = market_data.fetch_recent_news("AAPL")

    assert isinstance(failed, list) and failed == []
    assert failed.status == RESULT_FAILED
    assert failed.error is not None and failed.error.error_code.value == "timeout"
    assert get_default_health_registry().snapshot(market_data.YAHOO_SOURCE)["last_error_code"] == "timeout"
    from investment_assistant.api import app
    from fastapi.testclient import TestClient
    yahoo_health = next(item for item in TestClient(app).get("/api/source-health").json()["sources"] if item["source"] == market_data.YAHOO_SOURCE)
    assert yahoo_health["last_error_code"] == "timeout"
    assert _ledger("fetch_recent_news")[-1].result_status == RESULT_FAILED


def test_financial_snapshot_success_remains_dict_and_records_call(monkeypatch):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: FakeTicker())

    result = market_data.fetch_financial_snapshot("AAPL")

    assert isinstance(result, dict) and result["data_available"] is True
    assert _ledger("fetch_financial_snapshot")[-1].result_status == RESULT_SUCCESS


def test_timeout_retries_once_then_audits_actual_attempts(monkeypatch):
    calls = {"count": 0}
    class TimeoutThenSuccess:
        def history(self, **kwargs):
            calls["count"] += 1
            assert kwargs["timeout"] == market_data.YAHOO_CALL_POLICY.read_timeout_s
            if calls["count"] <= 2:
                raise TimeoutError("upstream timeout")
            return pd.DataFrame({"Close": [10.0], "Volume": [5]}, index=pd.to_datetime(["2026-09-26"], utc=True))
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: TimeoutThenSuccess())
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)

    result = market_data.fetch_market_snapshot("AAPL")

    assert result["data_available"] is False
    assert result["error_code"] == "timeout"
    assert calls["count"] == market_data.YAHOO_CALL_POLICY.max_attempts == 2
    record, = _ledger("fetch_market_snapshot")
    assert record.attempts == 2 and record.result_status == RESULT_FAILED


def test_single_timeout_retries_and_succeeds_with_attempt_count(monkeypatch):
    calls = {"count": 0}
    class FlakyTicker:
        def history(self, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("transient")
            return pd.DataFrame({"Close": [10.0], "Volume": [5]}, index=pd.to_datetime(["2026-09-26"], utc=True))
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: FlakyTicker())
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)
    result = market_data.fetch_market_snapshot("AAPL")
    assert result["data_available"] is True
    assert calls["count"] == 2
    assert _ledger("fetch_market_snapshot")[-1].attempts == 2


def test_yfinance_elapsed_total_budget_fails_closed(monkeypatch):
    from investment_assistant.source_governance import SourceCallPolicy
    calls = {"count": 0}
    class SlowTicker:
        def history(self, **kwargs):
            calls["count"] += 1
            import time as _time
            _time.sleep(0.02)
            return pd.DataFrame({"Close": [10.0]}, index=pd.to_datetime(["2026-09-26"], utc=True))
    monkeypatch.setattr(market_data, "YAHOO_CALL_POLICY", SourceCallPolicy(total_budget_s=0.005, max_attempts=2, backoff_seconds=0))
    monkeypatch.setattr(market_data.yf, "Ticker", lambda ticker: SlowTicker())
    result = market_data.fetch_market_snapshot("AAPL")
    assert result["error_code"] == "timeout"
    assert calls["count"] == 1
    assert _ledger("fetch_market_snapshot")[-1].attempts == 1
