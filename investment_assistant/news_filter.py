"""Deterministic issuer-relevance filter for news evidence."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

from .config import DATA_DIR
from .source_governance import (
    RESULT_FAILED,
    SourceErrorCode,
    ToolCallAuditRecord,
    ToolCallError,
    call_fingerprint,
    get_default_tool_call_ledger,
    sanitize_detail,
    tool_error,
)

ALIAS_PATH = DATA_DIR / "ticker_aliases.json"
REJECTION_REASON = "\u6807\u9898\u4e0e\u6458\u8981\u5747\u672a\u51fa\u73b0\u4e3b\u4f53\u8bc6\u522b\u540d"


def _normalized(value: str) -> str:
    return value.casefold()


class NewsFilterError(RuntimeError):
    """本地新闻过滤配置或执行失败，携带稳定错误契约。"""

    def __init__(self, error: ToolCallError):
        super().__init__(error.message)
        self.error = error


class AuditedNewsRecords(list[dict[str, Any]]):
    """保持列表接口并把上游抓取状态传给工作流的原始新闻字段。"""

    def __init__(self, records, *, fetch_status: str | None = None, fetch_error: ToolCallError | None = None):
        super().__init__(records)
        self.fetch_status = fetch_status
        self.fetch_error = fetch_error


def _record_local_failure(operation: str, ticker: str, error: ToolCallError) -> None:
    """本地过滤错误仅写工具账本，不污染网络来源健康状态。"""
    ledger = get_default_tool_call_ledger()
    now = datetime.now(UTC).isoformat()
    ledger.register(
        ToolCallAuditRecord(
            job_id="",
            requested_by="",
            source="local_news_filter",
            operation=operation,
            ticker=ticker,
            fingerprint=call_fingerprint(source="local_news_filter", operation=operation, ticker=ticker),
            attempts=1,
            started_at=now,
            finished_at=now,
            result_status=RESULT_FAILED,
            error_code=error.error_code.value,
        )
    )


def load_ticker_aliases(path: Path = ALIAS_PATH) -> dict[str, list[str]]:
    """读取人工维护的别名表；缺失或损坏时显式失败，不伪装为空配置。"""
    operation = "load_ticker_aliases"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or any(
            not isinstance(ticker, str)
            or not ticker.strip()
            or not isinstance(aliases, list)
            or any(not isinstance(alias, str) for alias in aliases)
            for ticker, aliases in raw.items()
        ):
            raise ValueError("alias schema must map non-empty tickers to string lists")
        return {
            str(ticker).upper().strip(): [str(alias).strip() for alias in aliases if str(alias).strip()]
            for ticker, aliases in raw.items()
        }
    except OSError as exc:
        error = tool_error(
            "local_news_filter", operation, SourceErrorCode.DEPENDENCY_ERROR,
            detail=sanitize_detail(f"{type(exc).__name__}: {exc}"),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        error = tool_error(
            "local_news_filter", operation, SourceErrorCode.PARSE_ERROR,
            detail=sanitize_detail(f"{type(exc).__name__}: {exc}"),
        )
    _record_local_failure(operation, "", error)
    raise NewsFilterError(error)


def subject_aliases(ticker: str, path: Path = ALIAS_PATH) -> list[str]:
    """Return ticker, suffix-free code, and configured aliases for literal matching."""
    normalized_ticker = ticker.upper().strip()
    aliases = [normalized_ticker]
    if "." in normalized_ticker:
        aliases.append(normalized_ticker.split(".", maxsplit=1)[0])
    aliases.extend(load_ticker_aliases(path).get(normalized_ticker, []))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def filter_news_records(records: list[dict[str, Any]], ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按本地别名表过滤；本地无匹配是正常结果，不作为来源故障登记。"""
    normalized_ticker = ticker.upper().strip()
    try:
        aliases = subject_aliases(normalized_ticker)
        passed: list[dict[str, Any]] = []
        audited: list[dict[str, Any]] = []
        for record in records:
            item = dict(record)
            title = str(item.get("title") or "")
            summary = str(item.get("summary") or "")
            searchable = _normalized(f"{title}\n{summary}")
            matched_aliases = [alias for alias in aliases if _normalized(alias) in searchable]
            item["filter"] = {
                "passed": bool(matched_aliases),
                "reason": None if matched_aliases else REJECTION_REASON,
                "matched_aliases": matched_aliases,
            }
            audited.append(item)
            if matched_aliases:
                passed.append(item)
        audited_result = AuditedNewsRecords(
            audited,
            fetch_status=getattr(records, "status", None),
            fetch_error=getattr(records, "error", None),
        )
        return passed, audited_result
    except NewsFilterError:
        raise
    except Exception as exc:
        error = tool_error(
            "local_news_filter", "filter_news_records", SourceErrorCode.UNKNOWN,
            detail=sanitize_detail(f"{type(exc).__name__}: {exc}"),
        )
        _record_local_failure("filter_news_records", normalized_ticker, error)
        raise NewsFilterError(error) from exc
