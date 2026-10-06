"""FastAPI facade for the frozen research workflow."""

from __future__ import annotations

import json
import re
import hashlib
import uuid
import os
import threading
from functools import wraps
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
from yfinance.exceptions import YFTickerMissingError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import JOB_DIR, REPORT_DIR
from .official_document_answers import OfficialDocumentAnswerService
from .official_document_ingestion import DocumentIngestError, OfficialDocumentStore
from .official_document_sources import OfficialDocumentDiscovery
from .research_answer import ResearchAnswerService
from .bounded_general_qa import IssuerAnswerService, IndustryObservationService, BoundedSourceUnavailable
from .company_qa import CompanyAnswerService, CompanySourceUnavailable, catalog, ANCHORS_PATH, _json
from .company_discovery import (
    CompanyDiscoveryInputError,
    default_discovery_registry,
    resolve_company_candidates,
)
from .company_onboarding import pending_requests, provision_approved, request_onboarding
from .chinese_qa_service import (
    ChineseKnowledgeAnswerService, KnowledgeCorpusUnavailable,
    KnowledgeInvalidInput, KnowledgeTickerNotFound,
)
from .access_control import (
    ROLE_ADMIN, ROLE_REVIEWER, ROLE_PUBLISHER, Principal, Unauthorized,
    default_auth_service, bind_report_ownership, check_report_tenant_access,
)
from .audit_log import record_audit_event
from .chat_session import DISCOVERY_STATUS_LABELS, company_discovery_error_code
from .conversation_memory import ConversationOrchestrator, clear_mode_specific_state
from .conversation_store import (
    ConversationStore,
    ConversationStoreError,
    get_store as get_conversation_store,
)
from .user_profile import FIELD_DISPLAY as PROFILE_FIELD_DISPLAY, ProfileManager, contains_sensitive_content
from .research_tools import (
    ToolInputError,
    ToolPermissionError,
    coverage_report,
    get_market_snapshot_tool,
    get_source_health_tool,
    get_structured_financial_candidates_tool,
    request_official_material_onboarding_tool,
    resolve_company_candidates_tool,
    tool_catalog,
)
from .evidence_gates import (
    GATE_BLOCKING_RELEASES,
    RELEASE_BLOCKED,
    RELEASE_NEEDS_REVIEW,
    ReportContext,
    ReportContextError,
    claims_from_answer,
    claims_from_audit,
    evaluate_delivery,
    load_material_index,
    load_report_context,
)
from .qa import (
    ANSWERED, GENERATOR_FAILED, INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE,
    ERROR_GENERATOR_FAILED, ERROR_OUT_OF_SCOPE,
    ReportEvidenceError, answer_question, load_report_evidence,
)
from .report_jobs import STATUS_COMPLETED, STATUS_FAILED, JobService, JobStore, persist_report
from .review_publish import (
    ReviewRecord, REVIEW_APPROVED, REVIEW_REJECTED, report_binding,
    effective_review, load_review_records, save_review_record,
    load_publish_record, publish_report, withdraw_report, publish_is_effective,
)
from .research_memory import (
    create_watch_entry, list_watch_entries, get_watch_entry, delete_watch_entry,
    check_watch_entry, reset_watch_baseline, create_memory_entry, list_memory_entries,
    get_memory_entry, delete_memory_entry, update_memory_entry, memory_injectable, compare_published_reports,
)
from .source_governance import (
    ToolCallError,
    build_degradation,
    classify_material_fetch_error,
    get_default_health_registry,
    sanitize_detail,
)
from .workflow import run_research

app = FastAPI(title="Investment Assistant Demo API", version="0.1.0")

# Phase A：报告任务化。任务状态独立持久化在 data/jobs/，既有同步端点保持原样。
job_service = JobService(JobStore(JOB_DIR))
knowledge_answer_service = ChineseKnowledgeAnswerService()
issuer_answer_service = IssuerAnswerService()
industry_answer_service = IndustryObservationService()
company_answer_service = CompanyAnswerService(knowledge_answer_service, issuer_answer_service)
research_answer_service = ResearchAnswerService(company_answer_service, industry_answer_service)
official_document_answer_service = OfficialDocumentAnswerService()
job_service.recover_stale_jobs()
team_transaction_lock = threading.RLock()


def serialized_team_action(function: Any) -> Any:
    """单进程内串行化受保护版本动作，避免审核拒绝/发布/撤回交错。"""
    @wraps(function)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with team_transaction_lock:
            return function(*args, **kwargs)
    return locked


def _require_role(principal: Principal, *roles: str) -> None:
    if not principal.has_role(*roles):
        raise HTTPException(status_code=403, detail={"error_code": "ROLE_REQUIRED", "message": "当前身份无权执行该操作。"})


@app.exception_handler(ConversationStoreError)
@app.exception_handler(OSError)
@app.exception_handler(ValueError)
def storage_failure(http_request: Request, exc: Exception) -> JSONResponse:
    """存储/版本损坏时拒绝，不返回本地路径或正文。"""
    return JSONResponse(status_code=503, content={"detail": {"error_code": "STORAGE_UNAVAILABLE", "message": "本地持久化或版本状态不可用。"}})


@app.middleware("http")
async def require_api_identity(request: Request, call_next: Any) -> Any:
    """统一认证与审计；不把路径原文、token 或未发布正文写入日志。"""
    if not request.url.path.startswith("/api/") or request.url.path == "/api/health":
        return await call_next(request)
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else None
    target = hashlib.sha256(request.url.path.encode("utf-8")).hexdigest()[:24]
    category = request.url.path.split('/')[2]
    if category not in {"me", "reports", "report-jobs", "answers", "knowledge-answers", "issuer-answers", "industry-observations", "company-answers", "company-coverage", "company-material-requests", "company-discovery", "official-documents", "company-market-snapshot", "company-financial-candidates", "company-onboarding-requests", "research-answers", "source-health", "research-tools", "watchlists", "memories", "conversations", "user-profile"}:
        category = "unknown"
    action = f"{request.method} /api/{category}"
    try:
        principal = default_auth_service.authenticate(token)
    except Unauthorized as exc:
        record_audit_event(actor_id=None, tenant_id=None, action=action, target=target,
                           result="deny", error_code=exc.error_code)
        return JSONResponse(status_code=401, content={"detail": {"error_code": exc.error_code, "message": str(exc)}})
    request.state.principal = principal
    # 先记录授权意图，存储不可用时不能执行后续受保护写操作。
    record_audit_event(actor_id=principal.actor_id, tenant_id=principal.tenant_id,
                       action=action, target=target, result="intent")
    response = await call_next(request)
    record_audit_event(actor_id=principal.actor_id, tenant_id=principal.tenant_id,
                       action=action, target=target, result="allow" if response.status_code < 400 else "deny",
                       error_code=str(response.status_code) if response.status_code >= 400 else None,
                       version=getattr(request.state, "audit_version", None))
    return response


def _principal(http_request: Request) -> Principal:
    return http_request.state.principal


