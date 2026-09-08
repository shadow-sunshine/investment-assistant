"""Streamlit demo UI for the Investment Assistant API."""

from __future__ import annotations

import html
import os
from typing import Any

import requests
import streamlit as st

API_BASE_URL = os.getenv("INVESTMENT_ASSISTANT_API_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="\u667a\u80fd\u6295\u8d44\u52a9\u624b", page_icon="\U0001f4ca", layout="wide", initial_sidebar_state="expanded")


APP_CSS = r"""
<style>
:root {
    --ia-navy: #1f3a5f;
    --ia-navy-deep: #142b48;
    --ia-ink: #172033;
    --ia-muted: #6b778c;
    --ia-bg: #f4f6f9;
    --ia-surface: #ffffff;
    --ia-border: #e2e8f0;
    --ia-red: #c84655;
    --ia-green: #2d8a62;
    --ia-shadow: 0 10px 26px rgba(29, 48, 76, 0.08);
}
html, body, [class*="css"] {
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif;
}
.stApp {
    background: var(--ia-bg);
    color: var(--ia-ink);
}
#MainMenu, header, footer, [data-testid="stToolbar"], [data-testid="stStatusWidget"], [data-testid="stDeployButton"] {
    display: none !important;
}
[data-testid="stAppViewContainer"] > .main {
    background: var(--ia-bg);
}
[data-testid="stMainBlockContainer"] {
    max-width: 1440px;
    padding-top: 2.25rem;
    padding-bottom: 3rem;
}
[data-testid="stSidebar"] {
    background: #182f4c;
    border-right: 0;
}
[data-testid="stSidebar"] * {
    color: #edf3fb;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="input"] > div {
    background: rgba(255,255,255,0.1);
    border-color: rgba(255,255,255,0.18);
}
[data-testid="stSidebar"] button {
    border-color: rgba(255,255,255,0.24);
    background: rgba(255,255,255,0.08);
}
h1 {
    color: var(--ia-navy-deep) !important;
    font-size: 2.2rem !important;
    font-weight: 760 !important;
    letter-spacing: -0.035em;
    margin-bottom: .1rem !important;
}
h2, h3 {
    color: var(--ia-navy-deep) !important;
    font-weight: 720 !important;
}
.ia-kicker {
    color: var(--ia-navy);
    font-size: .73rem;
    font-weight: 760;
    letter-spacing: .13em;
    text-transform: uppercase;
    margin: 0 0 .45rem;
}
.ia-hero {
    background: linear-gradient(118deg, #1f3a5f 0%, #294d79 66%, #3a5f8d 100%);
    border-radius: 18px;
    box-shadow: 0 16px 34px rgba(22, 47, 78, .20);
    color: #f8fbff;
    margin: .85rem 0 1.35rem;
    overflow: hidden;
    padding: 1.35rem 1.6rem;
    position: relative;
}
.ia-hero:after {
    border: 1px solid rgba(255,255,255,.16);
    border-radius: 999px;
    content: "";
    height: 220px;
    position: absolute;
    right: -72px;
    top: -120px;
    width: 220px;
}
.ia-hero__eyebrow {
    color: #b9d4ef;
    font-size: .75rem;
    font-weight: 700;
    letter-spacing: .1em;
    margin-bottom: .45rem;
    text-transform: uppercase;
}
.ia-hero__title {
    font-size: 1.16rem;
    font-weight: 700;
    line-height: 1.65;
    max-width: 760px;
    position: relative;
    z-index: 1;
}
.ia-hero__note {
    color: #d6e5f4;
    font-size: .86rem;
    margin-top: .55rem;
    position: relative;
    z-index: 1;
}
.ia-panel {
    background: var(--ia-surface);
    border: 1px solid var(--ia-border);
    border-radius: 16px;
    box-shadow: var(--ia-shadow);
    margin: .7rem 0 1.25rem;
    padding: 1.2rem 1.25rem;
}
.ia-panel--report {
    padding: 1.5rem 1.65rem;
}
.ia-panel-title {
    color: var(--ia-navy-deep);
    font-size: 1.06rem;
    font-weight: 750;
    margin: 0 0 .18rem;
}
.ia-panel-subtitle {
    color: var(--ia-muted);
    font-size: .83rem;
    margin: 0 0 1rem;
}
.ia-mode {
    align-items: center;
    border: 1px solid #b8d8c8;
    border-radius: 999px;
    color: #206747;
    display: inline-flex;
    font-size: .82rem;
    font-weight: 700;
    gap: .38rem;
    padding: .36rem .72rem;
}
.ia-mode--fallback {
    background: #fff7e8;
    border-color: #f1d49b;
    color: #8a5a0b;
}
.ia-mode--llm {
    background: #edf8f1;
}
.ia-mode-reason {
    color: var(--ia-muted);
    font-size: .84rem;
    margin: .55rem 0 1rem;
}
.ia-metric-card {
    background: #fff;
    border: 1px solid var(--ia-border);
    border-radius: 13px;
    min-height: 118px;
    padding: .82rem .92rem;
    box-shadow: 0 5px 15px rgba(28, 46, 72, .045);
}
.ia-metric-card .label {
    color: var(--ia-muted);
    font-size: .77rem;
    font-weight: 600;
    margin-bottom: .32rem;
}
.ia-metric-card .value {
    color: var(--ia-ink);
    font-size: 1.25rem;
    font-weight: 750;
    line-height: 1.22;
    overflow-wrap: anywhere;
}
.ia-metric-card .value--unavailable { color: #98a2b3; font-size: 1rem; }
.ia-metric-card .delta { font-size: .79rem; font-weight: 700; margin-top: .45rem; }
.ia-metric-card .delta--up { color: var(--ia-red); }
.ia-metric-card .delta--down { color: var(--ia-green); }
.ia-metric-card .delta--neutral { color: var(--ia-muted); }
.ia-metric-card .meta { color: var(--ia-muted); font-size: .72rem; margin-top: .44rem; }
.ia-notice {
    background: #f6f8fb;
    border-left: 3px solid #aeb8c8;
    border-radius: 8px;
    color: #536176;
    font-size: .86rem;
    margin-top: .9rem;
    padding: .7rem .8rem;
}
.ia-evidence-card {
    background: #fff;
    border: 1px solid var(--ia-border);
    border-radius: 13px;
    box-shadow: 0 4px 12px rgba(28,46,72,.04);
    margin: .7rem 0;
    padding: 1rem 1.05rem;
}
.ia-evidence-topline { align-items: baseline; display: flex; gap: .55rem; flex-wrap: wrap; }
.ia-citation {
    background: #eaf0f7;
    border-radius: 6px;
    color: var(--ia-navy);
    font-size: .76rem;
    font-weight: 800;
    padding: .18rem .42rem;
}
.ia-evidence-title { color: var(--ia-navy-deep); font-size: .96rem; font-weight: 720; }
.ia-evidence-meta { color: var(--ia-muted); font-size: .78rem; margin: .58rem 0; }
.ia-evidence-text { color: #4b5769; font-size: .88rem; line-height: 1.75; }
.ia-risk {
    border-radius: 10px;
    font-size: .88rem;
    margin: .58rem 0;
    padding: .76rem .85rem;
}
.ia-risk--high { background: #fff0f1; border: 1px solid #f4c6cb; color: #9d2635; }
.ia-risk--info { background: #f2f6fb; border: 1px solid #d6e1ee; color: #385675; }
.ia-risk--pass { background: #eef9f2; border: 1px solid #c7e6d2; color: #236b48; }
.ia-badge { border-radius: 999px; display: inline-block; font-size: .72rem; font-weight: 800; letter-spacing: .04em; margin-right: .5rem; padding: .18rem .45rem; }
.ia-badge--risk { background: #c84655; color: #fff; }
.ia-badge--info { background: #dce9f6; color: #285078; }
.ia-badge--pass { background: #2d8a62; color: #fff; }

[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ia-surface);
    border: 1px solid var(--ia-border) !important;
    border-radius: 16px !important;
    box-shadow: var(--ia-shadow);
    margin: .7rem 0 1.25rem;
    padding: .35rem;
}
[data-testid="stForm"] {
    background: var(--ia-surface);
    border: 1px solid var(--ia-border);
    border-radius: 16px;
    box-shadow: var(--ia-shadow);
    margin: .7rem 0 1.25rem;
    padding: 1.15rem 1.25rem .8rem;
}
[data-testid="stForm"] [data-testid="stFormSubmitButton"] button,
.stButton > button[kind="primary"] {
    background: var(--ia-navy) !important;
    border: 1px solid var(--ia-navy) !important;
    border-radius: 9px !important;
    font-weight: 700 !important;
}
[data-testid="stForm"] [data-testid="stFormSubmitButton"] button:hover,
.stButton > button[kind="primary"]:hover {
    background: var(--ia-navy-deep) !important;
    border-color: var(--ia-navy-deep) !important;
}
[data-testid="stTabs"] [data-baseweb="tab-list"] {
    border-bottom: 1px solid var(--ia-border);
    gap: .5rem;
}
[data-testid="stTabs"] button[role="tab"] { color: #637086; font-weight: 650; }
[data-testid="stTabs"] button[aria-selected="true"] { color: var(--ia-navy) !important; }
[data-testid="stTabs"] button[aria-selected="true"]::after { background: var(--ia-navy) !important; }
.stMarkdown p, [data-testid="stMarkdownContainer"] li { line-height: 1.8; }
@media (max-width: 900px) {
    [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
    .ia-hero { border-radius: 14px; padding: 1.15rem; }
    .ia-panel { padding: 1rem; }
}
</style>
"""

st.markdown(APP_CSS, unsafe_allow_html=True)


def _api_get(path: str) -> Any:
    response = requests.get(f"{API_BASE_URL}{path}", timeout=20)
    response.raise_for_status()
    return response.json()


def _api_post(path: str, payload: dict[str, Any]) -> Any:
    response = requests.post(f"{API_BASE_URL}{path}", json=payload, timeout=1800)
    response.raise_for_status()
    return response.json()


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
    if key in {"period_return_pct", "one_month_return_pct", "max_drawdown_pct"} and isinstance(value, (int, float)):
        if value > 0:
            return f"{value:+.2f}%", "up"
        if value < 0:
            return f"{value:+.2f}%", "down"
        return "0.00%", "neutral"
    return None, "neutral"


def _show_snapshot(title: str, snapshot: dict[str, Any], fields: list[tuple[str, str]]) -> None:
    st.markdown(f'<div class="ia-panel-title">{html.escape(title)}</div>', unsafe_allow_html=True)
    cards = st.columns(2)
    for index, (key, label) in enumerate(fields):
        value = snapshot.get(key)
        display = _display_value(value)
        delta, delta_kind = _metric_delta(key, snapshot)
        with cards[index % 2]:
            value_class = "value value--unavailable" if _is_unavailable(value) else "value"
            delta_html = f'<div class="delta delta--{delta_kind}">{html.escape(delta)}</div>' if delta else '<div class="meta">\u7ed3\u6784\u5316\u5feb\u7167</div>'
            st.markdown(
                f'<div class="ia-metric-card"><div class="label">{html.escape(label)}</div><div class="{value_class}">{html.escape(display)}</div>{delta_html}</div>',
                unsafe_allow_html=True,
            )
    if snapshot.get("data_available") is False:
        reason = snapshot.get("error") or "\u672a\u63d0\u4f9b\u539f\u56e0"
        st.markdown(f'<div class="ia-notice">\u6570\u636e\u72b6\u6001\uff1a\u6570\u636e\u4e0d\u53ef\u7528\u3002{html.escape(str(reason))}</div>', unsafe_allow_html=True)


def _show_sources(sources: list[dict[str, Any]]) -> None:
    st.markdown('<div class="ia-panel"><div class="ia-panel-title">\u8bc1\u636e\u4e0e\u6765\u6e90</div><div class="ia-panel-subtitle">\u6bcf\u6761\u5f15\u7528\u4fdd\u7559\u6587\u4ef6\u3001\u9875\u7801\u4e0e\u539f\u59cb\u94fe\u63a5\uff0c\u4fbf\u4e8e\u590d\u6838\u3002</div></div>', unsafe_allow_html=True)
    if not sources:
        st.markdown('<div class="ia-risk ia-risk--info"><span class="ia-badge ia-badge--info">\u63d0\u793a</span>\u672a\u68c0\u7d22\u5230\u8bc1\u636e\u3002</div>', unsafe_allow_html=True)
        return
    for source in sources:
        metadata = source.get("metadata") or {}
        title = metadata.get("title") or metadata.get("file_name") or metadata.get("source") or "\u672a\u547d\u540d\u6765\u6e90"
        citation = source.get("citation") or "S?"
        page = metadata.get("page") or "\u4e0d\u9002\u7528"
        source_name = metadata.get("file_name") or metadata.get("source") or "\u672a\u63d0\u4f9b"
        url = metadata.get("url")
        url_html = f'<a href="{html.escape(str(url), quote=True)}" target="_blank">\u6253\u5f00\u539f\u59cb\u94fe\u63a5</a>' if url else "\u65e0\u539f\u59cb\u94fe\u63a5"
        content = html.escape(str(source.get("content") or "\u65e0\u53ef\u63d0\u53d6\u6587\u672c\u3002"))
        st.markdown(
            f'<div class="ia-evidence-card"><div class="ia-evidence-topline"><span class="ia-citation">[{html.escape(str(citation))}]</span><span class="ia-evidence-title">{html.escape(str(title))}</span></div><div class="ia-evidence-meta">\u6587\u4ef6\uff1a{html.escape(str(source_name))} &nbsp;|&nbsp; \u9875\u7801\uff1a{html.escape(str(page))} &nbsp;|&nbsp; {url_html}</div><div class="ia-evidence-text">{content}</div></div>',
            unsafe_allow_html=True,
        )


def _risk_box(kind: str, label: str, text: str) -> None:
    st.markdown(
        f'<div class="ia-risk ia-risk--{kind}"><span class="ia-badge ia-badge--{kind}">{html.escape(label)}</span>{html.escape(text)}</div>',
        unsafe_allow_html=True,
    )


def _show_report(data: dict[str, Any]) -> None:
    mode = data.get("mode") or {}
    is_llm = mode.get("label") == "\u53d7\u63a7 LLM \u7248"
    mode_label = mode.get("label") or "\u89c4\u5219\u7248"
    mode_class = "ia-mode--llm" if is_llm else "ia-mode--fallback"
    mode_prefix = "\u53d7\u63a7\u751f\u6210" if is_llm else "\u56de\u9000\u8bf4\u660e"
    st.markdown(
        f'<div class="ia-panel"><span class="ia-mode {mode_class}">{html.escape(mode_label)}</span><div class="ia-mode-reason"><strong>{mode_prefix}\uff1a</strong>{html.escape(str(mode.get("reason") or "\u672a\u63d0\u4f9b"))}</div></div>',
        unsafe_allow_html=True,
    )

    overview, report_tab, evidence_tab, risks_tab = st.tabs(["\u6570\u636e\u5feb\u7167", "\u62a5\u544a\u5168\u6587", "\u8bc1\u636e\u4e0e\u6765\u6e90", "\u98ce\u9669\u4e0e\u5ba1\u8ba1"])
    with overview:
        left, right = st.columns(2, gap="large")
        with left:
            with st.container(border=True):
                _show_snapshot("\u884c\u60c5\u5feb\u7167", data.get("market_snapshot") or {}, [
                ("latest_close", "\u6700\u65b0\u6536\u76d8\u4ef7"), ("latest_trading_date", "\u4ea4\u6613\u65e5"),
                ("period_return_pct", "\u6837\u672c\u671f\u6536\u76ca"), ("one_month_return_pct", "\u8fd1\u4e00\u6708\u6536\u76ca"),
                ("annualized_volatility_pct", "\u5e74\u5316\u6ce2\u52a8\u7387"), ("max_drawdown_pct", "\u6700\u5927\u56de\u64a4"),
                ])
        with right:
            with st.container(border=True):
                _show_snapshot("\u8d22\u62a5\u4e0e\u4f30\u503c\u5feb\u7167", data.get("financial_snapshot") or {}, [
                ("revenue", "\u8425\u6536"), ("revenue_period_end", "\u8425\u6536\u671f\u672b"),
                ("net_income", "\u51c0\u5229\u6da6"), ("net_income_period_end", "\u51c0\u5229\u6da6\u671f\u672b"),
                ("free_cash_flow", "\u81ea\u7531\u73b0\u91d1\u6d41"), ("free_cash_flow_period_end", "\u73b0\u91d1\u6d41\u671f\u672b"),
                ("trailing_pe", "PE"), ("price_to_book", "PB"), ("valuation_as_of", "\u4f30\u503c\u6293\u53d6\u65f6\u95f4"),
                ])
    with report_tab:
        with st.container(border=True):
            st.markdown(data.get("report") or "\u62a5\u544a\u5185\u5bb9\u7f3a\u5931\u3002")
    with evidence_tab:
        _show_sources(data.get("sources") or [])
    with risks_tab:
        with st.container(border=True):
            st.markdown('<div class="ia-panel-title">\u98ce\u9669\u65d7\u6807</div><div class="ia-panel-subtitle">\u98ce\u9669\u3001\u4e00\u822c\u63d0\u793a\u4e0e\u7cfb\u7edf\u901a\u8fc7\u72b6\u6001\u5206\u5c42\u5c55\u793a\u3002</div>', unsafe_allow_html=True)
            flags = data.get("risk_flags") or []
            if flags:
                for flag in flags:
                    _risk_box("high", "\u98ce\u9669", str(flag))
            else:
                _risk_box("pass", "\u901a\u8fc7", "\u672a\u4ea7\u751f\u989d\u5916\u98ce\u9669\u65d7\u6807\u3002")
        with st.container(border=True):
            st.markdown('<div class="ia-panel-title">Safety \u6821\u9a8c</div>', unsafe_allow_html=True)
            evaluation = data.get("evaluation") or {}
            if evaluation.get("passed"):
                _risk_box("pass", "\u901a\u8fc7", "Safety \u6821\u9a8c\u901a\u8fc7\u3002")
            else:
                _risk_box("high", "\u672a\u901a\u8fc7", "Safety \u6821\u9a8c\u672a\u901a\u8fc7\u3002")
            for finding in evaluation.get("findings") or []:
                _risk_box("info", "\u63d0\u793a", str(finding))


st.markdown('<div class="ia-kicker">Research brief &middot; evidence first</div>', unsafe_allow_html=True)
st.title("\u667a\u80fd\u6295\u8d44\u52a9\u624b")
st.markdown(
    '<div class="ia-hero"><div class="ia-hero__eyebrow">Real data &middot; traceable evidence &middot; disclosed risk</div><div class="ia-hero__title">\u8f93\u5165\u80a1\u7968\u4ee3\u7801\uff0c\u5f97\u5230\u57fa\u4e8e\u771f\u5b9e\u6570\u636e\u3001\u8bc1\u636e\u53ef\u56de\u6eaf\u3001\u98ce\u9669\u5df2\u62ab\u9732\u7684\u7814\u7a76\u7b80\u62a5\u3002</div><div class="ia-hero__note">\u7ed3\u8bba\u4e0d\u66ff\u4ee3\u6295\u8d44\u5efa\u8bae\uff1b\u91cd\u70b9\u5448\u73b0\u6570\u636e\u6765\u6e90\u3001\u53e3\u5f84\u4e0e\u5df2\u77e5\u98ce\u9669\u3002</div></div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### \u5386\u53f2\u62a5\u544a")
    st.caption("\u4ece\u5df2\u751f\u6210\u7684\u7b80\u62a5\u4e2d\u5feb\u901f\u6062\u590d\u9605\u8bfb\u3002")
    try:
        history = _api_get("/api/reports")
    except requests.RequestException:
        history = []
        st.error("\u540e\u7aef\u4e0d\u53ef\u8fde\u63a5\u3002\u8bf7\u4f7f\u7528\u542f\u52a8\u811a\u672c\u6253\u5f00\u6f14\u793a\u3002")
    if history:
        labels = {f"{item['ticker']} | {item['topic']} | {item['created_at']}": item["id"] for item in history}
        options = ["\u4e0d\u67e5\u770b\u5386\u53f2\u62a5\u544a", *labels]
        selected = st.selectbox("\u9009\u62e9\u5df2\u751f\u6210\u62a5\u544a", options)
        if selected != "\u4e0d\u67e5\u770b\u5386\u53f2\u62a5\u544a" and st.button("\u6253\u5f00\u5386\u53f2\u62a5\u544a"):
            try:
                st.session_state.current_report = _api_get(f"/api/reports/{labels[selected]}")
            except requests.RequestException as exc:
                st.error(f"\u8bfb\u53d6\u5931\u8d25\uff1a{exc}")
        latest_valid_report = next(
            (
                item
                for item in history
                if any(char.isalpha() for char in str(item.get("ticker") or ""))
                and "?" not in str(item.get("topic") or "")
            ),
            history[0],
        )
        if st.button("\u6253\u5f00\u6700\u65b0\u6709\u6548\u62a5\u544a", use_container_width=True):
            try:
                st.session_state.current_report = _api_get(f"/api/reports/{latest_valid_report['id']}")
            except requests.RequestException as exc:
                st.error(f"\u8bfb\u53d6\u5931\u8d25\uff1a{exc}")

st.markdown('<div class="ia-panel-title">\u751f\u6210\u7814\u7a76\u7b80\u62a5</div><div class="ia-panel-subtitle">\u6570\u636e\u83b7\u53d6\u5931\u8d25\u65f6\u4f1a\u663e\u5f0f\u62ab\u9732\uff0c\u4e0d\u4f1a\u586b\u5145\u4f30\u7b97\u503c\u3002</div>', unsafe_allow_html=True)
with st.form("research_form"):
    col1, col2, col3 = st.columns([1, 2.25, 1])
    with col1:
        ticker = st.text_input("\u80a1\u7968\u4ee3\u7801", value="AAPL")
    with col2:
        topic = st.text_input("\u7814\u7a76\u4e3b\u9898", value="\u670d\u52a1\u4e1a\u52a1\u3001\u73b0\u91d1\u6d41\u4e0e\u4f30\u503c\u98ce\u9669")
    with col3:
        horizon = st.selectbox("\u89c2\u5bdf\u671f\u9650", ["\u77ed\u671f", "\u4e2d\u671f", "\u957f\u671f"], index=1)
    submitted = st.form_submit_button("\u751f\u6210\u7814\u7a76\u62a5\u544a", type="primary", use_container_width=True)

if submitted:
    if not ticker.strip():
        st.error("\u8bf7\u586b\u5199\u80a1\u7968\u4ee3\u7801\u3002")
    elif not topic.strip():
        st.error("\u7814\u7a76\u4e3b\u9898\u4e0d\u80fd\u4e3a\u7a7a\uff0c\u8bf7\u68c0\u67e5\u8f93\u5165\u3002")
    else:
        try:
            with st.spinner("\u6b63\u5728\u83b7\u53d6\u771f\u5b9e\u6570\u636e\u3001\u68c0\u7d22\u8bc1\u636e\u5e76\u751f\u6210\u62a5\u544a\uff0c\u8bf7\u8010\u5fc3\u7b49\u5f85\u2026"):
                st.session_state.current_report = _api_post("/api/reports", {"ticker": ticker, "topic": topic, "horizon": horizon})
            st.success("\u62a5\u544a\u5df2\u751f\u6210\u3002")
        except requests.HTTPError as exc:
            detail = exc.response.json().get("detail", str(exc)) if exc.response is not None else str(exc)
            st.error(f"\u751f\u6210\u5931\u8d25\uff1a{detail}")
        except requests.RequestException as exc:
            st.error(f"\u65e0\u6cd5\u8fde\u63a5\u540e\u7aef\uff1a{exc}")

if st.session_state.get("current_report"):
    _show_report(st.session_state.current_report)
else:
    st.markdown('<div class="ia-risk ia-risk--info"><span class="ia-badge ia-badge--info">\u63d0\u793a</span>\u8bf7\u5728\u4e0a\u65b9\u586b\u5199\u53c2\u6570\u540e\u70b9\u51fb\u201c\u751f\u6210\u7814\u7a76\u62a5\u544a\u201d\uff0c\u6216\u4ece\u5de6\u4fa7\u6253\u5f00\u5386\u53f2\u62a5\u544a\u3002</div>', unsafe_allow_html=True)
