"""会话内的受控研究意图和上下文状态；不负责身份认证或持久化。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from time import perf_counter
from typing import Any, Callable

from .chinese_qa_service import CHINESE_TICKERS
from .company_discovery import (
    EVIDENCE_FIELD_VERIFIED,
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_ONBOARDED,
    STATUS_SOURCE_UNAVAILABLE,
    STATUS_UNRESOLVED,
    explain_unresolved_company,
)
from .company_qa import catalog, references, UNKNOWN_COMPANY_PERIOD, CompanySourceUnavailable, YEAR, QUARTER, LIVE, unverified_company_subject
from .research_answer import classify_research
from .mcp_research import is_market_query, answer_market_snapshot

_TERMINAL = {"completed", "failed", "cancelled"}
_RESEARCH = re.compile(r"^(?:请|帮我|请帮我|麻烦)?\s*(?:分析|研究|生成(?:一份)?(?:研究)?报告|做一份(?:研究)?报告)\s*", re.I)
_REPORT_TERM = re.compile(r"财报|年报|年度报告", re.I)
_REPORT_ACTION = re.compile(r"给(?:我)?|查看|总结|概览|摘要|提供|整理|下载|想看|拿到", re.I)
_REPORT_FIELD = re.compile(r"收入|营收|利润|资产|负债|现金流|研发|费用|存款|净息差|股价|风险|同比|增长|多少|怎么样|如何|为什么", re.I)
_TICKER = re.compile(r"(?<![A-Za-z0-9.^=-])(?:[A-Z]{1,5}|\d{6}\.(?:SS|SZ)|\d{4,5}\.HK)(?![A-Za-z0-9.^=-])")
_INDUSTRY = re.compile(r"(?:关注|研究|观察|看看|看好|推荐|有前景).{0,16}(?:行业|赛道|板块)|(?:行业|赛道|板块).{0,16}(?:关注|研究|观察|推荐|前景)")
_COMPANY_INDUSTRY = re.compile(r"行业(?:发展|趋势|前景|情况|表现|变化)|所在行业")
_MARKET_SELECTIONS = {
    "港股": "HK", "港股市场": "HK", "HK": "HK", "HKEX": "HK",
    "A股": "A", "A 股": "A", "A股市场": "A", "沪深": "A", "A": "A",
    "美股": "US", "美股市场": "US", "US": "US", "SEC": "US",
}
_MARKET_LABELS = {"HK": "港股", "A": "A股", "US": "美股"}

# 仅处理不依赖外部证据的产品介绍；必须整句匹配，不能截获混有财务事实的提问。
_GREETING = frozenset({"你好", "您好", "嗨", "hi", "hello", "早上好", "下午好", "晚上好"})
_IDENTITY = frozenset({"你是谁", "你是什么", "你叫什么", "你叫什么名字", "介绍一下你自己"})
_CAPABILITY = frozenset({"你能做什么", "你会做什么", "你可以做什么", "你是做什么的", "有什么功能", "怎么使用", "如何使用", "帮助"})
_INTRO = "我是投研助手，主要帮你查询有证据来源的官方年报事实、创建研究报告，并追问已发布报告。例如：网易 2025 年收入、宁德时代 2025 年营收，或今年的行业观察。我不提供实时行情或投资建议。"
_HELP = "我是投研助手。官方资料模式可查询已收录公司的具体年度指标及来源；第三方数据模式可查看 AKShare/BaoStock 返回的结构化财务数据。你可以先说市场，再说公司和问题；未核验数据不等于官方年报，不提供买卖建议。"
_PERSONAL_INTRO = re.compile(r"^(?:我叫|我是|我的名字是)\s*([一-龥A-Za-z][一-龥A-Za-z0-9 _-]{0,19})[。！？!?]?$", re.I)
_MARKET_ONLY = re.compile(r"^(?:我想|我想要|帮我|请)?(?:看|看看|查询|查|选择|切换到|使用)?\s*(港股|港股市场|A股|A 股|A股市场|沪深|美股|美股市场|HKEX|SEC)(?:市场)?[。！？!?]?$", re.I)


def _product_reply(text: str) -> str | None:
    raw = unicodedata.normalize("NFKC", text).strip()
    personal = _PERSONAL_INTRO.fullmatch(raw)
    if personal:
        name = personal.group(1).strip(" _-")
        return f"你好，{name}！我是投研助手。你可以直接告诉我想看的市场、公司和问题；如果还没想好，也可以先说“我想看看港股”。"
    normalized = re.sub(r"[\s，,。！？!?：:～~]+", "", raw).casefold()
    if normalized in _GREETING:
        return "你好！" + _HELP
    if normalized in _IDENTITY:
        return _INTRO
    if normalized in _CAPABILITY:
        return _HELP
    return None


def _is_annual_report_request(text: str) -> bool:
    """识别“给我一份年报”这类资料请求，不把具体字段问题误送到报告路径。"""
    if not _REPORT_TERM.search(text):
        return False
    return bool(_REPORT_ACTION.search(text) or not _REPORT_FIELD.search(text))


def _annual_report_detail(text: str, ticker: str, record: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    """将相对年份绑定到资料清单；没有同年度证据时明确澄清，不静默降级。"""
    years = sorted(set(YEAR.findall(text)))
    if len(years) > 1:
        return "guidance", "一次只能请求一个年度的年报；请明确写出一个年份。"
    if years:
        requested_year = years[0]
    elif re.search(r"今年|本年度", text):
        requested_year = str(date.today().year)
    elif "去年" in text:
        requested_year = str(date.today().year - 1)
    else:
        requested_year = str(record.get("year") or "")
    available_year = str(record.get("year") or "")
    if not requested_year or requested_year != available_year:
        display = record.get("display_name") or ticker
        return "guidance", (f"已识别为{display}（{ticker}），但当前只核验了 {available_year or '指定'} 年官方年报，"
                             f"没有 {requested_year or '该'} 年可交付的年报证据；不会用其他年度资料替代。"
                             f"你可以改问：给我一份 {available_year} 年的{display}年报摘要。")
    used_latest = not years and "今年" not in text and "本年度" not in text and "去年" not in text
    return "annual_report", {"ticker": ticker, "question": text, "year": requested_year,
                              "topic": f"{requested_year}年年报摘要", "used_latest": str(used_latest).lower(),
                              "display_name": str(record.get("display_name") or ticker)}


_INTENT_LABELS = {
    "knowledge": "年报事实",
    "company": "公司资料",
    "annual_report": "年报摘要",
    "research": "研究报告",
    "issuer": "官方披露",
    "industry": "行业观察",
    "answer": "已发布报告追问",
    "discovery": "公司发现",
    "research_answer": "研究回答",
    "mcp_data": "第三方财务数据",
}

#: 候选解析状态 → 面向用户的状态标签（UI 与 API 共用同一套词）。
DISCOVERY_STATUS_LABELS = {
    STATUS_ONBOARDED: "已收录",
    STATUS_MATCHED: "已识别",
    STATUS_AMBIGUOUS: "待确认市场",
    STATUS_UNRESOLVED: "未识别",
    STATUS_SOURCE_UNAVAILABLE: "来源不可用",
}


def _company_label(text: str) -> str:
    """从问句里取出公司名用于提示；只做展示，不做身份判定。"""
    stripped = re.sub(r"(?:给我|请|帮我|查询|查一下)?\s*(?:一份|一版)?", "", text)
    stripped = re.sub(r"(?<!\d)20\d{2}\s*年?(?!\d)", "", stripped)
    stripped = re.sub(r"(财报|年报|年度报告|的)?(收入|营收|营业额|利润|现金流|怎么样|如何|情况|多少|是多少|数据|资料|报告|分析|研究|股价|股份|股票)", "", stripped)
    return re.sub(r"[\s，,。！？!?：:、；;（）()·]+", "", stripped) or text.strip()


def _discovery_guidance(text: str) -> tuple[str, str]:
    """未收录公司不再统一显示"无法确认公司"；按真实状态给出具体下一步。

    只读本地清单与路由提示，**不联网**；外部候选源只在显式 API 调用时参与。
    """
    label = _company_label(text)
    try:
        result = explain_unresolved_company(label)
    except Exception:  # noqa: BLE001 - 解析层异常也不得阻断会话
        return "guidance", f"暂未收录{label}的可核验年报；请换成已收录公司，或先提供证券代码和报告年度。"
    tickers = "、".join(f"{c.ticker}（{c.market}）" for c in result.candidates)
    if result.status == STATUS_AMBIGUOUS:
        return "guidance", (
            f"已识别公司「{label}」，但存在多个市场候选：{tickers}。"
            "请确认是该公司的港股 / A 股 / 美股哪一份；系统不会自行选择，也不会跨市场混用数据。"
        )
    if result.status == STATUS_MATCHED:
        return "guidance", (
            f"已识别公司「{label}」的候选标的 {result.selected_ticker}，但该公司的官方年报证据尚未接入。"
            "候选数据不能冒充年报事实，因此暂不提供收入等财务数字；需要先完成官方资料接入与字段核验。"
        )
    if result.status == STATUS_SOURCE_UNAVAILABLE:
        return "guidance", (
            f"已收到公司「{label}」的查询，但外部数据源暂时不可用，无法确认该公司候选标的；"
            "不会用其他公司的资料替代，也不会给出未经核验的数字。"
        )
    if result.status == STATUS_ONBOARDED and result.evidence_state != EVIDENCE_FIELD_VERIFIED:
        return "guidance", (
            f"已识别为公司「{label}」（{result.selected_ticker}），官方资料已入库，"
            "但该字段尚未建立证据锚点，暂不提供财务数字；不会用其他期间或其他公司的数据补足。"
        )
    if result.status == STATUS_ONBOARDED:
        return "guidance", f"已识别为公司「{label}」（{result.selected_ticker}），请写明要问的年份与具体字段。"
    return "guidance", (
        f"暂未收录公司「{label}」的可核验年报，也无法从证券代码确认其身份；"
        "请写明公司全称或证券代码，并说明港股 / A 股 / 美股；不会沿用当前报告作答。"
    )


def company_discovery_error_code(text: str) -> str:
    """把问句映射为稳定的公司发现错误码，供 API 与前端结构化展示。"""
    from .company_discovery import (
        ERROR_CATALOG_UNAVAILABLE,
        ERROR_COMPANY_AMBIGUOUS_MARKET,
        ERROR_COMPANY_NOT_FOUND,
        ERROR_COMPANY_NOT_ONBOARDED,
        ERROR_FIELD_NOT_VERIFIED,
        ERROR_OFFICIAL_MATERIAL_REQUIRED,
        ERROR_SOURCE_UNAVAILABLE,
    )

    try:
        records = catalog()
        result = explain_unresolved_company(_company_label(text))
    except CompanySourceUnavailable:
        return ERROR_CATALOG_UNAVAILABLE
    except Exception:  # noqa: BLE001
        return ERROR_COMPANY_NOT_FOUND
    if result.status == STATUS_AMBIGUOUS:
        return ERROR_COMPANY_AMBIGUOUS_MARKET
    if result.status == STATUS_SOURCE_UNAVAILABLE:
        return ERROR_SOURCE_UNAVAILABLE
    if result.status == STATUS_UNRESOLVED:
        # 问句里带的是本地清单也没有的证券代码时，区分"公司不存在"与"未接入"。
        _, unknown_codes = references(text, records)
        return ERROR_COMPANY_NOT_ONBOARDED if unknown_codes else ERROR_COMPANY_NOT_FOUND
    if result.status == STATUS_ONBOARDED:
        return (ERROR_FIELD_NOT_VERIFIED if result.evidence_state != EVIDENCE_FIELD_VERIFIED
                else ERROR_COMPANY_NOT_ONBOARDED)
    return ERROR_OFFICIAL_MATERIAL_REQUIRED


def _annotate_result(result: dict[str, Any], kind: str, started_at: float) -> dict[str, Any]:
    """给 UI 提供最小可解释元数据；不把内部检索细节塞进回答正文。"""
    if not isinstance(result, dict):
        result = {"status": "error", "answer": "服务返回格式无效。"}
    status = str(result.get("status") or "error")
    compliance = "通过" if status in {"answered", "queued"} else "部分完成，逐项见证据" if status == "partial" else "本地官方目录" if status == "documents_listed" else "官方原文待核验" if status == "evidence_retrieved" else "第三方候选" if status == "candidate" else "受控拒答" if status in {"refused", "out_of_scope", "insufficient_evidence"} else "需检查"
    return {**result, "intent": kind, "intent_label": _INTENT_LABELS.get(kind, kind),
            "latency_ms": int((perf_counter() - started_at) * 1000), "compliance": compliance}


CONTEXT_SOFT_BUDGET_TOKENS = 32_000


def _estimated_tokens(text: str) -> int:
    """提供界面用的保守估算；没有模型 tokenizer 时不能伪装成精确 usage。"""
    normalized = str(text or "")
    if not normalized:
        return 0
    # 中文字符和英文/数字混合文本采用统一的可解释估算，结果必须标注为估算。
    return max(1, (len(normalized) + 1) // 2)


def context_usage(context: dict[str, Any], *, answer_mode: str | None = None,
                  budget_tokens: int = CONTEXT_SOFT_BUDGET_TOKENS) -> dict[str, Any]:
    """仅估算本页历史文本长度；聊天历史并非全部发送给模型。"""
    messages = context.get("messages") or []
    message_chars = 0
    evidence_count = 0
    for message in messages:
        message_chars += len(str(message.get("text") or ""))
        answer = message.get("answer") or {}
        if isinstance(answer, dict):
            refs = answer.get("evidence_refs") or answer.get("sources") or []
            if isinstance(refs, list):
                evidence_count += len(refs)
    estimated = _estimated_tokens(" ".join(str(item.get("text") or "") for item in messages))
    budget = max(1, int(budget_tokens))
    ratio = estimated / budget
    if ratio >= 0.95:
        status = "critical"
        label = "接近提醒阈值"
    elif ratio >= 0.80:
        status = "warning"
        label = "可考虑新建会话"
    elif ratio >= 0.60:
        status = "notice"
        label = "注意历史长度"
    else:
        status = "normal"
        label = "正常"
    return {
        "estimated_tokens": estimated,
        "reminder_threshold_tokens": budget,
        "ratio": round(ratio, 4),
        "status": status,
        "label": label,
        "message_count": len(messages),
        "evidence_count": evidence_count,
        "answer_mode": answer_mode,
        "measurement": "estimated",
        "message_chars": message_chars,
    }


def new_context() -> dict[str, Any]:
    return {
        "report_id": None, "ticker": None, "knowledge_ticker": None, "knowledge_year": None,
        "general_scope": None, "job_id": None, "job_status": None,
        # 公司发现是聊天链路的一部分：保存待确认问题，下一轮“港股/A股/9988.HK”可继续完成选择。
        "discovery_query": None, "discovery_ticker": None, "discovery_market": None,
        "discovery_candidates": [], "pending_market": None, "pending_industry_ticker": None, "messages": [],
    }


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



def _discovery_selection(text: str, context: dict[str, Any]) -> dict[str, str] | None:
    """识别公司发现后的自然追问，避免用户必须重新输入整句公司问题。"""
    query = context.get("discovery_query")
    candidates = context.get("discovery_candidates") or []
    if not query or not candidates:
        return None
    normalized = unicodedata.normalize("NFKC", text).strip()
    compact = re.sub(r"[\s：:，,。！？!?（）()]+", "", normalized).upper()
    market = _MARKET_SELECTIONS.get(normalized) or _MARKET_SELECTIONS.get(compact)
    if market is None:
        for candidate in candidates:
            ticker = str(candidate.get("ticker") or "").upper()
            if ticker and compact == re.sub(r"\s+", "", ticker):
                market = str(candidate.get("market") or "").upper() or None
                break
    if market not in {"A", "HK", "US"}:
        return None
    return {"query": str(query), "market": market, "selection": normalized}


def route_message(
    text: str, context: dict[str, Any],
    company_discover: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[str, dict[str, str] | str]:
    normalized = text.strip()
    if not normalized:
        return "guidance", "请输入问题或研究任务。"
    if len(normalized) > 500:
        return "guidance", "问题过长，请控制在 500 字以内。"
    product_reply = _product_reply(normalized)
    if product_reply is not None:
        return "product", product_reply
    selected = _discovery_selection(normalized, context)
    if selected is not None:
        return "discovery", selected
    # 市场选择本身不是失败的公司问答；先保存市场意图，下一轮只需说公司名。
    market_match = _MARKET_ONLY.fullmatch(unicodedata.normalize("NFKC", normalized).strip())
    if market_match:
        market_text = market_match.group(1)
        market = _MARKET_SELECTIONS.get(market_text) or _MARKET_SELECTIONS.get(market_text.upper())
        label = _MARKET_LABELS.get(market, market_text)
        return "market_guidance", {"market": market, "label": label}
    pending_industry_ticker = str(context.get("pending_industry_ticker") or "").upper()
    if pending_industry_ticker and re.search(r"宏观|行业层面|看行业|只看行业|行业趋势", normalized):
        return "research_answer", {"question": normalized, "intent": "industry_observation"}
    if pending_industry_ticker and re.search(r"公司|业务|经营|年报|财报", normalized):
        return "company", {"ticker": pending_industry_ticker, "question": normalized}
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
    # 用户已先选择市场时，允许下一轮只说公司名；不再要求把公司、年份、指标一次填完。
    pending_market = str(context.get("pending_market") or "").upper()
    if pending_market in {"A", "HK", "US"} and not YEAR.search(normalized) and not _REPORT_FIELD.search(normalized):
        compact_company = re.sub(r"[\s，,。！？!?：:（）()]+", "", normalized)
        if 1 <= len(compact_company) <= 80 and not re.search(r"(?:收入|营收|利润|财报|年报|分析|研究|查询|看看|我要|想要)", compact_company):
            return "discovery", {"query": normalized, "market": pending_market}
    try:
        records = catalog()
        targets, unknown_codes = references(normalized, records)
    except CompanySourceUnavailable:
        return "guidance", "公司资料目录不可用，暂不沿用旧报告作答。请检查后端资料版本。"
    industry_explicit = bool(_INDUSTRY.search(normalized))
    research_intent = classify_research(normalized, targets)
    comparison_ready = research_intent != "company_comparison" or (
        len(targets) == 2 and bool(re.search(r"收入|营收|营业收入", normalized))
        and all(str(records[t].get("engine")) == "chinese" for t in targets)
    )
    # 未收录公司必须先进入发现/市场澄清；研究意图不能抢在主体解析之前拦截。
    unknown_company_subject = unverified_company_subject(normalized, records)
    company_research_without_subject = (
        research_intent in {"financial_summary", "company_overview", "company_comparison"}
        and unknown_company_subject
    )
    if (research_intent is not None and comparison_ready and not unknown_codes
            and not company_research_without_subject and not (industry_explicit and targets)):
        return "research_answer", {"question": normalized, "intent": research_intent}
    if len(targets) > 1 or (targets and unknown_codes):
        return "guidance", "请一次只问一家公司；不能混用其他标的的证据。"
    if industry_explicit:
        if targets or unknown_codes:
            if len(targets) == 1 and not unknown_codes and _COMPANY_INDUSTRY.search(normalized):
                return "company_industry_clarification", {
                    "ticker": next(iter(targets)), "question": normalized,
                }
            return "guidance", "行业观察和公司年报请分开提问，不能混用证据。"
        if classify_research(normalized, set()) == "industry_observation":
            return "research_answer", {"question": normalized, "intent": "industry_observation"}
        return "industry", {"question": normalized}
    # 已选定同一资料范围时，允许省略公司名继续请求该公司的年报。
    if not targets and not unknown_codes and context.get("knowledge_ticker") and _is_annual_report_request(normalized):
        targets = {str(context["knowledge_ticker"]).upper()}
    if targets and unknown_company_subject:
        return "guidance", "请一次只问一家公司；无法确认的其他公司不会混入当前年报。"
    if targets:
        ticker = next(iter(targets))
        if _is_annual_report_request(normalized):
            detail_kind, detail = _annual_report_detail(normalized, ticker, records[ticker])
            return detail_kind, detail if detail is not None else "无法解析年报请求。"
        if ticker in CHINESE_TICKERS:
            return "knowledge", {"ticker": ticker, "question": normalized}
        if ticker == "9999.HK":
            return "issuer", {"ticker": ticker, "question": normalized}
        return "company", {"ticker": ticker, "question": normalized}
    # 未命中目录不等于沿用旧报告：无法确认的新公司主体必须在路由层停下。
    if unknown_company_subject:
        if company_discover is not None:
            # 交给受控发现端点识别主体/市场；发现结果仍只是候选，不能直接回答财务数字。
            return "discovery", {"query": normalized}
        _kind, detail_text = _discovery_guidance(normalized)
        if _REPORT_TERM.search(normalized):
            # 保留"暂未收录该公司的可核验年报"这一既有拒答标记，再补充具体的识别状态。
            return "guidance", f"暂未收录该公司的可核验年报；{detail_text}"
        return "guidance", detail_text
    if unknown_codes == {context.get("ticker")} and context["report_id"]:
        return "answer", normalized
    if unknown_codes:
        if company_discover is not None:
            return "discovery", {"query": normalized}
        return "guidance", ("该标的尚未收录可核验的官方资料；请先确认市场、证券代码和所需报告年度，"
                            "不会用其他标的的年报替代。")
    if context.get("discovery_ticker") and not targets:
        return "company", {"ticker": str(context["discovery_ticker"]), "question": normalized}
    if context.get("general_scope") == "issuer" and context.get("ticker"):
        return "issuer", {"ticker": context["ticker"], "question": normalized}
    if not context["report_id"] and context.get("knowledge_ticker"):
        ticker = context["knowledge_ticker"]
        return ("knowledge" if ticker in CHINESE_TICKERS else "company"), {"ticker": ticker, "question": normalized}
    if context["report_id"] and UNKNOWN_COMPANY_PERIOD.search(normalized):
        return "guidance", "无法确认这是否在询问另一家公司，请明确写出已收录公司的代码或名称；不会沿用当前报告作答。"
    if not context["report_id"]:
        return "guidance", "可以先告诉我想看的市场（港股 / A股 / 美股），再说公司名；例如先说“我想看看港股”，下一条只需说“腾讯”。确定公司后，我会继续追问年份和指标。"
    return "answer", normalized


def _candidate_answer(data: dict[str, Any], ticker: str, question: str) -> dict[str, Any]:
    """只解释可识别字段和同期间计算；不猜测第三方接口未提供的币种与单位。"""
    rows = data.get("candidates") or []
    if not data.get("data_available") or not rows:
        return {"status": "refused", "answer": f"{ticker} 的第三方财务数据本次未取得；请检查来源状态或更换明确的年度。",
                "error_code": (data.get("error_codes") or ["MCP_DATA_UNAVAILABLE"])[0], "candidate_data": data}
    metrics: dict[str, tuple[str, Any, dict[str, Any]]] = {}
    for item in rows:
        row = item.get("row") or {}
        if not isinstance(row, dict):
            continue
        label = str(row.get("STD_ITEM_NAME") or row.get("指标") or "")
        value = row.get("AMOUNT")
        if value is None:
            period = str(row.get("statDate") or "")[:4]
            if row.get("netProfit") not in (None, ""):
                label, value = "净利润（BaoStock 原字段 netProfit）", row["netProfit"]
            else:
                years = YEAR.findall(question)
                fiscal_key = f"{years[-1]}1231" if years else ""
                if fiscal_key and row.get(fiscal_key) is not None:
                    value = row[fiscal_key]
                    period = fiscal_key
        else:
            period = str(row.get("REPORT_DATE") or "")[:10]
        if value in (None, ""):
            continue
        key = ("营业额" if label in {"营业额", "营业收入", "营运收入"} else
               "毛利" if label == "毛利" else
               "除税后溢利" if label in {"除税后溢利", "净利润"} or label.startswith("净利润（") else
               "归母净利润" if label in {"股东应占溢利", "归母净利润"} else "")
        if key and (key not in metrics or label == key):
            metrics[key] = (period, value, item)
    wants_revenue = bool(re.search(r"收入|营收|营业额", question))
    wants_profit = bool(re.search(r"净利润|利润|溢利", question))
    wanted = (["营业额"] if wants_revenue and not wants_profit else
              ["归母净利润", "除税后溢利"] if wants_profit and not wants_revenue else
              ["营业额", "毛利", "归母净利润", "除税后溢利"])
    selected = [(name, metrics[name]) for name in wanted if name in metrics]
    if not selected:
        return {"status": "refused", "answer": f"{ticker} 的来源返回了数据，但没有可安全对应到当前问题的字段；请具体问收入或利润，并查看原始字段。",
                "error_code": "MCP_FIELD_UNMAPPED", "candidate_data": data}
    fragments = []
    for name, (period, value, item) in selected[:3]:
        formatted = f"{value:,.0f}" if isinstance(value, (int, float)) else str(value)
        fragments.append(f"{name} {formatted}（报告期 {period or '未注明'}；{item.get('source') or '未知来源'}）")
    source_names = sorted({str(item.get("source")) for _, (_, _, item) in selected})
    answer = f"{ticker} 第三方结构化数据候选：" + "；".join(fragments) + "。原接口未确认币种与数值单位，不作货币换算；此处不是官方年报核验结论或投资建议。"
    return {"status": "candidate", "answer": answer, "verified": False, "evidence_level": "candidate",
            "source_label": " / ".join(source_names), "candidate_data": data}


def dispatch_message(
    context: dict[str, Any], text: str, create_job: Callable[[dict[str, str]], dict[str, Any]],
    ask: Callable[[dict[str, str]], dict[str, Any]], requested_by: str, horizon: str = "中期",
    knowledge_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    issuer_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    industry_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    research_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    candidate_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    company_ask: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    company_discover: Callable[[str, str | None], dict[str, Any]] | None = None,
    market_snapshot_ask: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    answer_mode: str = "official",
) -> dict[str, Any]:
    """按意图进入受控能力；公司识别、资料问答和研究任务不互相冒充。"""
    if answer_mode not in {"official", "mcp"}:
        raise ValueError("answer_mode_invalid")
    kind, detail = route_message(text, context, company_discover)
    updated = _message(context, "user", text.strip())
    if kind == "market_guidance":
        market = str(detail.get("market") or "")
        updated = {**updated, "pending_market": market}
        return _message(updated, "assistant", f"可以，已记住你要看{detail.get('label') or '这个市场'}。请告诉我公司名或证券代码；下一条直接说“腾讯”或“0700.HK”即可。")
    if kind == "company_industry_clarification":
        ticker = str(detail.get("ticker") or "")
        scoped = {**updated, "pending_industry_ticker": ticker}
        return _message(
            scoped, "assistant",
            f"我理解你想看 {ticker} 所在行业最近的发展。这里要区分两个口径："
            "你可以回复“看宏观行业”，我会用官方统计回答行业层面的趋势；"
            "也可以回复“看公司业务”，我会改查公司的年报和经营资料。"
        )
    if kind in {"guidance", "product"}:
        message = str(detail)
        if kind == "product" and answer_mode == "mcp":
            message = "当前为第三方数据模式：可问阿里巴巴 2025 年收入或平安银行 2025 年利润；先确认市场。数据仅供核对，不是官方年报或投资建议。"
        return _message(updated, "assistant", message)
    if kind == "discovery":
        if company_discover is None:
            return _message(updated, "assistant", "公司识别服务暂不可用，请明确写出证券代码和市场。")
        started = perf_counter()
        result = company_discover(str(detail["query"]), detail.get("market"))
        result = _annotate_result(result, kind, started)
        selected_ticker = result.get("selected_ticker")
        candidates = result.get("candidates") or []
        candidate_data = None
        if selected_ticker and result.get("status") == STATUS_MATCHED and candidate_ask is not None:
            years = YEAR.findall(str(detail.get("query") or ""))
            try:
                candidate_data = candidate_ask({"ticker": str(selected_ticker), "year": years[-1] if years else None})
            except Exception as exc:  # noqa: BLE001 - 候选源失败不能阻断公司发现
                candidate_data = {"data_available": False, "error_code": "CANDIDATE_SOURCE_ERROR", "message": type(exc).__name__}
            if isinstance(candidate_data, dict):
                result = {**result, "candidate_data": candidate_data,
                          "evidence_level": "candidate" if candidate_data.get("data_available") else result.get("evidence_level")}
        scoped = {
            **new_context(),
            "ticker": selected_ticker,
            "discovery_query": str(detail["query"]),
            "discovery_ticker": selected_ticker if selected_ticker else None,
            "discovery_market": detail.get("market"),
            "discovery_candidates": candidates,
        }
        scoped = _message(scoped, "user", text.strip())
        if result.get("status") == "ambiguous":
            message = "我找到了多个市场候选，请在下方选择市场；选择后可以直接继续问收入、利润等问题。"
        elif selected_ticker and isinstance(candidate_data, dict) and candidate_data.get("data_available"):
            preview = _candidate_answer(candidate_data, str(selected_ticker), str(detail.get("query") or text))
            message = (preview["answer"] if answer_mode == "mcp" else
                       f"已确认 {selected_ticker}。可切换到第三方数据模式查看结构化数据；官方财务事实仍需官方资料核验。")
            if answer_mode == "mcp":
                result = {**result, **preview}
        elif selected_ticker and not bool(next((c.get("verified") for c in candidates if c.get("ticker") == selected_ticker), False)):
            message = str(result.get("message") or "已确认标的，但官方年报证据尚未接入；可以继续提问，我会返回明确缺口。")
        else:
            message = str(result.get("message") or "已完成公司候选识别；候选资料仍需官方年报接入与字段核验。")
        return _message(scoped, "assistant", message, result)
    if answer_mode == "mcp":
        if market_snapshot_ask is not None and kind in {"company", "knowledge", "issuer"} and is_market_query(text):
            ticker = str(detail.get("ticker"))
            scoped = context if context.get("ticker") == ticker and not context.get("report_id") else {**new_context(), "ticker": ticker, "discovery_ticker": ticker}
            scoped = _message(scoped, "user", text.strip())
            started = perf_counter()
            try:
                snapshot = market_snapshot_ask({"ticker": ticker, "market": scoped.get("market") or scoped.get("discovery_market"), "question": text})
                result = answer_market_snapshot(snapshot, ticker, text)
            except Exception as exc:  # noqa: BLE001
                result = {"status": "refused", "error_code": "MCP_MARKET_SOURCE_ERROR",
                          "answer": "外部行情数据查询失败，请稍后重试；不会用本地旧年报代替当前行情。", "sources": [],
                          "failure": type(exc).__name__}
            result = _annotate_result(result, "mcp_market", started)
            return _message(scoped, "assistant", str(result.get("answer") or result.get("message") or "外部行情暂不可用。"), result)
        if kind == "research_answer":
            try:
                tickers, _ = references(text, catalog())
            except CompanySourceUnavailable:
                tickers = set()
            if len(tickers) != 1:
                return _message(updated, "assistant", "第三方数据模式请一次指定一家公司的市场、年度和指标；目前不做未经对齐的跨公司比较。")
            ticker = next(iter(tickers))
        elif kind in {"company", "knowledge", "issuer"}:
            ticker = str(detail["ticker"])
        else:
            return _message(updated, "assistant", "第三方数据模式只查询结构化公司财务指标；报告生成和行业资料请切换官方资料模式。")
        years = YEAR.findall(text)
        if not years and context.get("discovery_ticker") == ticker:
            years = YEAR.findall(str(context.get("discovery_query") or ""))
        year = years[-1] if years else (str(date.today().year) if "今年" in text else None)
        scoped = context if context.get("ticker") == ticker and not context.get("report_id") else {**new_context(), "ticker": ticker, "discovery_ticker": ticker}
        scoped = _message(scoped, "user", text.strip())
        if candidate_ask is None:
            return _message(scoped, "assistant", "第三方数据服务尚未连接，请检查两个 MCP 的状态。")
        started = perf_counter()
        try:
            candidate_data = candidate_ask({"ticker": ticker, "year": year})
        except Exception as exc:  # noqa: BLE001 - 候选源故障明确降级，不回退到官方资料
            candidate_data = {"data_available": False, "error_codes": ["MCP_SOURCE_ERROR"], "message": type(exc).__name__}
        result = _annotate_result(_candidate_answer(candidate_data, ticker, text), "mcp_data", started)
        return _message(scoped, "assistant", result["answer"], result)
    if kind == "research_answer":
        if research_ask is None:
            if detail.get("intent") == "industry_observation" and industry_ask is not None:
                scoped = {**new_context(), "general_scope": "industry"}
                scoped = _message(scoped, "user", text.strip())
                result = _annotate_result(industry_ask({"question": detail["question"], "requested_by": requested_by}), "industry", perf_counter())
                return _message(scoped, "assistant", str(result.get("answer") or "当前证据不足，暂不回答。"), result)
            return _message(updated, "assistant", "研究回答服务暂不可用，请稍后重试。")
        detail = {**detail, "requested_by": requested_by}
        scoped = context if context.get("general_scope") == "research" and not context.get("report_id") and not context.get("job_id") else {**new_context(), "general_scope": "research"}
        scoped = _message(scoped, "user", text.strip())
        started = perf_counter()
        result = _annotate_result(research_ask(detail), kind, started)
        return _message(scoped, "assistant", str(result.get("answer") or result.get("message") or "当前证据不足，暂不回答。"), result)
    if kind in {"issuer", "industry"}:
        callback = issuer_ask if kind == "issuer" else industry_ask
        if callback is None:
            return _message(updated, "assistant", "这类官方资料问答暂不可用，请稍后重试。")
        scoped = context if context.get("general_scope") == kind and context.get("ticker") == (detail.get("ticker") if kind == "issuer" else None) and not context.get("report_id") and not context.get("job_id") else {**new_context(), "general_scope": kind, "ticker": detail.get("ticker") if kind == "issuer" else None}
        scoped = _message(scoped, "user", text.strip())
        started = perf_counter()
        result = (company_ask if kind == "issuer" and company_ask else callback)({**detail, "requested_by": requested_by})
        result = _annotate_result(result, kind, started)
        return _message(scoped, "assistant", str(result.get("answer") or "当前证据不足，暂不回答。"), result)
    if kind in {"knowledge", "company"}:
        callback = (company_ask or knowledge_ask) if kind == "knowledge" else company_ask
        if callback is None:
            return _message(updated, "assistant", "中文年报问答暂不可用，请稍后重试。")
        ticker = detail["ticker"]
        scoped = select_knowledge_corpus(context, ticker)
        scoped = _message(scoped, "user", text.strip())
        question = detail["question"]
        if (context.get("knowledge_ticker") == ticker and context.get("knowledge_year")
                and not YEAR.search(question) and not QUARTER.search(question)
                and not LIVE.search(question) and "今年" not in question and "去年" not in question):
            question = f"{context['knowledge_year']}年，{question}"
        elif (context.get("discovery_ticker") == ticker and context.get("discovery_query")
              and not YEAR.search(question) and not QUARTER.search(question)
              and not LIVE.search(question) and "今年" not in question and "去年" not in question):
            # 市场确认后继续追问时，继承原问题中的年度，避免用户重复输入“2025年”。
            discovered_years = YEAR.findall(str(context["discovery_query"]))
            if discovered_years:
                question = f"{discovered_years[-1]}年，{question}"
        started = perf_counter()
        result = callback({"ticker": ticker, "question": question, "requested_by": requested_by})
        if (kind == "company" and candidate_ask is not None
                and result.get("error_code") == "MATERIAL_NOT_ONBOARDED"):
            years = YEAR.findall(question)
            try:
                candidate_data = candidate_ask({"ticker": ticker, "year": years[-1] if years else None})
            except Exception as exc:  # noqa: BLE001 - 候选源故障不能改变官方问答的拒答状态
                candidate_data = {"data_available": False, "error_code": "CANDIDATE_SOURCE_ERROR", "message": type(exc).__name__}
            if isinstance(candidate_data, dict) and candidate_data.get("data_available"):
                result = {**result, "candidate_data": candidate_data, "evidence_level": "candidate",
                          "answer": (str(result.get("answer") or "官方资料未接入。")
                                     + " 下方可查看未核验的数据源候选结果；不能当作官方年报结论。")}
        result = _annotate_result(result, kind, started)
        if result.get("status") == "answered":
            scoped = {**scoped, "knowledge_year": next(iter(set(YEAR.findall(question))), None)}
        return _message(scoped, "assistant", str(result.get("answer") or result.get("message") or "当前证据不足，暂不回答。"), result)
    if kind in {"research", "annual_report"}:
        request = {**detail, "horizon": horizon, "requested_by": requested_by}
        request.pop("question", None)
        request.pop("year", None)
        request.pop("used_latest", None)
        request.pop("display_name", None)
        started = perf_counter()
        created = create_job(request)
        if not created.get("job_id"):
            return _message(updated, "assistant", "任务提交未返回编号，未开始跟踪。")
        started_state = {**new_context(), "ticker": detail["ticker"], "job_id": created["job_id"], "job_status": created.get("status") or "queued"}
        started_state = _message(started_state, "user", text.strip())
        if kind == "annual_report":
            message = (f"已识别为{detail['display_name']} {detail['year']} 年年报请求，正在生成基于已核验资料的研究摘要。"
                       "这不是官方原始 PDF；完成后可继续追问有来源的事实。")
        else:
            message = created.get("message") or "报告任务已提交，正在显示七步进度。"
        meta = _annotate_result({"status": "queued", "answer": message}, kind, started)
        return _message(started_state, "assistant", message, meta)
    started = perf_counter()
    result = ask({"report_id": context["report_id"], "ticker": context["ticker"], "question": str(detail), "requested_by": requested_by})
    result = _annotate_result(result, "answer", started)
    return _message(updated, "assistant", str(result.get("answer") or result.get("message") or "本次没有可展示的回答。"), result)


def record_error(context: dict[str, Any], text: str, message: str) -> dict[str, Any]:
    """新公司请求即使后端失败也清掉旧报告，不给错误附着错误证据范围。"""
    kind, detail = route_message(text, context)
    if kind in {"knowledge", "company", "annual_report"}:
        context = select_knowledge_corpus(context, detail["ticker"])
    elif kind in {"issuer", "industry"}:
        context = {**new_context(), "general_scope": kind, "ticker": detail.get("ticker")}
    return _message(_message(context, "user", text.strip()), "assistant", message)