def _report_access(report_id: str, principal: Principal) -> dict[str, Any]:
    """旧报告无可信 tenant 时只向 admin 暴露；其他主体统一返回 404。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。"})
    target = REPORT_DIR / f"{report_id}.json"
    try:
        audit = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。"}) from None
    if not isinstance(audit, dict) or not check_report_tenant_access(audit, principal):
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。"})
    return audit


def _job_access(job_id: str, principal: Principal) -> Any:
    job = job_service.get(job_id)
    if job is None or (job.tenant_id != principal.tenant_id and not (job.tenant_id is None and principal.has_role(ROLE_ADMIN))):
        raise HTTPException(status_code=404, detail="Report job not found")
    return job


@app.get("/api/me")
def current_identity(http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    return {"actor_id": principal.actor_id, "tenant_id": principal.tenant_id, "roles": sorted(principal.roles)}


class GenerateReportRequest(BaseModel):
    ticker: str = Field(min_length=0, max_length=20)
    topic: str = Field(min_length=0, max_length=200)
    horizon: str = Field(default="\u4e2d\u671f", pattern=r"^(\u77ed\u671f|\u4e2d\u671f|\u957f\u671f)$")


class AnswerRequest(BaseModel):
    report_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=0, max_length=500)
    requested_by: str = Field(default="anonymous", min_length=0, max_length=80)
    # 仅用于调用方自带上下文的交叉校验，不构成身份认证。
    ticker: str | None = Field(default=None, min_length=0, max_length=20)
    memory_ids: list[str] = Field(default_factory=list, max_length=10)


class KnowledgeAnswerRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=500)


class CompanyMaterialRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    year: str = Field(pattern=r"^20\d{2}$")


class OfficialDocumentSearchRequest(BaseModel):
    ticker: str = Field(min_length=2, max_length=20)
    question: str = Field(min_length=1, max_length=500)


class OfficialDocumentDiscoveryRequest(BaseModel):
    source: str = Field(min_length=3, max_length=10)
    ticker: str = Field(min_length=2, max_length=20)
    year: str = Field(pattern=r"^20\d{2}$")
    limit: int = Field(default=10, ge=1, le=50)
    company_name: str | None = Field(default=None, max_length=100)


class OfficialDocumentIngestRequest(OfficialDocumentDiscoveryRequest):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{24}$")


class CompanyAnswerRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=500)


class IssuerAnswerRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=500)


class ResearchAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    ticker: str | None = Field(default=None, max_length=20)


class IndustryObservationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class CreateJobRequest(BaseModel):
    ticker: str = Field(min_length=0, max_length=20)
    topic: str = Field(min_length=0, max_length=200)
    horizon: str = Field(default="\u4e2d\u671f", pattern=r"^(\u77ed\u671f|\u4e2d\u671f|\u957f\u671f)$")
    requested_by: str = Field(default="anonymous", min_length=0, max_length=80)


def _ticker_exists(ticker: str) -> bool:
    """仅用于入口有效性判断，不复用或替代研究工作流。"""
    history = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True, timeout=10, raise_errors=True)
    return history is not None and not history.empty


def _normalize_request(ticker: str, topic: str) -> tuple[str, str]:
    """纯函数校验（不触网络），供异步任务入口使用，保证提交立即返回。"""
    normalized_ticker = ticker.upper().strip()
    normalized_topic = topic.strip()
    if not normalized_topic:
        raise HTTPException(status_code=422, detail="研究主题不能为空，请检查输入。")
    if not normalized_ticker or not re.fullmatch(r"[A-Z0-9.^=-]+", normalized_ticker):
        raise HTTPException(status_code=422, detail=f"未找到股票代码 {normalized_ticker or ticker.strip()}，请检查输入")
    return normalized_ticker, normalized_topic


def _validate_generate_request(ticker: str, topic: str) -> tuple[str, str]:
    """在调用冻结工作流前拦截无效输入，避免生成报告或触发检索。"""
    normalized_ticker, normalized_topic = _normalize_request(ticker, topic)
    try:
        exists = _ticker_exists(normalized_ticker)
    except YFTickerMissingError:
        exists = False
    except Exception:
        # 上游暂时不可达时不把可能有效的代码误判为无效；工作流会保留既有的数据失败披露。
        return normalized_ticker, normalized_topic
    if not exists:
        raise HTTPException(status_code=422, detail=f"未找到股票代码 {normalized_ticker}，请检查输入")
    return normalized_ticker, normalized_topic


def _report_id_from_path(path: Path) -> str:
    return path.stem


def _mode_info(audit: dict[str, Any]) -> dict[str, str]:
    llm_result = audit.get("llm_result") or {}
    if llm_result.get("used"):
        return {"label": "\u53d7\u63a7 LLM \u7248", "reason": "\u69fd\u4f4d\u3001\u5f15\u7528\u3001\u6570\u5b57\u6eaf\u6e90\u4e0e safety \u6821\u9a8c\u901a\u8fc7\u3002"}
    return {"label": "\u89c4\u5219\u7248", "reason": str(llm_result.get("reason") or "\u672a\u4f7f\u7528 LLM \u6216\u6821\u9a8c\u5931\u8d25\u3002")}


def _report_summary(report_path: Path) -> dict[str, Any]:
    audit_path = report_path.with_suffix(".json")
    audit: dict[str, Any] = {}
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    return {
        "id": _report_id_from_path(report_path),
        "ticker": audit.get("ticker") or report_path.stem.split("_")[0],
        "topic": audit.get("topic") or "",
        "horizon": audit.get("horizon") or "",
        "created_at": audit.get("created_at") or datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(),
        "mode": _mode_info(audit),
        "safety_passed": (audit.get("evaluation") or {}).get("passed"),
    }


def _load_report_payload(report_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。"})
    report_path = REPORT_DIR / f"{report_id}.md"
    audit_path = REPORT_DIR / f"{report_id}.json"
    if not report_path.exists() or not audit_path.exists():
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。"})
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes.decode("utf-8"))
    md_bytes = report_path.read_bytes()
    return {
        "_read_json_sha256": hashlib.sha256(audit_bytes).hexdigest(),
        "_read_md_sha256": hashlib.sha256(md_bytes).hexdigest(),
        **_report_summary(report_path),
        "report": md_bytes.decode("utf-8"),
        "tenant_id": audit.get("tenant_id"),
        "actor_id": audit.get("actor_id"),
        "market_snapshot": audit.get("market_snapshot") or {},
        "financial_snapshot": audit.get("financial_snapshot") or {},
        "sources": audit.get("sources") or [],
        "risk_flags": audit.get("risk_flags") or [],
        "evaluation": audit.get("evaluation") or {},
        "retrieval_evaluation": audit.get("retrieval_evaluation") or {},
        "mode": _mode_info(audit),
    }


def _safe_delivery_for_rejection(delivery: dict[str, Any]) -> dict[str, Any]:
    """拒绝交付时只返回状态与错误码；原 claim 文本、片段和值均不外发。"""
    safe = dict(delivery)
    claim_results = []
    for item in delivery.get("claim_results") or []:
        if not isinstance(item, dict):
            continue
        errors = [str(error).split(":", 1)[0] for error in item.get("validation_errors") or []]
        claim_results.append({"support_status": item.get("support_status"), "validation_error_codes": errors})
    safe["claim_results"] = claim_results
    if claim_results:
        error_codes = sorted({code for item in claim_results for code in item["validation_error_codes"]})
        safe["blocking_reasons"] = [
            "关键结论未通过证据校验：" + "、".join(error_codes)
            if error_codes else "整份报告尚无完整关键结论覆盖和人工审核记录。"
        ]
    return safe


def _load_report(report_id: str, principal: Principal | None = None) -> dict[str, Any]:
    """只返回通过门禁的报告；partial 必须显式携带 delivery，待审/阻断不返回正文。"""
    payload = _load_report_payload(report_id)
    try:
        delivery = _report_delivery(report_id)
    except ReportContextError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "REPORT_EVIDENCE_INVALID", "message": "报告审计 JSON 无法读取。"}) from exc
    try:
        audit = _report_access(report_id, principal) if principal else json.loads((REPORT_DIR / f"{report_id}.json").read_text(encoding="utf-8"))
        published, reason = _effective_publication(report_id, audit)
    except (ValueError, OSError, ReportContextError):
        published, reason = None, "version_drift"
    if published is not None and (payload["_read_md_sha256"] != published.binding.get("report_md_sha256") or
                                  payload["_read_json_sha256"] != published.binding.get("report_json_sha256")):
        published, reason = None, "version_drift"
    payload.pop("_read_md_sha256", None)
    payload.pop("_read_json_sha256", None)
    if published is not None:
        delivery["release_status"] = "released"
        delivery["blocking_reasons"] = []
    payload["delivery"] = delivery
    payload["publish_status"] = "published" if published else ("withdrawn" if reason == "withdrawn" else "not_published")
    if published is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "EVIDENCE_GATE_BLOCKED",
                "message": "报告尚未通过证据交付门禁，正文暂不可作为已验证结果交付。",
                "delivery": _safe_delivery_for_rejection(delivery),
            },
        )
    return payload


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _job_degradation(job: Any) -> dict[str, Any]:
    """只根据当前任务持久化的错误构造降级响应，不读全局健康状态。"""
    errors: list[ToolCallError] = []
    for raw in getattr(job, "source_errors", []) or []:
        if isinstance(raw, dict):
            try:
                errors.append(ToolCallError.from_dict(raw))
            except (TypeError, ValueError):
                continue
    raw_structured = getattr(job, "tool_error", None)
    if isinstance(raw_structured, dict):
        errors.append(ToolCallError.from_dict(raw_structured))
    elif job.status == STATUS_FAILED and job.error:
        classified = classify_material_fetch_error(job.error, source="research_job", operation="research_job")
        errors.append(dataclass_replace(classified, message=sanitize_detail(job.error)))
    # completed job 也总是提供同一稳定结构；无来源故障时为 ok + 空 reasons。
    return build_degradation(errors)


@app.get("/api/source-health")
def source_health(http_request: Request) -> dict[str, Any]:
    """只读来源健康快照；没有真实检查记录的来源一律为 unknown，不伪造健康。"""
    _require_role(_principal(http_request), ROLE_ADMIN)
    return {"sources": get_default_health_registry().snapshot_all()}


_ANSWER_GATE_REASONS = {
    INSUFFICIENT_EVIDENCE: "回答所需证据不足，未返回未经验证的内容。",
    OUT_OF_SCOPE: "问题超出当前报告的证据边界，未做回答。",
    GENERATOR_FAILED: "回答生成失败，未返回未经验证的内容。",
}


def _answer_delivery(result: Any, context: Any) -> dict[str, Any]:
    """把问答结果接入 R5 交付门禁：answered 走 claim 校验，其余状态按 blocked 交付。"""
    if result.status == ANSWERED:
        gate_context = ReportContext(
            report_id=context.report_id,
            ticker=context.ticker,
            audit=context.audit,
            material_index=load_material_index(),
        )
        return evaluate_delivery(claims_from_answer(result, gate_context), gate_context)
    return {
        "release_status": RELEASE_BLOCKED,
        "claim_results": [],
        "blocking_reasons": [_ANSWER_GATE_REASONS.get(result.status, "回答未通过交付门禁。")],
        "degradation_reasons": list(result.limitations or []),
        "report_version": result.report_id,
        "ticker": result.ticker,
        "claims_total": 0,
    }


def _report_delivery(report_id: str) -> dict[str, Any]:
    """R5 无完整 claim 覆盖证明：逐条校验可展示，但不能据此放行整份报告。"""
    context = load_report_context(REPORT_DIR, report_id)
    delivery = evaluate_delivery(claims_from_audit(context.audit, context.report_id), context)
    if delivery["release_status"] in {"released", "partial"}:
        delivery["release_status"] = RELEASE_NEEDS_REVIEW
        delivery["blocking_reasons"].append("当前报告尚无完整关键结论覆盖及人工审核记录，不能将逐条锚点通过等同整份报告可发布。")
    return delivery


def _current_review_state(report_id: str, audit: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """每次审核/发布/读取重新验证当前报告、证据和资料实际字节。"""
    context = load_report_context(REPORT_DIR, report_id)
    index = load_material_index()
    binding = report_binding(REPORT_DIR, report_id, audit=audit, material_index=index)
    binding.update({"tenant_id": audit.get("tenant_id"), "report_id": report_id})
    gate = evaluate_delivery(claims_from_audit(audit, report_id), context)
    if (audit.get("evaluation") or {}).get("passed") is False:
        gate["release_status"] = "blocked"
        gate["blocking_reasons"].append("报告风险校验未通过。")
    review = effective_review(report_id, binding)
    return binding, gate, review


def _effective_publication(report_id: str, audit: dict[str, Any]) -> tuple[Any, str | None]:
    binding, gate, review = _current_review_state(report_id, audit)
    if gate["release_status"] != "released" or review is None or review.tenant_id != audit.get("tenant_id"):
        return None, "review_required"
    ids = [item["claim_id"] for item in gate["claim_results"]]
    if not ids or len(set(ids)) != len(ids) or review.claim_decisions != dict.fromkeys(ids, "supported"):
        return None, "coverage_required"
    if review.reviewer_id == audit.get("actor_id"):
        return None, "separation_of_duties"
    record, reason = publish_is_effective(REPORT_DIR, report_id, audit=audit, material_index=load_material_index())
    if record is None or record.review_id != review.review_id or record.tenant_id != audit.get("tenant_id"):
        return None, reason or "review_changed"
    if record.publisher_id in {review.reviewer_id, audit.get("actor_id")}:
        return None, "separation_of_duties"
    return record, None


def _review_access(report_id: str, principal: Principal) -> dict[str, Any]:
    audit = _report_access(report_id, principal)
    if not principal.has_role(ROLE_REVIEWER, ROLE_ADMIN) or principal.actor_id == audit.get("actor_id"):
        raise HTTPException(status_code=403, detail={"error_code": "ROLE_REQUIRED", "message": "必须由独立审核人操作。"})
    if not audit.get("tenant_id") or not audit.get("actor_id"):
        raise HTTPException(status_code=409, detail={"error_code": "MIGRATION_PENDING", "message": "旧报告待可信归属迁移。"})
    return audit


def _publish_access(report_id: str, principal: Principal) -> dict[str, Any]:
    audit = _report_access(report_id, principal)
    if not principal.has_role(ROLE_PUBLISHER, ROLE_ADMIN) or principal.actor_id == audit.get("actor_id"):
        raise HTTPException(status_code=403, detail={"error_code": "ROLE_REQUIRED", "message": "必须由独立发布人操作。"})
    if not audit.get("tenant_id") or not audit.get("actor_id"):
        raise HTTPException(status_code=409, detail={"error_code": "MIGRATION_PENDING", "message": "旧报告待可信归属迁移。"})
    return audit


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    reason: str = Field(min_length=5, max_length=1000)
    claim_decisions: dict[str, str] = Field(default_factory=dict)
    coverage_attested: bool = False
    expected_md_sha256: str = Field(min_length=64, max_length=64)
    expected_json_sha256: str = Field(min_length=64, max_length=64)


@app.get("/api/reports/{report_id}/review")
def review_preview(report_id: str, http_request: Request) -> dict[str, Any]:
    audit = _review_access(report_id, _principal(http_request))
    try:
        binding, gate, review = _current_review_state(report_id, audit)
    except (ValueError, OSError, ReportContextError) as exc:
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_INVALID", "message": "报告或资料版本不可核验。"}) from exc
    return {"report_id": report_id, "report": (REPORT_DIR / f"{report_id}.md").read_text(encoding="utf-8"),
            "claims": audit.get("claims") or [], "binding": binding, "gate": gate,
            "evidence": {"market_snapshot": audit.get("market_snapshot") or {},
                         "financial_snapshot": audit.get("financial_snapshot") or {}, "sources": audit.get("sources") or []},
            "review_status": report_release_status(report_id, http_request)["review_status"]}


class ClaimSetRequest(BaseModel):
    claims: list[dict[str, Any]] = Field(min_length=1, max_length=80)
    expected_md_sha256: str = Field(min_length=64, max_length=64)
    expected_json_sha256: str = Field(min_length=64, max_length=64)


@app.put("/api/reports/{report_id}/claims")
@serialized_team_action
def author_claim_set(report_id: str, request: ClaimSetRequest, http_request: Request) -> dict[str, Any]:
    """人工补齐 claims，原文不变；审计字节变更使所有旧审核/发布自动失效。"""
    audit = _review_access(report_id, _principal(http_request))
    try:
        binding, _, _ = _current_review_state(report_id, audit)
    except (ValueError, OSError, ReportContextError) as exc:
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_INVALID", "message": "当前版本不可核验。"}) from exc
    if request.expected_md_sha256 != binding["report_md_sha256"] or request.expected_json_sha256 != binding["report_json_sha256"]:
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_DRIFT", "message": "版本已变化，请重新预览。"})
    ids = [item.get("claim_id") for item in request.claims]
    if not all(isinstance(item, str) and item.strip() for item in ids) or len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail={"error_code": "CLAIM_SET_INVALID", "message": "claim_id 必须唯一且非空。"})
    candidate = {**audit, "claims": request.claims}
    context = load_report_context(REPORT_DIR, report_id)
    context = ReportContext(report_id=context.report_id, ticker=context.ticker, audit=candidate, material_index=load_material_index())
    gate = evaluate_delivery(claims_from_audit(candidate, report_id), context)
    if gate["release_status"] != "released":
        raise HTTPException(status_code=422, detail={"error_code": "EVIDENCE_GATE_BLOCKED", "message": "人工 claim 集尚未通过结构证据校验。", "delivery": _safe_delivery_for_rejection(gate)})
    candidate["claims_authored_by"] = _principal(http_request).actor_id
    candidate["claims_authored_at"] = datetime.now(UTC).isoformat()
    target = REPORT_DIR / f"{report_id}.json"
    temporary = target.with_suffix(".review.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(candidate, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    return {"report_id": report_id, "claims_total": len(request.claims), "review_status": "pending", "publish_status": "not_published"}


@app.post("/api/reports/{report_id}/review")
@serialized_team_action
def review_report(report_id: str, request: ReviewRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    audit = _review_access(report_id, principal)
    try:
        binding, gate, _ = _current_review_state(report_id, audit)
    except (ValueError, OSError, ReportContextError) as exc:
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_INVALID", "message": "报告或资料版本不可核验。"}) from exc
    if (request.expected_md_sha256 != binding["report_md_sha256"] or
            request.expected_json_sha256 != binding["report_json_sha256"]):
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_DRIFT", "message": "报告版本已变化，请重新预览。"})
    claim_ids = [str(item.get("claim_id") or "") for item in gate["claim_results"]]
    if request.decision == REVIEW_APPROVED:
        if gate["release_status"] != "released" or not claim_ids or len(set(claim_ids)) != len(claim_ids):
            raise HTTPException(status_code=422, detail={"error_code": "EVIDENCE_GATE_BLOCKED", "message": "证据门禁未通过或 claim 集不完整。"})
        if not request.coverage_attested or request.claim_decisions != dict.fromkeys(claim_ids, "supported"):
            raise HTTPException(status_code=422, detail={"error_code": "COVERAGE_REQUIRED", "message": "必须逐条确认并人工声明已覆盖整份报告关键结论。"})
    record = save_review_record(ReviewRecord(
        review_id=uuid.uuid4().hex, report_id=report_id, tenant_id=principal.tenant_id,
        reviewer_id=principal.actor_id, decision=request.decision, reason=request.reason,
        reviewed_at=datetime.now().astimezone().isoformat(), binding=binding,
        claim_decisions=request.claim_decisions, coverage_attested=request.coverage_attested,
    ))
    http_request.state.audit_version = binding["report_md_sha256"]
    return {"review_id": record.review_id, "review_status": record.decision, "binding": binding}


@app.post("/api/reports/{report_id}/publish")
@serialized_team_action
def publish_reviewed_report(report_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    audit = _publish_access(report_id, principal)
    try:
        binding, gate, review = _current_review_state(report_id, audit)
    except (ValueError, OSError, ReportContextError) as exc:
        raise HTTPException(status_code=409, detail={"error_code": "VERSION_INVALID", "message": "报告或资料版本不可核验。"}) from exc
    if gate["release_status"] != "released" or review is None or review.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=409, detail={"error_code": "REVIEW_REQUIRED", "message": "当前版本未通过完整独立审核与证据门禁。"})
    existing = load_publish_record(report_id)
    if existing is not None and existing.withdrawn_at and existing.review_id == review.review_id:
        raise HTTPException(status_code=409, detail={"error_code": "RE_REVIEW_REQUIRED", "message": "撤回后必须重新审核，不能重放旧批准。"})
    if review.reviewer_id in {principal.actor_id, audit.get("actor_id")}:
        raise HTTPException(status_code=403, detail={"error_code": "SEPARATION_OF_DUTIES", "message": "创建、审核、发布必须是独立人员。"})
    record, created = publish_report(report_id=report_id, tenant_id=principal.tenant_id,
                                     publisher_id=principal.actor_id, binding=binding, review_id=review.review_id)
    http_request.state.audit_version = binding["report_md_sha256"]
    return {"publish_status": record.status, "created": created, "review_id": record.review_id}


@app.post("/api/reports/{report_id}/withdraw")
@serialized_team_action
def withdraw_reviewed_report(report_id: str, http_request: Request) -> dict[str, Any]:
    _publish_access(report_id, _principal(http_request))
    record = withdraw_report(report_id, publisher_id=_principal(http_request).actor_id)
    if record is None:
        raise HTTPException(status_code=409, detail={"error_code": "NOT_PUBLISHED", "message": "报告尚未发布。"})
    return {"publish_status": record.status}


@app.get("/api/reports/{report_id}/delivery")
def report_delivery(report_id: str, http_request: Request) -> dict[str, Any]:
    """只读交付门禁结果：released / partial / blocked / needs_review，绝不把待审伪装成正常交付。"""
    audit = _report_access(report_id, _principal(http_request))
    try:
        delivery = _report_delivery(report_id)
        try:
            published, _ = _effective_publication(report_id, audit)
        except (ValueError, OSError, ReportContextError):
            published = None
        if published is not None:
            delivery["release_status"] = "released"
            delivery["blocking_reasons"] = []
        if published is None:
            return JSONResponse(status_code=409, content=_safe_delivery_for_rejection(delivery))
        return delivery
    except ReportContextError:
        raise HTTPException(status_code=422, detail={"error_code": "REPORT_EVIDENCE_INVALID", "message": "报告审计 JSON 无法读取。"})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。", "report_id": report_id})


@app.post("/api/reports")
def generate_report(request: GenerateReportRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    ticker, topic = _validate_generate_request(request.ticker, request.topic)
    try:
        # Reuse the frozen workflow as the only research implementation.
        result = run_research(ticker=ticker, topic=topic, horizon=request.horizon, report_mode="auto")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Research workflow failed: {type(exc).__name__}: {exc}") from exc

    bind_report_ownership(result, principal)
    report_id = persist_report(result, REPORT_DIR)
    return _load_report(report_id, principal)


@app.get("/api/reports")
def list_reports(http_request: Request, limit: int = 30) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    reports = sorted(REPORT_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    principal = _principal(http_request)
    visible = []
    for path in reports:
        try:
            _report_access(path.stem, principal)
        except HTTPException:
            continue
        visible.append(_report_summary(path))
        if len(visible) >= bounded_limit:
            break
    return visible


@app.get("/api/reports/{report_id}")
@serialized_team_action
def get_report(report_id: str, http_request: Request) -> dict[str, Any]:
    _report_access(report_id, _principal(http_request))
    return _load_report(report_id, _principal(http_request))


@app.get("/api/reports/{report_id}/status")
@serialized_team_action
def report_release_status(report_id: str, http_request: Request) -> dict[str, Any]:
    audit = _report_access(report_id, _principal(http_request))
    try:
        binding, _, review = _current_review_state(report_id, audit)
        records = load_review_records(report_id)
        latest = next((item for item in reversed(records) if item.binding == binding), None)
        review_status = review.decision if review else (latest.decision if latest else "needs_review")
        record, reason = _effective_publication(report_id, audit)
    except (ValueError, OSError, ReportContextError):
        record, reason, review_status = None, "version_invalid", "needs_review"
    return {"report_id": report_id, "publish_status": "published" if record else
            ("withdrawn" if reason == "withdrawn" else "not_published"),
            "review_status": review_status, "reason": reason}


class WatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    file_names: list[str] = Field(default_factory=list, max_length=30)
    report_ids: list[str] = Field(default_factory=list, max_length=20)


def _published_binding(report_id: str, principal: Principal) -> dict[str, Any]:
    audit = _report_access(report_id, principal)
    try:
        record, _ = _effective_publication(report_id, audit)
    except (ValueError, OSError, ReportContextError):
        record = None
    if record is None:
        raise HTTPException(status_code=409, detail={"error_code": "REPORT_NOT_PUBLISHED", "message": "报告没有有效发布版本。"})
    return {**record.binding, "review_id": record.review_id, "published_at": record.published_at}


def _watch_report_bindings(entry: Any, principal: Principal) -> dict[str, dict[str, Any]]:
    current = {}
    for report_id in entry.report_baseline:
        try:
            current[report_id] = _published_binding(report_id, principal)
        except HTTPException:
            continue
    return current


@app.post("/api/watchlists", status_code=201)
def add_watch(request: WatchRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    bindings = {report_id: _published_binding(report_id, principal) for report_id in dict.fromkeys(request.report_ids)}
    try:
        entry, _ = create_watch_entry(tenant_id=principal.tenant_id, owner_id=principal.actor_id,
                                      name=request.name, file_names=request.file_names,
                                      material_index=load_material_index(), report_bindings=bindings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "WATCH_INVALID", "message": str(exc)}) from exc
    return entry.to_dict()


@app.get("/api/watchlists")
def watches(http_request: Request) -> list[dict[str, Any]]:
    principal = _principal(http_request)
    return [entry.to_dict() for entry in list_watch_entries(tenant_id=principal.tenant_id, owner_id=principal.actor_id)]


def _watch_access(watch_id: str, principal: Principal) -> Any:
    entry = get_watch_entry(watch_id, tenant_id=principal.tenant_id)
    if entry is None or entry.owner_id != principal.actor_id:
        raise HTTPException(status_code=404, detail={"error_code": "WATCH_NOT_FOUND", "message": "关注项不存在。"})
    return entry


@app.post("/api/watchlists/{watch_id}/check")
def check_watch(watch_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    entry = _watch_access(watch_id, principal)
    return check_watch_entry(entry, load_material_index(), _watch_report_bindings(entry, principal))


@app.post("/api/watchlists/{watch_id}/baseline")
@serialized_team_action
def confirm_watch_baseline(watch_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    entry = _watch_access(watch_id, principal)
    current = _watch_report_bindings(entry, principal)
    if len(current) != len(entry.report_baseline):
        raise HTTPException(status_code=409, detail={"error_code": "REPORT_UNAVAILABLE", "message": "报告版本不可用。"})
    try:
        updated = reset_watch_baseline(watch_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
                                       material_index=load_material_index(), report_bindings=current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error_code": "SOURCE_UNAVAILABLE", "message": str(exc)}) from exc
    return updated.to_dict()


@app.delete("/api/watchlists/{watch_id}")
def remove_watch(watch_id: str, http_request: Request) -> dict[str, bool]:
    principal = _principal(http_request)
    _watch_access(watch_id, principal)
    return {"deleted": delete_watch_entry(watch_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)}


class MemoryRequest(BaseModel):
    report_id: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=1000)
    ttl_days: int = Field(ge=1, le=90)


@app.post("/api/memories", status_code=201)
@serialized_team_action
def add_memory(request: MemoryRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    audit = _report_access(request.report_id, principal)
    binding = _published_binding(request.report_id, principal)
    entry = create_memory_entry(tenant_id=principal.tenant_id, owner_id=principal.actor_id,
                                report_id=request.report_id, ticker=str(audit.get("ticker") or ""),
                                content=request.content, expires_at=(datetime.now(UTC) + timedelta(days=request.ttl_days)).isoformat(),
                                binding=binding)
    return entry.to_dict()


def _memory_access(memory_id: str, principal: Principal) -> Any:
    entry = get_memory_entry(memory_id, tenant_id=principal.tenant_id)
    if entry is None or entry.owner_id != principal.actor_id:
        raise HTTPException(status_code=404, detail={"error_code": "MEMORY_NOT_FOUND", "message": "记忆不存在。"})
    return entry


def _memory_status(entry: Any, principal: Principal) -> str:
    try:
        current = _published_binding(entry.report_id, principal)
        effective = True
    except HTTPException:
        current, effective = None, False
    return memory_injectable(entry, publish_effective=effective, current_binding=current)[1]


@app.get("/api/memories")
@serialized_team_action
def memories(http_request: Request) -> list[dict[str, Any]]:
    principal = _principal(http_request)
    visible = []
    for entry in list_memory_entries(tenant_id=principal.tenant_id, owner_id=principal.actor_id):
        status = _memory_status(entry, principal)
        row = {**entry.to_dict(), "status": status}
        if status != "active":
            row.pop("content", None)
        visible.append(row)
    return visible


@app.get("/api/memories/{memory_id}")
@serialized_team_action
def get_memory(memory_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    entry = _memory_access(memory_id, principal)
    status = _memory_status(entry, principal)
    if status != "active":
        raise HTTPException(status_code=409, detail={"error_code": "MEMORY_INACTIVE", "message": status})
    return {**entry.to_dict(), "status": status}


@app.put("/api/memories/{memory_id}")
@serialized_team_action
def revise_memory(memory_id: str, request: MemoryRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    entry = _memory_access(memory_id, principal)
    if request.report_id != entry.report_id or _memory_status(entry, principal) != "active":
        raise HTTPException(status_code=409, detail={"error_code": "MEMORY_INACTIVE", "message": "不可更新失效记忆或替换关联报告。"})
    binding = _published_binding(entry.report_id, principal)
    updated = update_memory_entry(memory_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
                                   content=request.content, expires_at=(datetime.now(UTC) + timedelta(days=request.ttl_days)).isoformat(),
                                   binding=binding)
    return updated.to_dict()


@app.delete("/api/memories/{memory_id}")
def remove_memory(memory_id: str, http_request: Request) -> dict[str, bool]:
    principal = _principal(http_request)
    _memory_access(memory_id, principal)
    return {"deleted": delete_memory_entry(memory_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)}


class CompareRequest(BaseModel):
    left_report_id: str
    right_report_id: str


@app.post("/api/reports/compare")
@serialized_team_action
def compare_reports(request: CompareRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _published_binding(request.left_report_id, principal)
    _published_binding(request.right_report_id, principal)
    left = _report_access(request.left_report_id, principal)
    right = _report_access(request.right_report_id, principal)
    return compare_published_reports(left, right)


# --- Phase A：异步报告任务 ---------------------------------------------------


@app.post("/api/report-jobs", status_code=202)
def create_report_job(request: CreateJobRequest, http_request: Request) -> dict[str, Any]:
    """提交报告任务，立即返回 job_id（不做网络校验，保证快速返回）。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    ticker, topic = _normalize_request(request.ticker, request.topic)
    job, created = job_service.create(
        ticker=ticker,
        topic=topic,
        horizon=request.horizon,
        requested_by=principal.actor_id,
        tenant_id=principal.tenant_id,
        actor_id=principal.actor_id,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "相同参数的报告任务正在执行中，已返回既有任务。",
                "job_id": job.job_id,
                "status": job.status,
            },
        )
    return {"job_id": job.job_id, "status": job.status, "message": "报告任务已提交"}


