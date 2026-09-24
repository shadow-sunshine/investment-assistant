"""FastAPI facade for the frozen research workflow."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yfinance as yf
from yfinance.exceptions import YFTickerMissingError
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import JOB_DIR, REPORT_DIR
from .qa import (
    ANSWERED, GENERATOR_FAILED, INSUFFICIENT_EVIDENCE, OUT_OF_SCOPE,
    ERROR_GENERATOR_FAILED, ERROR_OUT_OF_SCOPE,
    ReportEvidenceError, answer_question, load_report_evidence,
)
from .report_jobs import STATUS_COMPLETED, JobService, JobStore, persist_report
from .workflow import run_research

app = FastAPI(title="Investment Assistant Demo API", version="0.1.0")

# Phase A：报告任务化。任务状态独立持久化在 data/jobs/，既有同步端点保持原样。
job_service = JobService(JobStore(JOB_DIR))
job_service.recover_stale_jobs()


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


def _load_report(report_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    report_path = REPORT_DIR / f"{report_id}.md"
    audit_path = REPORT_DIR / f"{report_id}.json"
    if not report_path.exists() or not audit_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    return {
        **_report_summary(report_path),
        "report": report_path.read_text(encoding="utf-8"),
        "market_snapshot": audit.get("market_snapshot") or {},
        "financial_snapshot": audit.get("financial_snapshot") or {},
        "sources": audit.get("sources") or [],
        "risk_flags": audit.get("risk_flags") or [],
        "evaluation": audit.get("evaluation") or {},
        "retrieval_evaluation": audit.get("retrieval_evaluation") or {},
        "mode": _mode_info(audit),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/reports")
def generate_report(request: GenerateReportRequest) -> dict[str, Any]:
    ticker, topic = _validate_generate_request(request.ticker, request.topic)
    try:
        # Reuse the frozen workflow as the only research implementation.
        result = run_research(ticker=ticker, topic=topic, horizon=request.horizon, report_mode="auto")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Research workflow failed: {type(exc).__name__}: {exc}") from exc

    report_id = persist_report(result, REPORT_DIR)
    return _load_report(report_id)


@app.get("/api/reports")
def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    reports = sorted(REPORT_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [_report_summary(path) for path in reports[:bounded_limit]]


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    return _load_report(report_id)


# --- Phase A：异步报告任务 ---------------------------------------------------


@app.post("/api/report-jobs", status_code=202)
def create_report_job(request: CreateJobRequest) -> dict[str, Any]:
    """提交报告任务，立即返回 job_id（不做网络校验，保证快速返回）。"""
    ticker, topic = _normalize_request(request.ticker, request.topic)
    job, created = job_service.create(
        ticker=ticker,
        topic=topic,
        horizon=request.horizon,
        requested_by=request.requested_by,
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
def answer_report_question(request: AnswerRequest) -> dict[str, Any]:
    """只针对一个已落盘报告回答问题，不联网、不检索新证据。"""
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

    result = answer_question(context, question, request.requested_by)
    payload = result.to_dict()
    if result.status == OUT_OF_SCOPE:
        return JSONResponse(status_code=422, content=payload)
    if result.status == GENERATOR_FAILED:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/report-jobs")
def list_report_jobs(limit: int = 30) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    return [job.to_dict() for job in job_service.list(limit=bounded_limit)]


@app.get("/api/report-jobs/{job_id}")
def get_report_job(job_id: str) -> dict[str, Any]:
    job = job_service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    return job.to_dict()


@app.get("/api/report-jobs/{job_id}/report")
def get_report_job_report(job_id: str) -> dict[str, Any]:
    """只有任务完成才返回报告；未完成返回 409，绝不返回半成品。"""
    job = job_service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    if job.status != STATUS_COMPLETED or not job.report_id:
        raise HTTPException(
            status_code=409,
            detail={"message": "任务尚未完成，暂无报告。", "job_id": job.job_id, "status": job.status},
        )
    return _load_report(job.report_id)


@app.post("/api/report-jobs/{job_id}/cancel")
def cancel_report_job(job_id: str) -> dict[str, Any]:
    job = job_service.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found")
    return job.to_dict()
