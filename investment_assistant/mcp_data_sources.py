"""通过隔离进程调用两个已安装的数据源 MCP。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from .market_data_sources import (
    MARKET_A,
    MARKET_HK,
    MARKET_US,
    STATUS_CANDIDATE,
    STATUS_EMPTY,
    STATUS_SUCCESS,
    SourceObservation,
    _error_observation,
    _now,
    _stamped,
)
from .source_governance import SourceErrorCode

SOURCE_AKSHARE_MCP = "akshare_mcp"
SOURCE_BAOSTOCK_MCP = "baostock_mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_CALL_SCRIPT = PROJECT_ROOT / "scripts" / "mcp_call.py"


def _ticker_parts(ticker: str) -> tuple[str, str]:
    normalized = str(ticker or "").strip().upper()
    if normalized.endswith(".HK"):
        return normalized[:-3].zfill(5), MARKET_HK
    if normalized.endswith(".US"):
        return normalized[:-3], MARKET_US
    if re.fullmatch("[A-Z]{1,5}", normalized):
        return normalized, MARKET_US
    if normalized.endswith(".SZ") or normalized.endswith(".SS"):
        return normalized.split(".", 1)[0], MARKET_A
    return normalized, MARKET_A


def _parse_result(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text.startswith("Error:"):
        return [] if not text else {"error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _rows(value: Any) -> list[dict[str, Any]]:
    value = _parse_result(value)
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, dict):
        if isinstance(value.get("data"), list):
            return [dict(row) for row in value["data"] if isinstance(row, Mapping)]
        # AKShare MCP 将 DataFrame 序列化为按列索引的 JSON，先还原为逐条记录。
        if value and all(isinstance(column, Mapping) for column in value.values()):
            indexes = list(next(iter(value.values())))
            if all(set(column) == set(indexes) for column in value.values()):
                return [{key: column[index] for key, column in value.items()} for index in indexes]
        if "text" in value:
            text = str(value["text"])
            lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
            if len(lines) >= 3 and set(lines[1]) <= set("|:- "):
                headers = [item.strip() for item in lines[0].strip("|").split("|")]
                return [dict(zip(headers, (item.strip() for item in line.strip("|").split("|"))))
                        for line in lines[2:] if len(line.strip("|").split("|")) == len(headers)]
            return []
    return []


def _find_code(row: Mapping[str, Any]) -> str:
    for key in ("代码", "证券代码", "股票代码", "SECUCODE", "SECURITY_CODE", "symbol", "code", "ticker", "f12"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return re.sub(r"[^A-Za-z0-9.]", "", str(value)).upper()
    return ""


def _same_ticker(row: Mapping[str, Any], ticker: str) -> bool:
    wanted, market = _ticker_parts(ticker)
    code = _find_code(row)
    normalized = code.split(".", 1)[0]
    return bool(code) and (code in {wanted, f"{wanted}.{market}", ticker.upper()}
                               or normalized.lstrip("0") == wanted.lstrip("0"))


def _call(source: str, tool: str, arguments: dict[str, Any], *, timeout: float = 50.0) -> tuple[Any, dict[str, Any] | None]:
    if not MCP_CALL_SCRIPT.is_file():
        return None, {"error_code": SourceErrorCode.DEPENDENCY_ERROR.value, "message": "mcp_call.py_missing"}
    if source == SOURCE_AKSHARE_MCP:
        python = os.environ.get("IA_AKSHARE_MCP_PYTHON", "").strip()
        url = os.environ.get("IA_AKSHARE_MCP_URL", "").strip()
        if not python or not url:
            return None, {"error_code": SourceErrorCode.DEPENDENCY_ERROR.value, "message": "akshare_mcp_not_configured"}
        command = [python, str(MCP_CALL_SCRIPT), "--transport", "http", "--url", url,
                   "--tool", tool, "--arguments-json", json.dumps(arguments, ensure_ascii=False)]
    elif source == SOURCE_BAOSTOCK_MCP:
        python = os.environ.get("IA_BAOSTOCK_MCP_PYTHON", "").strip()
        if not python:
            return None, {"error_code": SourceErrorCode.DEPENDENCY_ERROR.value, "message": "baostock_mcp_not_configured"}
        command = [python, str(MCP_CALL_SCRIPT), "--transport", "stdio", "--command", python,
                   "--server-arg=-m", "--server-arg=a_share_mcp.mcp_server", "--tool", tool,
                   "--arguments-json", json.dumps(arguments, ensure_ascii=False)]
    else:
        return None, {"error_code": SourceErrorCode.INVALID_INPUT.value, "message": "unknown_mcp_source"}
    started = time.monotonic()
    try:
        # Windows 子进程默认控制台编码可能为 GBK；协议 JSON 必须端到端保持 UTF-8。
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                   check=False)
    except subprocess.TimeoutExpired:
        return None, {"error_code": SourceErrorCode.TIMEOUT.value, "message": "mcp_call_timeout"}
    except OSError as exc:
        return None, {"error_code": SourceErrorCode.DEPENDENCY_ERROR.value, "message": type(exc).__name__}
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    payload = None
    for line in reversed(lines):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None, {"error_code": SourceErrorCode.UNKNOWN.value, "message": (payload or {}).get("message") if isinstance(payload, dict) else completed.stderr[-500:]}
    result = payload.get("result")
    if isinstance(result, dict) and set(result) == {"result"}:
        result = result["result"]
    return result, {"elapsed_ms": int((time.monotonic() - started) * 1000)}


def _call_meta(meta: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if meta and "error_code" in meta:
        return meta, {}
    return None, dict(meta or {})


class MCPDataSourceAdapter:
    """MCP 结果仍是候选或行情观察，不提升为官方年报证据。"""

    def __init__(self, source: str):
        self.source = source
        self.markets = {MARKET_A, MARKET_HK, MARKET_US} if source == SOURCE_AKSHARE_MCP else {MARKET_A}

    def _observation(self, operation: str, ticker: str | None, data: Any, error: dict[str, Any] | None,
                     *, company: str | None = None, provenance: dict[str, Any] | None = None) -> SourceObservation:
        if error:
            observation = _error_observation(self.source, operation, SourceErrorCode(error.get("error_code", "unknown")), error.get("message"))
        else:
            rows = data if isinstance(data, list) else _rows(data)
            observation = SourceObservation(
                source=self.source, operation=operation,
                status=STATUS_CANDIDATE if rows else STATUS_EMPTY,
                retrieved_at=_now(), ticker=ticker, company=company,
                data=rows, warnings=("candidate_not_official_annual_report", "mcp_source_unverified"),
                provenance={"transport": "mcp", **(provenance or {})},
            )
        return observation

    def resolve_company(self, query: str, *, market: str | None = None) -> SourceObservation:
        if self.source != SOURCE_AKSHARE_MCP:
            return self._observation("resolve_company", None, [], {"error_code": SourceErrorCode.UNSUPPORTED_TICKER.value, "message": market or "A"}, company=query)
        tool = {MARKET_A: "stock_zh_a_spot_em", MARKET_HK: "stock_hk_spot_em", MARKET_US: "stock_us_spot_em"}.get(str(market or "").upper(), "stock_hk_spot_em")
        result, meta = _call(self.source, tool, {})
        error, call_meta = _call_meta(meta)
        rows = [row for row in _rows(result) if query.strip().casefold() in str(row).casefold()]
        return self._observation("resolve_company", None, rows, error, company=query, provenance={"tool": tool, "market": market, **call_meta})

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> SourceObservation:
        code, detected = _ticker_parts(ticker)
        if detected == MARKET_A and self.source == SOURCE_BAOSTOCK_MCP:
            tool, args = "get_stock_analysis", {"code": f"{'sh' if ticker.upper().endswith('.SS') else 'sz'}.{code}"}
        elif self.source == SOURCE_AKSHARE_MCP:
            tool = {MARKET_A: "stock_zh_a_spot_em", MARKET_HK: "stock_hk_spot_em", MARKET_US: "stock_us_spot_em"}[detected]
            args = {}
        else:
            return self._observation("fetch_market_snapshot", ticker, [], {"error_code": SourceErrorCode.UNSUPPORTED_TICKER.value, "message": detected})
        result, meta = _call(self.source, tool, args)
        error, call_meta = _call_meta(meta)
        rows = [row for row in _rows(result) if _same_ticker(row, ticker)] if not error else []
        return self._observation("fetch_market_snapshot", ticker, rows, error, provenance={"tool": tool, "market": detected, **call_meta})

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> SourceObservation:
        code, detected = _ticker_parts(ticker)
        if self.source == SOURCE_BAOSTOCK_MCP:
            if detected != MARKET_A:
                return self._observation("fetch_structured_financials", ticker, [], {"error_code": SourceErrorCode.UNSUPPORTED_TICKER.value, "message": detected})
            tool, args = "get_profit_data", {"code": f"{'sh' if ticker.upper().endswith('.SS') else 'sz'}.{code}", "year": year or "2025", "quarter": 4}
        elif detected == MARKET_HK:
            tool, args = "stock_financial_hk_report_em", {"stock": code, "symbol": "利润表", "indicator": "年度"}
        elif detected == MARKET_US:
            tool, args = "stock_financial_us_report_em", {"stock": code, "symbol": "income", "indicator": "报告"}
        else:
            tool, args = "stock_financial_abstract", {"symbol": code}
        result, meta = _call(self.source, tool, args)
        error, call_meta = _call_meta(meta)
        rows = _rows(result) if not error else []
        # 港股/美股财务表包含多年度记录，不允许把别的财年当成本次问答证据。
        if detected in {MARKET_HK, MARKET_US}:
            rows = [row for row in rows if not _find_code(row) or _same_ticker(row, ticker)]
            dated = [row for row in rows if row.get("REPORT_DATE")]
            if dated:
                dates = {str(row["REPORT_DATE"])[:10] for row in dated}
                selected = max((date for date in dates if date.startswith(str(year))) , default=None) if year else max(dates)
                rows = [row for row in dated if str(row["REPORT_DATE"]).startswith(selected)] if selected else []
        return self._observation("fetch_structured_financials", ticker, rows, error,
                                 provenance={"tool": tool, "market": detected, "year": year, **call_meta})
