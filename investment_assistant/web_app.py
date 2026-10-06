"""Streamlit presentation layer for the Investment Assistant API."""

from __future__ import annotations

from datetime import UTC, datetime
import html
import os
import re
import uuid
from typing import Any

import requests
import streamlit as st
import yfinance as yf

from investment_assistant.chat_session import new_context, select_job, select_report, update_job

from investment_assistant.user_profile import FIELD_DISPLAY, profile_value_label

API_BASE_URL = os.getenv("INVESTMENT_ASSISTANT_API_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="\u667a\u80fd\u6295\u8d44\u52a9\u624b",
    page_icon="\U0001F4C8",
    layout="wide",
    initial_sidebar_state="auto",
)

APP_CSS = r"""
<style>
:root {
  --navy: #17395e;
  --navy-deep: #0d2948;
  --blue: #2b5d8f;
  --ink: #152235;
  --muted: #64748b;
  --canvas: #f8fafc;
  --surface: #ffffff;
  --line: #d9e3ee;
  --red: #c53c4a;
  --red-soft: #fff0f1;
  --green: #16805b;
  --green-soft: #eaf8f1;
  --amber: #9a6508;
  --amber-soft: #fff7e7;
  --shadow: 0 6px 22px rgba(18, 48, 80, .05);
}
html, body, [class*="css"] { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif; }
.stApp { background: var(--canvas); color: var(--ink); }
#MainMenu, footer, [data-testid="stStatusWidget"], [data-testid="stDeployButton"] { display: none !important; }
/* \u4fdd\u7559 Streamlit \u9876\u680f\u4e2d\u7684\u4fa7\u680f\u5c55\u5f00\u63a7\u4ef6\uff1a\u9876\u680f\u4e0d\u5360\u5e03\u5c40\uff0c\u6309\u94ae\u4ecd\u53ef\u70b9\u51fb\u3002 */
header[data-testid="stHeader"] { background: transparent !important; height: 0 !important; min-height: 0 !important; pointer-events: none !important; }
header[data-testid="stHeader"] [data-testid="stToolbar"] { display: block !important; }
/* \u6298\u53e0\u540e\u7684\u5c55\u5f00\u6309\u94ae\u6302\u8f7d\u4e8e header\uff1b\u56fa\u5b9a\u5b9a\u4f4d\u4e0d\u5360\u7528\u9876\u90e8\u5e03\u5c40\u3002 */
header[data-testid="stHeader"] [data-testid="stExpandSidebarButton"] { align-items: center !important; background: #ffffff !important; border: 1px solid #c7d7e8 !important; border-radius: 8px !important; box-shadow: 0 4px 12px rgba(13, 41, 72, .14) !important; color: var(--navy-deep) !important; display: flex !important; height: 32px !important; justify-content: center !important; left: .65rem !important; pointer-events: auto !important; position: fixed !important; top: .65rem !important; width: 32px !important; z-index: 1001 !important; }
/* \u5c55\u5f00\u65f6\u7684\u6536\u8d77\u6309\u94ae\u5b9e\u9645\u6302\u8f7d\u4e8e stSidebarCollapseButton\uff1b\u4fdd\u7559\u5176\u539f\u751f\u5e03\u5c40\u4e0e\u53ef\u70b9\u51fb\u72b6\u6001\uff0c\u4ec5\u8c03\u6574\u89c6\u89c9\u3002 */
[data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapseButton"] > button { visibility: visible !important; }
[data-testid="stSidebarCollapseButton"] > button { align-items: center !important; background: #eef3f8 !important; border: 1px solid var(--line) !important; border-radius: 7px !important; color: var(--navy-deep) !important; display: inline-flex !important; height: 30px !important; justify-content: center !important; width: 30px !important; }
header[data-testid="stHeader"] [data-testid="stExpandSidebarButton"]:hover { background: #edf4fb !important; border-color: #8eafd0 !important; }
[data-testid="stSidebarCollapseButton"] > button:hover { background: #e1ebf5 !important; border-color: #aebed1 !important; }
[data-testid="stMainBlockContainer"] { max-width: 980px; padding: 3rem 2.5rem 5rem; }
[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid var(--line); }
[data-testid="stSidebar"] [data-testid="stSidebarContent"] { color: var(--ink); }
[data-testid="stSidebar"] .stButton button { background: #ffffff !important; border-color: var(--line) !important; color: var(--navy-deep) !important; }
[data-testid="stSidebar"] .stButton button[kind="primary"] { background: var(--navy) !important; border-color: var(--navy) !important; color: #ffffff !important; }
[data-testid="stSidebar"] .stButton button[kind="primary"] p { color: #ffffff !important; }
[data-testid="stSidebar"] .stButton button:hover { border-color: var(--blue) !important; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3, [data-testid="stSidebar"] p, [data-testid="stSidebar"] [data-testid="stWidgetLabel"], [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] { color: var(--ink) !important; }
.ia-sidebar-brand { color: var(--navy-deep); font-size: 1.22rem; font-weight: 800; letter-spacing: -.03em; margin: .35rem 0 1.3rem; }
/* 会话列表：当前会话高亮 + 标题省略，长标题不撑破侧栏。 */
[data-testid="stSidebar"] .ia-conv-title { color: var(--ink); font-size: .88rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
[data-testid="stSidebar"] [data-testid="stChatMessage"] { background: var(--surface); }
[data-testid="stSidebar"] [data-testid="stSelectbox"] > div.react-aria-ComboBox > div[role="group"] { background: #ffffff !important; border: 1px solid var(--line) !important; border-radius: 8px !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] input[role="combobox"] { color: var(--ink) !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] input[role="combobox"]::placeholder { color: var(--muted) !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] button[aria-haspopup="listbox"], [data-testid="stSidebar"] [data-testid="stSelectbox"] button[aria-haspopup="listbox"] svg { color: var(--navy) !important; }
body [role="listbox"][data-rac], body [role="listbox"] { background: #ffffff !important; border: 1px solid var(--line) !important; box-shadow: var(--shadow) !important; color: var(--ink) !important; }
body [role="listbox"] [role="option"] { color: var(--ink) !important; }
body [role="listbox"] [role="option"][data-focused], body [role="listbox"] [role="option"]:hover { background: #eef4fa !important; color: var(--navy-deep) !important; }
h1 { color: var(--navy-deep) !important; font-size: clamp(2rem, 4vw, 3.1rem) !important; letter-spacing: -.055em; margin: .1rem 0 .45rem !important; }
h2, h3 { color: var(--navy-deep) !important; }
.ia-panel, [data-testid="stForm"] { background: var(--surface); border: 1px solid var(--line); border-radius: 15px; box-shadow: var(--shadow); margin: .8rem 0 1.25rem; padding: 1.15rem 1.25rem; }
.ia-panel-title { color: var(--navy-deep); font-size: 1.06rem; font-weight: 760; margin-bottom: .2rem; }
.ia-panel-subtitle { color: var(--muted); font-size: .84rem; line-height: 1.6; margin-bottom: .95rem; }
.ia-mode, .ia-badge, .ia-source-tag, .ia-score-tag { align-items: center; border-radius: 999px; display: inline-flex; font-size: .76rem; font-weight: 730; gap: .28rem; padding: .31rem .62rem; }
.ia-mode--llm { background: var(--green-soft); border: 1px solid #bde5cf; color: #106343; }
.ia-mode--fallback { background: var(--amber-soft); border: 1px solid #efd39a; color: #865404; }
.ia-mode-reason { color: var(--muted); font-size: .84rem; margin: .55rem 0 .1rem; }
.ia-metric-card { background: #fff; border: 1px solid var(--line); border-top: 3px solid #c8d8e9; border-radius: 11px; min-height: 116px; padding: .84rem .92rem; }
.ia-metric-card .label { color: var(--muted); font-size: .75rem; font-weight: 700; margin-bottom: .32rem; }
.ia-metric-card .value { color: var(--ink); font-size: 1.28rem; font-weight: 760; letter-spacing: -.025em; line-height: 1.25; overflow-wrap: anywhere; }
.ia-metric-card .value--unavailable { color: #98a4b3; font-size: 1rem; font-weight: 650; }
.ia-metric-card .delta { font-size: .82rem; font-weight: 750; margin-top: .44rem; }
.ia-metric-card .delta--up { color: var(--red); }
.ia-metric-card .delta--down { color: var(--green); }
.ia-metric-card .meta { color: var(--muted); font-size: .75rem; margin-top: .48rem; }
.ia-live-card { background: linear-gradient(115deg, #ffffff 0%, #f5f9fd 100%); border: 1px solid #cddded; border-left: 5px solid var(--navy); border-radius: 15px; box-shadow: var(--shadow); margin: .7rem 0 1.2rem; padding: 1.1rem 1.25rem; }
.ia-live-meta { color: var(--muted); font-size: .82rem; line-height: 1.8; margin-top: .72rem; }
.ia-evidence-card { background: #fff; border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 11px; margin: .72rem 0; padding: .9rem 1rem; }
.ia-evidence-topline { align-items: center; display: flex; flex-wrap: wrap; gap: .55rem; }
.ia-citation { background: var(--navy); border-radius: 5px; color: white; font-size: .73rem; font-weight: 750; padding: .16rem .38rem; }
.ia-evidence-title { color: var(--navy-deep); font-size: .94rem; font-weight: 760; overflow-wrap: anywhere; }
.ia-evidence-meta, .ia-score-grid { color: var(--muted); font-size: .8rem; line-height: 1.75; margin-top: .5rem; }
.ia-evidence-text { background: #f6f9fc; border-radius: 7px; color: #334155; font-size: .84rem; line-height: 1.7; margin-top: .62rem; padding: .56rem .68rem; }
.ia-source-tag { background: #eef4fb; color: #315b83; }
.ia-source-tag--official { background: #e9f7ee; color: #14724d; }
.ia-score-tag { background: #f4f6f8; color: #526173; }
.ia-risk { align-items: flex-start; border-radius: 9px; display: flex; font-size: .87rem; gap: .6rem; line-height: 1.65; margin: .55rem 0; padding: .7rem .78rem; }
.ia-risk--high { background: var(--red-soft); border: 1px solid #f4c9cd; color: #8e2832; }
.ia-risk--info { background: #eef5fb; border: 1px solid #cfe0f0; color: #315b83; }
.ia-risk--pass { background: var(--green-soft); border: 1px solid #c6e9d5; color: #116242; }
.ia-badge--high { background: #c43845; color: #fff; }
.ia-badge--info { background: #467aa9; color: #fff; }
.ia-badge--pass { background: #16805b; color: #fff; }
[data-testid="stForm"] { padding-bottom: 1rem; }
/* 输入框未聚焦时也要有清晰边界，不能依赖点击后才显形。 */
[data-testid="stTextInputRootElement"] { background: #f8fbfe !important; border: 1px solid #9eb4c9 !important; border-radius: 10px !important; min-height: 48px; transition: border-color .15s ease, box-shadow .15s ease; }
[data-testid="stTextInputRootElement"]:hover { border-color: var(--blue) !important; }
[data-testid="stTextInputRootElement"]:focus-within { border-color: var(--blue) !important; box-shadow: 0 0 0 3px rgba(43, 93, 143, .18) !important; }
[data-testid="stTextInput"] input { background: transparent !important; border-radius: 10px !important; font-size: 1.08rem !important; font-weight: 650 !important; min-height: 48px; }
[data-testid="stTextInput"] input::placeholder { color: #697d90 !important; font-weight: 400; opacity: 1; }
[data-testid="stFormSubmitButton"] button, .stButton > button[kind="primary"] { background: var(--navy) !important; border-color: var(--navy) !important; border-radius: 9px !important; font-weight: 760 !important; min-height: 48px; }
[data-testid="stTabs"] [data-baseweb="tab-list"] { border-bottom: 1px solid var(--line); gap: .5rem; }
[data-testid="stTabs"] button[role="tab"] { color: var(--muted); font-weight: 700; }
[data-testid="stTabs"] button[aria-selected="true"] { color: var(--navy) !important; }
[data-testid="stTabs"] button[aria-selected="true"]::after { background: var(--navy) !important; }
[data-testid="stProgressBar"] > div > div > div { background: var(--navy) !important; }
/* Phase A：报告任务七步进度 */
.ia-job-meta { color: var(--muted); font-size: .84rem; line-height: 1.8; margin: .1rem 0 .6rem; }
.ia-steps { display: flex; flex-direction: column; gap: .4rem; }
.ia-step { align-items: center; display: flex; font-size: .9rem; gap: .58rem; }
.ia-step-icon { align-items: center; border-radius: 50%; display: inline-flex; flex: 0 0 auto; font-size: .76rem; font-weight: 760; height: 22px; justify-content: center; width: 22px; }
.ia-step--completed { color: var(--ink); }
.ia-step--completed .ia-step-icon { background: var(--green-soft); color: var(--green); }
.ia-step--running { color: var(--navy-deep); font-weight: 700; }
.ia-step--running .ia-step-icon { background: #e8f1fa; color: var(--blue); }
.ia-step--pending, .ia-step--skipped { color: var(--muted); }
.ia-step--pending .ia-step-icon, .ia-step--skipped .ia-step-icon { background: #f1f4f7; color: #9aa6b4; }
.ia-step--failed { color: var(--red); }
.ia-step--failed .ia-step-icon { background: var(--red-soft); color: var(--red); }
.ia-step-meta { color: var(--muted); font-size: .78rem; font-weight: 500; margin-left: auto; padding-left: .6rem; white-space: nowrap; }
.ia-step-error { color: var(--red); font-size: .8rem; }
/* 活跃资料范围保留为小提示，而不是占用整屏的技术说明。 */
.ia-chat-context { color: var(--muted); font-size: .84rem; line-height: 1.6; margin: .4rem 0 1.1rem; overflow-wrap: anywhere; }
[data-testid="stChatMessage"] { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); margin-bottom: .8rem; }
[data-testid="stChatInput"] textarea:focus-visible, button:focus-visible { outline: 2px solid #2b5d8f !important; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; } }
@media (max-width: 800px) { [data-testid="stMainBlockContainer"] { padding: 1.25rem 1rem 3rem; } .ia-panel { padding: 1rem; } }
</style>
"""