@app.post("/api/answers")
@serialized_team_action
def answer_report_question(request: AnswerRequest, http_request: Request) -> dict[str, Any]:
    """只针对一个已落盘报告回答问题，不联网、不检索新证据。"""
    principal = _principal(http_request)
    audit = _report_access(request.report_id, principal)
    try:
        published, _ = _effective_publication(request.report_id, audit)
    except (ValueError, OSError, ReportContextError):
        published = None
    if published is None:
        raise HTTPException(status_code=409, detail={"error_code": "REPORT_NOT_PUBLISHED", "message": "报告未经有效发布，不能用于问答。"})
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail={"error_code": "QUESTION_EMPTY", "message": "问题不能为空。"})

    try:
        context = load_report_evidence(REPORT_DIR, request.report_id)
    except FileNotFoundError:
        # 若 report_id 实际对应仍在执行的任务，返回稳定的任务未完成错误。
        job = job_service.get(request.report_id)
        if job is not None and job.status != STATUS_COMPLETED:
            raise HTTPException(
                status_code=409,
                detail={"error_code": "REPORT_NOT_COMPLETED", "message": "报告任务尚未完成，暂不能提问。", "job_id": request.report_id, "status": job.status},
            )
        raise HTTPException(status_code=404, detail={"error_code": "REPORT_NOT_FOUND", "message": "指定报告不存在。", "report_id": request.report_id})
    except ReportEvidenceError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "REPORT_EVIDENCE_INVALID", "message": str(exc)}) from exc

    if request.ticker and request.ticker.strip().upper() != context.ticker:
        raise HTTPException(
            status_code=403,
            detail={"error_code": "TICKER_MISMATCH", "message": "请求 ticker 与报告 ticker 不一致。", "report_ticker": context.ticker},
        )

    # 用户记忆是偏好/注记，不是可用于证明事实的证据，禁止注入生成器替代来源。
    active_memory_ids = []
    for memory_id in dict.fromkeys(request.memory_ids):
        entry = _memory_access(memory_id, principal)
        if entry.report_id != request.report_id or _memory_status(entry, principal) != "active":
            raise HTTPException(status_code=409, detail={"error_code": "MEMORY_INACTIVE", "message": "记忆与报告不匹配、过期或版本已失效。"})
        active_memory_ids.append(memory_id)
    result = answer_question(context, question, principal.actor_id)
    payload = result.to_dict()
    payload["memory_context"] = {"active_ids": active_memory_ids, "mode": "user_note_not_factual_evidence"}
    payload["access_control"] = {"mode": "server_token", "tenant_id": principal.tenant_id}
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    # R5：问答响应附带交付门禁状态；已回答但门禁阻断/待审时不得按正常成功放行。
    delivery = _answer_delivery(result, context)
    try:
        latest_audit = _report_access(request.report_id, principal)
        latest_publication, _ = _effective_publication(request.report_id, latest_audit)
    except (ValueError, OSError, ReportContextError):
        latest_publication = None
    if latest_publication is None:
        raise HTTPException(status_code=409, detail={"error_code": "REPORT_NOT_PUBLISHED", "message": "回答期间报告或资料版本发生变化，结果未交付。"})
    payload["delivery"] = delivery
    if result.status == OUT_OF_SCOPE:
        return JSONResponse(status_code=422, content=payload)
    if result.status == GENERATOR_FAILED:
        return JSONResponse(status_code=503, content=payload)
    if delivery["release_status"] in GATE_BLOCKING_RELEASES:
        payload["error_code"] = payload.get("error_code") or "EVIDENCE_GATE_BLOCKED"
        payload["status"] = "needs_review" if delivery["release_status"] == RELEASE_NEEDS_REVIEW else INSUFFICIENT_EVIDENCE
        payload["answer"] = "该回答未通过证据交付门禁，系统未返回未经验证的结论。"
        payload["claims"] = []
        payload["evidence_refs"] = []
        payload["delivery"] = _safe_delivery_for_rejection(delivery)
        return JSONResponse(status_code=422, content=payload)
    return payload


