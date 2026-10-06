"""白名单工具边界：对外暴露的每个能力都有输入/输出 schema、权限、超时与审计。

为什么先做内部边界而不是直接上 MCP
------------------------------------
当前环境没有安装 MCP SDK（``import mcp`` 失败），而任务卡明确禁止自动安装依赖。
因此这里只实现**与传输层无关**的工具函数与稳定 schema：将来接 MCP server 时，
这些函数可以直接作为 tool handler，不需要重写业务规则。

硬约束（所有工具都必须满足）
--------------------------
* 权限：``requested_by`` 只是调用方标签，授权由 :class:`Principal` 显式传入；
* 超时：每次外部调用都有显式预算，超时 fail-closed；
* 审计：成功与失败都登记 fingerprint、健康状态与错误码；
* **不允许写正式资料清单**：只有受控接入服务能写 ``onboarded_materials.json``；
* **不允许执行任意 URL**：抓取只认官方白名单主机与固定路径；
* **不允许把 candidate 提升为 verified**：证据等级只由本地官方清单决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import company_onboarding, company_qa
from .access_control import ROLE_ADMIN, ROLE_ANALYST, Principal
from .company_discovery import (
    CompanyDiscoveryInputError,
    STATUS_MATCHED,
    STATUS_ONBOARDED,
    resolve_company_candidates,
)
from .market_data_sources import (
    STATUS_CANDIDATE,
    STATUS_SUCCESS,
    MarketDataSourceRegistry,
    default_registry,
    source_health_snapshot,
)
from .source_governance import get_default_health_registry, get_default_tool_call_ledger

TOOL_DISCOVERY = "resolve_company_candidates"
TOOL_MARKET_SNAPSHOT = "get_market_snapshot"
TOOL_FINANCIAL_CANDIDATES = "get_structured_financial_candidates"
TOOL_SOURCE_HEALTH = "get_source_health"
TOOL_REQUEST_ONBOARDING = "request_official_material_onboarding"

READ_ROLES = frozenset({ROLE_ANALYST, ROLE_ADMIN})
WRITE_ROLES = frozenset({ROLE_ADMIN})

#: 工具级外部调用预算（秒）；超时即返回结构化错误，不无限等待。
TOOL_TIMEOUT_SECONDS = 45.0

ToolHandler = Callable[..., dict[str, Any]]


class ToolPermissionError(PermissionError):
    """调用方没有该工具所需的角色。"""

    error_code = "ROLE_REQUIRED"


class ToolInputError(ValueError):
    """入参不满足工具 schema。"""

    error_code = "TOOL_INPUT_INVALID"


@dataclass(frozen=True)
class ToolSpec:
    """一个白名单工具的稳定契约。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    required_roles: frozenset[str]
    writes_formal_manifest: bool = False
    timeout_seconds: float = TOOL_TIMEOUT_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "required_roles": sorted(self.required_roles),
            "writes_formal_manifest": self.writes_formal_manifest,
            "timeout_seconds": self.timeout_seconds,
        }


_CANDIDATE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "company": {"type": "string"},
            "market": {"type": "string", "enum": ["A", "HK", "US"]},
            "source": {"type": "string"},
            "confidence": {"type": "number"},
            "verified": {"type": "boolean", "description": "只有本地官方清单命中才为 true"},
            "provenance": {"type": "object"},
        },
        "required": ["ticker", "company", "market", "source", "verified"],
    },
}

_OBSERVATION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "operation": {"type": "string"},
            "status": {"type": "string"},
            "retrieved_at": {"type": "string"},
            "provenance": {"type": "object"},
            "error": {"type": ["object", "null"]},
        },
        "required": ["source", "operation", "status", "retrieved_at"],
    },
}

