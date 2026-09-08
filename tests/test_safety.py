from datetime import UTC, datetime

from investment_assistant.safety import REQUIRED_DISCLAIMER, assess_risk, check_financial_data, validate_report


def _available_financial_snapshot():
    return {
        "data_available": True,
        "fetched_at": datetime.now(UTC).isoformat(),
        "revenue": 100.0,
        "revenue_period_end": "2025-09-30",
        "net_income": 20.0,
        "net_income_period_end": "2025-09-30",
        "free_cash_flow": 30.0,
        "free_cash_flow_period_end": "2025-09-30",
        "trailing_pe": 25.0,
        "price_to_book": 12.0,
        "unavailable_fields": [],
    }


def test_report_requires_disclaimer_and_citation():
    outcome = validate_report("研究观察 [S1]", [{"citation": "S1"}])
    assert not outcome["passed"]
    assert "缺少必需免责声明。" in outcome["findings"]


def test_report_blocks_return_guarantee():
    report = f"研究观察 [S1]\n{REQUIRED_DISCLAIMER}\n保证获得收益"
    outcome = validate_report(report, [{"citation": "S1"}])
    assert not outcome["passed"]
    assert "收益承诺" in outcome["blocked_patterns"]


def test_report_passes_minimum_policy_without_financial_snapshot():
    report = f"研究观察 [S1]\n{REQUIRED_DISCLAIMER}"
    outcome = validate_report(report, [{"citation": "S1"}])
    assert outcome["passed"]


def test_financial_data_missing_is_explicit_risk():
    snapshot = {"data_available": False, "error": "请求超时"}
    risks = check_financial_data(snapshot)
    assert risks == ["财报与估值数据不可用：请求超时。"]


def test_report_requires_financial_section_and_period_dates():
    financials = _available_financial_snapshot()
    report = f"研究观察 [S1]\n{REQUIRED_DISCLAIMER}"
    outcome = validate_report(report, [{"citation": "S1"}], financials)
    assert not outcome["passed"]
    assert "缺少财报与估值独立章节。" in outcome["findings"]
    assert "营收缺少报表期末日期披露。" in outcome["findings"]


def test_negative_free_cash_flow_is_risk():
    market = {"data_available": True, "fetched_at": datetime.now(UTC).isoformat(), "annualized_volatility_pct": 10, "max_drawdown_pct": -5}
    financials = _available_financial_snapshot()
    financials["free_cash_flow"] = -1.0
    risks = assess_risk(market, financials, [{"citation": "S1"}, {"citation": "S2"}])
    assert "最近财年自由现金流为负，现金流质量需进一步核验。" in risks


def test_report_rejects_local_evidence_from_a_different_ticker():
    report = f"\u7814\u7a76\u89c2\u5bdf [S1]\n{REQUIRED_DISCLAIMER}"
    sources = [{"citation": "S1", "metadata": {"source_type": "pdf", "ticker": "AAPL"}}]

    outcome = validate_report(report, sources, ticker="600519.SS")

    assert not outcome["passed"]
    assert "\u8bc1\u636e-\u6807\u7684\u4e0d\u5339\u914d\uff1a\u67e5 600519.SS \u5f15\u7528\u4e86 AAPL \u7684\u8d44\u6599\u3002" in outcome["findings"]


def test_report_allows_unknown_local_material_and_news_for_ticker_check():
    report = f"\u7814\u7a76\u89c2\u5bdf [S1]\n{REQUIRED_DISCLAIMER}"
    sources = [
        {"citation": "S1", "metadata": {"source_type": "text", "ticker": "unknown"}},
        
    ]

    outcome = validate_report(report, sources, ticker="600519.SS")

    assert outcome["passed"]