@app.post("/api/company-material-requests")
@serialized_team_action
def request_company_material(request: CompanyMaterialRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        result = provision_approved(request.ticker, request.year)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "MATERIAL_REQUEST_INVALID", "message": "市场代码或年度无效。"}) from exc
    except CompanySourceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": "MATERIAL_ONBOARDING_FAILED", "message": "预审来源或下载校验未通过。"}) from exc
    return {**result, "actor_id": principal.actor_id, "tenant_id": principal.tenant_id}


@app.get("/api/company-coverage")
def company_coverage(http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        records = catalog()
        anchored = _json(ANCHORS_PATH)
    except CompanySourceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": "COMPANY_CATALOG_UNAVAILABLE", "message": "公司资料清单不可用。"}) from exc
    return {"companies": [{"ticker": code, "year": row["year"], "answer_scope": "2025年中文年报已核验字段" if row["engine"] == "chinese" else "已核验收入/证券事实" if row["engine"] == "issuer" else "仅限已审定证据锚点" if code in anchored else "资料已登记，字段待核验", "company": next((alias for alias in row["aliases"] if any("\u4e00" <= c <= "\u9fff" for c in alias)), code)} for code, row in sorted(records.items())]}


class CompanyDiscoveryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    market: str | None = Field(default=None, max_length=8)
    # 显式开启才允许调用外部候选源；默认只读本地清单与路由提示，不联网。
    use_external_sources: bool = False


@app.post("/api/company-discovery")
def discover_company(request: CompanyDiscoveryRequest, http_request: Request) -> dict[str, Any]:
    """解析公司候选。普通 analyst 即可查询；不写正式资料清单，不提升证据等级。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = resolve_company_candidates_tool(
            principal=principal, query=request.query, market=request.market,
            registry=default_discovery_registry() if request.use_external_sources else None,
            allow_external=request.use_external_sources,
        )
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail={"error_code": exc.error_code, "message": "当前身份无权查询公司候选。"}) from exc
    except (ToolInputError, CompanyDiscoveryInputError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "COMPANY_DISCOVERY_INPUT_INVALID", "message": "公司查询条件无效。"}) from exc
    payload["status_label"] = DISCOVERY_STATUS_LABELS.get(str(payload.get("status")), str(payload.get("status")))
    payload["actor_id"] = principal.actor_id
    payload["tenant_id"] = principal.tenant_id
    return payload


class MarketSnapshotRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)
    market: str | None = Field(default=None, max_length=8)


@app.post("/api/company-market-snapshot")
def company_market_snapshot(request: MarketSnapshotRequest, http_request: Request) -> dict[str, Any]:
    """结构化行情快照；返回体显式标注不是官方年报证据，不能当财务事实。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = get_market_snapshot_tool(
            principal=principal, ticker=request.ticker, market=request.market,
        )
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail={"error_code": exc.error_code, "message": "当前身份无权查询行情快照。"}) from exc
    except ToolInputError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "MARKET_SNAPSHOT_INPUT_INVALID", "message": "证券代码无效。"}) from exc
    payload["actor_id"] = principal.actor_id
    return payload


class FinancialCandidatesRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)
    year: str | None = Field(default=None, pattern=r"^20\d{2}$")


@app.post("/api/company-financial-candidates")
def company_financial_candidates(request: FinancialCandidatesRequest, http_request: Request) -> dict[str, Any]:
    """结构化财务候选；``verified`` 恒为 false，必须经官方年报复核。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = get_structured_financial_candidates_tool(
            principal=principal, ticker=request.ticker, year=request.year,
        )
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail={"error_code": exc.error_code, "message": "当前身份无权查询财务候选。"}) from exc
    except ToolInputError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "FINANCIAL_CANDIDATES_INPUT_INVALID", "message": "证券代码或年度无效。"}) from exc
    payload["actor_id"] = principal.actor_id
    return payload


class OnboardingRequestBody(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)
    year: str = Field(pattern=r"^20\d{2}$")
    market: str | None = Field(default=None, max_length=8)


@app.get("/api/company-onboarding-requests")
def list_onboarding_requests(http_request: Request) -> dict[str, Any]:
    """待人工审批的接入申请台账（只读）。"""
    _require_role(_principal(http_request), ROLE_ADMIN)
    return {"requests": pending_requests()}


@app.post("/api/company-onboarding-requests", status_code=201)
def create_onboarding_request(request: OnboardingRequestBody, http_request: Request) -> dict[str, Any]:
    """登记官方资料接入申请；**不联网、不写正式资料清单**。"""
    principal = _principal(http_request)
    _require_role(principal, ROLE_ADMIN)
    try:
        payload = request_official_material_onboarding_tool(
            principal=principal, ticker=request.ticker, year=request.year, market=request.market,
        )
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail={"error_code": exc.error_code, "message": "只有管理员可登记接入申请。"}) from exc
    except (ToolInputError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "ONBOARDING_REQUEST_INVALID", "message": "证券代码或年度无效。"}) from exc
    payload["actor_id"] = principal.actor_id
    payload["tenant_id"] = principal.tenant_id
    return payload


@app.get("/api/research-tools")
def research_tool_catalog(http_request: Request) -> dict[str, Any]:
    """白名单工具目录：输入/输出 schema、所需角色与硬约束。"""
    _require_role(_principal(http_request), "analyst", ROLE_ADMIN)
    return {"tools": tool_catalog(), "mcp_exposed": False,
            "mcp_deferred_reason": "当前环境未安装 MCP SDK；任务卡禁止自动安装依赖，故只交付内部工具边界。"}


@app.get("/api/company-evidence-state")
def company_evidence_state(http_request: Request, question: str = "") -> dict[str, Any]:
    """把问句映射为稳定的公司发现错误码，供 UI 显示"已识别/已核验/待接入/来源不可用"。"""
    _require_role(_principal(http_request), "analyst", ROLE_ADMIN)
    payload: dict[str, Any] = {"companies": coverage_report()}
    if question.strip():
        payload["question"] = question.strip()[:200]
        payload["error_code"] = company_discovery_error_code(question.strip()[:200])
    return payload


@app.get("/api/official-documents")
def list_official_documents(http_request: Request, ticker: str | None = None) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        records = OfficialDocumentStore().list_documents(ticker=ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "OFFICIAL_SEARCH_INPUT_INVALID", "message": "检索输入无效。"}) from exc
    except (DocumentIngestError, CompanySourceUnavailable) as exc:
        raise HTTPException(status_code=503, detail={"error_code": "OFFICIAL_INDEX_UNAVAILABLE", "message": "官方资料索引或公司目录不可用。"}) from exc
    return {"documents": records, "count": len(records)}


@app.post("/api/official-documents/search")
def search_official_document(request: OfficialDocumentSearchRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        return official_document_answer_service.answer(request.ticker, request.question, requested_by=principal.actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "OFFICIAL_SEARCH_INPUT_INVALID", "message": "检索输入无效。"}) from exc
    except (DocumentIngestError, CompanySourceUnavailable) as exc:
        raise HTTPException(status_code=503, detail={"error_code": "OFFICIAL_INDEX_UNAVAILABLE", "message": "官方资料索引或公司目录不可用。"}) from exc


@app.post("/api/official-documents/discover")
def discover_official_document(request: OfficialDocumentDiscoveryRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, ROLE_ADMIN)
    try:
        candidates = OfficialDocumentDiscovery().discover(
            request.source, request.ticker, year=request.year, limit=request.limit, company_name=request.company_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "OFFICIAL_DISCOVERY_INPUT_INVALID", "message": str(exc)}) from exc
    except DocumentIngestError as exc:
        raise HTTPException(status_code=503, detail={"error_code": "OFFICIAL_DISCOVERY_UNAVAILABLE", "message": str(exc)}) from exc
    return {"candidates": candidates, "count": len(candidates), "state": "discovered_unverified"}


@app.post("/api/official-documents/ingest")
@serialized_team_action
def ingest_official_document(request: OfficialDocumentIngestRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, ROLE_ADMIN)
    try:
        return OfficialDocumentDiscovery().download_and_ingest(
            source=request.source, ticker=request.ticker, year=request.year,
            candidate_id=request.candidate_id, company_name=request.company_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "OFFICIAL_INGEST_INPUT_INVALID", "message": str(exc)}) from exc
    except DocumentIngestError as exc:
        raise HTTPException(status_code=503, detail={"error_code": "OFFICIAL_INGEST_FAILED", "message": str(exc)}) from exc


@app.post("/api/company-answers")
def answer_company_question(request: CompanyAnswerRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = company_answer_service.answer(request.ticker, request.question, requested_by=principal.actor_id)
    except (ValueError, KnowledgeInvalidInput) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "COMPANY_INPUT_INVALID", "message": "公司问答输入无效。"}) from exc
    except (CompanySourceUnavailable, KnowledgeCorpusUnavailable, BoundedSourceUnavailable) as exc:
        raise HTTPException(status_code=503, detail={"error_code": "COMPANY_SOURCE_UNAVAILABLE", "message": "官方资料缺失或证据版本不匹配。"}) from exc
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    return payload


@app.post("/api/knowledge-answers")
@serialized_team_action
def answer_knowledge_question(request: KnowledgeAnswerRequest, http_request: Request) -> dict[str, Any]:
    """认证后仅在所选标的的官方中文年报中检索，无法核验时拒答。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = knowledge_answer_service.answer(request.ticker, request.question, requested_by=principal.actor_id)
    except KnowledgeInvalidInput as exc:
        raise HTTPException(status_code=422, detail={"error_code": "KNOWLEDGE_INPUT_INVALID", "message": str(exc)}) from exc
    except KnowledgeTickerNotFound as exc:
        raise HTTPException(status_code=404, detail={"error_code": "CHINESE_MATERIAL_NOT_FOUND", "message": "当前标的没有可用的中文官方年报。"}) from exc
    except KnowledgeCorpusUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": "KNOWLEDGE_CORPUS_UNAVAILABLE", "message": "中文年报语料或本地模型不可用。"}) from exc
    payload["access_control"] = {"mode": "server_token", "tenant_id": principal.tenant_id}
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    return payload