TOOL_SPECS: dict[str, ToolSpec] = {
    TOOL_DISCOVERY: ToolSpec(
        name=TOOL_DISCOVERY,
        description="解析公司名称或证券代码为候选标的；候选不是年报证据，不可直接回答财务数字。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 100},
                "market": {"type": ["string", "null"], "enum": ["A", "HK", "US", None]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "query": {"type": "string"},
                "selected_ticker": {"type": ["string", "null"]},
                "candidates": _CANDIDATE_SCHEMA,
                "source_observations": _OBSERVATION_SCHEMA,
                "warnings": {"type": "array", "items": {"type": "string"}},
                "error_code": {"type": ["string", "null"]},
                "evidence_state": {"type": "string"},
                "answerable": {"type": "boolean"},
            },
            "required": ["status", "query", "selected_ticker", "candidates", "evidence_state"],
        },
        required_roles=READ_ROLES,
    ),
    TOOL_MARKET_SNAPSHOT: ToolSpec(
        name=TOOL_MARKET_SNAPSHOT,
        description="获取结构化行情快照；是行情数据，不是官方年报证据。",
        input_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "minLength": 1, "maxLength": 24},
                "market": {"type": ["string", "null"], "enum": ["A", "HK", "US", None]},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "observations": {"type": "array", "items": {"type": "object"}},
                "data_available": {"type": "boolean"},
                "error_codes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "observations", "data_available"],
        },
        required_roles=READ_ROLES,
    ),
    TOOL_FINANCIAL_CANDIDATES: ToolSpec(
        name=TOOL_FINANCIAL_CANDIDATES,
        description="获取结构化财务候选数据；必须经官方年报复核后才能作为事实交付。",
        input_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "minLength": 1, "maxLength": 24},
                "year": {"type": ["string", "null"], "pattern": "^20\\d{2}$"},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "candidates": {"type": "array", "items": {"type": "object"}},
                "data_available": {"type": "boolean"},
                "requires_official_verification": {"type": "boolean"},
                "error_codes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "candidates", "requires_official_verification"],
        },
        required_roles=READ_ROLES,
    ),
    TOOL_SOURCE_HEALTH: ToolSpec(
        name=TOOL_SOURCE_HEALTH,
        description="读取来源健康快照；从未真实检查过的来源保持 unknown，不伪造可用。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"sources": {"type": "array", "items": {"type": "object"}}},
            "required": ["sources"],
        },
        required_roles=READ_ROLES,
    ),
    TOOL_REQUEST_ONBOARDING: ToolSpec(
        name=TOOL_REQUEST_ONBOARDING,
        description="登记官方资料接入申请；只写申请台账，不联网、不写正式资料清单。",
        input_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "minLength": 1, "maxLength": 24},
                "year": {"type": "string", "pattern": "^20\\d{2}$"},
                "market": {"type": ["string", "null"]},
            },
            "required": ["ticker", "year"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "year": {"type": "string"},
                "status": {"type": "string"},
                "official_channel": {"type": ["string", "null"]},
                "missing_fields": {"type": "array", "items": {"type": "string"}},
                "network_started": {"type": "boolean"},
            },
            "required": ["ticker", "year", "status", "network_started"],
        },
        required_roles=WRITE_ROLES,
    ),
}

#: 明确禁止从外部来源提升证据等级；任何工具输出都不能带 verified=True 的外部候选。
FORBIDDEN_INPUT_KEYS = frozenset({"url", "source_url", "html", "body", "path", "file_path", "endpoint"})


def _authorize(spec: ToolSpec, principal: Principal | None) -> Principal:
    if principal is None:
        raise ToolPermissionError("缺少已验证身份。")
    if not principal.has_role(*spec.required_roles):
        raise ToolPermissionError("当前身份无权执行该工具。")
    return principal


def _reject_forbidden_inputs(payload: Mapping[str, Any]) -> None:
    """阻断任意 URL / 任意路径注入；工具只接受声明过的字段。"""
    for name, value in payload.items():
        if name in FORBIDDEN_INPUT_KEYS:
            raise ToolInputError(f"field_not_allowed:{name}")
        if isinstance(value, str) and ("://" in value or value.lower().startswith("www.")):
            raise ToolInputError("url_not_accepted")


