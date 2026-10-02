"""Streamlit presentation layer for the Investment Assistant API."""

from __future__ import annotations

from datetime import UTC, datetime
import html
import json
import os
from typing import Any

import requests
import streamlit as st
import yfinance as yf

from investment_assistant.chat_session import (
    dispatch_message, new_context, record_error, select_job, select_report, select_knowledge_corpus, update_job,
)

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
  --canvas: #eef3f8;
  --surface: #ffffff;
  --line: #d9e3ee;
  --red: #c53c4a;
  --red-soft: #fff0f1;
  --green: #16805b;
  --green-soft: #eaf8f1;
  --amber: #9a6508;
  --amber-soft: #fff7e7;
  --shadow: 0 12px 32px rgba(18, 48, 80, .09);
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
[data-testid="stSidebarCollapseButton"] > button { align-items: center !important; background: rgba(255,255,255,.11) !important; border: 1px solid rgba(255,255,255,.28) !important; border-radius: 7px !important; color: #e7f1fb !important; display: inline-flex !important; height: 30px !important; justify-content: center !important; width: 30px !important; }
header[data-testid="stHeader"] [data-testid="stExpandSidebarButton"]:hover { background: #edf4fb !important; border-color: #8eafd0 !important; }
[data-testid="stSidebarCollapseButton"] > button:hover { background: rgba(255,255,255,.2) !important; border-color: rgba(255,255,255,.52) !important; }
[data-testid="stMainBlockContainer"] { max-width: 1360px; padding: 2.1rem 2rem 4rem; }
[data-testid="stSidebar"] { background: #102e50; }
[data-testid="stSidebar"] [data-testid="stSidebarContent"] { color: #f4f8fc; }
[data-testid="stSidebar"] .stButton button { background: #1e466f !important; border-color: #6484a3 !important; color: #f4f8fc !important; }
[data-testid="stSidebar"] .stButton button:hover { background: #2b5d8f !important; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3, [data-testid="stSidebar"] p, [data-testid="stSidebar"] [data-testid="stWidgetLabel"], [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] { color: #f4f8fc !important; }
/* Streamlit 1.5x \u4f7f\u7528 react-aria ComboBox\uff1b\u76f4\u63a5\u4e3a\u5185\u5c42 role=group \u63d0\u4f9b\u6df1\u8272\u5bb9\u5668\u4e0e\u8fb9\u6846\u3002 */
[data-testid="stSidebar"] [data-testid="stSelectbox"] > div.react-aria-ComboBox > div[role="group"] { background: rgba(255,255,255,.11) !important; border: 1px solid rgba(255,255,255,.34) !important; border-radius: 9px !important; box-shadow: inset 0 1px 0 rgba(255,255,255,.08) !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] input[role="combobox"] { color: #f8fbff !important; font-weight: 600 !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] input[role="combobox"]::placeholder { color: #c3d4e5 !important; }
[data-testid="stSidebar"] [data-testid="stSelectbox"] button[aria-haspopup="listbox"], [data-testid="stSidebar"] [data-testid="stSelectbox"] button[aria-haspopup="listbox"] svg { color: #dceafa !important; }
/* \u9009\u9879\u5f39\u5c42\u6302\u8f7d\u5728 body \u5c42\uff0c\u4ee5 role=listbox \u548c react-aria \u5c5e\u6027\u4f5c\u5b9a\u4f4d\u3002 */
body [role="listbox"][data-rac], body [role="listbox"] { background: #17395e !important; border: 1px solid #537da7 !important; box-shadow: 0 14px 28px rgba(8, 27, 49, .28) !important; color: #f8fbff !important; }
body [role="listbox"] [role="option"] { color: #f8fbff !important; }
body [role="listbox"] [role="option"][data-focused], body [role="listbox"] [role="option"]:hover { background: rgba(255,255,255,.14) !important; color: #ffffff !important; }
h1 { color: var(--navy-deep) !important; font-size: clamp(2rem, 4vw, 3.1rem) !important; letter-spacing: -.055em; margin: .1rem 0 .45rem !important; }
h2, h3 { color: var(--navy-deep) !important; }
.ia-topline { align-items: center; color: var(--blue); display: flex; font-size: .75rem; font-weight: 760; gap: .52rem; letter-spacing: .09em; }
.ia-live-dot { background: #20a36c; border-radius: 50%; box-shadow: 0 0 0 5px rgba(32,163,108,.12); height: 8px; width: 8px; }
.ia-hero { background: var(--navy-deep); border: 1px solid rgba(255,255,255,.12); border-radius: 18px; box-shadow: 0 18px 38px rgba(13,41,72,.18); color: #f7fbff; margin: 1rem 0 1.3rem; overflow: hidden; padding: 1.45rem 1.65rem; position: relative; }
.ia-hero:after { border: 1px solid rgba(117,177,230,.35); border-radius: 50%; content: ""; height: 240px; position: absolute; right: -80px; top: -140px; width: 240px; }
.ia-hero-title { font-size: 1.18rem; font-weight: 700; line-height: 1.7; max-width: 800px; position: relative; z-index: 1; }
.ia-hero-copy { color: #bfd5ea; font-size: .88rem; line-height: 1.7; margin-top: .42rem; position: relative; z-index: 1; }
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
[data-testid="stTextInput"] input { border-radius: 10px !important; font-size: 1.08rem !important; font-weight: 650 !important; min-height: 48px; }
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
/* 对话沿用现有海军蓝/浅灰视觉，只强调绑定的研究范围。 */
.ia-chat-context { background: #e7f0f8; border-left: 4px solid var(--navy); border-radius: 7px; color: var(--navy-deep); line-height: 1.6; margin: .8rem 0; padding: .75rem 1rem; overflow-wrap: anywhere; }
[data-testid="stChatMessage"] { border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
[data-testid="stChatInput"] textarea:focus-visible, button:focus-visible { outline: 2px solid #2b5d8f !important; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; } }
@media (max-width: 800px) { [data-testid="stMainBlockContainer"] { padding: 1.25rem 1rem 3rem; } .ia-hero { padding: 1.15rem; border-radius: 13px; } .ia-panel { padding: 1rem; } }
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


# token 仅存本次 Streamlit 会话，不存进 URL、全局变量或浏览器持久缓存。
if not st.session_state.get("auth_token"):
    st.title("投研服务台 · 登录")
    st.caption("使用后端 IA_AUTH_TOKENS 配置的个人凭证；演示级本地身份，不是企业 SSO。")
    with st.form("login_form"):
        entered = st.text_input("访问凭证", type="password")
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


def _show_chat_answer(answer: dict[str, Any]) -> None:
    status = answer.get("status") or "error"
    st.caption(f"回答状态：{status}")
    refs = answer.get("evidence_refs") or answer.get("sources") or []
    for ref in refs:
        metadata = ref.get("metadata") or ref.get("identity") or ref
        location = f"{metadata.get('file_name') or ref.get('ref') or ref.get('source_id')}，第 {metadata.get('page') or '未提供'} 页"
        st.caption(f"证据：{ref.get('label') or ref.get('citation') or ref.get('source_id') or ref.get('ref')}; {location}")
    if answer.get("evidence"):
        with st.expander("查看证据片段", expanded=False):
            st.code(str(answer["evidence"].get("excerpt") if isinstance(answer["evidence"], dict) else answer["evidence"]))
    for limitation in answer.get("limitations") or []:
        st.caption(f"限制：{limitation}")


def _show_chat() -> None:
    context = st.session_state.chat_context
    st.markdown("### 研究对话")
    st.caption("支持创建报告、追问已发布报告，或对已绑定的中文官方年报做有据回答；证据不足时明确拒答。")
    scope = (f"报告：{context['report_id']} | 标的：{context['ticker']}" if context["report_id"] else
             f"任务：{context['job_id']} | 标的：{context['ticker']}" if context["job_id"] else
             f"中文年报：{context['knowledge_ticker']}（同标的隔离）" if context.get("knowledge_ticker") else
             "尚未绑定报告或语料；可在侧栏选择中文年报，或明确提出研究任务。")
    st.markdown(f'<div class="ia-chat-context">{html.escape(scope)}</div>', unsafe_allow_html=True)
    if not context["messages"]:
        st.info("试试：分析 AAPL 服务业务和现金流风险；中文年报模式可问：宁德时代 2025 年货币资金是多少？")
    for message in context["messages"]:
        with st.chat_message(message["role"]):
            st.write(message["text"])
            if message.get("answer"):
                _show_chat_answer(message["answer"])

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
    active = context["job_id"] and context["job_status"] not in JOB_TERMINAL_STATUSES
    prompt = st.chat_input("输入研究任务或对当前报告提问", key="research_chat_input", max_chars=500, disabled=bool(active))
    if prompt:
        try:
            st.session_state.chat_context = dispatch_message(
                st.session_state.chat_context, prompt, _create_chat_job, _answer_request,
                st.session_state.get("requested_by_input", "anonymous"),
                knowledge_ask=_knowledge_answer_request,
            )
        except (requests.RequestException, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                try:
                    payload = exc.response.json()
                    detail = payload.get("detail", payload) if isinstance(payload, dict) else detail
                    if isinstance(detail, dict):
                        detail = detail.get("message") or detail.get("error_code") or "请求失败"
                except ValueError:
                    detail = "请求失败，请检查后端状态。"
            st.session_state.chat_context = record_error(st.session_state.chat_context, prompt, f"请求未完成：{detail}")
        new_context_value = st.session_state.chat_context
        if new_context_value["job_id"] and new_context_value["job_status"] not in JOB_TERMINAL_STATUSES:
            st.session_state.current_report = None
            st.query_params.clear()
            st.query_params["job"] = new_context_value["job_id"]
        st.rerun()


def _show_review_workbench(history: list[dict[str, Any]]) -> None:
    roles = set(st.session_state.identity.get("roles") or [])
    if not roles.intersection({"reviewer", "publisher", "admin"}) or not history:
        return
    with st.expander("审核与发布工作台", expanded=False):
        st.caption("创建、审核、发布由不同人员完成；人工逐条确认及整篇覆盖声明不等于系统自动证明语义蕴含。")
        report_id = st.selectbox("选择报告版本", [item["id"] for item in history], key="review_report_id")
        try:
            status = _api_get(f"/api/reports/{report_id}/status")
            st.info(f"发布状态：{status['publish_status']} · 审核状态：{status['review_status']}")
        except requests.RequestException:
            st.warning("无法确认当前发布状态。")
            return
        if roles.intersection({"reviewer", "admin"}):
            try:
                preview = _api_get(f"/api/reports/{report_id}/review")
            except requests.RequestException:
                st.caption("当前身份不可审核该报告，或证据版本不可核验。")
            else:
                st.markdown("#### 待审核正文")
                st.markdown(preview["report"])
                st.caption(f"结构证据门禁：{preview['gate']['release_status']}；claim 数：{preview['gate']['claims_total']}。这不代表已发布，也不自动证明语义支持。")
                with st.expander("审核证据快照与人工 claim 编辑"):
                    st.json(preview.get("evidence") or {})
                    st.caption("自动 claim 抽取尚未实现。审核人可手工登记完整 claim 集；保存后旧审核/发布立即失效，仍须独立发布人发布。")
                    edited = st.text_area("完整 claims JSON 数组", value=json.dumps(preview.get("claims") or [], ensure_ascii=False, indent=2),
                                          height=240, key=f"claims_json_{report_id}")
                    st.code('[{"claim_id":"c1","claim_text":"收入为已核验数值","anchors":[{"kind":"snapshot","field_path":"financial_snapshot.revenue","value":100,"period":"2025-09-30","unit":"USD"}]}]', language="json")
                    if st.button("保存人工 claim 集", key=f"save_claims_{report_id}"):
                        try:
                            binding = preview["binding"]
                            response = requests.put(f"{API_BASE_URL}/api/reports/{report_id}/claims", headers=_auth_headers(), timeout=30,
                                json={"claims": json.loads(edited), "expected_md_sha256": binding["report_md_sha256"],
                                      "expected_json_sha256": binding["report_json_sha256"]})
                            response.raise_for_status()
                        except (requests.RequestException, ValueError) as exc:
                            st.error(_api_error(exc) if isinstance(exc, requests.RequestException) else "JSON 格式不正确。")
                        else:
                            st.success("已保存，须重新逐条审核。")
                            st.rerun()
                claims = preview.get("claims") or []
                with st.form(f"review_{report_id}"):
                    decisions = {}
                    for claim in claims:
                        claim_id = str(claim.get("claim_id") or "")
                        if claim_id:
                            checked = st.checkbox(f"已核对 {claim_id}：{str(claim.get('claim_text') or '')[:100]}", key=f"claim_{report_id}_{claim_id}")
                            if checked:
                                decisions[claim_id] = "supported"
                    covered = st.checkbox("我已核对整份正文的所有关键结论，所列 claims 覆盖完整")
                    decision = st.radio("审核决定", ["rejected", "approved"], horizontal=True)
                    reason = st.text_area("审核理由（至少五字）", max_chars=1000)
                    submitted = st.form_submit_button("记录审核决定")
                if submitted:
                    binding = preview["binding"]
                    try:
                        _api_post(f"/api/reports/{report_id}/review", {
                            "decision": decision, "reason": reason, "claim_decisions": decisions,
                            "coverage_attested": covered, "expected_md_sha256": binding["report_md_sha256"],
                            "expected_json_sha256": binding["report_json_sha256"]})
                        st.success("审核已记录；发布需独立发布人操作。")
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(f"审核未生效：{_api_error(exc)}")
        if roles.intersection({"publisher", "admin"}):
            col_publish, col_withdraw = st.columns(2)
            with col_publish:
                if st.button("发布已审核版本", key=f"publish_{report_id}", use_container_width=True):
                    try:
                        _api_post(f"/api/reports/{report_id}/publish", {})
                        st.success("当前版本已发布。")
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(f"发布被拒绝：{_api_error(exc)}")
            with col_withdraw:
                if st.button("撤回发布", key=f"withdraw_{report_id}", use_container_width=True):
                    try:
                        _api_post(f"/api/reports/{report_id}/withdraw", {})
                        st.session_state.current_report = None
                        st.session_state.chat_context = new_context()
                        st.success("已撤回，旧会话正文与回答缓存已清除。")
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(f"撤回失败：{_api_error(exc)}")


def _api_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        try:
            payload = exc.response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
            if isinstance(detail, dict):
                return str(detail.get("message") or detail.get("error_code") or exc.response.status_code)
            return str(detail)
        except ValueError:
            pass
    return "后端不可用或请求未通过授权/版本校验。"


def _show_research_memory(history: list[dict[str, Any]]) -> None:
    with st.expander("版本关注与研究记忆", expanded=False):
        st.caption("只监测白名单本地资料/已发布报告。记忆为有限期用户注记，不作为事实证据，不参与投资建议。")
        watch_tab, memory_tab, compare_tab = st.tabs(["版本关注", "研究记忆", "报告比较"])
        with watch_tab:
            with st.form("new_watch"):
                name = st.text_input("关注项名称")
                files = st.text_input("白名单资料文件名（逗号分隔，可留空）")
                report_ids = st.multiselect("关注报告版本（必须已发布）", [item["id"] for item in history])
                submitted = st.form_submit_button("创建关注项")
            if submitted:
                try:
                    _api_post("/api/watchlists", {"name": name, "file_names": [item.strip() for item in files.split(",") if item.strip()], "report_ids": report_ids})
                    st.rerun()
                except requests.RequestException as exc:
                    st.error(_api_error(exc))
            try:
                watches = _api_get("/api/watchlists")
            except requests.RequestException:
                watches = []
            for watch in watches:
                st.write(f"{watch['name']} · {watch['watch_id']}")
                if watch.get("last_check"):
                    st.json(watch["last_check"])
                controls = st.columns(3)
                for column, label, operation in zip(controls, ["检查版本", "确认新基线", "删除关注"], ["check", "baseline", "delete"]):
                    with column:
                        if st.button(label, key=f"watch_{operation}_{watch['watch_id']}"):
                            try:
                                if operation == "delete":
                                    response = requests.delete(f"{API_BASE_URL}/api/watchlists/{watch['watch_id']}", headers=_auth_headers(), timeout=20)
                                    response.raise_for_status()
                                else:
                                    _api_post(f"/api/watchlists/{watch['watch_id']}/{operation}", {})
                                st.rerun()
                            except requests.RequestException as exc:
                                st.error(_api_error(exc))
        with memory_tab:
            with st.form("new_memory"):
                linked = st.selectbox("记忆关联报告", ["请选择", *[item["id"] for item in history]])
                note = st.text_area("用户注记 / 阅读偏好（不是证据）", max_chars=1000)
                ttl = st.number_input("有效天数（最长 90 天）", min_value=1, max_value=90, value=7)
                submitted = st.form_submit_button("保存有限期记忆")
            if submitted:
                try:
                    _api_post("/api/memories", {"report_id": linked, "content": note, "ttl_days": int(ttl)})
                    st.rerun()
                except requests.RequestException as exc:
                    st.error(_api_error(exc))
            try:
                memories = _api_get("/api/memories")
            except requests.RequestException:
                memories = []
            for memory in memories:
                st.caption(f"{memory['report_id']} · {memory['status']} · 到期 {memory['expires_at']}")
                if memory.get("content"):
                    st.write(memory["content"])
                if st.button("撤销记忆", key=f"delete_memory_{memory['memory_id']}"):
                    try:
                        response = requests.delete(f"{API_BASE_URL}/api/memories/{memory['memory_id']}", headers=_auth_headers(), timeout=20)
                        response.raise_for_status()
                        st.rerun()
                    except requests.RequestException as exc:
                        st.error(_api_error(exc))
        with compare_tab:
            with st.form("compare_reports"):
                ids = [item["id"] for item in history]
                left = st.selectbox("左侧报告", ["请选择", *ids])
                right = st.selectbox("右侧报告", ["请选择", *ids])
                submitted = st.form_submit_button("对比同标的事实")
            if submitted:
                try:
                    st.json(_api_post("/api/reports/compare", {"left_report_id": left, "right_report_id": right}))
                except requests.RequestException as exc:
                    st.error(_api_error(exc))


if "current_report" not in st.session_state:
    st.session_state.current_report = None
if "chat_context" not in st.session_state:
    st.session_state.chat_context = new_context()

# 深链接仅恢复报告或任务；浏览器重载后的聊天消息不会恢复。
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

st.markdown('<div class="ia-topline"><span class="ia-live-dot"></span>实时研究界面 · REAL-DATA CONNECTED</div>', unsafe_allow_html=True)
st.title("智能投资助手")
st.markdown('<div class="ia-hero"><div class="ia-hero-title">用对话提出研究任务，并对已发布报告继续追问。</div><div class="ia-hero-copy">任务进度、报告正文、数据来源与回答限制在同一研究会话中呈现；证据不足时不猜测。</div></div>', unsafe_allow_html=True)

with st.sidebar:
    identity = st.session_state.identity
    st.markdown(f"**{identity['actor_id']}** · 租户 {identity['tenant_id']}")
    if st.button("退出登录", use_container_width=True):
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()
    st.markdown("### 中文年报问答")
    st.caption("绑定一份中文官方年报后，可直接提问；回答只使用该标的资料，证据不足自动拒答。")
    corpus_labels = {
        "不绑定中文年报": None,
        "贵州茅台（600519.SS）": "600519.SS",
        "宁德时代（300750.SZ）": "300750.SZ",
        "平安银行（000001.SZ）": "000001.SZ",
        "五粮液（000858.SZ）": "000858.SZ",
    }
    current_corpus = st.session_state.chat_context.get("knowledge_ticker")
    current_label = next((label for label, value in corpus_labels.items() if value == current_corpus), "不绑定中文年报")
    selected_corpus = st.selectbox("选择语料", list(corpus_labels), index=list(corpus_labels).index(current_label), key="knowledge_corpus_select")
    if st.button("进入中文问答", use_container_width=True):
        selected_ticker = corpus_labels[selected_corpus]
        if selected_ticker:
            st.session_state.chat_context = select_knowledge_corpus(st.session_state.chat_context, selected_ticker)
            st.session_state.current_report = None
            st.query_params.clear()
        else:
            st.session_state.chat_context = new_context()
            st.session_state.current_report = None
            st.query_params.clear()
        st.rerun()
    st.markdown("### 历史报告")
    st.caption("浏览器重载后聊天消息会清空；报告和任务可从这里找回。")
    try:
        history = _api_get("/api/reports")
    except requests.RequestException:
        history = []
        st.caption("后端暂不可连接。")
    if history:
        labels = {f"{item['ticker']} | {item['topic']} | {item['created_at']}": item["id"] for item in history}
        selected = st.selectbox("选择历史报告", ["不打开", *labels])
        if selected != "不打开" and st.button("打开报告", use_container_width=True):
            try:
                report = _api_get(f"/api/reports/{labels[selected]}")
                st.session_state.chat_context = select_report(st.session_state.chat_context, report)
                st.session_state.current_report = report
                st.query_params.clear()
                st.query_params["report"] = report["id"]
                st.rerun()
            except requests.RequestException:
                st.warning("报告尚待审核发布、已撤回或无权限；正文不可见。")
    st.markdown("### 报告任务")
    try:
        jobs = _api_get("/api/report-jobs")
    except requests.RequestException:
        jobs = []
    if jobs:
        job_labels = {f"{item['ticker']} | {item['status']} | {item['job_id'][-6:]}": item["job_id"] for item in jobs}
        selected_job = st.selectbox("选择任务", ["不查看", *job_labels], key="job_select")
        if selected_job != "不查看" and st.button("查看任务进度", use_container_width=True):
            job = next(item for item in jobs if item["job_id"] == job_labels[selected_job])
            st.session_state.chat_context = update_job(select_job(st.session_state.chat_context, job), job)
            st.session_state.current_report = None
            st.query_params.clear()
            st.query_params["job"] = job["job_id"]
            st.rerun()
    else:
        st.caption("暂无任务记录。")
    st.caption("发起人由服务端身份绑定，不能用表单字段代替授权。")

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
_show_review_workbench(history)
_show_research_memory(history)
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
