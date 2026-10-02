"""真实行情、财报、估值与新闻采集。"""

from __future__ import annotations

from datetime import UTC, datetime
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
import yfinance as yf

from .source_governance import (
    RESULT_FAILED,
    RESULT_SUCCESS,
    SourceCallFailure,
    SourceCallPolicy,
    SourceErrorCode,
    ToolCallAuditRecord,
    ToolCallError,
    call_fingerprint,
    classify_exception,
    execute_source_call,
    get_default_health_registry,
    get_default_tool_call_ledger,
    tool_error,
)

YAHOO_SOURCE = "Yahoo Finance via yfinance"
YAHOO_CALL_POLICY = SourceCallPolicy(
    connect_timeout_s=10.0,
    read_timeout_s=30.0,
    total_budget_s=90.0,
    max_attempts=2,
    backoff_seconds=1.0,
)


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


def _run_yahoo_call(call: Callable[[], Any], operation: str, attempts: list[int]) -> Any:
    """所有 yfinance 网络入口共享同一重试、超时配置和总预算策略。"""
    started = time.monotonic()

    def counted_attempt() -> Any:
        attempts[0] += 1
        result = call()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= YAHOO_CALL_POLICY.total_budget_s * 1000:
            raise SourceCallFailure(tool_error(
                YAHOO_SOURCE, operation, SourceErrorCode.TIMEOUT, retryable=False,
                attempts=attempts[0], elapsed_ms=elapsed_ms,
                detail="yfinance call exceeded configured total budget",
            ))
        return result

    return execute_source_call(
        counted_attempt,
        source=YAHOO_SOURCE,
        operation=operation,
        policy=YAHOO_CALL_POLICY,
    )


def _error_with_actual_attempts(error: ToolCallError, attempts: int) -> ToolCallError:
    from dataclasses import replace
    return replace(error, attempts=attempts)