def _audit(spec: ToolSpec, principal: Principal, payload: Mapping[str, Any], result_status: str,
           error_code: str | None = None) -> None:
    """每次工具调用都登记审计记录；``requested_by`` 来自已验证身份，不是请求参数。"""
    from datetime import UTC, datetime

    from .source_governance import ToolCallAuditRecord, call_fingerprint

    now = datetime.now(UTC).isoformat()
    get_default_tool_call_ledger().register(ToolCallAuditRecord(
        job_id=str(payload.get("job_id") or ""),
        requested_by=principal.actor_id,
        source="internal_tool",
        operation=spec.name,
        ticker=str(payload.get("ticker") or payload.get("query") or ""),
        fingerprint=call_fingerprint(source="internal_tool", operation=spec.name,
                                     ticker=str(payload.get("ticker") or payload.get("query") or ""),
                                     period=str(payload.get("year") or "") or None),
        attempts=1, started_at=now, finished_at=now,
        result_status=result_status, error_code=error_code,
    ))


def resolve_company_candidates_tool(
    *, principal: Principal, query: str, market: str | None = None,
    registry: MarketDataSourceRegistry | None = None, allow_external: bool = False,
) -> dict[str, Any]:
    """公司候选解析工具。默认 ``allow_external=False``：不注入 registry 就不联网。"""
    spec = TOOL_SPECS[TOOL_DISCOVERY]
    _authorize(spec, principal)
    _reject_forbidden_inputs({"query": query, "market": market})
    try:
        result = resolve_company_candidates(
            query, market=market,
            registry=registry if allow_external else None,
            allow_external=allow_external and registry is not None,
        )
    except CompanyDiscoveryInputError as exc:
        _audit(spec, principal, {"query": str(query)}, "invalid_input", "invalid_input")
        raise ToolInputError(str(exc)) from exc
    _audit(spec, principal, {"query": str(query)}, result.status, result.error_code)
    return result.to_dict()


def get_market_snapshot_tool(
    *, principal: Principal, ticker: str, market: str | None = None,
    registry: MarketDataSourceRegistry | None = None,
) -> dict[str, Any]:
    spec = TOOL_SPECS[TOOL_MARKET_SNAPSHOT]
    _authorize(spec, principal)
    _reject_forbidden_inputs({"ticker": ticker, "market": market})
    if not str(ticker or "").strip():
        raise ToolInputError("ticker_required")
    registry = registry or default_registry()
    observations = registry.fetch_market_snapshot(str(ticker).strip(), market=market)
    errors = [dict(o.error) for o in observations if o.error]
    # 有数据即可用（行情是结构化数据，不是年报证据）；状态限定在成功/候选两类。
    available = any(o.status in {STATUS_SUCCESS, STATUS_CANDIDATE} and o.data for o in observations)
    payload = {
        "ticker": str(ticker).strip().upper(),
        "observations": [o.to_dict() for o in observations],
        "data_available": available,
        "error_codes": sorted({str(e.get("error_code")) for e in errors if e.get("error_code")}),
        "evidence_level": "market_data_not_official_annual_report",
        "answerable": False,
    }
    _audit(spec, principal, {"ticker": payload["ticker"]}, "success" if available else "unavailable",
           None if available else (payload["error_codes"][0] if payload["error_codes"] else "empty_response"))
    return payload


def get_structured_financial_candidates_tool(
    *, principal: Principal, ticker: str, year: str | None = None,
    registry: MarketDataSourceRegistry | None = None,
) -> dict[str, Any]:
    spec = TOOL_SPECS[TOOL_FINANCIAL_CANDIDATES]
    _authorize(spec, principal)
    _reject_forbidden_inputs({"ticker": ticker, "year": year})
    code = str(ticker or "").strip().upper()
    if not code:
        raise ToolInputError("ticker_required")
    if year is not None and not str(year).strip().startswith("20"):
        raise ToolInputError("year_invalid")
    registry = registry or default_registry()
    observations = registry.fetch_structured_financials(code, year=str(year) if year else None)
    errors = [dict(o.error) for o in observations if o.error]
    rows: list[dict[str, Any]] = []
    for observation in observations:
        for row in observation.data or []:
            if isinstance(row, Mapping):
                rows.append({"source": observation.source, "row": dict(row),
                             "retrieved_at": observation.retrieved_at,
                             "provenance": dict(observation.provenance)})
    payload = {
        "ticker": code,
        "candidates": rows,
        "data_available": bool(rows),
        # 结构化候选永远只是候选：必须经官方年报复核。
        "requires_official_verification": True,
        "verified": False,
        "error_codes": sorted({str(e.get("error_code")) for e in errors if e.get("error_code")}),
    }
    _audit(spec, principal, {"ticker": code, "year": year}, "candidate" if rows else "unavailable",
           None if rows else (payload["error_codes"][0] if payload["error_codes"] else "empty_response"))
    return payload