st.markdown(APP_CSS, unsafe_allow_html=True)


def _auth_headers() -> dict[str, str]:
    token = st.session_state.get("auth_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_get(path: str) -> Any:
    response = requests.get(f"{API_BASE_URL}{path}", headers=_auth_headers(), timeout=20)
    response.raise_for_status()
    return response.json()


def _api_post(path: str, payload: dict[str, Any]) -> Any:
    response = requests.post(f"{API_BASE_URL}{path}", json=payload, headers=_auth_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def _api_put(path: str, payload: dict[str, Any]) -> Any:
    response = requests.put(f"{API_BASE_URL}{path}", json=payload, headers=_auth_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def _api_delete(path: str) -> Any:
    response = requests.delete(f"{API_BASE_URL}{path}", headers=_auth_headers(), timeout=30)
    response.raise_for_status()
    return response.json() if response.content else {}


# --- 会话与画像：中文错误提示 ---------------------------------------------------
#
# 后端 detail 可能是 str 或 {"error_code","message"}；这里统一成"中文正文 + 可选错误码"，
# 不把内部错误码当成用户提示的主体。

_ERROR_TEXT = {
    "AUTH_REQUIRED": "登录已失效，请重新登录。",
    "AUTH_INVALID": "访问凭证无效。",
    "AUTH_MISSING": "缺少访问凭证，请重新登录。",
    "AUTH_NOT_CONFIGURED": "服务端未配置身份认证。",
    "AUTH_EXPIRED": "访问凭证已过期，请重新登录。",
    "CONVERSATION_NOT_FOUND": "该会话不存在或不属于当前身份。",
    "CANDIDATE_NOT_FOUND": "该待确认项不存在或已失效。",
    "CANDIDATE_ALREADY_RESOLVED": "该待确认项已处理过，不能重复确认。",
    "CANDIDATE_VERSION_MISMATCH": "待确认内容已被替换，请重新说明一次。",
    "PROFILE_FIELD_UNKNOWN": "不支持的偏好字段。",
    "PROFILE_FIELD_NOT_FOUND": "该偏好不存在或已删除。",
    "PROFILE_VALUE_INVALID": "偏好内容无效。",
    "CONVERSATION_STORE_UNAVAILABLE": "会话存储暂时不可用，请稍后重试。",
    "ROLE_REQUIRED": "当前身份无权执行该操作。",
    "FORBIDDEN": "访问被拒绝。",
    "STORAGE_UNAVAILABLE": "本地持久化或版本状态不可用。",
}


def _friendly_error(exc: Exception, fallback: str) -> str:
    """把 HTTP/请求异常转成可读中文提示；错误码只作为可选诊断附在后面。"""
    code = None
    detail: Any = None
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        try:
            body = exc.response.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = None
        code = str(exc.response.status_code)
    elif isinstance(exc, requests.Timeout):
        return "请求超时，后端未在限定时间内返回。可以重试；不会自动重复提交。"
    elif isinstance(exc, requests.ConnectionError):
        return "无法连接后端服务。请确认 API 已启动后重试。"
    message = ""
    if isinstance(detail, dict):
        code = str(detail.get("error_code") or code or "")
        message = str(detail.get("message") or "")
    elif isinstance(detail, str):
        message = detail
    text = _ERROR_TEXT.get(code, "") or message or str(exc) or fallback
    return f"{text}（诊断码 {code}）" if code and code not in text else text


def _conversation_request(conversation_id: str, text: str, *, request_id: str,
                          answer_mode: str, horizon: str = "中期") -> Any:
    """提交一轮对话。超时与失败都转成中文结果，不让异常冒泡成红色堆栈。"""
    try:
        return _api_post(f"/api/conversations/{conversation_id}/turns",
                         {"text": text, "request_id": request_id,
                          "answer_mode": answer_mode, "horizon": horizon})
    except requests.RequestException as exc:
        return {"status": "failed", "message": _friendly_error(exc, "请求未完成，请重试。"),
                "retryable": True}


def _load_conversation_list(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    try:
        return _api_get(f"/api/conversations?limit={limit}&offset={offset}")
    except requests.RequestException as exc:
        return {"conversations": [], "total": 0, "has_more": False,
                "error": _friendly_error(exc, "无法取得会话列表。")}


# token 仅存本次 Streamlit 会话，不存进 URL、全局变量或浏览器持久缓存。
# 在 `streamlit run` 中 `st.stop()` 会终止本轮脚本；裸 import/静态检查模式下它可能只返回，
# 因此先初始化变量，避免登录分支未执行后继续落到身份同步代码时触发 NameError。
current_identity: dict[str, Any] = {"actor_id": "anonymous", "tenant_id": "anonymous", "roles": []}
if not st.session_state.get("auth_token"):
    st.title("投研服务台 · 登录")
    st.caption("使用后端 IA_AUTH_TOKENS 配置的个人凭证；演示级本地身份，不是企业 SSO。")
    with st.form("login_form"):
        entered = st.text_input("访问凭证", type="password", placeholder="请输入访问凭证")
        login = st.form_submit_button("进入服务台", type="primary")
    if login:
        st.session_state.auth_token = entered
        try:
            st.session_state.identity = _api_get("/api/me")
        except requests.RequestException:
            st.session_state.pop("auth_token", None)
            st.error("凭证无效、服务未配置身份或后端不可用。")
        else:
            authenticated_identity = st.session_state.identity
            st.session_state.clear()
            st.session_state.auth_token = entered
            st.session_state.identity = authenticated_identity
            st.session_state.chat_context = new_context()
            st.query_params.clear()
            st.rerun()
    st.stop()
try:
    current_identity = _api_get("/api/me")
except requests.RequestException:
    st.session_state.clear()
    st.error("会话身份已失效，请重新登录。")
    st.stop()
previous_identity = st.session_state.get("identity")
if previous_identity and previous_identity != current_identity:
    token = st.session_state.auth_token
    st.session_state.clear()
    st.session_state.auth_token = token
    st.session_state.chat_context = new_context()
    st.query_params.clear()
st.session_state.identity = current_identity


# --- Phase A：报告任务进度 ---------------------------------------------------

JOB_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

_STEP_ICON = {
    "completed": "✓",
    "running": "▶",
    "failed": "✕",
    "skipped": "–",
    "pending": "○",
}

_JOB_STATUS_BADGE = {
    "queued": ("排队中", "info"),
    "running": ("执行中", "info"),
    "completed": ("已完成", "pass"),
    "failed": ("失败", "high"),
    "cancelled": ("已取消", "info"),
}


def _submit_job(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """提交任务。返回 ``(job_id, 提示语)``；重复提交（409）时复用既有任务。"""
    try:
        created = requests.post(f"{API_BASE_URL}/api/report-jobs", json=payload, headers=_auth_headers(), timeout=30)
        created.raise_for_status()
    except requests.HTTPError as exc:
        detail = exc.response.json().get("detail") if exc.response is not None else None
        if exc.response is not None and exc.response.status_code == 409 and isinstance(detail, dict):
            return str(detail.get("job_id")), "相同参数的报告任务已在执行中，接续查看该任务。"
        return None, detail if isinstance(detail, str) else str(exc)
    except requests.RequestException as exc:
        return None, f"无法连接后端：{exc}"
    return str(created.json().get("job_id")), None


def _render_job_progress(job: dict[str, Any]) -> None:
    """按七步语义展示任务进度：状态、耗时、失败原因。"""
    status = str(job.get("status") or "")
    badge_label, badge_kind = _JOB_STATUS_BADGE.get(status, (status or "未知", "info"))
    rows = []
    for step in job.get("steps") or []:
        step_status = str(step.get("status") or "pending")
        text = html.escape(str(step.get("label") or step.get("name") or ""))
        if step.get("error"):
            text += f' <span class="ia-step-error">{html.escape(str(step["error"]))}</span>'
        duration = step.get("duration_ms")
        if duration is not None:
            meta = f"{duration / 1000:.1f}s"
        else:
            meta = "进行中…" if step_status == "running" else ""
        rows.append(
            f'<div class="ia-step ia-step--{html.escape(step_status)}">'
            f'<span class="ia-step-icon">{html.escape(_STEP_ICON.get(step_status, "○"))}</span>'
            f'<span>{text}</span><span class="ia-step-meta">{html.escape(meta)}</span></div>'
        )
    st.markdown(
        '<div class="ia-live-card"><div class="ia-panel-title">报告任务 '
        f'<span class="ia-badge ia-badge--{badge_kind}">{html.escape(badge_label)}</span></div>'
        f'<div class="ia-job-meta">任务编号：{html.escape(str(job.get("job_id")))} &nbsp;|&nbsp; '
        f'发起人：{html.escape(str(job.get("requested_by")))} &nbsp;|&nbsp; '
        f'进度：{html.escape(str(job.get("progress", 0)))}% &nbsp;|&nbsp; '
        f'创建时间：{html.escape(str(job.get("created_at")))}</div>'
        f'<div class="ia-steps">{"".join(rows)}</div></div>',
        unsafe_allow_html=True,
    )
    if job.get("error"):
        _risk_box("high", "失败原因", str(job["error"]))


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "\u6570\u636e\u4e0d\u53ef\u7528"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _is_unavailable(value: Any) -> bool:
    return value is None or value == "" or str(value) == "\u6570\u636e\u4e0d\u53ef\u7528"


def _metric_delta(key: str, snapshot: dict[str, Any]) -> tuple[str | None, str]:
    value = snapshot.get(key)
    if key in {"period_return_pct", "one_month_return_pct", "max_drawdown_pct", "change_pct"} and isinstance(value, (int, float)):
        if value > 0:
            return f"{value:+.2f}%", "up"
        if value < 0:
            return f"{value:+.2f}%", "down"
        return "0.00%", "neutral"
    return None, "neutral"


def _metric_cards(snapshot: dict[str, Any], fields: list[tuple[str, str]], columns: int = 3) -> None:
    cards = st.columns(columns)
    for index, (key, label) in enumerate(fields):
        value = snapshot.get(key)
        display = _display_value(value)
        delta, delta_kind = _metric_delta(key, snapshot)
        with cards[index % columns]:
            value_class = "value value--unavailable" if _is_unavailable(value) else "value"
            delta_html = f'<div class="delta delta--{delta_kind}">{html.escape(delta)}</div>' if delta else '<div class="meta">\u7ed3\u6784\u5316\u5feb\u7167</div>'
            st.markdown(f'<div class="ia-metric-card"><div class="label">{html.escape(label)}</div><div class="{value_class}">{html.escape(display)}</div>{delta_html}</div>', unsafe_allow_html=True)


def _load_live_market(ticker: str) -> dict[str, Any]:
    """Presentation-only quote preview; the frozen API workflow remains the report source."""
    fetched_at = datetime.now(UTC).isoformat()
    try:
        history = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True, timeout=12, raise_errors=True)
        if history is None or history.empty:
            return {"data_available": False, "error": "Yahoo Finance \u672a\u8fd4\u56de\u53ef\u7528\u884c\u60c5\u6570\u636e\u3002", "fetched_at": fetched_at}
        close = float(history["Close"].iloc[-1])
        previous_close = float(history["Close"].iloc[-2]) if len(history) > 1 else None
        change_pct = ((close / previous_close) - 1) * 100 if previous_close else None
        trade_day = history.index[-1]
        if hasattr(trade_day, "isoformat"):
            trade_day = trade_day.isoformat()
        return {"data_available": True, "latest_close": close, "change_pct": change_pct, "latest_trading_date": str(trade_day), "source": "Yahoo Finance via yfinance", "fetched_at": fetched_at}
    except Exception as exc:
        return {"data_available": False, "error": f"{type(exc).__name__}: {exc}", "fetched_at": fetched_at, "source": "Yahoo Finance via yfinance"}


def _show_live_market(snapshot: dict[str, Any], ticker: str) -> None:
    st.markdown('<div class="ia-live-card"><div class="ia-panel-title">\u5b9e\u65f6\u884c\u60c5\u5feb\u7167</div><div class="ia-panel-subtitle">\u5728\u751f\u6210\u62a5\u544a\u524d\u5148\u5c55\u793a\u5f53\u524d\u884c\u60c5\uff1b\u5b8c\u6574\u7814\u7a76\u4ecd\u7531\u73b0\u6709\u540e\u7aef\u5de5\u4f5c\u6d41\u751f\u6210\u3002</div>', unsafe_allow_html=True)
    if snapshot.get("data_available"):
        _metric_cards(snapshot, [("latest_close", "\u6700\u65b0\u4ef7"), ("change_pct", "\u6da8\u8dcc\u5e45"), ("latest_trading_date", "\u4ea4\u6613\u65e5")])
        st.markdown(f'<div class="ia-live-meta">\u6807\u7684\uff1a{html.escape(ticker.upper())} &nbsp;|&nbsp; \u6765\u6e90\uff1a{html.escape(str(snapshot.get("source") or "\u672a\u63d0\u4f9b"))} &nbsp;|&nbsp; \u6293\u53d6\u65f6\u95f4\uff1a{html.escape(str(snapshot.get("fetched_at") or "\u672a\u63d0\u4f9b"))}</div></div>', unsafe_allow_html=True)
    else:
        _risk_box("info", "\u62ab\u9732", f"\u5b9e\u65f6\u884c\u60c5\u6570\u636e\u4e0d\u53ef\u7528\uff1a{snapshot.get('error') or '\u672a\u63d0\u4f9b\u539f\u56e0'}")
        st.markdown('</div>', unsafe_allow_html=True)


def _source_type(metadata: dict[str, Any]) -> str:
    source_type = str(metadata.get("source_type") or "")
    if source_type == "news":
        return "\u65b0\u95fb\u8bc1\u636e"
    if metadata.get("file_name"):
        return "\u672c\u5730 PDF \u7814\u7a76\u8d44\u6599"
    return str(metadata.get("source") or "\u672a\u547d\u540d\u6765\u6e90")


def _score_details(source: dict[str, Any], metadata: dict[str, Any]) -> list[tuple[str, Any]]:
    keys = ["score", "distance", "similarity", "rerank_score", "lexical_score", "anchor_score"]
    return [(key, source.get(key, metadata.get(key))) for key in keys if source.get(key, metadata.get(key)) is not None]


def _show_sources(sources: list[dict[str, Any]]) -> None:
    st.markdown('<div class="ia-panel"><div class="ia-panel-title">\u8bc1\u636e\u5899</div><div class="ia-panel-subtitle">\u9010\u6761\u5c55\u793a\u6587\u4ef6\u3001\u9875\u7801\u6743\u5a01\u6027\u3001\u6765\u6e90\u7c7b\u578b\u4e0e\u5df2\u900f\u4f20\u7684\u68c0\u7d22\u5f97\u5206\u5b57\u6bb5\u3002</div>', unsafe_allow_html=True)
    if not sources:
        _risk_box("info", "\u62ab\u9732", "\u672a\u68c0\u7d22\u5230\u8bc1\u636e\u3002")
        st.markdown('</div>', unsafe_allow_html=True)
        return
    for source in sources:
        metadata = source.get("metadata") or {}
        citation = source.get("citation") or "S?"
        filename = metadata.get("file_name") or metadata.get("title") or metadata.get("source") or "\u672a\u547d\u540d\u6765\u6e90"
        page = metadata.get("page") or "\u4e0d\u9002\u7528"
        page_authority = metadata.get("page_authority")
        page_label = "\u5b98\u65b9\u9875\u7801" if page_authority == "official" else "\u672c\u5730\u8f6c\u6362\u9875\u7801\uff08\u975e\u5b98\u65b9\u5206\u9875\uff09" if page_authority == "generated" else "\u9875\u7801"
        official_class = " ia-source-tag--official" if page_authority == "official" else ""
        content = html.escape(str(source.get("content") or "\u65e0\u53ef\u63d0\u53d6\u6587\u672c\u3002"))
        url = metadata.get("url")
        link = f'<a href="{html.escape(str(url), quote=True)}" target="_blank">\u6253\u5f00\u539f\u59cb\u94fe\u63a5</a>' if url else "\u65e0\u539f\u59cb\u94fe\u63a5"
        score_html = "".join(f'<span class="ia-score-tag">{html.escape(key)}: {html.escape(_display_value(value))}</span>' for key, value in _score_details(source, metadata)) or '<span class="ia-score-tag">\u68c0\u7d22\u5f97\u5206\uff1a\u5f53\u524d\u5ba1\u8ba1\u8bb0\u5f55\u672a\u900f\u4f20</span>'
        st.markdown(
            f'<div class="ia-evidence-card"><div class="ia-evidence-topline"><span class="ia-citation">[{html.escape(str(citation))}]</span><span class="ia-evidence-title">{html.escape(str(filename))}</span><span class="ia-source-tag{official_class}">{html.escape(_source_type(metadata))}</span></div><div class="ia-evidence-meta">{page_label}\uff1a{html.escape(str(page))} &nbsp;|&nbsp; {link}</div><div class="ia-score-grid">{score_html}</div><div class="ia-evidence-text">{content}</div></div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


def _risk_box(kind: str, label: str, text: str) -> None:
    st.markdown(f'<div class="ia-risk ia-risk--{kind}"><span class="ia-badge ia-badge--{kind}">{html.escape(label)}</span>{html.escape(text)}</div>', unsafe_allow_html=True)


def _show_report(data: dict[str, Any]) -> None:
    mode = data.get("mode") or {}
    is_llm = mode.get("label") == "\u53d7\u63a7 LLM \u7248"
    mode_label = mode.get("label") or "\u89c4\u5219\u7248"
    mode_class = "ia-mode--llm" if is_llm else "ia-mode--fallback"
    status = "\u53d7\u63a7 LLM \u7248" if is_llm else "\u89c4\u5219\u7248\u56de\u9000"
    st.markdown('<div class="ia-panel ia-panel--report">', unsafe_allow_html=True)
    st.markdown(f'<span class="ia-mode {mode_class}">{html.escape(status)}</span><div class="ia-mode-reason">\u62a5\u544a\u6a21\u5f0f\uff1a{html.escape(mode_label)}\uff1b\u539f\u56e0\uff1a{html.escape(str(mode.get("reason") or "\u672a\u63d0\u4f9b"))}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    overview, report_tab, evidence_tab, risks_tab = st.tabs(["\u6570\u636e\u5feb\u7167", "\u62a5\u544a\u5168\u6587", "\u8bc1\u636e\u5899", "\u98ce\u9669\u4e0e\u5ba1\u8ba1"])
    with overview:
        st.markdown('<div class="ia-panel"><div class="ia-panel-title">\u884c\u60c5\u4e0e\u8d22\u52a1\u5feb\u7167</div><div class="ia-panel-subtitle">\u6bcf\u4e2a\u6307\u6807\u4ec5\u5c55\u793a\u5de5\u4f5c\u6d41\u5df2\u83b7\u53d6\u7684\u7ed3\u6784\u5316\u5b9e\u9645\u6570\u636e\uff1b\u65e0\u6cd5\u83b7\u53d6\u65f6\u663e\u5f0f\u62ab\u9732\u3002</div>', unsafe_allow_html=True)
        _metric_cards(data.get("market_snapshot") or {}, [("latest_close", "\u6700\u65b0\u6536\u76d8\u4ef7"), ("latest_trading_date", "\u4ea4\u6613\u65e5"), ("period_return_pct", "\u6837\u672c\u671f\u6536\u76ca"), ("one_month_return_pct", "\u8fd1\u4e00\u6708\u6536\u76ca"), ("annualized_volatility_pct", "\u5e74\u5316\u6ce2\u52a8\u7387"), ("max_drawdown_pct", "\u6700\u5927\u56de\u64a4")])
        st.markdown('<div style="height:.7rem"></div>', unsafe_allow_html=True)
        _metric_cards(data.get("financial_snapshot") or {}, [("revenue", "\u8425\u6536"), ("net_income", "\u51c0\u5229\u6da6"), ("free_cash_flow", "\u81ea\u7531\u73b0\u91d1\u6d41"), ("trailing_pe", "PE"), ("price_to_book", "PB"), ("valuation_as_of", "\u4f30\u503c\u6293\u53d6\u65f6\u95f4")])
        st.markdown('</div>', unsafe_allow_html=True)
    with report_tab:
        st.markdown('<div class="ia-panel ia-panel--report">', unsafe_allow_html=True)
        st.markdown(data.get("report") or "\u62a5\u544a\u5185\u5bb9\u7f3a\u5931\u3002")
        st.markdown('</div>', unsafe_allow_html=True)
    with evidence_tab:
        _show_sources(data.get("sources") or [])
    with risks_tab:
        st.markdown('<div class="ia-panel"><div class="ia-panel-title">\u98ce\u9669\u65d7\u6807\u4e0e\u6821\u9a8c</div><div class="ia-panel-subtitle">\u7ea2\u8272\u4ee3\u8868\u98ce\u9669\uff0c\u84dd\u8272\u4e3a\u9700\u5173\u6ce8\u63d0\u793a\uff0c\u7eff\u8272\u4e3a\u5df2\u901a\u8fc7\u7684\u7cfb\u7edf\u6821\u9a8c\u3002</div>', unsafe_allow_html=True)
        flags = data.get("risk_flags") or []
        if flags:
            for flag in flags:
                _risk_box("high", "\u98ce\u9669", str(flag))
        else:
            _risk_box("pass", "\u901a\u8fc7", "\u672a\u4ea7\u751f\u989d\u5916\u98ce\u9669\u65d7\u6807\u3002")
        evaluation = data.get("evaluation") or {}
        _risk_box("pass" if evaluation.get("passed") else "high", "\u901a\u8fc7" if evaluation.get("passed") else "\u672a\u901a\u8fc7", "Safety \u6821\u9a8c\u901a\u8fc7\u3002" if evaluation.get("passed") else "Safety \u6821\u9a8c\u672a\u901a\u8fc7\u3002")
        for finding in evaluation.get("findings") or []:
            _risk_box("info", "\u63d0\u793a", str(finding))
        st.markdown('</div>', unsafe_allow_html=True)




def _knowledge_answer_request(payload: dict[str, str]) -> dict[str, Any]:
    """针对中文官方年报语料回答；拒答响应也作为可展示结果保留。"""
    response = requests.post(f"{API_BASE_URL}/api/knowledge-answers", json=payload, headers=_auth_headers(), timeout=90)
    if response.status_code in {409, 422}:
        body = response.json()
        if isinstance(body, dict) and body.get("status") in {"answered", "refused", "insufficient_evidence", "out_of_scope"}:
            return body
    response.raise_for_status()
    return response.json()


def _candidate_financial_request(payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(f"{API_BASE_URL}/api/company-financial-candidates", json=payload, headers=_auth_headers(), timeout=90)
    response.raise_for_status()
    return response.json()


def _research_answer_request(payload: dict[str, str]) -> dict[str, Any]:
    response = requests.post(f"{API_BASE_URL}/api/research-answers", json=payload, headers=_auth_headers(), timeout=90)
    response.raise_for_status()
    return response.json()


def _company_answer_request(payload: dict[str, str]) -> dict[str, Any]:
    response = requests.post(f"{API_BASE_URL}/api/company-answers", json=payload, headers=_auth_headers(), timeout=90)
    response.raise_for_status()
    result = response.json()
    # 既有已核验回答优先；仅字段/年度资料未覆盖时探索已入库官方原文。
    if result.get("error_code") not in {"FIELD_NOT_VERIFIED", "REPORT_PERIOD_UNSUPPORTED", "MATERIAL_NOT_ONBOARDED",
                                      "OFFICIAL_DOCUMENT_FIELD_UNVERIFIED", "OFFICIAL_PERIOD_NOT_ONBOARDED",
                                      "LATEST_OFFICIAL_DOCUMENT_REQUIRED"}:
        return result
    years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", payload.get("question", "")))
    question_text = payload.get("question", "")
    if len(years) > 1 or (not years and not re.search(r"最新|当前|现在|截至目前|今年", question_text)):
        return result
    try:
        search = requests.post(f"{API_BASE_URL}/api/official-documents/search", json=payload, headers=_auth_headers(), timeout=45)
        if search.status_code != 200:
            return result
        evidence = search.json()
    except (requests.RequestException, ValueError):
        # 原文检索失联不影响既有官方问答的安全拒答。
        return result
    if evidence.get("status") == "evidence_retrieved":
        return evidence
    # 保留官方覆盖状态，让用户知道是“当前资料缺口”，不是系统只允许 2025 年。
    if evidence.get("coverage") or result.get("official_coverage"):
        return {**result, "official_coverage": evidence.get("coverage") or result.get("official_coverage"),
                "answer": evidence.get("answer") or result.get("answer"),
                "limitations": evidence.get("limitations") or result.get("limitations")}
    return result


def _discover_company_request(query: str, market: str | None = None) -> dict[str, Any]:
    """聊天中的未知公司显式走候选源；候选结果仍不能提升为官方年报证据。"""
    response = requests.post(
        f"{API_BASE_URL}/api/company-discovery",
        json={"query": query, "market": market, "use_external_sources": True},
        headers=_auth_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _issuer_answer_request(payload: dict[str, str]) -> dict[str, Any]:
    response = requests.post(f"{API_BASE_URL}/api/issuer-answers", json=payload, headers=_auth_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def _industry_answer_request(payload: dict[str, str]) -> dict[str, Any]:
    response = requests.post(f"{API_BASE_URL}/api/industry-observations", json=payload, headers=_auth_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def _answer_request(payload: dict[str, str]) -> dict[str, Any]:
    """保留问答端点返回的拒答状态，而不是把 422 当成无内容异常。"""
    response = requests.post(f"{API_BASE_URL}/api/answers", json=payload, headers=_auth_headers(), timeout=30)
    if response.status_code == 422:
        body = response.json()
        if isinstance(body, dict) and body.get("status") == "out_of_scope":
            return body
    response.raise_for_status()
    return response.json()


def _create_chat_job(payload: dict[str, str]) -> dict[str, str]:
    job_id, note = _submit_job(payload)
    if not job_id:
        raise ValueError(note or "提交任务失败，请稍后重试。")
    return {"job_id": job_id, "status": "queued", "message": note or "报告任务已提交，正在显示七步进度。"}


def _show_chat_answer(answer: dict[str, Any], message_index: int) -> None:
    status = answer.get("status") or "error"
    meta = []
    if answer.get("intent_label"):
        meta.append(f"意图：{answer['intent_label']}")
    if answer.get("source_label"):
        meta.append(f"来源：{answer['source_label']}")
    if answer.get("evidence_label"):
        meta.append(f"状态：{answer['evidence_label']}")
    if answer.get("latency_ms") is not None:
        meta.append(f"耗时：{answer['latency_ms']} ms")
    if answer.get("compliance"):
        meta.append(f"合规：{answer['compliance']}")
    if meta:
        st.caption(" · ".join(meta))
    if status == "evidence_retrieved":
        st.caption("检索式摘录，不是经过字段锚点核验的数值结论。")
    if status in {"refused", "out_of_scope", "insufficient_evidence"}:
        st.caption("证据不足或超出回答范围，已拒绝推测。")
    if answer.get("error_code"):
        st.caption(f"错误码：{answer['error_code']}")
    _show_company_candidates(answer, message_index)
    coverage = answer.get("official_coverage") or answer.get("coverage")
    if isinstance(coverage, dict) and coverage.get("indexed_years"):
        with st.expander("查看官方资料覆盖状态", expanded=False):
            st.caption("已入库的一手官方资料期间：" + "、".join(str(item) for item in coverage["indexed_years"]))
            if coverage.get("annual_report_year"):
                st.caption(f"当前年度年报锚点：{coverage['annual_report_year']}；其他期间不会用年报替代。")
            for document in (coverage.get("documents") or [])[:5]:
                title = document.get("title") or document.get("filing_form") or "官方资料"
                st.caption(f"{document.get('year') or document.get('report_date') or '未知期间'} · {title} · {document.get('source') or '官方来源'}")
                if document.get("source_url"):
                    st.link_button("打开官方原文", document["source_url"], key=f"coverage_{message_index}_{document.get('document_id')}")

    candidate_data = answer.get("candidate_data") or {}
    if isinstance(candidate_data, dict) and candidate_data.get("data_available"):
        rows = candidate_data.get("candidates") or candidate_data.get("observations") or []
        with st.expander("查看第三方原始字段（未核验年报）", expanded=False):
            st.warning("以下来自结构化数据源候选，不等同于官方年报事实。")
            for row in rows[:8]:
                st.write(row)
    sections = answer.get("sections") or {}
    if isinstance(sections, dict):
        metrics = sections.get("core_metrics") or []
        highlights = sections.get("highlights") or []
        risks = sections.get("risks") or []
        if metrics or highlights or risks:
            with st.expander("结构化研究结果", expanded=True):
                if metrics:
                    st.markdown("**核心指标**")
                    for metric in metrics:
                        if isinstance(metric, dict):
                            st.write(f"{metric.get('ticker', '')}：{metric.get('statement', '')}")
                        else:
                            st.write(str(metric))
                if highlights:
                    st.markdown("**亮点**")
                    for item in highlights:
                        st.write(f"- {item}")
                if risks:
                    st.markdown("**风险与缺口**")
                    for item in risks:
                        st.write(f"- {item}")
    refs = answer.get("evidence_refs") or answer.get("sources") or []
    limitations = answer.get("limitations") or []
    if refs or answer.get("evidence") or limitations:
        with st.expander("查看依据与限制", expanded=False):
            for ref in refs:
                metadata = ref.get("metadata") or ref.get("identity") or ref
                page = metadata.get("page")
                location = f"{metadata.get('file_name') or ref.get('ref') or ref.get('source_id')}"
                if page:
                    authority = "本地转换页码（非官方页码）" if metadata.get("page_authority") == "generated" else "页码"
                    location += f"，{authority} {page}"
                if metadata.get("published_at"):
                    location += f"，发布日期 {metadata['published_at']}"
                if metadata.get("retrieved_at"):
                    location += f"，抓取时间 {metadata['retrieved_at']}"
                st.caption(f"来源：{ref.get('label') or ref.get('citation') or ref.get('source_id') or ref.get('ref')}; {location}")
                if metadata.get("source_sha256"):
                    st.caption(f"文件 SHA256：{metadata['source_sha256']}")
                elif metadata.get("unverified_reason"):
                    st.caption(f"未核验说明：{metadata['unverified_reason']}")
                url = metadata.get("source_url") or metadata.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    st.link_button("打开官方原文", url, key=f"source_{message_index}_{ref.get('citation') or ref.get('ref') or ref.get('source_id')}_{metadata.get('file_name')}")
                if ref.get("excerpt"):
                    st.code(str(ref["excerpt"]))
            if answer.get("evidence"):
                st.code(str(answer["evidence"].get("excerpt") if isinstance(answer["evidence"], dict) else answer["evidence"]))
            for limitation in limitations:
                st.caption(f"限制：{limitation}")


#: 证据状态 → 面向用户的标签；与后端状态机一一对应。
_EVIDENCE_LABELS = {
    "verified_field_available": "已核验",
    "onboarded_material_only": "资料已接入，字段待核验",
    "not_onboarded": "待接入",
    "onboarded": "已收录",
    "matched": "已识别",
    "ambiguous": "待确认市场",
    "unresolved": "未识别",
    "source_unavailable": "来源不可用",
}


def _show_company_candidates(answer: dict[str, Any], message_index: int) -> None:
    """简洁展示候选，并把市场选择接回原问题，不让用户重新输入整句。"""
    candidates = answer.get("candidates") or []
    if not candidates:
        return
    if answer.get("status") == "ambiguous":
        st.caption("请选择市场，选择后会继续处理刚才的问题：")
        for index, candidate in enumerate(candidates):
            market = str(candidate.get("market") or "")
            market_label = {"HK": "港股", "A": "A股", "US": "美股"}.get(market, market)
            ticker = str(candidate.get("ticker") or "")
            company = str(candidate.get("company") or ticker)
            if st.button(f"使用 {market_label} · {ticker}", key=f"candidate_{message_index}_{index}", use_container_width=True):
                # 走同一条会话提交链路，市场选择也必须落库，不能绕过会话状态。
                _submit_prompt(market_label)
        return
    st.caption("公司候选：")
    for candidate in candidates:
        verified = bool(candidate.get("verified"))
        level = "已核验身份" if verified else "候选"
        st.caption(f"· {candidate.get('company') or candidate.get('ticker')} · {candidate.get('ticker')} · {level}")
    warnings = answer.get("warnings") or []
    if warnings:
        with st.expander("查看识别诊断", expanded=False):
            for warning in warnings:
                st.caption(str(warning))


def _show_source_health() -> None:
    """来源健康与工具目录：只读展示，未真实检查的来源保持 unknown。"""
    with st.expander("数据源健康与工具边界", expanded=False):
        try:
            health = _api_get("/api/source-health")
        except requests.RequestException:
            st.caption("暂时无法取得来源健康快照。")
        else:
            for row in health.get("sources", []):
                st.caption(
                    f"· {row.get('source')} · {row.get('status')} · "
                    f"最近成功 {row.get('last_success_at') or '从未'} · "
                    f"最近错误 {row.get('last_error_code') or '无'}"
                )
        try:
            tools = _api_get("/api/research-tools")
        except requests.RequestException:
            return
        st.caption(
            f"白名单工具 {len(tools.get('tools', []))} 个；"
            f"MCP 暴露：{'是' if tools.get('mcp_exposed') else '否'}"
            + (f"（{tools['mcp_deferred_reason']}）" if tools.get("mcp_deferred_reason") else "")
        )


def _show_quick_questions() -> None:
    """在空会话中提供少量可直接提交的常见问题。"""
    st.markdown("**常见问题**")
    st.caption("点击后直接发送到当前会话")
    prompts = (
        "腾讯最新官方资料有哪些？",
        "腾讯 2025 年收入和净利润怎么样？",
        "我想查看港股公司的最新公告",
        "如何查询已接入的官方年报？",
        "我偏保守，主要看港股",
    )
    columns = st.columns(2)
    for index, prompt in enumerate(prompts):
        with columns[index % 2]:
            if st.button(prompt, key=f"quick_question_{index}", use_container_width=True):
                st.session_state.quick_prompt = prompt
                st.rerun()


def _show_chat() -> None:
    context = st.session_state.chat_context
    discovery_market = context.get("discovery_market")
    discovery_market_label = {"HK": "港股", "A": "A股", "US": "美股"}.get(str(discovery_market), discovery_market or "待确认市场")
    scope = (f"当前报告 · {context['ticker']}" if context["report_id"] else
             f"正在研究 · {context['ticker']}" if context["job_id"] else
             f"当前资料 · {context['knowledge_ticker']} 官方资料（期间见证据）" if context.get("knowledge_ticker") else
             f"已关联标的 · {context['discovery_ticker']} · {discovery_market_label}（候选/待核验）" if context.get("discovery_ticker") else
             "当前资料 · 网易 2025 年 SEC 年报" if context.get("general_scope") == "issuer" else
             "当前资料 · 2026 年行业观察" if context.get("general_scope") == "industry" else None)
    if scope:
        st.markdown(f'<div class="ia-chat-context">{html.escape(scope)}</div>', unsafe_allow_html=True)
    if not context["messages"]:
        with st.chat_message("assistant"):
            st.write("你好，我是投研助手。可以查询已收录年报事实、获取年报摘要、做行业观察或生成研究报告。")
            st.caption("例如：腾讯最新官方资料有哪些？腾讯 2025 年收入怎么样？今年可以关注什么行业？")
            st.caption("未收录或证据失效时，我会说明资料缺口，不用旧年度替代当前期间。")
            st.caption("也可以说：分析 AAPL 服务业务和现金流风险。")
            with st.expander("常见问题", expanded=False):
                _show_quick_questions()
            with st.expander("查看已收录资料范围", expanded=False):
                try:
                    coverage = _api_get("/api/company-evidence-state")
                    for item in coverage.get("companies", []):
                        state = _EVIDENCE_LABELS.get(str(item.get("evidence_state")), str(item.get("evidence_state")))
                        st.caption(f"{item['company']} · {item['ticker']} · {item['year'] or '期间未核验'} · {state}")
                    st.caption("仅列出资料登记范围，不代表所有字段已能回答；未收录需经预审来源接入。")
                except requests.RequestException:
                    st.caption("暂时无法取得覆盖清单，请检查后端连接。")
            _show_source_health()
    for message_index, message in enumerate(context["messages"]):
        with st.chat_message(message["role"]):
            st.write(message["text"])
            if message.get("status") in {"processing", "interrupted"}:
                st.caption("这条请求尚未完成。如处理已中断，请重新发送问题；不会自动复用旧回答。")
            if message.get("answer"):
                _show_chat_answer(message["answer"], message_index)

    _render_turn_outcome()

    @st.fragment(run_every=2 if context["job_id"] and context["job_status"] not in JOB_TERMINAL_STATUSES else None)
    def show_job() -> None:
        current = st.session_state.chat_context
        if not current["job_id"]:
            return
        try:
            job = _api_get(f"/api/report-jobs/{current['job_id']}")
        except requests.RequestException:
            st.warning("暂时无法查询任务状态；任务编号已保留，可以稍后重试或在侧栏找回。")
            return
        if job.get("job_id") != current["job_id"]:
            return
        updated = update_job(current, job)
        _render_job_progress(job)
        if updated != current:
            st.session_state.chat_context = updated
            if updated["report_id"] and updated["report_id"] != current["report_id"]:
                st.query_params.clear()
                st.query_params["report"] = updated["report_id"]
            if job["status"] in JOB_TERMINAL_STATUSES:
                st.rerun()

    show_job()
    _render_pending_profile_confirmation()

    quick_prompt = st.session_state.pop("quick_prompt", None)
    if quick_prompt:
        _submit_prompt(quick_prompt)
        return
    active = context["job_id"] and context["job_status"] not in JOB_TERMINAL_STATUSES
    placeholder = ("问第三方结构化数据，如阿里巴巴 2025 年收入…" if st.session_state.answer_mode == "mcp"
                   else "问官方资料，如腾讯最新官方资料或 2025 年收入…")
    prompt = st.chat_input(placeholder, key="research_chat_input", max_chars=500, disabled=bool(active))
    if prompt:
        _submit_prompt(prompt)


def _render_pending_profile_confirmation() -> None:
    """把待确认的偏好候选显示成可点击的确认/取消，而不是让用户猜一句"是"指什么。"""
    try:
        candidates = [c for c in _api_get("/api/user-profile").get("pending_candidates", [])
                      if c.get("conversation_id") == st.session_state.get("conversation_id")]
        st.session_state.pending_profile_candidates = candidates
    except requests.RequestException:
        candidates = st.session_state.get("pending_profile_candidates") or []
    if not candidates:
        return
    st.markdown("---")
    st.markdown("**待确认的研究偏好**")
    for candidate in candidates:
        if candidate.get("op") == "delete":
            st.caption(f"将删除：{FIELD_DISPLAY.get(candidate['field'], candidate['field'])}（当前值 {profile_value_label(candidate['field'], candidate.get('previous_value'))}）")
        elif candidate.get("previous_value"):
            st.caption(f"{FIELD_DISPLAY.get(candidate['field'], candidate['field'])}：{profile_value_label(candidate['field'], candidate['previous_value'])} → {profile_value_label(candidate['field'], candidate['proposed_value'])}")
        else:
            st.caption(f"{FIELD_DISPLAY.get(candidate['field'], candidate['field'])}：{profile_value_label(candidate['field'], candidate['proposed_value'])}")
    st.caption("确认后才会写入长期偏好；这些是研究偏好，不是投资建议。")
    column_confirm, column_reject = st.columns(2)
    if column_confirm.button("确认保存", key="profile_confirm", use_container_width=True, type="primary"):
        _resolve_profile_candidates(accept=True)
    if column_reject.button("取消", key="profile_reject", use_container_width=True):
        _resolve_profile_candidates(accept=False)


def _resolve_profile_candidates(*, accept: bool) -> None:
    """按candidate_id + version 确认或拒绝；不能靠一句"是"确认别的操作。"""
    candidates = list(st.session_state.get("pending_profile_candidates") or [])
    for candidate in candidates:
        action = "confirm" if accept else "reject"
        try:
            _api_post(f"/api/user-profile/candidates/{candidate['candidate_id']}/{action}",
                      {"version": candidate.get("version")})
        except requests.RequestException as exc:
            st.error(_friendly_error(exc, "偏好操作失败。"))
    st.session_state.pending_profile_candidates = []
    st.rerun()


def _submit_prompt(prompt: str) -> None:
    """提交一轮：先立即显示用户消息与真实处理中提示，再调用受控能力。

    * request_id 按 (会话, 序号) 生成，重复提交由后端去重，不会产生第二条消息。
    * 失败也落成可理解的中文结果 + 重试入口，不留无限旋转的转圈。
    """
    conversation_id = st.session_state.get("conversation_id")
    if not conversation_id:
        st.error("当前没有可用会话，请点击“新对话”重试。")
        return
    with st.chat_message("user"):
        st.write(prompt)
    # 只有真正进入这一阶段才显示对应提示；MCP 模式说明正在识别公司与查第三方数据。
    status_line = ("正在识别公司并查询第三方数据…" if st.session_state.answer_mode == "mcp"
                   else "正在处理你的问题…")
    with st.chat_message("assistant"):
        with st.status(status_line, expanded=True) as status_area:
            request_id = f"ui-{conversation_id}-{uuid.uuid4().hex[:16]}"
            result = _conversation_request(conversation_id, prompt, request_id=request_id,
                                          answer_mode=st.session_state.answer_mode)
            status_area.update(label="请求未完成" if result.get("status") in {"failed", "persist_failed", "request_in_progress", "conversation_busy"} else "已完成",
                               state="error" if result.get("status") in {"failed", "persist_failed"} else "complete", expanded=False)
    _apply_turn_result(conversation_id, prompt, result)


def _apply_turn_result(conversation_id: str, prompt: str, result: dict[str, Any]) -> None:
    """把后端结果落到界面状态；失败时给重试入口，成功时刷新会话上下文。

    注意：结尾会 ``st.rerun()``，所以失败提示与重试所需信息必须先写进
    ``st.session_state``；直接 ``st.warning(...)`` 会在 rerun 后消失，
    用户只会看到"什么都没发生"。
    """
    status = str(result.get("status") or "")
    message = result.get("message")
    st.session_state.pop("turn_failure", None)
    if st.session_state.get("conversation_id") != conversation_id:
        # 用户已换会话，迟到回复只留在服务端原会话，绝不覆盖当前页面。
        st.rerun()
    if status == "duplicate_request":
        st.session_state.turn_notice = "该请求已处理，未重复提交。"
    elif status in {"failed", "persist_failed", "conversation_busy", "request_conflict", "request_in_progress"} or not message:
        st.session_state.turn_failure = {
            "prompt": prompt,
            "message": result.get("message") or "请求未完成，请重试。",
            "retryable": bool(result.get("retryable", True)),
        }
    else:
        with st.chat_message("assistant"):
            st.write(message)
            answer = result.get("answer")
            if isinstance(answer, dict):
                _show_chat_answer(answer, len(st.session_state.chat_context.get("messages") or []))
            if result.get("effective_question"):
                st.caption(f"本轮有效查询：{result['effective_question']}")
            if result.get("answer_mode"):
                st.caption(f"本轮数据模式：{'官方资料' if result['answer_mode'] == 'official' else '第三方数据'}")
        if status == "needs_confirmation":
            st.session_state.pending_profile_candidates = result.get("candidates") or []
        if result.get("summary") == "degraded":
            st.caption("历史摘要本次未更新（降级），上一份有效摘要保持不变；不影响本轮回答。")
    # 重新拉取权威状态：会话状态由服务端持久化，前端不自己拼。
    try:
        detail = _api_get(f"/api/conversations/{conversation_id}")
        restored = _api_get(f"/api/conversations/{conversation_id}/messages")
    except requests.RequestException:
        st.session_state.chat_context["messages"] = [
            *st.session_state.chat_context.get("messages", []),
            {"role": "user", "text": prompt},
            *([{"role": "assistant", "text": message, "answer": result.get("answer")}] if message else []),
        ]
    else:
        st.session_state.chat_context = {**new_context(), **(detail.get("state") or {}),
                                         "conversation_id": conversation_id}
        st.session_state.chat_context["messages"] = [
            {"role": item["role"], "text": item["text"], "answer": item.get("answer"), "status": item.get("status")}
            for item in restored.get("messages", [])
        ]
    st.query_params["conversation"] = conversation_id
    st.rerun()


def _render_turn_outcome() -> None:
    """在 rerun 之后重新呈现上一轮的成功/失败结果。

    失败必须有中文说明 + 重试入口，且不能靠无限旋转表达"处理中"。
    """
    notice = st.session_state.pop("turn_notice", None)
    if notice:
        st.info(notice)
    failure = st.session_state.get("turn_failure")
    if not failure:
        return
    with st.chat_message("assistant"):
        st.warning(failure["message"])
        if failure.get("retryable", True):
            if st.button("重试本轮", key="retry_turn", use_container_width=True):
                st.session_state.pop("turn_failure", None)
                _submit_prompt(str(failure["prompt"]))



if "current_report" not in st.session_state:
    st.session_state.current_report = None
if "chat_context" not in st.session_state:
    st.session_state.chat_context = new_context()
if "answer_mode" not in st.session_state:
    st.session_state.answer_mode = "official"
# 会话列表状态：当前会话 ID、待确认的偏好候选、待重命名/删除的目标。
st.session_state.setdefault("conversation_id", None)
st.session_state.setdefault("conversation_offset", 0)
st.session_state.setdefault("pending_profile_candidates", [])
st.session_state.setdefault("rename_target", None)
st.session_state.setdefault("delete_target", None)
st.session_state.setdefault("profile_open", False)
st.session_state.setdefault("in_flight", None)


def _ensure_conversation() -> str | None:
    """确保有一个可用会话；没有就新建。

    刷新后用**会话 ID**恢复，不在 URL 或浏览器缓存里保存令牌与偏好正文。
    """
    existing = st.session_state.get("conversation_id")
    if existing:
        return str(existing)
    linked = st.query_params.get("conversation")
    if linked:
        try:
            detail = _api_get(f"/api/conversations/{linked}")
            messages = _api_get(f"/api/conversations/{linked}/messages")
        except requests.RequestException:
            st.query_params.pop("conversation", None)
            st.sidebar.warning("此会话已删除或无权访问，请从列表重新选择。")
        else:
            st.session_state.conversation_id = linked
            st.session_state.answer_mode = detail.get("answer_mode", "official")
            st.session_state.previous_answer_mode = st.session_state.answer_mode
            st.session_state.chat_context = {**new_context(), **(detail.get("state") or {}), "conversation_id": linked,
                "messages": [{"role": m["role"], "text": m["text"], "answer": m.get("answer"), "status": m.get("status")}
                             for m in messages.get("messages", [])]}
            return str(linked)
    try:
        created = _api_post("/api/conversations",
                             {"title": "", "answer_mode": st.session_state.answer_mode})
    except requests.RequestException as exc:
        st.sidebar.error(_friendly_error(exc, "无法创建会话，请检查后端连接。"))
        return None
    st.session_state.conversation_id = created["conversation_id"]
    st.session_state.conversation_offset = 0
    st.query_params["conversation"] = created["conversation_id"]
    return str(created["conversation_id"])


def _switch_conversation(conversation_id: str) -> None:
    """切换会话：按 ID 重新拉取状态与消息，经服务端权限校验后才显示。"""
    try:
        detail = _api_get(f"/api/conversations/{conversation_id}")
        messages = _api_get(f"/api/conversations/{conversation_id}/messages")
    except requests.RequestException as exc:
        st.sidebar.error(_friendly_error(exc, "无法打开该会话。"))
        return
    st.session_state.conversation_id = conversation_id
    st.session_state.answer_mode = detail.get("answer_mode", "official")
    st.session_state.previous_answer_mode = st.session_state.answer_mode
    st.session_state.chat_context = new_context()
    st.session_state.chat_context.update(detail.get("state") or {})
    st.session_state.chat_context["messages"] = [
        {"role": item["role"], "text": item["text"], "answer": item.get("answer")}
        for item in messages.get("messages", [])
    ]
    st.session_state.chat_context["conversation_id"] = conversation_id
    st.session_state.pending_profile_candidates = []
    st.session_state.current_report = None
    st.session_state.in_flight = None
    st.session_state.pop("turn_failure", None)
    st.session_state.pop("turn_notice", None)
    st.query_params.clear()
    st.query_params["conversation"] = conversation_id
    st.rerun()


_ensure_conversation()

# 会话 ID 经权限校验后恢复历史；URL 只保存 ID，不保存登录凭证或偏好正文。
report_id = st.query_params.get("report")
job_id = st.query_params.get("job")
if report_id and st.session_state.chat_context["report_id"] != report_id:
    try:
        report = _api_get(f"/api/reports/{report_id}")
        st.session_state.current_report = report
        st.session_state.chat_context = select_report(st.session_state.chat_context, report)
    except requests.RequestException:
        st.info("该报告未发布、已撤回或不可访问；未释放报告正文。")
elif job_id and st.session_state.chat_context["job_id"] != job_id:
    try:
        job = _api_get(f"/api/report-jobs/{job_id}")
        st.session_state.chat_context = update_job(select_job(st.session_state.chat_context, job), job)
        st.session_state.current_report = None
    except requests.RequestException:
        st.warning("指定任务不存在或暂时无法查询。")

st.title("投研助手")

with st.sidebar:
    st.markdown('<div class="ia-sidebar-brand">投研助手</div>', unsafe_allow_html=True)
    if st.button("新对话", use_container_width=True, type="primary"):
        try:
            created = _api_post("/api/conversations",
                                 {"title": "", "answer_mode": st.session_state.answer_mode})
        except requests.RequestException as exc:
            st.error(_friendly_error(exc, "无法创建新对话。"))
        else:
            st.session_state.conversation_id = created["conversation_id"]
            st.session_state.conversation_offset = 0
            st.session_state.chat_context = new_context()
            st.session_state.chat_context["conversation_id"] = created["conversation_id"]
            st.query_params["conversation"] = created["conversation_id"]
            st.session_state.pending_profile_candidates = []
            st.session_state.current_report = None
            st.session_state.in_flight = None
            st.session_state.pop("turn_failure", None)
            st.query_params.clear()
            st.query_params["conversation"] = created["conversation_id"]
            st.rerun()

    # --- 最近会话列表：每行使用省略号菜单，不再占用独立管理面板 ---
    with st.expander("最近会话", expanded=True):
        st.markdown("**最近会话**")
        listing = _load_conversation_list(limit=10, offset=int(st.session_state.conversation_offset))
        if listing.get("error"):
            st.caption(listing["error"])
        target_options = {item["conversation_id"]: (item["title"] or "未命名会话")
                          for item in listing.get("conversations", [])}
        for item in listing.get("conversations", []):
            conversation_id = item["conversation_id"]
            is_current = conversation_id == st.session_state.get("conversation_id")
            row, menu = st.columns([8, 1], gap="small")
            with row:
                label = f"{'● ' if is_current else ''}{item['title'] or '未命名会话'}"
                if st.button(label, key=f"conv_{conversation_id}", use_container_width=True,
                             type="primary" if is_current else "secondary"):
                    _switch_conversation(conversation_id)
            with menu:
                with st.popover("⋯", use_container_width=True):
                    st.caption(item["title"] or "未命名会话")
                    if st.button("重命名", key=f"rename_{conversation_id}", use_container_width=True):
                        st.session_state.rename_target = conversation_id
                        st.rerun()
                    if st.button("删除会话", key=f"delete_{conversation_id}", use_container_width=True):
                        st.session_state.delete_target = conversation_id
                        st.rerun()
            updated = str(item.get("updated_at") or "")[:16].replace("T", " ")
            st.caption(f"　{updated}")
        if listing.get("has_more"):
            if st.button("加载更多", key="conv_more", use_container_width=True):
                st.session_state.conversation_offset = int(st.session_state.conversation_offset) + 10
                st.rerun()
        if st.session_state.get("rename_target") in target_options:
            new_title = st.text_input("会话新名称", key="rename_value",
                                      value=target_options[st.session_state.rename_target])
            if st.button("保存名称", key="rename_save", use_container_width=True):
                try:
                    _api_post(f"/api/conversations/{st.session_state.rename_target}/rename",
                              {"title": new_title})
                except requests.RequestException as exc:
                    st.error(_friendly_error(exc, "重命名失败。"))
                else:
                    st.session_state.rename_target = None
                    st.rerun()
            if st.button("取消重命名", key="rename_cancel", use_container_width=True):
                st.session_state.rename_target = None
                st.rerun()
        if st.session_state.get("delete_target") in target_options:
            st.warning(f"确认删除「{target_options[st.session_state.delete_target]}」？该会话的消息、状态与摘要都会删除，长期偏好会保留。")
            if st.button("确认删除", key="delete_confirm", use_container_width=True):
                try:
                    _api_delete(f"/api/conversations/{st.session_state.delete_target}")
                except requests.RequestException as exc:
                    st.error(_friendly_error(exc, "删除失败。"))
                else:
                    st.session_state.delete_target = None
                    st.session_state.conversation_id = None
                    st.query_params.clear()
                    st.session_state.chat_context = new_context()
                    st.session_state.in_flight = None
                    st.rerun()
            if st.button("取消删除", key="delete_cancel", use_container_width=True):
                st.session_state.delete_target = None
                st.rerun()

    st.divider()
    chosen_mode = st.radio("数据模式", ["official", "mcp"],
                           format_func=lambda mode: "官方资料（年报/公告）" if mode == "official" else "第三方数据（MCP）",
                           key="answer_mode")
    if st.session_state.get("previous_answer_mode", chosen_mode) != chosen_mode:
        # 切换模式**不删消息**：只清不适用证据状态，并让本轮回答标明模式。
        current_id = st.session_state.get("conversation_id")
        if current_id:
            try:
                _api_post(f"/api/conversations/{current_id}/answer-mode",
                          {"answer_mode": chosen_mode})
            except requests.RequestException as exc:
                st.error(_friendly_error(exc, "切换数据模式失败。"))
        st.session_state.chat_context = {**st.session_state.chat_context,
                                         "evidence_scope": None, "answer_mode": chosen_mode}
        st.session_state.current_report = None
        st.query_params.clear()
        if current_id:
            st.query_params["conversation"] = current_id
    st.session_state.previous_answer_mode = chosen_mode

    # --- 研究偏好入口（小入口，不恢复已删除的面板） ---
    st.divider()
    if st.button("研究偏好", key="profile_toggle", use_container_width=True):
        st.session_state.profile_open = not st.session_state.get("profile_open")
        st.rerun()
    if st.session_state.get("profile_open"):
        try:
            profile_payload = _api_get("/api/user-profile")
        except requests.RequestException as exc:
            st.error(_friendly_error(exc, "无法读取研究偏好。"))
        else:
            profile = profile_payload.get("profile") or {}
            if not profile:
                st.caption("暂无已确认的长期偏好。可以说“我偏保守，主要看港股”，我会先请你确认。")
            for field, entry in profile.items():
                label = next((item["label"] for item in profile_payload.get("fields", [])
                              if item["field"] == field), field)
                st.caption(f"{label}：{profile_value_label(field, entry['value'])}")
                if st.button(f"删除 {label}", key=f"prof_del_{field}", use_container_width=True):
                    try:
                        st.session_state.profile_delete_target = field
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(_friendly_error(exc, "删除偏好失败。"))
                    else:
                        st.rerun()
            if profile:
                st.caption("画像是研究偏好，不是投资适当性结论。")
                if st.button("删除全部偏好", key="prof_del_all", use_container_width=True):
                    try:
                        st.session_state.profile_delete_target = "all"
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(_friendly_error(exc, "删除偏好失败。"))
                    else:
                        st.rerun()

    if st.session_state.get("profile_delete_target"):
        target = st.session_state.profile_delete_target
        st.warning("确认删除全部长期偏好？" if target == "all" else "确认删除这项长期偏好？")
        if st.button("确认删除偏好", key="profile_delete_confirm"):
            try:
                _api_delete("/api/user-profile" if target == "all" else f"/api/user-profile/{target}")
            except requests.RequestException as exc:
                st.error(_friendly_error(exc, "删除失败。"))
            else:
                st.session_state.profile_delete_target = None
                st.session_state.pending_profile_candidates = []
                st.rerun()
        if st.button("取消删除偏好", key="profile_delete_cancel"):
            st.session_state.profile_delete_target = None
            st.rerun()
    st.divider()
    if st.button("退出登录", use_container_width=True):
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()

# 每次重绘都向服务端复核；撤回/漂移后先清空旧正文和会话回答缓存，再渲染对话。
active_report_id = st.session_state.chat_context.get("report_id")
if active_report_id:
    try:
        fresh = _api_get(f"/api/reports/{active_report_id}")
        st.session_state.current_report = fresh
    except requests.RequestException:
        st.session_state.current_report = None
        st.session_state.chat_context["messages"] = []
        st.warning("报告尚未发布、已撤回、版本失效或不可访问；本会话已清除旧正文与回答。")

_show_chat()
context = st.session_state.chat_context
if context["report_id"] and (not st.session_state.current_report or st.session_state.current_report.get("id") != context["report_id"]):
    try:
        st.session_state.current_report = _api_get(f"/api/reports/{context['report_id']}")
    except requests.RequestException:
        st.session_state.current_report = None
        st.session_state.chat_context["messages"] = []
        st.info("报告待审、已撤回或访问受限；不展示正文或旧回答。")
if st.session_state.current_report and st.session_state.current_report.get("id") == context["report_id"]:
    with st.expander("查看当前报告全文、证据与风险", expanded=False):
        _show_report(st.session_state.current_report)
