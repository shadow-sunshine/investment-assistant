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
from pydantic import BaseModel, Field

from .config import REPORT_DIR
from .workflow import audit_json, run_research

app = FastAPI(title="Investment Assistant Demo API", version="0.1.0")


class GenerateReportRequest(BaseModel):
    ticker: str = Field(min_length=0, max_length=20)
    topic: str = Field(min_length=0, max_length=200)
    horizon: str = Field(default="\u4e2d\u671f", pattern=r"^(\u77ed\u671f|\u4e2d\u671f|\u957f\u671f)$")


def _safe_ticker(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9._-]", "_", ticker.upper().strip())


def _ticker_exists(ticker: str) -> bool:
    """仅用于入口有效性判断，不复用或替代研究工作流。"""
    history = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True, timeout=10, raise_errors=True)
    return history is not None and not history.empty


def _validate_generate_request(ticker: str, topic: str) -> tuple[str, str]:
    """在调用冻结工作流前拦截无效输入，避免生成报告或触发检索。"""
    normalized_ticker = ticker.upper().strip()
    normalized_topic = topic.strip()
    if not normalized_topic:
        raise HTTPException(status_code=422, detail="研究主题不能为空，请检查输入。")
    if not normalized_ticker or not re.fullmatch(r"[A-Z0-9.^=-]+", normalized_ticker):
        raise HTTPException(status_code=422, detail=f"未找到股票代码 {normalized_ticker or ticker.strip()}，请检查输入")
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_id = f"{_safe_ticker(ticker)}_{timestamp}"
    report_path = REPORT_DIR / f"{report_id}.md"
    audit_path = REPORT_DIR / f"{report_id}.json"
    report_path.write_text(result["report"], encoding="utf-8")
    audit_path.write_text(audit_json(result), encoding="utf-8")
    return _load_report(report_id)


@app.get("/api/reports")
def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    reports = sorted(REPORT_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [_report_summary(path) for path in reports[:bounded_limit]]


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    return _load_report(report_id)