def get_source_health_tool(*, principal: Principal, registry: MarketDataSourceRegistry | None = None) -> dict[str, Any]:
    spec = TOOL_SPECS[TOOL_SOURCE_HEALTH]
    _authorize(spec, principal)
    sources = source_health_snapshot(registry)
    _audit(spec, principal, {}, "success")
    return {"sources": sources, "known_sources": get_default_health_registry().snapshot_all()}


def request_official_material_onboarding_tool(
    *, principal: Principal, ticker: str, year: str, market: str | None = None,
) -> dict[str, Any]:
    """登记接入申请（受控服务/admin）。**不写正式资料清单，也不联网。**"""
    spec = TOOL_SPECS[TOOL_REQUEST_ONBOARDING]
    _authorize(spec, principal)
    _reject_forbidden_inputs({"ticker": ticker, "year": year, "market": market})
    try:
        result = company_onboarding.request_onboarding(
            ticker, year, requested_by=principal.actor_id, market=market,
        )
    except ValueError as exc:
        _audit(spec, principal, {"ticker": str(ticker), "year": str(year)}, "invalid_input", "invalid_input")
        raise ToolInputError(str(exc)) from exc
    _audit(spec, principal, {"ticker": str(ticker), "year": str(year)}, str(result.get("status")))
    return result


TOOL_HANDLERS: Mapping[str, ToolHandler] = {
    TOOL_DISCOVERY: resolve_company_candidates_tool,
    TOOL_MARKET_SNAPSHOT: get_market_snapshot_tool,
    TOOL_FINANCIAL_CANDIDATES: get_structured_financial_candidates_tool,
    TOOL_SOURCE_HEALTH: get_source_health_tool,
    TOOL_REQUEST_ONBOARDING: request_official_material_onboarding_tool,
}


def tool_catalog() -> list[dict[str, Any]]:
    """白名单工具目录（含 schema 与权限），供 API/前端展示与将来的 MCP 暴露。"""
    return [TOOL_SPECS[name].to_dict() for name in sorted(TOOL_SPECS)]


def invoke_tool(name: str, *, principal: Principal, payload: Mapping[str, Any]) -> dict[str, Any]:
    """按名称调用白名单工具；未知工具与越权一律拒绝。"""
    handler = TOOL_HANDLERS.get(str(name))
    if handler is None:
        raise ToolInputError("unknown_tool")
    if not isinstance(payload, Mapping):
        raise ToolInputError("payload_type_invalid")
    unknown = set(payload) - set(TOOL_SPECS[str(name)].input_schema.get("properties", {}))
    if unknown:
        raise ToolInputError("unknown_fields:" + ",".join(sorted(unknown)))
    return handler(principal=principal, **dict(payload))


def coverage_report() -> list[dict[str, Any]]:
    """已收录公司的覆盖与证据状态；只读本地清单。"""
    try:
        records = company_qa.catalog()
    except company_qa.CompanySourceUnavailable:
        return []
    rows: list[dict[str, Any]] = []
    for code, record in sorted(records.items()):
        state = company_onboarding.coverage_state(code)
        rows.append({
            "ticker": code,
            "company": record.get("display_name") or code,
            "year": record.get("year"),
            "engine": record.get("engine"),
            "evidence_state": state.get("evidence_state"),
            "official_channel": state.get("official_channel"),
            "source_url": state.get("source_url"),
            "sha256": state.get("sha256"),
            "answerable": state.get("evidence_state") == "verified_field_available",
        })
    return rows


__all__ = [
    "TOOL_SPECS", "TOOL_HANDLERS", "ToolSpec", "ToolInputError", "ToolPermissionError",
    "invoke_tool", "tool_catalog", "coverage_report",
    "resolve_company_candidates_tool", "get_market_snapshot_tool",
    "get_structured_financial_candidates_tool", "get_source_health_tool",
    "request_official_material_onboarding_tool",
    "STATUS_MATCHED", "STATUS_ONBOARDED",
]
