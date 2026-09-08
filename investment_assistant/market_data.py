"""真实行情、财报、估值与新闻采集。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _format_statement_date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return str(value)


def _latest_statement_value(statement: pd.DataFrame, row_name: str) -> tuple[float | None, str | None]:
    """读取报表中最近披露期的指定行；无数据时返回空值，不编造。"""
    if statement is None or statement.empty or row_name not in statement.index:
        return None, None
    row = statement.loc[row_name].dropna()
    if row.empty:
        return None, None
    latest_date = max(row.index)
    return _as_float(row.loc[latest_date]), _format_statement_date(latest_date)


def fetch_market_snapshot(ticker: str, period: str = "1y") -> dict[str, Any]:
    """获取可审计的历史行情快照；请求失败时返回明确的错误字段。"""
    normalized_ticker = ticker.upper().strip()
    fetched_at = datetime.now(UTC).isoformat()
    try:
        security = yf.Ticker(normalized_ticker)
        history = security.history(period=period, interval="1d", auto_adjust=True, timeout=15, raise_errors=True)
        if history.empty or "Close" not in history:
            return {"ticker": normalized_ticker, "fetched_at": fetched_at, "data_available": False, "error": "未获取到有效历史行情。"}

        close = history["Close"].dropna()
        daily_returns = close.pct_change().dropna()
        latest = _as_float(close.iloc[-1])
        start = _as_float(close.iloc[0])
        high_water_mark = close.cummax()
        drawdown = close / high_water_mark - 1
        latest_timestamp = close.index[-1]
        latest_date = latest_timestamp.isoformat() if hasattr(latest_timestamp, "isoformat") else str(latest_timestamp)
        one_month_index = max(0, len(close) - 22)

        return {
            "ticker": normalized_ticker,
            "fetched_at": fetched_at,
            "data_available": True,
            "source": "Yahoo Finance via yfinance",
            "latest_trading_date": latest_date,
            "period": period,
            "observations": int(len(close)),
            "latest_close": round(latest, 4) if latest is not None else None,
            "period_return_pct": round((latest / start - 1) * 100, 2) if latest and start else None,
            "one_month_return_pct": round((latest / float(close.iloc[one_month_index]) - 1) * 100, 2) if latest and float(close.iloc[one_month_index]) else None,
            "annualized_volatility_pct": round(float(daily_returns.std() * np.sqrt(252) * 100), 2) if not daily_returns.empty else None,
            "max_drawdown_pct": round(float(drawdown.min() * 100), 2),
            "volume_latest": int(history["Volume"].iloc[-1]) if "Volume" in history and not np.isnan(history["Volume"].iloc[-1]) else None,
        }
    except Exception as exc:
        return {"ticker": normalized_ticker, "fetched_at": fetched_at, "data_available": False, "error": f"行情请求失败：{type(exc).__name__}: {exc}"}


def fetch_financial_snapshot(ticker: str) -> dict[str, Any]:
    """获取年度营收、净利润、自由现金流和估值指标，并保留各自数据日期。"""
    normalized_ticker = ticker.upper().strip()
    fetched_at = datetime.now(UTC).isoformat()
    try:
        security = yf.Ticker(normalized_ticker)
        income_stmt = security.get_income_stmt(freq="yearly")
        cash_flow = security.get_cash_flow(freq="yearly")
        info = security.get_info()

        revenue, revenue_date = _latest_statement_value(income_stmt, "TotalRevenue")
        net_income, net_income_date = _latest_statement_value(income_stmt, "NetIncome")
        free_cash_flow, free_cash_flow_date = _latest_statement_value(cash_flow, "FreeCashFlow")
        trailing_pe = _as_float(info.get("trailingPE"))
        price_to_book = _as_float(info.get("priceToBook"))

        unavailable_fields = []
        values = {
            "revenue": revenue,
            "net_income": net_income,
            "free_cash_flow": free_cash_flow,
            "trailing_pe": trailing_pe,
            "price_to_book": price_to_book,
        }
        labels = {
            "revenue": "营收",
            "net_income": "净利润",
            "free_cash_flow": "自由现金流",
            "trailing_pe": "滚动市盈率（PE）",
            "price_to_book": "市净率（PB）",
        }
        for key, value in values.items():
            if value is None:
                unavailable_fields.append(labels[key])

        return {
            "ticker": normalized_ticker,
            "fetched_at": fetched_at,
            "data_available": bool(revenue is not None or net_income is not None or free_cash_flow is not None or trailing_pe is not None or price_to_book is not None),
            "source": "Yahoo Finance via yfinance",
            "revenue": revenue,
            "revenue_period_end": revenue_date,
            "net_income": net_income,
            "net_income_period_end": net_income_date,
            "free_cash_flow": free_cash_flow,
            "free_cash_flow_period_end": free_cash_flow_date,
            "trailing_pe": trailing_pe,
            "price_to_book": price_to_book,
            "valuation_as_of": fetched_at,
            "unavailable_fields": unavailable_fields,
            "error": None if not unavailable_fields else "部分财报或估值指标不可用。",
        }
    except Exception as exc:
        return {
            "ticker": normalized_ticker,
            "fetched_at": fetched_at,
            "data_available": False,
            "source": "Yahoo Finance via yfinance",
            "error": f"财报与估值请求失败：{type(exc).__name__}: {exc}",
            "unavailable_fields": ["营收", "净利润", "自由现金流", "滚动市盈率（PE）", "市净率（PB）"],
        }


def fetch_recent_news(ticker: str, limit: int = 5) -> list[dict[str, str]]:
    """获取近期新闻，并保留发布/抓取时间与原始链接。"""
    fetched_at = datetime.now(UTC).isoformat()
    normalized_ticker = ticker.upper().strip()
    try:
        raw_news = yf.Ticker(normalized_ticker).get_news(count=limit, tab="news") or []
    except Exception:
        return []

    records: list[dict[str, str]] = []
    for index, item in enumerate(raw_news[:limit], start=1):
        content = item.get("content", item)
        title = content.get("title") or item.get("title") or "未命名新闻"
        url = content.get("canonicalUrl", {}).get("url") or content.get("clickThroughUrl", {}).get("url") or item.get("link") or ""
        provider = content.get("provider", {}).get("displayName") or item.get("publisher") or "Yahoo Finance"
        publish_time = content.get("pubDate") or str(item.get("providerPublishTime") or "")
        records.append({
            "id": f"news-{normalized_ticker}-{index}",
            "title": str(title),
            "text": f"{title}\n来源：{provider}\n发布日期：{publish_time}\n链接：{url}",
            "source": str(provider),
            "url": str(url),
            "published_at": str(publish_time),
            "fetched_at": fetched_at,
            "ticker": normalized_ticker,
        })
    return records
