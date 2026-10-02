"""会话内的受控研究意图和上下文状态；不负责身份认证或持久化。"""

from __future__ import annotations

import re
from typing import Any, Callable

_TERMINAL = {"completed", "failed", "cancelled"}
_RESEARCH = re.compile(r"^(?:请|帮我|请帮我|麻烦)?\s*(?:分析|研究|生成(?:一份)?(?:研究)?报告|做一份(?:研究)?报告)\s*", re.I)
_TICKER = re.compile(r"(?<![A-Za-z0-9.^=-])(?:[A-Z]{1,5}|\d{6}\.(?:SS|SZ)|\d{4,5}\.HK)(?![A-Za-z0-9.^=-])")


def new_context() -> dict[str, Any]:
    return {"report_id": None, "ticker": None, "knowledge_ticker": None, "job_id": None, "job_status": None, "messages": []}


def _message(context: dict[str, Any], role: str, text: str, answer: dict[str, Any] | None = None) -> dict[str, Any]:
    updated = {**context, "messages": [*context["messages"], {"role": role, "text": text, "answer": answer}]}
    return updated


def select_knowledge_corpus(context: dict[str, Any], ticker: str) -> dict[str, Any]:
    """绑定中文官方年报语料；切换语料时清空旧报告和聊天消息。"""
    normalized = str(ticker).strip().upper()
    if context.get("knowledge_ticker") == normalized and not context.get("report_id") and not context.get("job_id"):
        return context
    return {**new_context(), "ticker": normalized, "knowledge_ticker": normalized}


def select_report(context: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """仅同一报告可保留历史；任务完成后绑定其报告由 update_job 处理。"""
    report_id, ticker = str(report["id"]), str(report["ticker"]).upper()
    if context["report_id"] == report_id and context["ticker"] == ticker:
        return context
    return {**new_context(), "report_id": report_id, "ticker": ticker}


def select_job(context: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    if context["job_id"] == job["job_id"]:
        return context
    # 选中另一个任务时不得继续显示先前报告的回答。
    return {**new_context(), "ticker": job["ticker"], "job_id": job["job_id"], "job_status": job["status"]}


def update_job(context: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    if context["job_id"] != job["job_id"]:
        return context
    status = str(job["status"])
    if context["job_status"] == status and (status != "completed" or context["report_id"] == job.get("report_id")):
        return context
    updated = {**context, "job_status": status}
    if status == "completed" and job.get("report_id"):
        updated = {**updated, "report_id": job["report_id"], "ticker": job["ticker"]}
        return _message(updated, "assistant", "报告已完成，可以查看报告并继续追问已审计的指标、来源或风险。")
    if status in {"failed", "cancelled"}:
        return _message(updated, "assistant", f"任务{ '失败' if status == 'failed' else '已取消'}：{job.get('error') or '可查看七步进度了解状态。'}")
    return updated


def route_message(text: str, context: dict[str, Any]) -> tuple[str, dict[str, str] | str]:
    normalized = text.strip()
    if not normalized:
        return "guidance", "请输入问题或研究任务。"
    if context["job_id"] and context["job_status"] not in _TERMINAL:
        return "guidance", "当前任务仍在执行。请等待完成，或在侧栏选择另一份已完成报告。"
    match = _RESEARCH.match(normalized)
    if match:
        remainder = normalized[match.end():].strip(" ：:，,。")
        found = _TICKER.search(remainder)
        if not found:
            return "guidance", "要生成报告，请写明股票代码和主题，例如：分析 AAPL 服务业务和现金流风险。"
        topic = (remainder[:found.start()] + " " + remainder[found.end():]).strip(" ：:，,。 ")
        if not topic:
            return "guidance", "请补充研究主题，例如：分析 AAPL 服务业务和现金流风险。"
        if len(topic) > 200:
            return "guidance", "研究主题过长，请控制在 200 字以内。"
        return "research", {"ticker": found.group(), "topic": topic}
    if not context["report_id"] and context.get("knowledge_ticker"):
        if len(normalized) > 500:
            return "guidance", "问题过长，请控制在 500 字以内。"
        return "knowledge", normalized
    if not context["report_id"]:
        return "guidance", "请先在侧栏选择中文年报语料或已完成报告，或输入“分析 AAPL 服务业务和现金流风险”创建任务。"
    if len(normalized) > 500:
        return "guidance", "问题过长，请控制在 500 字以内。"
    return "answer", normalized


def dispatch_message(
    context: dict[str, Any], text: str, create_job: Callable[[dict[str, str]], dict[str, Any]],
    ask: Callable[[dict[str, str]], dict[str, Any]], requested_by: str, horizon: str = "中期",
    knowledge_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """只有明确的研究指令才触发任务；普通追问绝不走网络研究路径。"""
    kind, detail = route_message(text, context)
    updated = _message(context, "user", text.strip())
    if kind == "guidance":
        return _message(updated, "assistant", str(detail))
    if kind == "knowledge":
        if knowledge_ask is None:
            return _message(updated, "assistant", "中文年报问答暂不可用，请稍后重试。")
        result = knowledge_ask({"ticker": str(context["knowledge_ticker"]), "question": str(detail), "requested_by": requested_by})
        return _message(updated, "assistant", str(result.get("answer") or result.get("message") or "当前证据不足，暂不回答。"), result)
    if kind == "research":
        request = {**detail, "horizon": horizon, "requested_by": requested_by}
        created = create_job(request)
        # 服务端幂等冲突也返回已有 job_id；未获取 ID 不绑定会话。
        if not created.get("job_id"):
            return _message(updated, "assistant", "任务提交未返回编号，未开始跟踪。")
        started = {**new_context(), "ticker": detail["ticker"], "job_id": created["job_id"], "job_status": created.get("status") or "queued"}
        started = _message(started, "user", text.strip())
        return _message(started, "assistant", created.get("message") or "报告任务已提交，正在显示七步进度。")
    result = ask({"report_id": context["report_id"], "ticker": context["ticker"], "question": str(detail), "requested_by": requested_by})
    return _message(updated, "assistant", str(result.get("answer") or result.get("message") or "本次没有可展示的回答。"), result)


def record_error(context: dict[str, Any], text: str, message: str) -> dict[str, Any]:
    return _message(_message(context, "user", text.strip()), "assistant", message)
