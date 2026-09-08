from types import SimpleNamespace

import pandas as pd

from investment_assistant.market_data import fetch_financial_snapshot


class FakeTicker:
    def get_income_stmt(self, freq: str):
        assert freq == "yearly"
        return pd.DataFrame({pd.Timestamp("2025-09-30"): {"TotalRevenue": 391_000_000_000, "NetIncome": 93_000_000_000}})

    def get_cash_flow(self, freq: str):
        assert freq == "yearly"
        return pd.DataFrame({pd.Timestamp("2025-09-30"): {"FreeCashFlow": 108_000_000_000}})

    def get_info(self):
        return {"trailingPE": 28.5, "priceToBook": 42.1}


class BrokenTicker:
    def get_income_stmt(self, freq: str):
        raise TimeoutError("upstream timeout")


def test_fetch_financial_snapshot_extracts_values_and_dates(monkeypatch):
    monkeypatch.setattr("investment_assistant.market_data.yf.Ticker", lambda _: FakeTicker())
    result = fetch_financial_snapshot("aapl")
    assert result["data_available"]
    assert result["revenue"] == 391_000_000_000
    assert result["net_income"] == 93_000_000_000
    assert result["free_cash_flow"] == 108_000_000_000
    assert result["revenue_period_end"] == "2025-09-30"
    assert result["trailing_pe"] == 28.5
    assert result["price_to_book"] == 42.1


def test_fetch_financial_snapshot_discloses_failure(monkeypatch):
    monkeypatch.setattr("investment_assistant.market_data.yf.Ticker", lambda _: BrokenTicker())
    result = fetch_financial_snapshot("aapl")
    assert not result["data_available"]
    assert "财报与估值请求失败" in result["error"]
    assert "营收" in result["unavailable_fields"]


def test_financial_section_calls_missing_values_snapshot_fields(monkeypatch):
    from investment_assistant.workflow import _financial_section

    monkeypatch.setattr("investment_assistant.market_data.yf.Ticker", lambda _: FakeTicker())
    snapshot = fetch_financial_snapshot("aapl")
    assert "\u5feb\u7167\u5b57\u6bb5\u7f3a\u5931\uff1a\u65e0" in _financial_section(snapshot)
