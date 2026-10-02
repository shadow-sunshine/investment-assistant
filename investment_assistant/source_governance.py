"""R4 数据源/工具治理：统一错误契约、超时/重试边界、源健康、新鲜度、失败影响与审计。

设计边界（不破坏冻结区）
------------------------
* 不修改 ``rag.py`` / ``workflow.py`` / ``llm_generation.py`` / ``safety.py``；
* 不改变线上默认检索路径；本模块只提供治理契约与外层记录能力；
* 不把任意异常字符串直接返回给用户：所有用户可见 message 来自固定文案或
  显式脱敏（``sanitize_detail``），内部 detail 不含完整 URL 查询参数与凭证；
* 没有真实检查时健康状态一律为 ``unknown``，不伪造 available；
* ``requested_by`` 只是调用方标签，不是认证或授权。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

import requests

# --- 错误码枚举 ---------------------------------------------------------------


class SourceErrorCode(str, Enum):
    """稳定、可序列化的来源/工具错误码。"""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_TICKER = "unsupported_ticker"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    ANTI_BOT_OR_BLOCKED = "anti_bot_or_blocked"
    EMPTY_RESPONSE = "empty_response"
    PARSE_ERROR = "parse_error"
    VALIDATION_ERROR = "validation_error"
    DEPENDENCY_ERROR = "dependency_error"
    UNKNOWN = "unknown"


#: 只对这些明确瞬时错误重试；4xx、解析、校验、反爬拦截一律不重试。
RETRYABLE_CODES: frozenset[SourceErrorCode] = frozenset(
    {SourceErrorCode.TIMEOUT, SourceErrorCode.RATE_LIMITED, SourceErrorCode.DEPENDENCY_ERROR}
)

# 模块级别名：调用方可直接用常量比较，等价于 SourceErrorCode 成员（str 子类）。
INVALID_INPUT = SourceErrorCode.INVALID_INPUT
UNSUPPORTED_TICKER = SourceErrorCode.UNSUPPORTED_TICKER
TIMEOUT = SourceErrorCode.TIMEOUT
RATE_LIMITED = SourceErrorCode.RATE_LIMITED
HTTP_ERROR = SourceErrorCode.HTTP_ERROR
ANTI_BOT_OR_BLOCKED = SourceErrorCode.ANTI_BOT_OR_BLOCKED
EMPTY_RESPONSE = SourceErrorCode.EMPTY_RESPONSE
PARSE_ERROR = SourceErrorCode.PARSE_ERROR
VALIDATION_ERROR = SourceErrorCode.VALIDATION_ERROR
DEPENDENCY_ERROR = SourceErrorCode.DEPENDENCY_ERROR
UNKNOWN = SourceErrorCode.UNKNOWN

_DEFAULT_MESSAGES: dict[SourceErrorCode, str] = {
    SourceErrorCode.INVALID_INPUT: "输入参数不完整或不合法，无法调用该来源。",
    SourceErrorCode.UNSUPPORTED_TICKER: "该标的暂不受支持，无法路由到官方信源。",
    SourceErrorCode.TIMEOUT: "来源请求超时，已按策略停止。",
    SourceErrorCode.RATE_LIMITED: "来源触发限流（HTTP 429），需等待后重试。",
    SourceErrorCode.HTTP_ERROR: "来源返回 HTTP 错误。",
    SourceErrorCode.ANTI_BOT_OR_BLOCKED: "来源疑似被反爬拦截，已停止访问。",
    SourceErrorCode.EMPTY_RESPONSE: "来源返回空数据，未获取到可用证据。",
    SourceErrorCode.PARSE_ERROR: "来源返回内容无法解析。",
    SourceErrorCode.VALIDATION_ERROR: "来源资料未通过校验，需要人工复核。",
    SourceErrorCode.DEPENDENCY_ERROR: "上游依赖或网络暂时不可用。",
    SourceErrorCode.UNKNOWN: "来源调用发生未知错误。",
}


# --- 脱敏 ---------------------------------------------------------------------

_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|access[_-]?key|authorization|credentials?)\s*([=:])\s*([^\s;&，；]+)"
)
_QUERY_STRING_RE = re.compile(r"\?[^\s'\"<>]+")

DETAIL_MAX_LENGTH = 500


def sanitize_detail(text: Any, max_length: int = DETAIL_MAX_LENGTH) -> str:
    """把内部错误文本压成安全单行：去 URL 查询参数、遮蔽凭证、限长、不换行。"""
    if text is None:
        return ""
    value = str(text).replace("\r", " ").replace("\n", "；")
    value = _SENSITIVE_PAIR_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    value = _QUERY_STRING_RE.sub("?...", value)
    value = value.strip()
    if len(value) > max_length:
        value = value[: max_length - 3] + "..."
    return value


# --- 错误模型 ------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ToolCallError:
    """一次来源/工具调用的失败记录（稳定契约，可序列化）。

    ``message`` 面向用户（不含凭证、URL 查询参数与堆栈）；
    ``detail`` 面向内部排查（同样经过脱敏，但不承诺友好措辞）。
    """

    source: str
    operation: str
    error_code: SourceErrorCode
    retryable: bool
    attempts: int = 1
    elapsed_ms: int = 0
    checked_at: str = field(default_factory=_utc_now_iso)
    message: str = ""
    detail: str | None = None
    retry_after_ms: int | None = None
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "operation": self.operation,
            "error_code": self.error_code.value if isinstance(self.error_code, SourceErrorCode) else str(self.error_code),
            "retryable": self.retryable,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "checked_at": self.checked_at,
            "message": self.message,
            "detail": self.detail,
            "retry_after_ms": self.retry_after_ms,
            "http_status": self.http_status,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolCallError:
        try:
            code = SourceErrorCode(str(raw.get("error_code")))
        except ValueError:
            code = SourceErrorCode.UNKNOWN
        return cls(
            source=str(raw.get("source") or "unknown"),
            operation=str(raw.get("operation") or "unknown"),
            error_code=code,
            retryable=bool(raw.get("retryable")),
            attempts=int(raw.get("attempts") or 1),
            elapsed_ms=int(raw.get("elapsed_ms") or 0),
            checked_at=str(raw.get("checked_at") or _utc_now_iso()),
            message=str(raw.get("message") or ""),
            detail=raw.get("detail"),
            retry_after_ms=raw.get("retry_after_ms"),
            http_status=raw.get("http_status"),
        )


def tool_error(
    source: str,
    operation: str,
    error_code: SourceErrorCode,
    *,
    message: str | None = None,
    detail: str | None = None,
    retryable: bool | None = None,
    attempts: int = 1,
    elapsed_ms: int = 0,
    retry_after_ms: int | None = None,
    http_status: int | None = None,
    checked_at: str | None = None,
) -> ToolCallError:
    """构造 ToolCallError；retryable 默认由错误码决定，message 默认用固定安全文案。"""
    if retryable is None:
        retryable = error_code in RETRYABLE_CODES
    return ToolCallError(
        source=source,
        operation=operation,
        error_code=error_code,
        retryable=retryable,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        checked_at=checked_at or _utc_now_iso(),
        message=message or _DEFAULT_MESSAGES[error_code],
        detail=detail,
        retry_after_ms=retry_after_ms,
        http_status=http_status,
    )


class SourceCallFailure(RuntimeError):
    """来源调用按策略终止（fail-closed）时抛出，携带结构化 ``ToolCallError``。"""

    def __init__(self, error: ToolCallError) -> None:
        super().__init__(error.message or error.error_code.value)
        self.error = error


# --- HTTP / 异常分类 -------------------------------------------------------------


def classify_http_status(
    status: int,
    source: str,
    operation: str,
    *,
    attempts: int = 1,
    elapsed_ms: int = 0,
    retry_after_seconds: float | None = None,
) -> ToolCallError:
    """把 HTTP 状态码映射为稳定错误码。

    429 → ``rate_limited``（可重试，尊重 Retry-After）；5xx → ``http_error``（可重试）；
    其余 4xx/3xx → ``http_error``（不可重试）。
    """
    retry_after_ms: int | None = None
    if retry_after_seconds is not None and retry_after_seconds > 0:
        retry_after_ms = int(retry_after_seconds * 1000)
    if status == 429:
        return tool_error(
            source,
            operation,
            SourceErrorCode.RATE_LIMITED,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            retry_after_ms=retry_after_ms,
            http_status=status,
            detail=f"HTTP {status}",
        )
    if 500 <= status < 600:
        return tool_error(
            source,
            operation,
            SourceErrorCode.HTTP_ERROR,
            retryable=True,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            http_status=status,
            detail=f"HTTP {status}",
        )
    return tool_error(
        source,
        operation,
        SourceErrorCode.HTTP_ERROR,
        retryable=False,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        http_status=status,
        detail=f"HTTP {status}",
    )


def classify_http_response(
    response: Any,
    source: str,
    operation: str,
    *,
    attempts: int = 1,
    elapsed_ms: int = 0,
) -> ToolCallError | None:
    """检查响应状态码；<400 返回 None，否则返回分类结果。"""
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 400:
        return None
    raw_retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
    retry_after_seconds: float | None = None
    if raw_retry_after:
        try:
            retry_after_seconds = float(raw_retry_after)
        except (TypeError, ValueError):
            retry_after_seconds = None
    return classify_http_status(
        status,
        source,
        operation,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        retry_after_seconds=retry_after_seconds,
    )


def classify_exception(
    exc: BaseException,
    source: str,
    operation: str,
    *,
    attempts: int = 1,
    elapsed_ms: int = 0,
) -> ToolCallError:
    """把异常映射为稳定错误码；detail 一律脱敏，不含堆栈。"""
    detail = sanitize_detail(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, SourceCallFailure):
        return exc.error
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return tool_error(source, operation, SourceErrorCode.TIMEOUT, retryable=True, attempts=attempts, elapsed_ms=elapsed_ms, detail=detail)
    if isinstance(exc, (requests.exceptions.ConnectionError, OSError)):
        return tool_error(source, operation, SourceErrorCode.DEPENDENCY_ERROR, retryable=True, attempts=attempts, elapsed_ms=elapsed_ms, detail=detail)
    if isinstance(exc, requests.exceptions.RequestException):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            error = classify_http_status(status, source, operation, attempts=attempts, elapsed_ms=elapsed_ms)
            return replace(error, detail=detail)
        return tool_error(source, operation, SourceErrorCode.DEPENDENCY_ERROR, retryable=True, attempts=attempts, elapsed_ms=elapsed_ms, detail=detail)
    if isinstance(exc, ValueError):
        return tool_error(source, operation, SourceErrorCode.PARSE_ERROR, attempts=attempts, elapsed_ms=elapsed_ms, detail=detail)
    return tool_error(source, operation, SourceErrorCode.UNKNOWN, attempts=attempts, elapsed_ms=elapsed_ms, detail=detail)


_MATERIAL_ERROR_RULES: tuple[tuple[str, SourceErrorCode], ...] = (
    ("疑似被反爬拦截", SourceErrorCode.ANTI_BOT_OR_BLOCKED),
    ("校验失败", SourceErrorCode.VALIDATION_ERROR),
    ("为空或不存在", SourceErrorCode.VALIDATION_ERROR),
    ("页数为 0", SourceErrorCode.VALIDATION_ERROR),
    ("文字层", SourceErrorCode.VALIDATION_ERROR),
    ("不是有效 JSON", SourceErrorCode.PARSE_ERROR),
    ("JSONP", SourceErrorCode.PARSE_ERROR),
    ("无法识别 ticker", SourceErrorCode.UNSUPPORTED_TICKER),
    ("未找到", SourceErrorCode.EMPTY_RESPONSE),
)


def classify_material_fetch_error(
    text: str | BaseException,
    *,
    source: str,
    operation: str,
    attempts: int = 1,
    elapsed_ms: int = 0,
    message: str | None = None,
) -> ToolCallError:
    """优先保留调用方携带的结构化错误，旧文本异常再走兼容映射。"""
    structured = getattr(text, "error", None)
    if isinstance(structured, ToolCallError):
        return structured

    raw = str(text)
    for marker, code in _MATERIAL_ERROR_RULES:
        if marker in raw:
            return tool_error(
                source,
                operation,
                code,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
                message=message,
                detail=sanitize_detail(raw),
            )
    return tool_error(
        source,
        operation,
        SourceErrorCode.UNKNOWN,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        message=message,
        detail=sanitize_detail(raw),
    )


# --- 超时、重试与总预算 -----------------------------------------------------------


@dataclass(frozen=True)
class SourceCallPolicy:
    """来源调用显式策略：连接/读取超时、总时间预算、最大尝试次数与退避。"""

    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    total_budget_s: float = 90.0
    max_attempts: int = 2
    backoff_seconds: float = 1.0

    @property
    def request_timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_s, self.read_timeout_s)


def _budget_exhausted_error(
    source: str,
    operation: str,
    policy: SourceCallPolicy,
    attempts: int,
    elapsed_ms: int,
) -> ToolCallError:
    return tool_error(
        source,
        operation,
        SourceErrorCode.TIMEOUT,
        retryable=False,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        message=f"来源调用总时间预算（{policy.total_budget_s:g}s）已耗尽，为避免继续占用网络已停止。",
        detail=f"total_budget_s={policy.total_budget_s:g}; attempts={attempts}",
    )


def execute_source_call(
    attempt: Callable[[], Any],
    *,
    source: str,
    operation: str,
    policy: SourceCallPolicy,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    """按策略执行一次来源调用，fail-closed：预算耗尽或不可重试即终止，绝不吞异常伪造成功。

    * ``attempt`` 内部应自行完成状态码/内容检查，并用 :class:`SourceCallFailure`
      抛出已分类的错误；未分类异常交给 :func:`classify_exception`；
    * 只对 :data:`RETRYABLE_CODES` 中的错误重试，退避优先使用 ``retry_after_ms``；
    * 达到总预算后立即 fail-closed，不再发起任何网络请求。
    """
    started = clock()
    attempts = 0
    last_error: ToolCallError | None = None
    while True:
        elapsed = clock() - started
        if elapsed >= policy.total_budget_s:
            raise SourceCallFailure(_budget_exhausted_error(source, operation, policy, attempts, int(elapsed * 1000)))
        if attempts >= policy.max_attempts:
            raise SourceCallFailure(last_error or tool_error(source, operation, SourceErrorCode.UNKNOWN))
        attempts += 1
        try:
            return attempt()
        except SourceCallFailure as failure:
            last_error = replace(
                failure.error,
                attempts=attempts,
                elapsed_ms=int((clock() - started) * 1000),
                checked_at=_utc_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - 统一分类后按策略处理，不吞
            last_error = classify_exception(exc, source, operation, attempts=attempts, elapsed_ms=int((clock() - started) * 1000))
        if not last_error.retryable:
            raise SourceCallFailure(last_error)
        if attempts >= policy.max_attempts:
            raise SourceCallFailure(last_error)
        elapsed = clock() - started
        if elapsed >= policy.total_budget_s:
            raise SourceCallFailure(_budget_exhausted_error(source, operation, policy, attempts, int(elapsed * 1000)))
        wait = (last_error.retry_after_ms / 1000) if last_error.retry_after_ms else policy.backoff_seconds * attempts
        sleeper(max(wait, 0.0))


# --- 健康快照 -------------------------------------------------------------------

HEALTH_AVAILABLE = "available"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAVAILABLE = "unavailable"
HEALTH_UNKNOWN = "unknown"

#: 当前已知来源目录；从未真实检查过的来源以 ``unknown`` 呈现，不伪造 available。
KNOWN_SOURCES: tuple[str, ...] = (
    "SEC EDGAR",
    "巨潮资讯",
    "披露易",
    "Yahoo Finance via yfinance",
)


def health_status_for_error(error: ToolCallError) -> str:
    """可重试的瞬时失败 → degraded；硬失败（4xx/反爬/解析/校验等）→ unavailable。"""
    return HEALTH_DEGRADED if error.retryable else HEALTH_UNAVAILABLE


class SourceHealthRegistry:
    """来源健康快照登记表。线程安全；无记录时状态为 ``unknown``。"""

    def __init__(self, known_sources: tuple[str, ...] = KNOWN_SOURCES) -> None:
        self._known = known_sources
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    def _ensure(self, source: str) -> dict[str, Any]:
        if source not in self._state:
            self._state[source] = {
                "status": HEALTH_UNKNOWN,
                "checked_at": None,
                "latency_ms": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error_code": None,
                "attempts": None,
                "retry_after_ms": None,
                "data_fetched_at": None,
                "last_error_message": None,
            }
        return self._state[source]

    def record_success(
        self,
        source: str,
        operation: str,
        *,
        latency_ms: int,
        data_fetched_at: str | None = None,
        checked_at: str | None = None,
    ) -> None:
        del operation  # 操作维度在审计账本中记录；健康表按来源聚合
        with self._lock:
            state = self._ensure(source)
            state.update(
                status=HEALTH_AVAILABLE,
                checked_at=checked_at or _utc_now_iso(),
                latency_ms=latency_ms,
                last_success_at=checked_at or _utc_now_iso(),
                last_error_code=None,
                last_error_message=None,
                retry_after_ms=None,
            )
            if data_fetched_at:
                state["data_fetched_at"] = data_fetched_at

    def record_failure(self, source: str, operation: str, error: ToolCallError, *, checked_at: str | None = None) -> None:
        del operation
        with self._lock:
            state = self._ensure(source)
            state.update(
                status=health_status_for_error(error),
                checked_at=checked_at or error.checked_at,
                latency_ms=error.elapsed_ms or None,
                last_failure_at=checked_at or error.checked_at,
                last_error_code=error.error_code.value,
                last_error_message=error.message,
                attempts=error.attempts,
                retry_after_ms=error.retry_after_ms,
            )

    def snapshot(self, source: str) -> dict[str, Any]:
        with self._lock:
            state = dict(self._ensure(source))
        state["source"] = source
        state["freshness"] = freshness_state(state.get("data_fetched_at"))
        return state

    def snapshot_all(self) -> list[dict[str, Any]]:
        sources = list(self._known)
        with self._lock:
            sources.extend(name for name in self._state if name not in self._known)
        return [self.snapshot(name) for name in sources]


_default_registry: SourceHealthRegistry | None = None
_default_registry_lock = threading.Lock()


def get_default_health_registry() -> SourceHealthRegistry:
    """进程级默认健康登记表，供来源调用外层（如 fetch_materials）记录真实结果。"""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = SourceHealthRegistry()
        return _default_registry


# --- 新鲜度 ---------------------------------------------------------------------

DEFAULT_MAX_DATA_AGE_SECONDS = 7 * 24 * 3600


def freshness_state(
    data_fetched_at: str | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_DATA_AGE_SECONDS,
) -> dict[str, Any]:
    """判断资料新鲜度：``fresh`` / ``stale`` / ``unknown``（无抓取时间或时间不可解析）。"""
    max_age_seconds = float(max_age_seconds)
    if not data_fetched_at:
        return {"status": "unknown", "age_seconds": None, "max_age_seconds": max_age_seconds, "data_fetched_at": None}
    try:
        fetched = datetime.fromisoformat(str(data_fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return {"status": "unknown", "age_seconds": None, "max_age_seconds": max_age_seconds, "data_fetched_at": str(data_fetched_at)}
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    now_dt = now or datetime.now(UTC)
    age = max(0, int((now_dt - fetched).total_seconds()))
    return {
        "status": "fresh" if age <= max_age_seconds else "stale",
        "age_seconds": age,
        "max_age_seconds": max_age_seconds,
        "data_fetched_at": str(data_fetched_at),
    }


# --- 失败影响范围与可解释降级 -------------------------------------------------------


class DegradationImpact:
    SOURCE_UNAVAILABLE = "source_unavailable"
    PARTIAL_EVIDENCE = "partial_evidence"
    STALE_DATA = "stale_data"
    NEEDS_REVIEW = "needs_review"


_REVIEW_CODES: frozenset[SourceErrorCode] = frozenset({SourceErrorCode.VALIDATION_ERROR, SourceErrorCode.PARSE_ERROR})
_PARTIAL_CODES: frozenset[SourceErrorCode] = frozenset({SourceErrorCode.EMPTY_RESPONSE})


def impact_from_error(error: ToolCallError) -> str:
    """把来源失败映射为业务影响；校验/解析冲突需要人工复核，空响应算部分证据缺口。"""
    if error.error_code in _REVIEW_CODES:
        return DegradationImpact.NEEDS_REVIEW
    if error.error_code in _PARTIAL_CODES:
        return DegradationImpact.PARTIAL_EVIDENCE
    return DegradationImpact.SOURCE_UNAVAILABLE


_IMPACT_PRIORITY: tuple[tuple[str, str], ...] = (
    (DegradationImpact.NEEDS_REVIEW, "needs_review"),
    (DegradationImpact.SOURCE_UNAVAILABLE, "source_unavailable"),
    (DegradationImpact.PARTIAL_EVIDENCE, "partial_evidence"),
    (DegradationImpact.STALE_DATA, "stale_data"),
)


def build_degradation(
    errors: list[ToolCallError],
    freshness_by_source: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """汇总失败与新鲜度，输出稳定状态与 ``degradation_reasons[]``。

    不用旧缓存/另一标的/未校验内容静默替代：缺口必须显式出现在 reasons 里。
    """
    impacts: set[str] = set()
    reasons: list[str] = []
    seen_reasons: set[str] = set()
    for error in errors:
        impact = impact_from_error(error)
        impacts.add(impact)
        reason = f"{error.source} {impact}：{error.message}"
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            reasons.append(reason)
    for source, state in (freshness_by_source or {}).items():
        status = str(state.get("status") or "")
        if status == "stale":
            impacts.add(DegradationImpact.STALE_DATA)
            reason = f"{source} stale_data：资料已超过新鲜度阈值（{state.get('age_seconds')}s）。"
        elif status == "unknown":
            impacts.add(DegradationImpact.STALE_DATA)
            reason = f"{source} stale_data：资料新鲜度未知，无法确认时效。"
        else:
            continue
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            reasons.append(reason)
    status = "ok"
    for impact, label in _IMPACT_PRIORITY:
        if impact in impacts:
            status = label
            break
    return {"status": status, "degradation_reasons": reasons}


# --- 调用 fingerprint 与审计账本 ---------------------------------------------------


def normalize_fingerprint_value(value: Any) -> str:
    """规范化 fingerprint 输入：去首尾空白、casefold、压缩空白；dict 递归并按键排序。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        normalized = {str(key): normalize_fingerprint_value(item) for key, item in value.items()}
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def call_fingerprint(
    *,
    source: str,
    operation: str,
    ticker: str,
    period: str | None = None,
    extra_inputs: dict[str, Any] | None = None,
) -> str:
    """可测试的请求 fingerprint：source + operation + 标的 + 资料期间 + 规范化关键输入。"""
    payload = {
        "source": normalize_fingerprint_value(source),
        "operation": normalize_fingerprint_value(operation),
        "ticker": normalize_fingerprint_value(ticker),
        "period": normalize_fingerprint_value(period),
        "extra": {
            normalize_fingerprint_value(key): normalize_fingerprint_value(value)
            for key, value in sorted((extra_inputs or {}).items(), key=lambda item: str(item[0]))
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"


@dataclass(frozen=True)
class ToolCallAuditRecord:
    """一次工具调用的审计记录（``requested_by`` 只是标签，不是权限凭证）。"""

    job_id: str
    requested_by: str
    source: str
    operation: str
    ticker: str
    fingerprint: str
    attempts: int
    started_at: str
    finished_at: str
    result_status: str
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "requested_by": self.requested_by,
            "source": self.source,
            "operation": self.operation,
            "ticker": self.ticker,
            "fingerprint": self.fingerprint,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result_status": self.result_status,
            "error_code": self.error_code,
        }


class ToolCallLedger:
    """任务边界内的工具调用账本：同一 fingerprint 只登记一次，重复调用可被测试发现。"""

    def __init__(self) -> None:
        self._records: list[ToolCallAuditRecord] = []
        # 幂等边界是“同一任务 + 同一请求”，不能把不同 job 的合法调用互相吞掉。
        self._seen: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _dedupe_key(record: ToolCallAuditRecord) -> tuple[str, str]:
        return (str(record.job_id or ""), record.fingerprint)

    def register(self, record: ToolCallAuditRecord) -> bool:
        """登记调用；同一 job 内的 fingerprint 已存在时返回 False。"""
        key = self._dedupe_key(record)
        with self._lock:
            # 没有 job_id 时无法判断是否属于同一任务内的重复调用，保留每次真实调用记录。
            if record.job_id and key in self._seen:
                return False
            if record.job_id:
                self._seen[key] = record.job_id
            self._records.append(record)
            return True

    def has_fingerprint(self, fingerprint: str, job_id: str | None = None) -> bool:
        """查询 fingerprint；传 job_id 时限定任务边界，否则查询任意任务。"""
        with self._lock:
            if job_id is not None:
                return (str(job_id or ""), fingerprint) in self._seen
            return any(key[1] == fingerprint for key in self._seen)

    def records(self) -> tuple[ToolCallAuditRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._seen.clear()

_default_ledger: ToolCallLedger | None = None
_default_ledger_lock = threading.Lock()


def get_default_tool_call_ledger() -> ToolCallLedger:
    """返回进程级工具调用账本，供来源调用边界登记真实审计记录。"""
    global _default_ledger
    with _default_ledger_lock:
        if _default_ledger is None:
            _default_ledger = ToolCallLedger()
        return _default_ledger
