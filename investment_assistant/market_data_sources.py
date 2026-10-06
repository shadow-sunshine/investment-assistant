"""可选市场数据源适配层。

Baostock 与 AKShare 只负责公司候选、行情和结构化财务线索，不直接成为官方年报证据。
正式年报仍必须进入 company_onboarding.py 的官方 URL、SHA256、页数和字段锚点流程。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import importlib
import math
import re
import time
from typing import Any, Callable, Mapping, Protocol

from .source_governance import (
    SourceErrorCode,
    ToolCallAuditRecord,
    call_fingerprint,
    get_default_health_registry,
    get_default_tool_call_ledger,
    tool_error,
)

SOURCE_BAOSTOCK = "baostock"
SOURCE_AKSHARE = "akshare"
STATUS_SUCCESS = "success"
STATUS_CANDIDATE = "candidate"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INVALID = "invalid"

#: 适配器声明支持的市场；解析层据此裁剪调用，避免无谓的跨市场抓取。
MARKET_A = "A"
MARKET_HK = "HK"
MARKET_US = "US"

#: 外部候选源调用策略：显式超时与总预算，测试默认不联网。
ADAPTER_CALL_POLICY = {
    "connect_timeout_s": 10.0,
    "read_timeout_s": 30.0,
    "total_budget_s": 45.0,
}


def record_observation(observation: SourceObservation, *, period: str | None = None) -> None:
    """把一次真实外部调用写入健康登记表与审计账本（与 yfinance 链路同一套治理）。"""
    error = None
    if observation.error:
        try:
            from .source_governance import ToolCallError

            error = ToolCallError.from_dict(dict(observation.error))
        except (TypeError, ValueError):
            error = tool_error(observation.source, observation.operation, SourceErrorCode.UNKNOWN)
    health = get_default_health_registry()
    ticker = observation.ticker or observation.company or ""
    if error is None and observation.status in {STATUS_SUCCESS, STATUS_CANDIDATE}:
        health.record_success(observation.source, observation.operation,
                              latency_ms=int(float(observation.provenance.get("elapsed_ms") or 0)),
                              data_fetched_at=observation.retrieved_at)
    elif error is not None:
        health.record_failure(observation.source, observation.operation, error)
    get_default_tool_call_ledger().register(ToolCallAuditRecord(
        job_id=str(observation.provenance.get("job_id") or ""),
        requested_by=str(observation.provenance.get("requested_by") or ""),
        source=observation.source, operation=observation.operation, ticker=str(ticker),
        fingerprint=call_fingerprint(source=observation.source, operation=observation.operation,
                                     ticker=str(ticker), period=period),
        attempts=int(observation.provenance.get("attempts") or 1),
        started_at=str(observation.provenance.get("started_at") or observation.retrieved_at),
        finished_at=observation.retrieved_at,
        result_status=observation.status,
        error_code=(error.error_code.value if error else None),
    ))


@dataclass(frozen=True)
class SourceObservation:
    """一次外部数据观察；candidate 永远不能直接冒充 verified。"""

    source: str
    operation: str
    status: str
    retrieved_at: str
    ticker: str | None = None
    company: str | None = None
    data: Any = None
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "operation": self.operation,
            "status": self.status,
            "retrieved_at": self.retrieved_at,
            "ticker": self.ticker,
            "company": self.company,
            "data": self.data,
            "warnings": list(self.warnings),
            "provenance": dict(self.provenance),
            "error": dict(self.error) if self.error else None,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_observation(source: str, operation: str, code: SourceErrorCode, detail: Any = None) -> SourceObservation:
    error = tool_error(source, operation, code, detail=str(detail) if detail else None)
    return SourceObservation(
        source=source,
        operation=operation,
        status=STATUS_UNAVAILABLE,
        retrieved_at=_now(),
        warnings=("external_source_not_verified",),
        error=error.to_dict(),
    )


def _rows_from_response(response: Any) -> tuple[list[dict[str, Any]], str | None]:
    """兼容 BaoStock ResultData、DataFrame 和 fake client 的列表响应。"""
    if response is None:
        return [], "empty_response"
    if isinstance(response, list):
        rows = [dict(row) for row in response if isinstance(row, Mapping)]
        return rows, None if rows else "empty_response"
    if hasattr(response, "to_dict") and callable(response.to_dict):
        try:
            raw = response.to_dict(orient="records")
        except TypeError:
            raw = response.to_dict()
        if isinstance(raw, list):
            rows = [dict(row) for row in raw if isinstance(row, Mapping)]
            return rows, None if rows else "empty_response"
    if hasattr(response, "error_code") and str(getattr(response, "error_code")) not in {"0", "", "None"}:
        return [], str(getattr(response, "error_msg", "source_error"))
    fields = list(getattr(response, "fields", ()) or ())
    getter = getattr(response, "get_data", None)
    if callable(getter):
        raw_rows = getter()
        rows = []
        for row in raw_rows or []:
            if isinstance(row, Mapping):
                rows.append(dict(row))
            elif fields and isinstance(row, (list, tuple)):
                rows.append(dict(zip(fields, row)))
        return rows, None if rows else "empty_response"
    return [], "invalid_response_format"


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_query(query: str) -> str:
    normalized = str(query or "").strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("company_query_invalid")
    return normalized


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _shift_days(iso_date: str, days: int) -> str:
    from datetime import date as _date, timedelta

    try:
        parsed = _date.fromisoformat(iso_date)
    except ValueError:
        parsed = _date.today()
    return (parsed + timedelta(days=days)).isoformat()


def _stamped(observation: SourceObservation, started: float) -> SourceObservation:
    """补齐耗时与开始时间，使每次外部调用都可审计。"""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return SourceObservation(
        source=observation.source, operation=observation.operation, status=observation.status,
        retrieved_at=observation.retrieved_at, ticker=observation.ticker, company=observation.company,
        data=observation.data, warnings=observation.warnings,
        provenance={**dict(observation.provenance), "elapsed_ms": elapsed_ms,
                    "started_at": observation.provenance.get("started_at") or _now()},
        error=observation.error,
    )


class Adapter(Protocol):
    source: str

    def resolve_company(self, query: str, *, market: str | None = None) -> SourceObservation:
        ...

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> SourceObservation:
        ...

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> SourceObservation:
        ...


class _OptionalAdapter:
    source: str

    def __init__(self, client: Any = None, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_instance = client
        self._client_factory = client_factory

    def _client(self, module_name: str) -> Any:
        if self._client_instance is None:
            try:
                self._client_instance = self._client_factory() if self._client_factory else importlib.import_module(module_name)
            except (ImportError, OSError, RuntimeError) as exc:
                raise _DependencyUnavailable(module_name) from exc
        return self._client_instance


class _DependencyUnavailable(RuntimeError):
    pass


class BaoStockAdapter(_OptionalAdapter):
    """Baostock 适配器；面向 A 股身份、行情和结构化财务候选。"""

    source = SOURCE_BAOSTOCK

    @staticmethod
    def to_baostock_code(ticker: str) -> str:
        normalized = str(ticker or "").strip().upper()
        if normalized.endswith(".SH") or normalized.endswith(".SS"):
            return "sh." + normalized.split(".")[0]
        if normalized.endswith(".SZ"):
            return "sz." + normalized.split(".")[0]
        raise ValueError("baostock_only_supports_a_share")

    def _session(self) -> tuple[Any, Any | None]:
        client = self._client("baostock")
        login = getattr(client, "login", None)
        logout = getattr(client, "logout", None)
        if callable(login):
            result = login()
            code = str(getattr(result, "error_code", "0"))
            if code not in {"0", "", "None"}:
                raise RuntimeError(str(getattr(result, "error_msg", "login_failed")))
        return client, logout

    def resolve_company(self, query: str, *, market: str | None = None) -> SourceObservation:
        operation = "resolve_company"
        try:
            normalized = _normalize_query(query)
            if market and market.upper() not in {"A", "CN", "SZ", "SH", "SS"}:
                return _error_observation(self.source, operation, SourceErrorCode.UNSUPPORTED_TICKER, market)
            client, logout = self._session()
            try:
                response = client.query_stock_basic(code="", code_name=normalized)
                rows, parse_error = _rows_from_response(response)
            finally:
                if callable(logout):
                    logout()
            if parse_error:
                code = SourceErrorCode.EMPTY_RESPONSE if parse_error == "empty_response" else SourceErrorCode.PARSE_ERROR
                return _error_observation(self.source, operation, code, parse_error)
            candidates = [{"ticker": row.get("code"), "company": row.get("code_name"), "market": "A"}
                          for row in rows if row.get("code") and row.get("code_name")]
            return SourceObservation(
                source=self.source, operation=operation,
                status=STATUS_CANDIDATE if candidates else STATUS_EMPTY,
                retrieved_at=_now(), company=normalized, data=candidates,
                warnings=("candidate_not_official_annual_report",),
                provenance={"provider": "BaoStock", "endpoint": "query_stock_basic"},
            )
        except ValueError as exc:
            return _error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc)
        except _DependencyUnavailable as exc:
            return _error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc)
        except Exception as exc:  # noqa: BLE001 - 统一转为可审计来源错误
            return _error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__)

    def fetch_history(self, ticker: str, *, start_date: str, end_date: str) -> SourceObservation:
        operation = "fetch_history"
        try:
            code = self.to_baostock_code(ticker)
            client, logout = self._session()
            try:
                response = client.query_history_k_data_plus(
                    code, "date,code,open,high,low,close,volume,amount,pctChg",
                    start_date=start_date, end_date=end_date, frequency="d", adjustflag="3",
                )
                rows, parse_error = _rows_from_response(response)
            finally:
                if callable(logout):
                    logout()
            if parse_error:
                error_code = SourceErrorCode.EMPTY_RESPONSE if parse_error == "empty_response" else SourceErrorCode.PARSE_ERROR
                return _error_observation(self.source, operation, error_code, parse_error)
            return SourceObservation(
                source=self.source, operation=operation, status=STATUS_SUCCESS,
                retrieved_at=_now(), ticker=ticker.upper(), data=rows,
                warnings=("structured_market_data_not_official_annual_report",),
                provenance={"provider": "BaoStock", "endpoint": "query_history_k_data_plus",
                            "start_date": start_date, "end_date": end_date, "adjustflag": "3"},
            )
        except (ValueError, KeyError) as exc:
            return _error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc)
        except _DependencyUnavailable as exc:
            return _error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc)
        except Exception as exc:  # noqa: BLE001
            return _error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__)

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> SourceObservation:
        """A 股日线快照；结构化行情不是年报证据。"""
        started = time.monotonic()
        normalized = str(ticker or "").strip().upper()
        end = _today()
        start = _shift_days(end, -30)
        # 委托给 fetch_history 复用校验，但重贴 operation/耗时，保留真实调用语义。
        observation = self.fetch_history(normalized, start_date=start, end_date=end)
        return SourceObservation(
            source=observation.source, operation="fetch_market_snapshot", status=observation.status,
            retrieved_at=observation.retrieved_at, ticker=observation.ticker, company=observation.company,
            data=observation.data, warnings=observation.warnings,
            provenance={**dict(observation.provenance), "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "started_at": _now(), "window_days": 30},
            error=observation.error,
        )

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> SourceObservation:
        """BaoStock 季度财务指标；只作候选线索，需官方年报复核。"""
        operation = "fetch_structured_financials"
        started = time.monotonic()
        try:
            code = self.to_baostock_code(ticker)
            period = str(year or "")[:4]
            client, logout = self._session()
            try:
                response = client.query_profit_data(
                    code=code, year=int(period), quarter=4,
                ) if period else client.query_profit_data(code=code, year="", quarter="")
            finally:
                if callable(logout):
                    logout()
            rows, parse_error = _rows_from_response(response)
            if parse_error:
                if parse_error == "empty_response":
                    # 财务表为空是"该期无披露"，属于正常空结果，不是调用失败。
                    return _stamped(SourceObservation(
                        source=self.source, operation=operation, status=STATUS_EMPTY,
                        retrieved_at=_now(), ticker=str(ticker).upper(), data=[],
                        warnings=("no_disclosed_financials_for_period",),
                        provenance={"provider": "BaoStock", "endpoint": "query_profit_data",
                                    "year": period or None},
                    ), started)
                return _stamped(_error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, parse_error), started)
            if not rows:
                return _stamped(SourceObservation(
                    source=self.source, operation=operation, status=STATUS_EMPTY,
                    retrieved_at=_now(), ticker=str(ticker).upper(), data=[],
                    warnings=("structured_market_data_not_official_annual_report",),
                    provenance={"provider": "BaoStock", "endpoint": "query_profit_data",
                                "year": period or None},
                ), started)
            return _stamped(SourceObservation(
                source=self.source, operation=operation, status=STATUS_CANDIDATE,
                retrieved_at=_now(), ticker=str(ticker).upper(), data=rows,
                warnings=("candidate_not_official_annual_report",
                          "structured_market_data_not_official_annual_report"),
                provenance={"provider": "BaoStock", "endpoint": "query_profit_data",
                            "year": period or None},
            ), started)
        except (ValueError, KeyError, TypeError) as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc), started)
        except _DependencyUnavailable as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc), started)
        except Exception as exc:  # noqa: BLE001
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__), started)


class AKShareAdapter(_OptionalAdapter):
    """AKShare 适配器；优先做候选发现和多市场结构化数据，不提升证据等级。"""

    source = SOURCE_AKSHARE

    def _table(self, function_name: str, **kwargs: Any) -> Any:
        client = self._client("akshare")
        function = getattr(client, function_name, None)
        if not callable(function):
            raise AttributeError(function_name)
        return function(**kwargs)

    def resolve_company(self, query: str, *, market: str | None = None) -> SourceObservation:
        operation = "resolve_company"
        try:
            normalized = _normalize_query(query)
            target_market = (market or "A").upper()
            functions = {"A": "stock_zh_a_spot_em", "HK": "stock_hk_spot_em", "US": "stock_us_spot_em"}
            function_name = functions.get(target_market)
            if not function_name:
                return _error_observation(self.source, operation, SourceErrorCode.UNSUPPORTED_TICKER, target_market)
            rows, parse_error = _rows_from_response(self._table(function_name))
            if parse_error:
                code = SourceErrorCode.EMPTY_RESPONSE if parse_error == "empty_response" else SourceErrorCode.PARSE_ERROR
                return _error_observation(self.source, operation, code, parse_error)
            candidates = []
            for row in rows:
                name = row.get("名称") or row.get("name") or row.get("公司名称")
                code = row.get("代码") or row.get("symbol") or row.get("code")
                if name and code and normalized.casefold() in str(name).casefold():
                    candidates.append({"ticker": str(code), "company": str(name), "market": target_market})
            return SourceObservation(
                source=self.source, operation=operation,
                status=STATUS_CANDIDATE if candidates else STATUS_EMPTY,
                retrieved_at=_now(), company=normalized, data=candidates,
                warnings=("candidate_not_official_annual_report", "upstream_schema_may_change"),
                provenance={"provider": "AKShare", "endpoint": function_name},
            )
        except ValueError as exc:
            return _error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc)
        except _DependencyUnavailable as exc:
            return _error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc)
        except (AttributeError, TypeError, KeyError) as exc:
            return _error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, exc)
        except Exception as exc:  # noqa: BLE001
            return _error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__)

    def fetch_a_history(self, ticker: str, *, start_date: str, end_date: str) -> SourceObservation:
        operation = "fetch_a_history"
        try:
            normalized = str(ticker or "").strip().upper()
            if not normalized or not normalized.endswith((".SZ", ".SS", ".SH")):
                raise ValueError("akshare_a_ticker_invalid")
            rows, parse_error = _rows_from_response(self._table(
                "stock_zh_a_hist", symbol=normalized.split(".")[0], period="daily",
                start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="",
            ))
            if parse_error:
                code = SourceErrorCode.EMPTY_RESPONSE if parse_error == "empty_response" else SourceErrorCode.PARSE_ERROR
                return _error_observation(self.source, operation, code, parse_error)
            return SourceObservation(
                source=self.source, operation=operation, status=STATUS_SUCCESS,
                retrieved_at=_now(), ticker=normalized, data=rows,
                warnings=("structured_market_data_not_official_annual_report", "upstream_schema_may_change"),
                provenance={"provider": "AKShare", "endpoint": "stock_zh_a_hist", "start_date": start_date,
                            "end_date": end_date, "adjust": ""},
            )
        except ValueError as exc:
            return _error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc)
        except _DependencyUnavailable as exc:
            return _error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc)
        except (AttributeError, TypeError, KeyError) as exc:
            return _error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, exc)
        except Exception as exc:  # noqa: BLE001
            return _error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__)

    #: 各市场的行情快照接口；A 股复用 ``stock_zh_a_hist``，港股/美股用各自接口。
    _SNAPSHOT_ENDPOINTS = {
        MARKET_HK: ("stock_hk_hist", {"period": "daily", "adjust": ""}),
        MARKET_US: ("stock_us_hist", {"period": "daily", "adjust": ""}),
    }

    def _split_ticker(self, ticker: str) -> tuple[str, str]:
        """返回 ``(symbol, market)``；AKShare 接口按市场分开，这里只做规范化。"""
        normalized = str(ticker or "").strip().upper()
        if normalized.endswith(".HK"):
            return normalized.split(".")[0].zfill(5), MARKET_HK
        if normalized.endswith((".SZ", ".SS", ".SH")):
            return normalized.split(".")[0], MARKET_A
        if re.fullmatch(r"[A-Z]{1,5}", normalized):
            return normalized, MARKET_US
        raise ValueError("akshare_ticker_invalid")

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> SourceObservation:
        """多市场日线快照；结构化行情不是年报证据。"""
        operation = "fetch_market_snapshot"
        started = time.monotonic()
        try:
            symbol, detected = self._split_ticker(ticker)
            target = (market or detected).upper()
            if target not in {MARKET_A, MARKET_HK, MARKET_US}:
                return _stamped(_error_observation(self.source, operation, SourceErrorCode.UNSUPPORTED_TICKER, target), started)
            end = _today().replace("-", "")
            start = _shift_days(_today(), -30).replace("-", "")
            if target == MARKET_A:
                return _stamped(self.fetch_a_history(str(ticker).upper(), start_date=start, end_date=end), started)
            function_name, template = self._SNAPSHOT_ENDPOINTS[target]
            rows, parse_error = _rows_from_response(self._table(
                function_name, **{**template, "symbol": symbol, "start_date": start, "end_date": end},
            ))
            if parse_error:
                code = SourceErrorCode.EMPTY_RESPONSE if parse_error == "empty_response" else SourceErrorCode.PARSE_ERROR
                return _stamped(_error_observation(self.source, operation, code, parse_error), started)
            return _stamped(SourceObservation(
                source=self.source, operation=operation,
                status=STATUS_SUCCESS if rows else STATUS_EMPTY,
                retrieved_at=_now(), ticker=str(ticker).upper(), data=rows,
                warnings=("structured_market_data_not_official_annual_report", "upstream_schema_may_change"),
                provenance={"provider": "AKShare", "endpoint": function_name, "market": target,
                            "start_date": start, "end_date": end, "adjust": ""},
            ), started)
        except (ValueError, KeyError, TypeError) as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc), started)
        except _DependencyUnavailable as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc), started)
        except AttributeError as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, exc), started)
        except Exception as exc:  # noqa: BLE001
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__), started)

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> SourceObservation:
        """AKShare 财务摘要候选；字段漂移时结构化报错，不静默补零。"""
        operation = "fetch_structured_financials"
        started = time.monotonic()
        try:
            symbol, detected = self._split_ticker(ticker)
            function_name = {"A": "stock_financial_abstract",
                             MARKET_HK: "stock_financial_hk_report_em",
                             MARKET_US: "stock_financial_us_report_em"}.get(detected)
            if function_name is None:
                return _stamped(_error_observation(self.source, operation, SourceErrorCode.UNSUPPORTED_TICKER, detected), started)
            if detected == MARKET_A:
                rows, parse_error = _rows_from_response(self._table(
                    function_name, symbol=symbol, **({"date": year} if year else {}),
                ))
            else:
                rows, parse_error = _rows_from_response(self._table(function_name, symbol=symbol))
            if parse_error:
                if parse_error == "empty_response":
                    return _stamped(SourceObservation(
                        source=self.source, operation=operation, status=STATUS_EMPTY,
                        retrieved_at=_now(), ticker=str(ticker).upper(), data=[],
                        warnings=("no_disclosed_financials_for_period", "upstream_schema_may_change"),
                        provenance={"provider": "AKShare", "endpoint": function_name,
                                    "market": detected, "year": year},
                    ), started)
                return _stamped(_error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, parse_error), started)
            return _stamped(SourceObservation(
                source=self.source, operation=operation,
                status=STATUS_CANDIDATE if rows else STATUS_EMPTY,
                retrieved_at=_now(), ticker=str(ticker).upper(), data=rows,
                warnings=("candidate_not_official_annual_report",
                          "structured_market_data_not_official_annual_report",
                          "upstream_schema_may_change"),
                provenance={"provider": "AKShare", "endpoint": function_name, "market": detected,
                            "year": year},
            ), started)
        except (ValueError, KeyError, TypeError) as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.INVALID_INPUT, exc), started)
        except _DependencyUnavailable as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.DEPENDENCY_ERROR, exc), started)
        except AttributeError as exc:
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.PARSE_ERROR, exc), started)
        except Exception as exc:  # noqa: BLE001
            return _stamped(_error_observation(self.source, operation, SourceErrorCode.UNKNOWN, type(exc).__name__), started)


@dataclass
class MarketDataSourceRegistry:
    """数据源注册表；默认不实例化外部依赖，调用方可注入 fake adapter 做离线测试。"""

    adapters: dict[str, Adapter] = field(default_factory=dict)

    def register(self, adapter: Adapter) -> None:
        self.adapters[adapter.source] = adapter

    def resolve_company(self, query: str, *, market: str | None = None) -> list[SourceObservation]:
        return [adapter.resolve_company(query, market=market) for adapter in self.adapters.values()]

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> list[SourceObservation]:
        """只调用支持该市场的适配器；每个结果都记录健康与审计。"""
        return self._dispatch("fetch_market_snapshot", ticker, market=market)

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> list[SourceObservation]:
        return self._dispatch("fetch_structured_financials", ticker, year=year)

    def _dispatch(self, operation: str, ticker: str, **kwargs: Any) -> list[SourceObservation]:
        results: list[SourceObservation] = []
        for name, adapter in self.adapters.items():
            # 不支持该市场的适配器不应被调用，也不应因预期内的不支持而变为故障状态。
            requested_market = str(kwargs.get("market") or "").upper()
            if not requested_market:
                requested_market = MARKET_HK if ticker.upper().endswith(".HK") else (MARKET_US if ticker.upper().endswith(".US") else MARKET_A)
            if requested_market not in _adapter_markets(adapter):
                continue
            getter = getattr(adapter, operation, None)
            if not callable(getter):
                continue
            try:
                observation = getter(ticker, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 适配器异常也要结构化，不伪造成功
                observation = _error_observation(name, operation, SourceErrorCode.UNKNOWN, type(exc).__name__)
            if isinstance(observation, SourceObservation):
                record_observation(observation, period=str(kwargs.get("year") or "") or None)
                results.append(observation)
        return results

    def health(self) -> list[dict[str, Any]]:
        return [{"source": name, "status": "configured"} for name in sorted(self.adapters)]


def source_health_snapshot(registry: MarketDataSourceRegistry | None = None) -> list[dict[str, Any]]:
    """合并"已配置"与"真实调用健康"；从未真实检查过的来源保持 unknown，不伪造可用。"""
    registry = registry or default_registry()
    health = get_default_health_registry()
    rows: list[dict[str, Any]] = []
    for name in sorted(registry.adapters):
        snapshot = health.snapshot(name)
        rows.append({
            "source": name,
            "configured": True,
            "status": snapshot.get("status"),
            "checked_at": snapshot.get("checked_at"),
            "last_error_code": snapshot.get("last_error_code"),
            "last_error_message": snapshot.get("last_error_message"),
            "last_success_at": snapshot.get("last_success_at"),
            "last_failure_at": snapshot.get("last_failure_at"),
            "freshness": snapshot.get("freshness"),
            "markets": sorted(_adapter_markets(registry.adapters[name])),
        })
    return rows


def _adapter_markets(adapter: Any) -> frozenset[str]:
    markets = {MARKET_A} if adapter.source == SOURCE_BAOSTOCK else {MARKET_A, MARKET_HK, MARKET_US}
    declared = getattr(adapter, "markets", None)
    return frozenset(declared) if isinstance(declared, (set, frozenset)) else frozenset(markets)


def default_registry() -> MarketDataSourceRegistry:
    """构造默认注册表；演示环境可显式启用两个 MCP 数据源。"""
    import os

    registry = MarketDataSourceRegistry()
    if os.environ.get("IA_MCP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}:
        from .mcp_data_sources import MCPDataSourceAdapter, SOURCE_AKSHARE_MCP, SOURCE_BAOSTOCK_MCP

        registry.register(MCPDataSourceAdapter(SOURCE_BAOSTOCK_MCP))
        registry.register(MCPDataSourceAdapter(SOURCE_AKSHARE_MCP))
    else:
        registry.register(BaoStockAdapter())
        registry.register(AKShareAdapter())
    return registry