@app.post("/api/issuer-answers")
def answer_issuer_question(request: IssuerAnswerRequest, http_request: Request) -> dict[str, Any]:
    """仅由服务端身份授权；官方资料不完整或漂移时拒绝交付。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = issuer_answer_service.answer(request.ticker, request.question, requested_by=principal.actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "ISSUER_INPUT_INVALID", "message": str(exc)}) from exc
    except BoundedSourceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": "ISSUER_SOURCE_UNAVAILABLE", "message": "官方公司资料缺失或证据版本不匹配。"}) from exc
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    return payload


@app.post("/api/industry-observations")
def answer_industry_question(request: IndustryObservationRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = industry_answer_service.answer(request.question, requested_by=principal.actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "INDUSTRY_INPUT_INVALID", "message": str(exc)}) from exc
    except BoundedSourceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": "INDUSTRY_SOURCE_UNAVAILABLE", "message": "官方行业资料缺失、漂移或已过期。"}) from exc
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    return payload


@app.post("/api/research-answers")
def answer_research_question(request: ResearchAnswerRequest, http_request: Request) -> dict[str, Any]:
    """编排已核验子服务；客户端不能指定证据等级或伪造身份。"""
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    try:
        payload = research_answer_service.answer(request.question, ticker=request.ticker,
                                                 requested_by=principal.actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "RESEARCH_INPUT_INVALID", "message": str(exc)}) from exc
    except (CompanySourceUnavailable, KnowledgeCorpusUnavailable, BoundedSourceUnavailable) as exc:
        raise HTTPException(status_code=503, detail={"error_code": "RESEARCH_SOURCE_UNAVAILABLE", "message": "核验资料不可用或发生版本漂移。"}) from exc
    payload["tenant_id"] = principal.tenant_id
    payload["actor_id"] = principal.actor_id
    return payload

@app.get("/api/report-jobs")
def list_report_jobs(http_request: Request, limit: int = 30) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    principal = _principal(http_request)
    return [job.to_dict() for job in job_service.list(limit=500) if job.tenant_id == principal.tenant_id][:bounded_limit]


@app.get("/api/report-jobs/{job_id}")
def get_report_job(job_id: str, http_request: Request) -> dict[str, Any]:
    job = _job_access(job_id, _principal(http_request))
    payload = job.to_dict()
    degradation = _job_degradation(job)
    payload["degradation"] = degradation
    payload["degradation_reasons"] = degradation["degradation_reasons"]
    # R5：完成任务附带其报告的交付门禁状态；审计数据不可读时显式 needs_review，不伪装正常。
    if job.status == STATUS_COMPLETED and job.report_id:
        try:
            _report_access(job.report_id, _principal(http_request))
            delivery = _report_delivery(job.report_id)
            payload["delivery"] = _safe_delivery_for_rejection(delivery) if delivery["release_status"] in GATE_BLOCKING_RELEASES else delivery
        except (ReportContextError, FileNotFoundError):
            payload["delivery"] = {
                "release_status": RELEASE_NEEDS_REVIEW,
                "claim_results": [],
                "blocking_reasons": ["报告审计数据不可读，无法完成交付门禁核验。"],
                "degradation_reasons": [],
                "report_version": job.report_id,
                "ticker": job.ticker,
                "claims_total": 0,
            }
    return payload


@app.get("/api/report-jobs/{job_id}/report")
@serialized_team_action
def get_report_job_report(job_id: str, http_request: Request) -> dict[str, Any]:
    """只有任务完成才返回报告；未完成返回 409，绝不返回半成品。"""
    principal = _principal(http_request)
    job = _job_access(job_id, principal)
    if job.status != STATUS_COMPLETED or not job.report_id:
        raise HTTPException(
            status_code=409,
            detail={"message": "任务尚未完成，暂无报告。", "job_id": job.job_id, "status": job.status},
        )
    _report_access(job.report_id, principal)
    return _load_report(job.report_id, principal)


@app.post("/api/report-jobs/{job_id}/cancel")
def cancel_report_job(job_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    existing = _job_access(job_id, principal)
    if existing.actor_id != principal.actor_id and not principal.has_role(ROLE_ADMIN):
        raise HTTPException(status_code=403, detail={"error_code": "OWNER_REQUIRED", "message": "只有发起人或管理员可取消任务。"})
    job = job_service.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    return job.to_dict()


# --- 会话列表、短期记忆与用户画像 -------------------------------------------------
#
# 归属边界：客户端**不得**提交可信 owner_id/tenant_id。全部从服务端 Principal 推导，
# 因此知道别人的 conversation_id / message_id / candidate_id 也读不到、改不了、删不掉。
# 归属不匹配一律 404，不区分"不存在"与"不属于你"，避免泄露资源存在性。


def _conversation_store() -> ConversationStore:
    return get_conversation_store()


def _profile_manager() -> ProfileManager:
    return ProfileManager(_conversation_store())


def _orchestrator() -> ConversationOrchestrator:
    return ConversationOrchestrator(_conversation_store())


def _conversation_or_404(conversation_id: str, principal: Principal) -> Any:
    conversation = _conversation_store().get_conversation(
        conversation_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    if conversation is None:
        raise HTTPException(status_code=404,
                            detail={"error_code": "CONVERSATION_NOT_FOUND", "message": "指定会话不存在。"})
    return conversation


class CreateConversationRequest(BaseModel):
    title: str = Field(default="", max_length=120)
    answer_mode: str = Field(default="official", pattern=r"^(official|mcp)$")


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AnswerModeRequest(BaseModel):
    answer_mode: str = Field(pattern=r"^(official|mcp)$")


class ConversationTurnRequest(BaseModel):
    """一轮对话。``request_id`` 由客户端生成，用于防重复提交。"""

    text: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=8, max_length=80)
    answer_mode: str | None = Field(default=None, pattern=r"^(official|mcp)$")
    horizon: str = Field(default="中期", pattern=r"^(短期|中期|长期)$")


class ProfileUpdateRequest(BaseModel):
    """改值请求体。

    **故意不含 ``field``**：字段名只从路径参数取。若在 body 里声明同名字段，
    FastAPI 会把它当必填项，客户端只传 value 时直接 422。
    """

    value: str = Field(min_length=1, max_length=200)


class ProfileCandidateRequest(BaseModel):
    """确认请求体。

    **故意不含``candidate_id``**：候选编号只从路径参数取。
    若在 body 里声明同名字段，FastAPI 会把它当必填项，客户端漏传就变成 422，
    看起来像"确认接口坏了"，实际是参数归属混淆。
    """

    version: int | None = Field(default=None, ge=1)


@app.post("/api/conversations", status_code=201)
def create_conversation(request: CreateConversationRequest, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    conversation = _conversation_store().create_conversation(
        tenant_id=principal.tenant_id, owner_id=principal.actor_id,
        title=request.title, answer_mode=request.answer_mode)
    return conversation.to_dict()


@app.get("/api/conversations")
def list_conversations(http_request: Request, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """按更新时间倒序返回**本人**会话；分页用于"加载更多"。"""
    principal = _principal(http_request)
    store = _conversation_store()
    items = store.list_conversations(tenant_id=principal.tenant_id, owner_id=principal.actor_id,
                                     limit=limit, offset=offset)
    total = store.count_conversations(tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    return {"conversations": [item.to_dict() for item in items],
            "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    conversation = _conversation_or_404(conversation_id, principal)
    store = _conversation_store()
    state = store.load_state(conversation_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    summary = store.load_summary(conversation_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    payload = conversation.to_dict()
    # state里含 messages 镜像，这里只回结构化状态，避免同一份消息出现两次。
    payload["state"] = {key: value for key, value in ((state or {}).get("state") or {}).items()
                        if key != "messages"}
    payload["state_version"] = (state or {}).get("version", 0)
    payload["summary"] = summary
    return payload


@app.get("/api/conversations/{conversation_id}/messages")
def get_conversation_messages(conversation_id: str, http_request: Request,
                              limit: int | None = None, since_seq: int = 0) -> dict[str, Any]:
    """恢复历史仅供查看：旧报告撤回/证据漂移后这里仍返回原文，但**不冒充当前有效官方事实**。"""
    principal = _principal(http_request)
    _conversation_or_404(conversation_id, principal)
    messages = _conversation_store().list_messages(
        conversation_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
        limit=limit, since_seq=since_seq)
    return {"conversation_id": conversation_id,
            "messages": [item.to_dict() for item in messages],
            "count": len(messages),
            "history_notice": "历史消息按原文展示，不代表其中的财务结论仍为当前已核验的官方事实。"}


@app.post("/api/conversations/{conversation_id}/rename")
def rename_conversation(conversation_id: str, request: RenameConversationRequest,
                        http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    updated = _conversation_store().rename_conversation(
        conversation_id, request.title, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    if updated is None:
        raise HTTPException(status_code=404,
                            detail={"error_code": "CONVERSATION_NOT_FOUND", "message": "指定会话不存在。"})
    return updated.to_dict()


@app.post("/api/conversations/{conversation_id}/answer-mode")
def set_conversation_answer_mode(conversation_id: str, request: AnswerModeRequest,
                                 http_request: Request) -> dict[str, Any]:
    """切换数据模式**不删消息**；只清不适用证据状态由编排层处理。"""
    principal = _principal(http_request)
    _conversation_or_404(conversation_id, principal)
    store = _conversation_store()
    store.set_answer_mode(conversation_id, request.answer_mode, tenant_id=principal.tenant_id,
                          owner_id=principal.actor_id)
    orchestrator = _orchestrator()
    context = orchestrator.load_context(conversation_id, tenant_id=principal.tenant_id,
                                       owner_id=principal.actor_id)
    updated_context = clear_mode_specific_state(context, request.answer_mode)
    orchestrator.persist(updated_context, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    payload = store.get_conversation(conversation_id, tenant_id=principal.tenant_id,
                                     owner_id=principal.actor_id).to_dict()
    payload["messages_retained"] = True
    payload["note"] = "已切换数据模式，历史消息保留；本轮回答会标明本次模式。"
    return payload


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, http_request: Request) -> dict[str, Any]:
    """单事务删除消息、状态、摘要与本会话待确认项；长期画像不随之删除。"""
    principal = _principal(http_request)
    deleted = _conversation_store().delete_conversation(
        conversation_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    if not deleted:
        raise HTTPException(status_code=404,
                            detail={"error_code": "CONVERSATION_NOT_FOUND", "message": "指定会话不存在。"})
    return {"deleted": True, "conversation_id": conversation_id,
            "note": "会话消息、状态、摘要与本会话待确认项已删除；长期画像保留。"}


@app.post("/api/conversations/{conversation_id}/turns")
def post_conversation_turn(conversation_id: str, request: ConversationTurnRequest,
                           http_request: Request) -> dict[str, Any]:
    """一轮真实对话：意图/补充项 → 合并状态 → 有效查询 → 受控能力 → 持久化。

    这里复用既有受控服务（``/api/company-answers`` 等的同一批service 对象），
    **不绕过证据边界**让模型凭记忆生成财务数字。
    """
    principal = _principal(http_request)
    _require_role(principal, "analyst", ROLE_ADMIN)
    conversation = _conversation_or_404(conversation_id, principal)
    orchestrator = _orchestrator()
    effective_mode = request.answer_mode or conversation.answer_mode

    def dispatch(context: dict[str, Any], effective_question: str, **kwargs: Any) -> dict[str, Any]:
        return _dispatch_conversation_turn(context, effective_question, principal=principal, **kwargs)

    try:
        result = orchestrator.handle_turn(
            conversation_id, request.text, tenant_id=principal.tenant_id,
            owner_id=principal.actor_id, request_id=request.request_id, dispatch=dispatch,
            requested_by=principal.actor_id, answer_mode=request.answer_mode,
            horizon=request.horizon,
            extra_dispatch_kwargs={
                "knowledge_ask": _conversation_company_ask_factory(principal),
                "company_ask": _conversation_company_ask_factory(principal),
                "issuer_ask": lambda payload: issuer_answer_service.answer(
                    payload["ticker"], payload["question"], requested_by=payload.get("requested_by", principal.actor_id)),
                "industry_ask": lambda payload: industry_answer_service.answer(
                    payload["question"], requested_by=payload.get("requested_by", principal.actor_id)),
                "document_ask": lambda payload: official_document_answer_service.inventory(payload["ticker"], payload["question"], requested_by=principal.actor_id),
                "research_ask": lambda payload: research_answer_service.answer(
                    payload["question"], requested_by=payload.get("requested_by", principal.actor_id),
                    ticker=payload.get("ticker")),
                "candidate_ask": _conversation_candidate_ask_factory(principal),
                "market_snapshot_ask": _conversation_market_snapshot_factory(principal),
                "create_job": _conversation_create_job_factory(principal),
                "ask": _conversation_report_ask_factory(principal),
                "company_discover": (lambda query, market=None: _conversation_discover(
                    query, market, allow_external=(effective_mode == "mcp"))),
            })
    except ConversationStoreError:
        raise HTTPException(status_code=503,
                            detail={"error_code": "CONVERSATION_STORE_UNAVAILABLE",
                                    "message": "会话存储暂时不可用，请稍后重试。"})
    if result.get("status") == "conversation_not_found":
        raise HTTPException(status_code=404,
                            detail={"error_code": "CONVERSATION_NOT_FOUND", "message": "指定会话不存在。"})
    return result


def _conversation_market_snapshot_factory(principal: Principal) -> Any:
    """实时/最新行情统一走结构化来源；结果仍标注为第三方候选。"""
    def ask(payload: dict[str, Any]) -> dict[str, Any]:
        return get_market_snapshot_tool(principal=principal, ticker=str(payload.get("ticker") or ""),
                                        market=payload.get("market"))
    return ask


def _conversation_candidate_ask_factory(principal: Principal) -> Any:
    """MCP 候选数据源适配器；复用既有白名单工具，失败时明确不可用，不回退官方资料。

    用闭包绑定 principal，而不是模块级全局：请求作用域内不共享状态，
    也不会跨请求泄漏身份。
    """
    def ask(payload: dict[str, Any]) -> dict[str, Any]:
        ticker = str(payload.get("ticker") or "").strip().upper()
        year = payload.get("year")
        if not ticker:
            return {"data_available": False, "error_codes": ["MCP_TICKER_MISSING"]}
        try:
            return get_structured_financial_candidates_tool(
                principal=principal, ticker=ticker, year=str(year) if year else None)
        except (ToolPermissionError, ToolInputError) as exc:
            return {"data_available": False, "error_codes": [exc.error_code],
                    "message": "第三方数据查询被拒绝或输入无效。"}
    return ask


def _conversation_create_job_factory(principal: Principal) -> Any:
    """报告任务适配器；归属由服务端 Principal 绑定，不接受请求参数里的身份。"""
    def create(payload: dict[str, str]) -> dict[str, Any]:
        _require_role(principal, "analyst", ROLE_ADMIN)
        ticker, topic = _normalize_request(str(payload["ticker"]), str(payload["topic"]))
        job, created = job_service.create(ticker=ticker, topic=topic,
            horizon=payload.get("horizon", "中期"), requested_by=principal.actor_id,
            tenant_id=principal.tenant_id, actor_id=principal.actor_id)
        return {"job_id": job.job_id, "status": job.status,
                "message": "报告任务已提交。" if created else "相同任务正在执行，已关联该任务。"}
    return create


def _conversation_report_ask_factory(principal: Principal) -> Any:
    """复用已发布报告问答完整门禁，不直接从历史 report_id 加载未审资料。"""
    def ask(payload: dict[str, Any]) -> dict[str, Any]:
        request = Request({"type": "http", "state": {"principal": principal}})
        return answer_report_question(AnswerRequest(**payload), request)
    return ask


def _conversation_company_ask_factory(principal: Principal) -> Any:
    """官方字段问答优先；未覆盖时保留既有官方原文检索能力。"""
    def ask(payload: dict[str, Any]) -> dict[str, Any]:
        result = company_answer_service.answer(payload["ticker"], payload["question"], requested_by=principal.actor_id)
        if result.get("error_code") not in {"FIELD_NOT_VERIFIED", "REPORT_PERIOD_UNSUPPORTED", "MATERIAL_NOT_ONBOARDED",
                                           "OFFICIAL_DOCUMENT_FIELD_UNVERIFIED", "OFFICIAL_PERIOD_NOT_ONBOARDED", "LATEST_OFFICIAL_DOCUMENT_REQUIRED"}:
            return result
        years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", payload["question"]))
        if len(years) > 1 or (not years and not re.search(r"最新|最近|当前|现在|截至目前|今年", payload["question"])):
            return result
        try:
            evidence = official_document_answer_service.answer(payload["ticker"], payload["question"], requested_by=principal.actor_id)
        except (DocumentIngestError, CompanySourceUnavailable, ValueError):
            return result
        return evidence if evidence.get("status") == "evidence_retrieved" else result
    return ask


def _conversation_discover(query: str, market: str | None = None, *, allow_external: bool = False) -> dict[str, Any]:
    """公司发现：本地优先；MCP 模式可显式启用外部候选，不能升级为官方证据。"""
    registry = default_discovery_registry() if allow_external else None
    result = resolve_company_candidates(query, market=market, registry=registry, allow_external=allow_external)
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def _dispatch_conversation_turn(context: dict[str, Any], text: str, *, principal: Principal,
                                requested_by: str = "anonymous", horizon: str = "中期",
                                answer_mode: str = "official", **callbacks: Any) -> dict[str, Any]:
    """把编排层的有效查询交给既有 :func:`dispatch_message`，不复制路由逻辑。"""
    from .chat_session import dispatch_message
    create_job = callbacks.pop("create_job", None) or _conversation_create_job_factory(principal)
    memory = callbacks.pop("conversation_memory", None)
    context = {**context, "conversation_memory": memory} if memory else context
    ask = callbacks.pop("ask", None) or _conversation_report_ask_factory(principal)
    return dispatch_message(context, text, create_job, ask, requested_by, horizon=horizon,
                            answer_mode=answer_mode, **callbacks)


@app.get("/api/user-profile")
def get_user_profile(http_request: Request) -> dict[str, Any]:
    """只显示本人**已确认**画像；不要求输入公司和年份。"""
    principal = _principal(http_request)
    manager = _profile_manager()
    entries = manager.active_profile(tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    pending = manager.pending_candidates(tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    return {
        "profile": entries,
        "fields": [{"field": key, "label": label} for key, label in PROFILE_FIELD_DISPLAY.items()],
        "pending_candidates": [item.to_dict() for item in pending],
        "note": "画像是用户自述偏好，不是投资适当性结论，也不构成任何公司的财务事实。",
    }


@app.put("/api/user-profile/{field}")
def update_user_profile(field: str, request: ProfileUpdateRequest, http_request: Request) -> dict[str, Any]:
    """直接改值；返回修改前后，便于前端展示确认。"""
    principal = _principal(http_request)
    if field not in PROFILE_FIELD_DISPLAY or contains_sensitive_content(request.value):
        raise HTTPException(status_code=422,
                            detail={"error_code": "PROFILE_FIELD_UNKNOWN", "message": "不支持的偏好字段。"})
    manager = _profile_manager()
    previous = manager.active_profile(tenant_id=principal.tenant_id, owner_id=principal.actor_id).get(field)
    entry = manager.set_field(field, request.value, tenant_id=principal.tenant_id,
                              owner_id=principal.actor_id)
    if entry is None:
        raise HTTPException(status_code=422,
                            detail={"error_code": "PROFILE_VALUE_INVALID", "message": "偏好内容无效。"})
    return {"field": field, "previous_value": (previous or {}).get("value"),
            "entry": entry.to_dict()}


@app.delete("/api/user-profile/{field}")
def delete_user_profile_field(field: str, http_request: Request) -> dict[str, Any]:
    """单项删除：软删除 + 撤销标记，后续摘要/画像提取都不会复活它。"""
    principal = _principal(http_request)
    if not _profile_manager().delete_field(field, tenant_id=principal.tenant_id,
                                           owner_id=principal.actor_id):
        raise HTTPException(status_code=404,
                            detail={"error_code": "PROFILE_FIELD_NOT_FOUND", "message": "该偏好不存在或已删除。"})
    return {"deleted": True, "field": field,
            "note": "已删除；历史消息仍可展示，但后续回答不再使用该偏好。"}


@app.delete("/api/user-profile")
def delete_user_profile_all(http_request: Request) -> dict[str, Any]:
    """全部删除；返回实际删除条数，不谎报成功。"""
    principal = _principal(http_request)
    deleted = _profile_manager().delete_all(tenant_id=principal.tenant_id, owner_id=principal.actor_id)
    return {"deleted": deleted, "note": "已删除全部长期偏好；下一轮不会继续使用。"}


class ProfileStageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=80)


@app.post("/api/user-profile/candidates")
def stage_profile_candidates(request: ProfileStageRequest, http_request: Request) -> dict[str, Any]:
    """生成待确认候选；**不写画像**，前端必须再走确认。"""
    principal = _principal(http_request)
    if request.conversation_id:
        _conversation_or_404(request.conversation_id, principal)
    candidates = _profile_manager().stage_candidates(
        request.text, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
        conversation_id=request.conversation_id)
    return {"candidates": [item.to_dict() for item in candidates],
            "note": "候选尚未保存；需按 candidate_id 确认。"}


@app.post("/api/user-profile/candidates/{candidate_id}/confirm")
def confirm_profile_candidate(candidate_id: str, request: ProfileCandidateRequest,
                              http_request: Request) -> dict[str, Any]:
    """确认绑定 candidate_id（可选 version）；跨用户/已处理/版本不符一律拒绝。"""
    principal = _principal(http_request)
    result = _profile_manager().confirm_candidate(
        candidate_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
        expected_version=request.version)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404,
                            detail={"error_code": "CANDIDATE_NOT_FOUND", "message": "指定候选不存在。"})
    if result["status"] in {"already_resolved", "expired"}:
        raise HTTPException(status_code=409,
                            detail={"error_code": "CANDIDATE_ALREADY_RESOLVED",
                                    "message": "该候选已处理，不能重复确认。"})
    if result["status"] == "version_mismatch":
        raise HTTPException(status_code=409,
                            detail={"error_code": "CANDIDATE_VERSION_MISMATCH",
                                    "message": "候选已被替换，请重新确认。"})
    return result


@app.post("/api/user-profile/candidates/{candidate_id}/reject")
def reject_profile_candidate(candidate_id: str, request: ProfileCandidateRequest,
                             http_request: Request) -> dict[str, Any]:
    principal = _principal(http_request)
    result = _profile_manager().reject_candidate(
        candidate_id, tenant_id=principal.tenant_id, owner_id=principal.actor_id,
        expected_version=request.version)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404,
                            detail={"error_code": "CANDIDATE_NOT_FOUND", "message": "指定候选不存在。"})
    return result