def fetch_market_snapshot(ticker: str, period: str = "1y") -> dict[str, Any]:
    """获取可审计的历史行情快照；空结果和调用失败均不伪造成可用数据。"""
    normalized_ticker = ticker.upper().strip()
    fetched_at = datetime.now(UTC).isoformat()
    operation = "fetch_market_snapshot"
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    attempts = [0]
    error: ToolCallError | None = None
    status = RESULT_FAILED
    try:
        def fetch() -> pd.DataFrame:
            history = yf.Ticker(normalized_ticker).history(
                period=period, interval="1d", auto_adjust=True,
                timeout=YAHOO_CALL_POLICY.read_timeout_s, raise_errors=True,
            )
            if history is None or history.empty or "Close" not in history or history["Close"].dropna().empty:
                raise SourceCallFailure(tool_error(YAHOO_SOURCE, operation, SourceErrorCode.EMPTY_RESPONSE))
            return history

        history = _run_yahoo_call(fetch, operation, attempts)
        close = history["Close"].dropna()
        daily_returns = close.pct_change().dropna()
        latest = _as_float(close.iloc[-1])
        start = _as_float(close.iloc[0])
        high_water_mark = close.cummax()
        drawdown = close / high_water_mark - 1
        latest_timestamp = close.index[-1]
        latest_date = latest_timestamp.isoformat() if hasattr(latest_timestamp, "isoformat") else str(latest_timestamp)
        one_month_index = max(0, len(close) - 22)
        status = RESULT_SUCCESS
        return {
            "ticker": normalized_ticker,
            "fetched_at": fetched_at,
            "data_available": True,
            "source": YAHOO_SOURCE,
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
        error = _error_with_actual_attempts(classify_exception(exc, YAHOO_SOURCE, operation, attempts=attempts[0], elapsed_ms=int((time.monotonic() - started) * 1000)), attempts[0])
        return {
            "ticker": normalized_ticker, "fetched_at": fetched_at, "data_available": False,
            "source": YAHOO_SOURCE, "error": error.message, "error_code": error.error_code.value,
        }
    finally:
        _record_yahoo_call(operation, normalized_ticker, started, started_at, status, error, attempts[0], period=period)


def fetch_financial_snapshot(ticker: str) -> dict[str, Any]:
    """获取年度财报/估值；部分缺失明确降级，完全空响应不伪装成功。"""
    normalized_ticker = ticker.upper().strip()
    fetched_at = datetime.now(UTC).isoformat()
    operation = "fetch_financial_snapshot"
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    attempts = [0]
    error: ToolCallError | None = None
    status = RESULT_FAILED
    labels = ["营收", "净利润", "自由现金流", "滚动市盈率（PE）", "市净率（PB）"]
    try:
        def fetch_financials():
            security = yf.Ticker(normalized_ticker)
            return security.get_income_stmt(freq="yearly"), security.get_cash_flow(freq="yearly"), security.get_info()

        income_stmt, cash_flow, info = _run_yahoo_call(fetch_financials, operation, attempts)
        revenue, revenue_date = _latest_statement_value(income_stmt, "TotalRevenue")
        net_income, net_income_date = _latest_statement_value(income_stmt, "NetIncome")
        free_cash_flow, free_cash_flow_date = _latest_statement_value(cash_flow, "FreeCashFlow")
        trailing_pe = _as_float(info.get("trailingPE"))
        price_to_book = _as_float(info.get("priceToBook"))
        values = [revenue, net_income, free_cash_flow, trailing_pe, price_to_book]
        unavailable_fields = [label for label, value in zip(labels, values) if value is None]
        data_available = len(unavailable_fields) < len(labels)
        if unavailable_fields:
            error = tool_error(YAHOO_SOURCE, operation, SourceErrorCode.EMPTY_RESPONSE, attempts=attempts[0], elapsed_ms=int((time.monotonic() - started) * 1000))
        else:
            status = RESULT_SUCCESS
        return {
            "ticker": normalized_ticker,
            "fetched_at": fetched_at,
            "data_available": data_available,
            "source": YAHOO_SOURCE,
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
            "error": "部分财报或估值指标不可用。" if unavailable_fields else None,
            "error_code": error.error_code.value if error else None,
        }
    except Exception as exc:
        error = _error_with_actual_attempts(classify_exception(exc, YAHOO_SOURCE, operation, attempts=attempts[0], elapsed_ms=int((time.monotonic() - started) * 1000)), attempts[0])
        return {
            "ticker": normalized_ticker, "fetched_at": fetched_at, "data_available": False,
            "source": YAHOO_SOURCE, "error": f"财报与估值请求失败：{error.message}",
            "error_code": error.error_code.value, "unavailable_fields": labels,
        }
    finally:
        _record_yahoo_call(operation, normalized_ticker, started, started_at, status, error, attempts[0])


class NewsRecords(list[dict[str, str]]):
    """兼容 list 调用方，同时保留空结果与请求失败的可检查区别。"""

    def __init__(self, records=(), *, error: ToolCallError | None = None):
        super().__init__(records)
        self.error = error
        self.status = RESULT_FAILED if error else RESULT_SUCCESS


def _record_yahoo_call(
    operation: str,
    ticker: str,
    started: float,
    started_at: str,
    result_status: str,
    error: ToolCallError | None,
    attempts: int = 1,
    *,
    period: str | None = None,
) -> None:
    """把真实调用结果写入进程级健康登记表与审计账本。"""
    registry = get_default_health_registry()
    ledger = get_default_tool_call_ledger()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if error is None:
        registry.record_success(YAHOO_SOURCE, operation, latency_ms=elapsed_ms)
    else:
        registry.record_failure(YAHOO_SOURCE, operation, error)
    ledger.register(ToolCallAuditRecord(
        job_id="", requested_by="", source=YAHOO_SOURCE, operation=operation,
        ticker=ticker,
        fingerprint=call_fingerprint(source=YAHOO_SOURCE, operation=operation, ticker=ticker, period=period),
        attempts=attempts, started_at=started_at, finished_at=datetime.now(UTC).isoformat(),
        result_status=result_status, error_code=error.error_code.value if error else None,
    ))


def fetch_recent_news(ticker: str, limit: int = 15) -> NewsRecords:
    """请求成功但无新闻是正常空列表；调用失败通过结构化元数据区分。"""
    fetched_at = datetime.now(UTC).isoformat()
    normalized_ticker = ticker.upper().strip()
    operation = "fetch_recent_news"
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    attempts = [0]
    error: ToolCallError | None = None
    status = RESULT_FAILED
    try:
        raw_news = _run_yahoo_call(lambda: yf.Ticker(normalized_ticker).get_news(count=limit, tab="news") or [], operation, attempts)
        records: list[dict[str, str]] = []
        for index, item in enumerate(raw_news[:limit], start=1):
            content = item.get("content", item)
            title = content.get("title") or item.get("title") or "未命名新闻"
            summary = content.get("summary") or item.get("summary") or item.get("description") or ""
            url = content.get("canonicalUrl", {}).get("url") or content.get("clickThroughUrl", {}).get("url") or item.get("link") or ""
            provider = content.get("provider", {}).get("displayName") or item.get("publisher") or "Yahoo Finance"
            publish_time = content.get("pubDate") or str(item.get("providerPublishTime") or "")
            records.append({
                "id": f"news-{normalized_ticker}-{index}", "title": str(title), "summary": str(summary),
                "text": f"{title}\n摘要：{summary}\n来源：{provider}\n发布日期：{publish_time}\n链接：{url}",
                "source": str(provider), "url": str(url), "published_at": str(publish_time),
                "fetched_at": fetched_at, "ticker": normalized_ticker,
            })
        status = RESULT_SUCCESS
        return NewsRecords(records)
    except Exception as exc:
        error = _error_with_actual_attempts(classify_exception(exc, YAHOO_SOURCE, operation, attempts=attempts[0], elapsed_ms=int((time.monotonic() - started) * 1000)), attempts[0])
        return NewsRecords(error=error)
    finally:
        _record_yahoo_call(operation, normalized_ticker, started, started_at, status, error, attempts[0], period=str(limit))
