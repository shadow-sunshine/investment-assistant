"""对话编排：短期状态合并、上下文组装、受控回答调用与持久化。

这一层是"真实回答链路"，不是又一组精确短语正则
----------------------------------------------------
单轮流程固定为::

    提取本轮意图/补充项 → 合并适用会话状态 → 生成有效查询
    → 调用已有受控能力 → 组织回答 → 持久化

关键设计
--------
* **槽位继承靠状态合并，不靠正则猜测**：公司/市场/年度/指标保存在会话状态里，
  追问"那净利润呢"时从状态继承年度，而不是让用户重复输入。
* **换公司必须清掉不适用状态**（公司/年度/证据范围），但保留本会话原始消息与适用偏好；
  不同会话之间绝不共用 pending_slot / 公司 / 年度 / 摘要 / 临时策略。
* **摘要是受控提取式**：从结构化状态 + 已确认约束里提取，标注 ``structured_extractive``，
  保存覆盖到的消息序号与状态版本。摘要失败**不覆盖**上一份有效摘要，也不虚构成功。
* **上下文受预算约束**：近期完整对话默认 8～12 轮作为起点，同时受长度预算裁剪；
  没有可靠 tokenizer 时明确标注估算。32,000 仅作为产品提醒，不是模型真实额度。
* **画像必须真的进入有效查询/上下文**，而不是只返回 profile_id。
* 官方证据与 MCP 候选严格分区：切换模式不删消息，但只清不适用证据状态。

不做的事
--------
不向量化全部聊天、不跨会话全文语义搜索、不自动推断真实资产或持仓、不自动交易、
不自动发布、不把候选数据升级为官方证据。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from .chat_session import (
    dispatch_message, new_context, record_error, route_message, _product_reply,
)
from .company_qa import YEAR
from .question_intents import classify_question, YEAR_REPLY, SELECTION_ALL, LATEST, requested_metrics
from .conversation_store import (
    ConversationStore, ConversationStoreError, DuplicateRequest, get_store,
)
from .user_profile import (
    ProfileCommand, ProfileManager, classify_profile_command, contains_sensitive_content,
    detect_session_tone, hard_constraint_conflict, is_preference_statement,
    render_profile_block, extract_profile_candidates, FIELD_DISPLAY, profile_value_label,
)

#: 近期完整对话默认轮数起点（可配置起点，不是"保证不遗忘"的承诺）。
DEFAULT_RECENT_TURNS = 10
#: 应用层文本估算提醒阈值。产品提醒，不是模型真实上下文容量。
CONTEXT_BUDGET_TOKENS = 32_000
#: 触发摘要/裁剪的预算比例。
SUMMARY_TRIGGER_RATIO = 0.60
#: 组装上下文时保留的最小估算 token 预算。
MIN_CONTEXT_BUDGET = 400

MEASUREMENT_ESTIMATED = "estimated"

_YEAR_TERMS = {"今年": str(date.today().year), "去年": str(date.today().year - 1), "前年": str(date.today().year - 2)}
_METRIC_TERMS = (
    ("收入", "revenue"), ("营收", "revenue"), ("营业额", "revenue"),
    ("净利润", "net_income"), ("归母净利润", "net_income_attributable"), ("利润", "profit"),
    ("毛利", "gross_profit"), ("毛利率", "gross_margin"),
    ("现金流", "cash_flow"), ("经营现金流", "operating_cash_flow"),
    ("资产", "assets"), ("负债", "liabilities"), ("研发", "rd_expense"), ("费用", "expenses"),
)
_OPEN_QUESTIONS = re.compile(r"(?:还(?:需要|差)|请(?:补充|明确|提供)|暂未|未(?:知|确认)|无法确认|待(?:确认|补充)|不清楚)[^。；;]{0,40}$")
_NEGATION_TERMS = ("不要", "不考虑", "不接受", "不看", "别用", "不使用", "排除", "避开")


def _now_context(conversation_id: str | None = None) -> dict[str, Any]:
    """基于既有 ``chat_session.new_context`` 扩展会话级字段，避免重复定义键。"""
    context = new_context()
    context.update({
        "conversation_id": conversation_id,
        "market": None, "year": None, "metric": None,
        "pending_slot": None, "open_questions": [],
        "session_preferences": {}, "profile_disabled_for_session": False,
        "evidence_scope": None, "state_version": 0,
    })
    return context


def estimate_tokens(text: str) -> int:
    """保守估算；**不是** tokenizer 结果，调用方必须标注 ``measurement=estimated``。"""
    normalized = str(text or "")
    return max(1, (len(normalized) + 1) // 2) if normalized else 0


def build_session_titles(text: str) -> str:
    """自动标题第一版：取首条有意义用户消息的前若干字，不依赖模型。"""
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized:
        return ""
    return normalized[:24]


# --- 短期状态合并 ---------------------------------------------------------------

#: 明确的"换主体"动词。出现这些词且剩余文本不是当前公司的别名时，
#: 就要清掉旧公司的年度/证据状态，避免拿旧证据回答新主体。
_SWITCH_MARKERS = re.compile(r"换成|改成|改问|换到|换一家|切换到|切到|看一下|看看|说说|再说|改为")


def _names_other_subject(text: str, current_ticker: str, records: dict[str, dict[str, Any]]) -> bool:
    """文本是否提到了**当前标的之外**的公司主体。

    不能只依赖 ``unverified_company_subject``：它要求句子里有财务事实词，
    所以"换成阿里巴巴"这种纯换主体指令不会被识别，会导致旧年度/旧证据被沿用。
    这里改成：剥掉换主体动词后，剩余文本若既不是当前标的的别名、
    又确实像一个公司名（且能被本地清单或路由提示识别为另一个主体），就判定换主体。
    """
    residual = _SWITCH_MARKERS.sub("", str(text or "")).strip()
    if not residual:
        return False
    current_aliases = {str(item).upper() for item in (records.get(current_ticker) or {}).get("aliases", [])}
    if current_ticker:
        current_aliases.add(str(current_ticker).upper())
    # 残句里若包含当前标的的别名，说明没换主体（"看看腾讯的利润"）。
    if any(alias and alias in residual.upper() for alias in current_aliases if len(alias) >= 2):
        return False
    # 残句里出现**另一个**已收录公司的别名，才是确定的换主体。
    # 别名可能比用户说法更长（清单里是"贵州茅台"，用户常说"茅台"），
    # 因此双向子串都要比：任一方向命中即视为提到该公司。
    for ticker, record in records.items():
        if ticker.upper() == current_ticker.upper():
            continue
        for alias in record.get("aliases", []):
            token = str(alias).strip()
            if not token:
                continue
            if token.upper() in residual.upper() or residual.upper() in token.upper() and len(residual) >= 2:
                return True
    # 命中本地路由提示里的未收录主体（如阿里巴巴/BABA）也算换主体。
    # 路由提示只是导航（navigation_only），这里只用它判断"用户是否提了别家公司"，
    # 不用它回答任何财务数字。
    try:
        from .company_discovery import ROUTING_HINTS
        for name in ROUTING_HINTS:
            if str(name) and str(name).lower() in residual.lower():
                return True
    except Exception:  # noqa: BLE001 - 路由提示不可用不应阻断会话
        return False
    return False


def merge_turn_state(context: dict[str, Any], text: str) -> dict[str, Any]:
    """把本轮明确说出的市场/年度/指标/否定条件合并进会话状态。

    * 只在用户**明确写出**时写入，不从措辞猜测资产、持仓或风险等级。
    * 换公司时由调用方通过 :func:`switch_company_state` 清理，不在这里隐式清空。
    """
    normalized = str(text or "").strip()
    updated = dict(context)
    years = YEAR.findall(normalized)
    if years:
        updated["year"] = years[-1]
    for term, value in _YEAR_TERMS.items():
        if term in normalized:
            updated["year"] = _YEAR_TERMS[term]
            break
    for term, metric in sorted(_METRIC_TERMS, key=lambda item: len(item[0]), reverse=True):
        if term in normalized:
            updated["metric"] = metric
            break
    market = updated.get("market")
    if market in {"A", "HK", "US"} and not re.search(r"港股|a\s*股|美股|hkex|sec|沪深", normalized, re.I):
        # 状态里的市场只在用户本轮显式改写时才覆盖，避免误清。
        pass
    negatives = re.findall(r"(?:不要|不考虑|不接受|不看|别用|不使用|排除|避开)[^，。；;！？!?]{1,40}", normalized)
    existing = list(updated.get("negations") or [])
    updated["negations"] = list(dict.fromkeys([*existing, *negatives]))
    if _OPEN_QUESTIONS.search(normalized):
        questions = list(updated.get("open_questions") or [])
        questions.append(normalized[:80])
        updated["open_questions"] = questions[-5:]
    if updated.get("year") and updated.get("metric") and not updated.get("pending_slot"):
        updated["pending_slot"] = None
    elif not updated.get("year"):
        updated["pending_slot"] = "year" if updated.get("knowledge_ticker") or updated.get("ticker") else updated.get("pending_slot")
    return updated


def switch_company_state(context: dict[str, Any], ticker: str | None) -> dict[str, Any]:
    """切换标的：清掉不适用的公司/年度/证据状态，**保留消息与适用偏好**。"""
    normalized = str(ticker or "").strip().upper() or None
    if normalized and str(context.get("ticker") or "").upper() == normalized:
        return context
    return {**context, "ticker": normalized, "knowledge_ticker": None, "knowledge_year": None,
            "year": None, "evidence_scope": None, "report_id": None,
            "discovery_ticker": None, "discovery_query": None, "discovery_candidates": [],
            "pending_industry_ticker": None, "pending_market": None, "market": None,
            "discovery_market": None, "metric": None, "pending_slot": None, "open_questions": [],
            "job_id": None, "job_status": None, "general_scope": None}


def clear_mode_specific_state(context: dict[str, Any], answer_mode: str) -> dict[str, Any]:
    """切换官方/MCP 模式：**不删消息**，只清不适用证据范围，并标注本轮模式。"""
    if answer_mode not in {"official", "mcp"}:
        raise ValueError("answer_mode_invalid")
    return {**context, "evidence_scope": None, "answer_mode": answer_mode,
            "report_id": None, "job_id": None, "job_status": None, "general_scope": None}


def effective_query_for(context: dict[str, Any], text: str) -> str:
    """只给本会话财务追问补期间；最新、目录、产品问答不沿用旧年报年度。"""
    query = str(text).strip()
    if _product_reply(query) is not None:
        return query
    intent = classify_question(query)
    if intent.kind in {"document_inventory", "market_exploration", "product_help"} or intent.latest:
        return query
    ticker = str(context.get("ticker") or context.get("knowledge_ticker") or context.get("discovery_ticker") or "").strip()
    if ticker and intent.kind in {"financial_question", "year_selection", "metric_selection"}:
        # 选择“年份/都要/那净利润呢”时，必须把当前标的显式交给旧路由，
        # 否则它会把补充槽位误判成新公司问题并进入公司发现。
        try:
            from .company_qa import catalog, references
            targets, unknown_codes = references(query, catalog())
        except Exception:  # noqa: BLE001 - 仅用于判断是否已有主体，不阻断后续受控路由
            targets, unknown_codes = set(), set()
        if not targets and not unknown_codes:
            query = f"{ticker}，{query}"
    year = str(context.get("year") or context.get("knowledge_year") or "").strip()
    if year and not YEAR.search(query) and not re.search(r"今年|去年|前年", query):
        query = f"{year}年，{query}"
    if YEAR_REPLY.fullmatch(str(text).strip()) and context.get("metric"):
        labels = dict((v, k) for k, v in _METRIC_TERMS)
        query += "，" + labels.get(context["metric"], "") + "是多少？"
    return query


def preference_note(values: dict[str, str]) -> str:
    """仅组织研究侧重点，绝不从用户偏好推导公司事实或投资建议。"""
    parts = []
    risk = values.get("risk_preference")
    if risk == "conservative":
        parts.append("按你的保守研究偏好，后续优先核对现金流、负债和下行风险，不把单项增长当作买入依据")
    elif risk == "aggressive":
        parts.append("按本次进取研究偏好，可重点核对增长驱动，同时单独评估波动和估值风险")
    if values.get("research_horizon"):
        parts.append(f"研究期限偏好为 {values['research_horizon']}，单期指标不能代表长期表现")
    if values.get("leverage_constraint") == "none":
        parts.append("继续保留不使用杠杆的约束")
    return "研究偏好提示：" + "；".join(parts) + "。" if parts else ""


def clarification_for(context: dict[str, Any], text: str) -> str | None:
    """自然澄清：只在**确实缺前置条件**时出现，不做填答式猜测。"""
    normalized = str(text or "").strip()
    kind, _detail = route_message(normalized, {**context, "messages": []})
    if kind == "guidance":
        pending = context.get("pending_slot")
        if pending == "year" or (context.get("ticker") and not context.get("year")
                                 and not YEAR.search(normalized) and "今年" not in normalized):
            return "请补充要查询的年份，例如“2025 年收入”。"
    return None


# --- 摘要 -----------------------------------------------------------------------

def build_summary(messages: list[dict[str, Any]], context: dict[str, Any], *,
                  revoked_fields: set[str] | None = None) -> dict[str, Any]:
    """受控提取式摘要：只从结构化状态与硬约束提取，**不生成新事实**。

    摘要覆盖"已确认需求、策略约束、否定条件、当前标的、未解决问题"，
    不包含任何财务数字结论，也不复制检索文档正文。
    """
    revoked = set(revoked_fields or set())
    open_questions = [q for q in (context.get("open_questions") or [])]
    summary = {
        "company": context.get("ticker") or context.get("knowledge_ticker") or context.get("discovery_ticker"),
        "market": context.get("market"),
        "year": context.get("year") or context.get("knowledge_year"),
        "metric": context.get("metric"),
        "pending_slot": context.get("pending_slot"),
        "open_questions": open_questions[-5:],
        "negations": list(context.get("negations") or []),
        "session_preferences": {k: v for k, v in (context.get("session_preferences") or {}).items()
                                if k not in revoked},
        "hard_constraints": {k: v for k, v in (context.get("hard_constraints") or {}).items()
                             if k not in revoked},
        "user_messages": [str(m.get("text") or "")[:60] for m in messages if m.get("role") == "user"][:8],
        "summary_type": "structured_extractive",
    }
    if not any(summary[key] for key in ("company", "year", "metric", "open_questions", "negations", "user_messages")):
        raise ValueError("summary_empty")
    return summary


# --- 上下文组装 -----------------------------------------------------------------

def assemble_context(*, profile: dict[str, dict[str, Any]], context: dict[str, Any],
                     messages: list[dict[str, Any]], summary: dict[str, Any] | None,
                     current_question: str, budget_tokens: int = CONTEXT_BUDGET_TOKENS,
                     recent_turns: int = DEFAULT_RECENT_TURNS) -> dict[str, Any]:
    """按任务书规定的顺序组装上下文，并受预算裁剪。

    顺序：系统规则 → 已确认画像 → 会话结构化状态/临时约束 → 摘要与近期消息
    → 本轮问题 →（本次证据由受控服务单独分区提供，不与历史记忆混流）。
    """
    budget = max(MIN_CONTEXT_BUDGET, int(budget_tokens))
    profile_lines = render_profile_block(
        profile, session_preferences=context.get("session_preferences") or {},
        disabled=bool(context.get("profile_disabled_for_session")),
    )
    state_lines = _state_lines(context)
    constraint_lines = _constraint_lines(context, revoked=set())
    summary_lines = _summary_lines(summary)
    history = [m for m in (messages or []) if not contains_sensitive_content(str(m.get("text") or ""))
               and classify_profile_command(str(m.get("text") or "")).kind == "none"
               and not is_preference_statement(str(m.get("text") or ""))
               and not ((m.get("answer") or {}).get("status") in {"answered", "candidate", "evidence_retrieved"})]
    history = history[-max(1, int(recent_turns)) * 2:]
    # 预算裁剪：从最旧的历史开始丢，优先保留近期消息。
    reserved_lines = profile_lines + state_lines + constraint_lines + summary_lines + [current_question]
    reserved = estimate_tokens("\n".join(_SYSTEM_RULES + reserved_lines)) + 10
    kept = _fit_history(history, budget=budget, reserved=reserved)
    trimmed = len(kept) != len(history)
    history_lines = [f"{'用户' if m.get('role') == 'user' else '助手'}：{str(m.get('text') or '')[:400]}"
                     for m in kept]
    sections = [
        {"order": 1, "name": "system_rules", "lines": _SYSTEM_RULES},
        {"order": 2, "name": "profile", "lines": profile_lines},
        {"order": 3, "name": "session_state", "lines": state_lines + constraint_lines},
        {"order": 4, "name": "history", "lines": summary_lines + history_lines},
        {"order": 5, "name": "current_question", "lines": [current_question] if current_question else []},
    ]
    rendered = "\n".join("\n".join(section["lines"]) for section in sections if section["lines"])
    estimated = estimate_tokens(rendered)
    return {
        "sections": sections,
        "rendered": rendered,
        "estimated_tokens": estimated,
        "measurement": MEASUREMENT_ESTIMATED,
        "budget_tokens": budget,
        "reminder_threshold_tokens": CONTEXT_BUDGET_TOKENS,
        "trimmed": trimmed,
        "recent_turns_configured": max(1, int(recent_turns)),
        "retained_messages": len(kept),
        "dropped_messages": len(messages or []) - len(kept),
        "over_budget": estimated > budget,
        "profile_applied": bool(profile_lines),
        "profile_disabled": bool(context.get("profile_disabled_for_session")),
        "note": "预算为应用层文本估算，不是模型真实上下文额度；裁剪优先丢弃冗余历史，不丢本轮问题与硬约束。",
    }


_SYSTEM_RULES = [
    "只回答已核验的官方年报事实；未覆盖字段明确说明缺口，不用其他期间或其他公司补足。",
    "第三方 MCP 数据是候选，不与官方证据混用，不冒充年报结论。",
    "用户画像只是偏好，不是公司事实，也不是投资适当性证明。",
]


def _state_lines(context: dict[str, Any]) -> list[str]:
    labels = (("公司/标的", context.get("ticker") or context.get("knowledge_ticker")
               or context.get("discovery_ticker")),
              ("市场", context.get("market") or context.get("discovery_market")),
              ("年度", context.get("year") or context.get("knowledge_year")),
              ("指标", context.get("metric")),
              ("待补槽位", context.get("pending_slot")),
              ("数据模式", context.get("answer_mode")))
    return [f"{label}：{value}" for label, value in labels if value]


def _constraint_lines(context: dict[str, Any], *, revoked: set[str]) -> list[str]:
    lines: list[str] = []
    negations = [item for item in (context.get("negations") or []) if item]
    if negations:
        lines.append("本轮否定条件（不得违反）：" + "、".join(negations))
    for field, value in (context.get("hard_constraints") or {}).items():
        if field in revoked or not value:
            continue
        lines.append(f"本轮硬约束 {field}：{value}（与新要求冲突时先澄清）")
    open_questions = context.get("open_questions") or []
    if open_questions:
        lines.append("尚未解决的问题：" + "；".join(str(item) for item in open_questions[-3:]))
    return lines


def _summary_lines(summary: dict[str, Any] | None) -> list[str]:
    if not summary:
        return []
    return [f"历史摘要（{summary.get('summary_type', 'structured_extractive')}，非事实证据）：{summary.get('rendered') or _render_summary(summary)}"]


def _render_summary(summary: dict[str, Any]) -> str:
    parts = []
    for key in ("company", "market", "year", "metric"):
        if summary.get(key):
            parts.append(f"{key}={summary[key]}")
    if summary.get("negations"):
        parts.append("否定条件=" + "、".join(summary["negations"]))
    if summary.get("open_questions"):
        parts.append("待澄清=" + "；".join(summary["open_questions"][-2:]))
    return "；".join(parts) or "无可提取的结构化约束"


def _fit_history(messages: list[dict[str, Any]], *, budget: int, reserved: int) -> list[dict[str, Any]]:
    """按估算预算保留最近消息；本轮问题与硬约束已在 reserved 中预留。"""
    remaining = max(0, budget - reserved)
    kept: list[dict[str, Any]] = []
    for message in reversed(messages):
        cost = estimate_tokens("用户：" + str(message.get("text") or "")[:400]) + 1
        if cost > remaining:
            break
        remaining -= cost
        kept.append(message)
    kept.reverse()
    return kept


# --- 单轮编排 -------------------------------------------------------------------

class ConversationOrchestrator:
    """把存储、画像与既有受控能力串成一条真实链路。"""

    def __init__(self, store: ConversationStore | None = None, *,
                 profile_manager: ProfileManager | None = None) -> None:
        self._store = store if store is not None else get_store()
        self._profiles = profile_manager or ProfileManager(self._store)

    # --- 载入/保存 ---------------------------------------------------------

    def load_context(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> dict[str, Any]:
        """恢复会话：结构化状态 + 原始消息。不同会话之间状态绝不共享。"""
        conversation = self._store.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        if conversation is None:
            raise ConversationStoreError("会话不存在或不可访问。")
        stored = self._store.load_state(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        context = _now_context(conversation_id)
        if stored:
            context.update(stored.get("state") or {})
            context["state_version"] = stored.get("version", 0)
        context["conversation_id"] = conversation_id
        context["answer_mode"] = conversation.answer_mode
        messages = [{"role": item.role, "text": item.text, "answer": item.answer,
                     "message_id": item.message_id, "seq": item.seq, "status": item.status}
                    for item in self._store.list_messages(conversation_id, tenant_id=tenant_id, owner_id=owner_id)]
        context["messages"] = messages
        return context

    def persist(self, context: dict[str, Any], *, tenant_id: str, owner_id: str) -> int | None:
        """保存结构化状态（消息单独走``append_*``，不在这里重复写）。"""
        conversation_id = str(context.get("conversation_id") or "")
        if not conversation_id:
            return None
        persistable = {key: value for key, value in context.items() if key != "messages"}
        return self._store.save_state(conversation_id, persistable, tenant_id=tenant_id, owner_id=owner_id)

    # --- 摘要维护 ---------------------------------------------------------

    def refresh_summary(self, context: dict[str, Any], *, tenant_id: str, owner_id: str,
                        state_version: int, force: bool = False) -> dict[str, Any]:
        """预算触发摘要；**失败时保留上一份有效摘要**并返回可追踪的降级原因。"""
        conversation_id = str(context.get("conversation_id") or "")
        messages = context.get("messages") or []
        existing = self._store.load_summary(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        used = estimate_tokens(" ".join(str(m.get("text") or "") for m in messages))
        triggered = force or (used / max(1, CONTEXT_BUDGET_TOKENS)) >= SUMMARY_TRIGGER_RATIO
        if not triggered:
            return {"status": "not_triggered", "summary": existing}
        revoked = self._store.revoked_profile_fields(tenant_id=tenant_id, owner_id=owner_id)
        try:
            summary = build_summary(messages, context, revoked_fields=revoked)
        except (ValueError, ConversationStoreError) as exc:
            return {"status": "degraded", "reason": type(exc).__name__,
                    "summary": existing, "note": "摘要未更新，上一份有效摘要保持不变。"}
        covered = max((int(m.get("seq") or 0) for m in messages), default=0)
        saved = self._store.save_summary(conversation_id, tenant_id=tenant_id, owner_id=owner_id,
                                         covered_through_seq=covered, state_version=state_version,
                                         summary=summary, summary_type="structured_extractive")
        if not saved:
            return {"status": "degraded", "reason": "save_failed", "summary": existing,
                    "note": "摘要未写入，上一份有效摘要保持不变。"}
        stored = self._store.load_summary(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        return {"status": "ok", "summary": stored}

    # --- 画像在真实链路里的使用 ------------------------------------------

    def profile_effective_context(self, context: dict[str, Any], *, tenant_id: str,
                                  owner_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """返回 (画像, 注入上下文)。画像在这里**真的被渲染成上下文行**。"""
        profile = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)
        assembled = assemble_context(
            profile=profile, context=context, messages=context.get("messages") or [],
            summary=(self._store.load_summary(str(context.get("conversation_id") or ""),
                                              tenant_id=tenant_id, owner_id=owner_id) or {}).get("summary"),
            current_question="", budget_tokens=CONTEXT_BUDGET_TOKENS,
        )
        return profile, assembled

    # --- 单轮 -------------------------------------------------------------

    def handle_turn(self, conversation_id: str, text: str, *, tenant_id: str, owner_id: str,
                    request_id: str, dispatch: Callable[..., dict[str, Any]],
                    requested_by: str = "anonymous", answer_mode: str | None = None,
                    horizon: str = "中期", extra_dispatch_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
        """处理一轮：意图/补充项 → 合并状态 → 有效查询 → 受控回答 → 持久化。

        ``dispatch`` 是既有 :func:`chat_session.dispatch_message` 的适配器，
        由调用方（API/UI）注入实际受控能力；本方法不自己触网、不自己生成答案。
        """
        conversation = self._store.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        if conversation is None:
            return {"status": "conversation_not_found"}
        normalized = str(text or "").strip()
        if not normalized:
            return {"status": "empty_input", "message": "请输入问题或研究任务。"}
        if contains_sensitive_content(normalized):
            normalized = "[敏感信息已隐藏]"
        claimed = self._store.claim_turn(conversation_id, normalized, request_id,
                                         tenant_id=tenant_id, owner_id=owner_id)
        if claimed["status"] != "claimed":
            return claimed
        context = self.load_context(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        context["messages"] = [m for m in context.get("messages", []) if m.get("seq") != claimed["seq"]]
        context["source_message_id"] = claimed["message_id"]
        context.pop("failure", None)
        mode = str(answer_mode or conversation.answer_mode or "official")
        if mode != conversation.answer_mode:
            self._store.set_answer_mode(conversation_id, mode, tenant_id=tenant_id, owner_id=owner_id)
        if mode != context.get("answer_mode"):
            context = clear_mode_specific_state(context, mode)
        profile = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)

        conflict = hard_constraint_conflict(profile, normalized, session_preferences=context.get("session_preferences"))
        if conflict and classify_profile_command(normalized).kind not in {"update", "delete_field", "delete_all", "view", "confirm", "reject"}:
            return self._finish({"status": "needs_clarification", "message": conflict}, context=context,
                conversation_id=conversation_id, user_text=normalized, request_id=request_id,
                tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)
        if normalized == "[敏感信息已隐藏]":
            return self._finish({"status": "refused", "message": "这条消息可能包含密码、账号或令牌，已隐藏且不会写入研究画像或发送给数据服务。请不要提供这些敏感信息。"},
                context=context, conversation_id=conversation_id, user_text=normalized, request_id=request_id,
                tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)
        # 1) 画像命令优先：这些不是财务问题，不能被路由进受控问答。
        profile_result = self._handle_profile_command(
            normalized, context=context, tenant_id=tenant_id, owner_id=owner_id,
            conversation_id=conversation_id, mode=mode)
        if profile_result is not None:
            return self._finish(profile_result, context=context, conversation_id=conversation_id,
                                user_text=normalized, request_id=request_id, tenant_id=tenant_id,
                                owner_id=owner_id, answer_mode=mode)

        # 2) 会话临时偏好（"这次激进一点"）：只改本会话，不覆盖长期画像。
        tone = detect_session_tone(normalized)
        if tone:
            context = {**context, "session_preferences": {**(context.get("session_preferences") or {}),
                                                         "risk_preference": tone}}

        # 3) 硬约束冲突先澄清，不能用优先级默默取消。
        conflict = hard_constraint_conflict(profile, normalized,
                                            session_preferences=context.get("session_preferences"))
        if conflict:
            return self._finish({"status": "needs_clarification", "message": conflict},
                                context=context, conversation_id=conversation_id, user_text=normalized,
                                request_id=request_id, tenant_id=tenant_id, owner_id=owner_id,
                                answer_mode=mode)

        user_text_for_history = normalized
        try:
            task_result, normalized = self._prepare_task(context, normalized, requested_by=requested_by,
                                                         answer_mode=mode, callbacks=extra_dispatch_kwargs or {})
        except Exception as exc:  # 目录/意图阶段出错也必须结束请求并落可重试的中文回复。
            message = "本次任务识别或官方资料查询未完成，来源可能暂不可用。请稍后重试；不会用上轮公司的资料补答。"
            return self._finish({"status": "failed", "message": message,
                                "answer": {"status": "failed", "error_code": type(exc).__name__, "retryable": True}},
                context=context, conversation_id=conversation_id, user_text=user_text_for_history,
                request_id=request_id, tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)
        if task_result is not None:
            return self._finish(task_result, context=context, conversation_id=conversation_id,
                user_text=str(text).strip(), request_id=request_id, tenant_id=tenant_id,
                owner_id=owner_id, answer_mode=mode)
        # 4) 合并短期状态；换公司清掉不适用状态。
        context = self._merge_target(context, normalized)
        if context.get("ticker") and not context.get("report_id") and not context.get("job_id"):
            bare_slot = bool(re.fullmatch(r"(?:20\d{2}年?|今年|去年|前年的?|收入|营收|净利润|利润|现金流|归母净利润|毛利率)[。？?！!]?", normalized))
            from .company_qa import catalog, references
            targets, _ = references(normalized, catalog())
            company_only = bool(targets) and not re.search(r"收入|利润|年报|财报|摘要|概览|风险|业务|现金流|资产|研发|行业|资料|公告|披露|清单|目录|文件", normalized)
            if (bare_slot or company_only) and not (context.get("year") and context.get("metric")):
                pending = "year" if not context.get("year") else "metric"
                context["pending_slot"] = pending
                context["pending_metric_options"] = ["营业收入", "净利润", "经营现金流"]
                question = (f"已选定 {context['ticker']}，想看哪一年？下一条直接说“2025年”即可。"
                            if pending == "year" else f"已记住 {context['year']} 年。想看收入、利润还是现金流？")
                return self._finish({"status": "needs_clarification", "message": question}, context=context,
                                    conversation_id=conversation_id, user_text=normalized, request_id=request_id,
                                    tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)

        # 5) 组装上下文（画像真的进入这里），再交给受控能力。
        summary_record = self._store.load_summary(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        try:
            summary = build_summary(context.get("messages") or [], context,
                        revoked_fields=self._store.revoked_profile_fields(tenant_id=tenant_id, owner_id=owner_id)) if summary_record else None
        except (ValueError, ConversationStoreError):
            summary = None
        assembled = assemble_context(
            profile=profile, context=context, messages=context.get("messages") or [],
            summary=summary, current_question=normalized, budget_tokens=CONTEXT_BUDGET_TOKENS,
        )
        effective_question = self.effective_question(context, normalized, assembled)
        if assembled.get("over_budget"):
            return self._finish({"status": "needs_clarification", "message": "当前问题与有效约束超过本次上下文预算；请缩短问题或整理本次研究条件。不会静默丢弃你的约束。"},
                context=context, conversation_id=conversation_id, user_text=normalized, request_id=request_id,
                tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)
        try:
            updated = dispatch(
                {**context, "messages": [], "conversation_id": conversation_id},
                effective_question, requested_by=requested_by, horizon=horizon,
                answer_mode=mode, conversation_memory=assembled,
                **{k: v for k, v in (extra_dispatch_kwargs or {}).items() if k != "document_ask"},
            )
        except Exception as exc:  # 受控能力异常也保存中文回复，不把内部地址或凭证显示给用户。
            reply = "本次查询未完成，数据服务可能超时或暂不可用。请稍后重试；我不会用旧数据补答。"
            updated = {**context, "messages": [{"role": "assistant", "text": reply,
                       "answer": {"status": "failed", "error_code": type(exc).__name__, "retryable": True}}],
                       "failure": {"error_code": type(exc).__name__, "retryable": True, "message": reply}}
        merged_context, answer, answer_text = self._absorb(updated, context)
        if answer_text is None:
            answer_text = "本次未取得有效回复，请稍后重试。"
            merged_context["failure"] = {"retryable": True, "message": answer_text}
        # 偏好用于组织回答与提示，不改变受控服务的财务事实正文。
        applied = profile if not context.get("profile_disabled_for_session") else {}
        preferences = {k: str(v.get("value") or "") for k, v in applied.items()}
        preferences.update(context.get("session_preferences") or {})
        note = preference_note(preferences)
        if note and not merged_context.get("failure") and (answer or {}).get("status") in {"answered", "partial", "candidate", "evidence_retrieved"}:
            answer_text = answer_text + "\n\n" + note
        elif note and not answer and classify_question(normalized).kind == "financial_question":
            answer_text = answer_text + "\n\n" + note
        return self._finish(
            {"status": "failed" if merged_context.get("failure") else (answer or {}).get("status", "answered"),
             "message": answer_text, "answer": answer, "context": assembled,
             "effective_question": effective_question, "failure": merged_context.get("failure")},
            context=merged_context, conversation_id=conversation_id, user_text=user_text_for_history,
            request_id=request_id, tenant_id=tenant_id, owner_id=owner_id, answer_mode=mode)

    # --- 内部 -------------------------------------------------------------

    def _prepare_task(self, context: dict[str, Any], text: str, *, requested_by: str,
                      answer_mode: str, callbacks: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """先确定业务任务和澄清对象，再借用字段槽位；新意图不能被旧公司/年份抢占。"""
        from .company_qa import catalog, references, unverified_company_subject
        records = catalog()
        targets, unknown = references(text, records)
        product = _product_reply(text)
        if product is not None:
            return {"status": "product", "message": product}, text
        from .research_answer import classify_research
        if not targets and not unknown and classify_research(text, set()) == "concept":
            if callbacks.get("research_ask"):
                answer = callbacks["research_ask"]({"question": text, "intent": "concept", "requested_by": requested_by})
                return {"status": answer.get("status", "answered"), "message": answer.get("answer"), "answer": answer}, text
        intent = classify_question(text, has_explicit_company=bool(targets or unknown))
        if intent.kind == "year_selection" and context.get("task_kind") == "document_inventory" and context.get("ticker"):
            # 文档目录中的“2025年”是筛选目录，不是切回此前的收入字段。
            explicit_year = YEAR.search(text)
            if explicit_year:
                text = f"{context['ticker']} {explicit_year.group()}年官方资料有哪些？"
                targets = {str(context['ticker'])}
                intent = classify_question(text, has_explicit_company=True)
        if intent.latest:
            # “最新”覆盖本轮期间选择，不能被旧 knowledge_year/discovery_query 二次补回。
            context["year"] = None
            context["knowledge_year"] = None
            context["discovery_query"] = None
        if intent.kind == "product_help":
            return {"status": "product", "message": "可以直接说公司名或证券代码，再说明要看官方资料目录、公告或财务指标。查目录不需要先选年份；问财务指标时再选择报告期间。左侧可以新建/切换会话，研究偏好需确认后保存。"}, text
        if intent.kind == "market_exploration" or (intent.kind == "document_inventory" and intent.market and not targets and not unknown):
            # 广义市场问题不是“仍然在问腾讯”。清旧财务范围，仅保留适用偏好与消息。
            for key in ("ticker", "knowledge_ticker", "knowledge_year", "year", "metric", "report_id", "job_id", "job_status", "discovery_ticker", "discovery_query", "pending_industry_ticker", "general_scope"):
                context[key] = None
            context["market"] = intent.market
            context["pending_market"] = intent.market
            context["pending_slot"] = "company"
            context["pending_task_kind"] = "document_inventory" if intent.kind == "document_inventory" else None
            label = {"HK": "港股", "US": "美股", "A": "A股"}.get(intent.market, "这个市场")
            if intent.kind == "document_inventory":
                message = f"可以查看{label}公司的官方公告。你想看哪家公司？直接说公司名或代码即可，比如“腾讯”或“0700.HK”。"
            else:
                message = f"你想了解{label}的市场走势、某个行业，还是某家公司的公告/业绩？当前我能核对已入库官方披露，但没有覆盖此刻全市场行情；先告诉我关注方向或公司即可。"
            return {"status": "needs_clarification", "message": message}, text
        if _SWITCH_MARKERS.match(text) and not intent.metrics and intent.kind == "other":
            if len(targets) == 1 and not unknown:
                context.update(switch_company_state(context, next(iter(targets))))
                text = str(next(iter(targets)))
            elif callbacks.get("company_discover"):
                query = _SWITCH_MARKERS.sub("", text, count=1).strip("的 。？！!?")
                context.update(switch_company_state(context, None))
                result = callbacks["company_discover"](query, None)
                ticker = result.get("selected_ticker")
                if ticker:
                    context.update(ticker=ticker, discovery_ticker=ticker, discovery_query=query,
                                   discovery_candidates=result.get("candidates") or [], pending_slot="year",
                                   pending_market=None, pending_metric_options=["营业收入", "净利润", "经营现金流"])
                    return {"status": "needs_clarification", "message": f"已切换到 {ticker}，旧公司的年度与证据范围已清理。想查哪一年，或要先看最新官方资料目录？"}, text
                return {"status": result.get("status", "needs_clarification"), "message": result.get("message") or "请补充公司全称或代码和市场。", "answer": result}, text
        if context.get("pending_task_kind") == "document_inventory" and len(targets) == 1 and not intent.metrics:
            intent = classify_question("最新官方资料有哪些？", has_explicit_company=True)
        if intent.kind == "document_inventory":
            if len(targets) > 1 or unknown:
                return {"status": "needs_clarification", "message": "请先确定一家公司的官方资料目录；未知代码需要先确认公司与市场。"}, text
            ticker = next(iter(targets)) if targets else context.get("ticker") or context.get("discovery_ticker")
            if not ticker:
                context["pending_slot"] = "company"
                context["pending_task_kind"] = "document_inventory"
                return {"status": "needs_clarification", "message": "想查哪家公司的官方资料或公告？先说公司名/代码即可，不需要先选年份或财务指标。"}, text
            if targets:
                context.update(self._merge_target(context, text))
            context["ticker"] = ticker
            context["pending_slot"] = None
            context["pending_task_kind"] = None
            context["task_kind"] = "document_inventory"
            context["metric"] = None
            context["pending_metric_options"] = []
            # 原句仍由目录服务做主体校验；只替换明确的无主体追问外壳。
            query = text
            if not targets:
                if unverified_company_subject(text, records, include_non_fact=True):
                    return {"status": "needs_clarification", "message": "请明确公司名或代码，避免把另一家公司的披露混入当前目录。"}, text
                period = YEAR.search(text)
                selector = f"{period.group()}年" if period else "最新"
                query = f"{ticker} {selector}公告有哪些？" if "公告" in text else f"{ticker} {selector}官方资料有哪些？"
            elif context.get("pending_task_kind") == "document_inventory" or intent.kind == "document_inventory" and not re.search(r"资料|公告|披露|年报|清单|目录", text):
                query = f"{ticker} 最新官方资料有哪些？"
            if answer_mode == "mcp":
                return {"status": "needs_clarification", "message": "官方公告目录请切换到官方资料模式；MCP 模式用于第三方结构化财务候选，不冒充官方披露。"}, text
            callback = callbacks.get("document_ask")
            if callback is None:
                from .official_document_answers import OfficialDocumentAnswerService
                answer = OfficialDocumentAnswerService().inventory(str(ticker), query, requested_by=requested_by)
            else:
                answer = callback({"ticker": ticker, "question": query, "requested_by": requested_by})
            return {"status": answer.get("status", "refused"), "message": answer.get("answer"), "answer": answer,
                    "effective_question": query}, text
        if intent.kind == "metric_selection":
            options = context.get("pending_metric_options") if context.get("pending_slot") == "metric" else None
            if not options:
                return {"status": "needs_clarification", "message": "你说的“都要”是指哪些内容？可以说“收入、净利润和经营现金流都看”，我会逐项说明证据和缺口。"}, text
            text = "和".join(options) + "是多少？"
            context["pending_slot"] = None
        if intent.kind == "financial_question":
            context["task_kind"] = "financial_question"
            context["pending_task_kind"] = None
        # 纯年份补充必须有明确待补槽位，避免把旧指标当作用户本轮重新选择的指标。
        if intent.kind == "year_selection" and context.get("pending_slot") == "year":
            context["metric"] = None
        return None, text

    def _merge_target(self, context: dict[str, Any], text: str) -> dict[str, Any]:
        """检测本轮是否换标的；换了才清公司/年度/证据状态。"""
        from .company_qa import CompanySourceUnavailable, catalog, references
        try:
            records = catalog()
            targets, unknown = references(text, records)
        except (CompanySourceUnavailable, Exception):  # noqa: BLE001 - 目录不可用不应阻断会话
            records, targets, unknown = {}, set(), set()
        current = str(context.get("ticker") or context.get("knowledge_ticker") or "").upper()
        if len(targets) == 1 and not unknown:
            ticker = next(iter(targets))
            if current and ticker != current:
                context = switch_company_state(context, ticker)
            elif not current:
                context = {**context, "ticker": ticker}
            context["knowledge_ticker"] = ticker
        elif current and _names_other_subject(text, current, records):
            # 明确提出新公司（可能尚未接入官方资料）：清掉旧公司的年度/证据状态，
            # 让受控能力按新主体澄清，而不是拿旧公司的证据作答。
            context = switch_company_state(context, None)
        merged = merge_turn_state(context, text)
        for term, market in (("港股", "HK"), ("美股", "US"), ("A股", "A"), ("A 股", "A")):
            if term.casefold() in text.casefold():
                merged["market"] = market
        if merged.get("ticker"):
            t = str(merged["ticker"])
            actual_market = "HK" if t.endswith(".HK") else "A" if t.endswith((".SZ", ".SS")) else "US"
            merged["market"] = actual_market
        if merged.get("year") and merged.get("metric"):
            merged["pending_slot"] = None
        if merged.get("year") and merged.get("knowledge_ticker") and not merged.get("knowledge_year"):
            merged["knowledge_year"] = merged["year"]
        # 上一轮路由留下的"待选市场"是本会话已确认的市场意图，要提升为会话状态，
        # 否则"港股 → 腾讯"这类多轮选择会在下一轮丢失市场，跨会话也可能串味。
        pending_market = str(context.get("pending_market") or context.get("discovery_market") or "").upper()
        if pending_market in {"A", "HK", "US"} and not merged.get("market"):
            merged["market"] = pending_market
        if merged.get("discovery_market") and not merged.get("market"):
            merged["market"] = str(merged["discovery_market"]).upper()
        return merged

    def effective_question(self, context: dict[str, Any], text: str,
                           assembled: dict[str, Any] | None = None) -> str:
        """实例方法包装 :func:`effective_query_for`，便于在测试与调用方复用同一实现。"""
        return effective_query_for(context, text)

    def _absorb(self, updated: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        """把dispatch 返回的新状态合并回会话状态。

        返回 ``(新状态, 本轮 answer 元数据, 本轮助手回复正文)``。
        历史消息由 :meth:`_finish` 通过 ``append_turn`` 统一落库，这里**不能**把
        ``updated["messages"]`` 塞回 context，否则本轮回复会被历史覆盖、丢给下一轮。
        """
        failure = updated.get("failure")
        merged = {**context, **{k: v for k, v in updated.items() if k != "messages"}}
        merged["messages"] = context.get("messages") or []
        if merged.get("discovery_ticker"):
            merged["ticker"] = merged["discovery_ticker"]
        if not merged.get("report_id") and merged.get("ticker") and not merged.get("knowledge_ticker"):
            merged["discovery_ticker"] = merged["ticker"]
        if merged.get("pending_market"):
            merged["market"] = merged["pending_market"]
        if failure:
            merged["failure"] = failure
        reply: dict[str, Any] | None = None
        reply_text: str | None = None
        produced = [m for m in (updated.get("messages") or []) if m.get("role") == "assistant"]
        if produced:
            last = produced[-1]
            reply = last.get("answer") if isinstance(last.get("answer"), dict) else None
            reply_text = str(last.get("text") or "")
        return merged, reply, reply_text

    def _handle_profile_command(self, text: str, *, context: dict[str, Any], tenant_id: str,
                                owner_id: str, conversation_id: str, mode: str) -> dict[str, Any] | None:
        """处理画像命令；不是画像命令返回 ``None`` 交给正常链路。"""
        if contains_sensitive_content(text):
            return None
        command = classify_profile_command(text)
        if command.kind not in {"confirm", "reject"}:
            for pending in self._profiles.pending_candidates(tenant_id=tenant_id, owner_id=owner_id):
                if pending.conversation_id == conversation_id:
                    self._store.resolve_profile_candidate(pending.candidate_id, tenant_id=tenant_id,
                        owner_id=owner_id, status="superseded", expected_version=pending.version)
        if command.kind == "none":
            # "我偏保守，主要看港股"这类陈述没有命令动词，但按任务书仍要先问是否保存。
            if not is_preference_statement(text):
                return None
            command = ProfileCommand("remember", body=text)
        if command.kind == "view":
            active = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)
            if not active:
                message = "当前没有已确认的长期偏好。可以说“我偏保守，主要看港股”，我会先请你确认再保存。"
            else:
                lines = [f"- {FIELD_DISPLAY.get(entry['field'], entry['field'])}：{profile_value_label(entry['field'], entry['value'])}" for entry in
                         sorted(active.values(), key=lambda item: item["field"])]
                message = "你已确认的长期偏好：\n" + "\n".join(lines) + "\n这些都是研究偏好，不是投资适当性结论。"
            return {"status": "profile_viewed", "message": message, "profile": active}
        if command.kind == "disable_for_session":
            context["profile_disabled_for_session"] = True
            return {"status": "profile_disabled", "message": "本次对话不使用长期偏好；长期档案没有被删除。"}
        if command.kind in {"confirm", "reject"}:
            return self._resolve_pending(context, tenant_id=tenant_id, owner_id=owner_id,
                                         conversation_id=conversation_id, accept=command.kind == "confirm")
        if command.kind == "delete_all":
            active = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)
            pending = [self._store.create_profile_candidate(tenant_id=tenant_id, owner_id=owner_id,
                       field=field, proposed_value="", previous_value=entry["value"], op="delete",
                       conversation_id=conversation_id, source_message_id=context.get("source_message_id"))
                       for field, entry in active.items()]
            if not pending:
                active = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)
                if not active:
                    return {"status": "nothing_to_delete", "message": "当前没有已确认的长期偏好。"}
                return {"status": "needs_confirmation", "candidates": [], "message":
                        f"将删除你全部 {len(active)} 项长期偏好（"
                        + "、".join(sorted(active)) + "）。请回复“确认”后再执行。",
                        "pending_all_delete": True}
            return {"status": "needs_confirmation", "candidates": [c.to_dict() for c in pending],
                    "message": "请确认要删除哪些长期偏好后回复“确认”。"}
        if command.kind == "delete_field":
            candidates = self._profiles.stage_field_deletion(text, tenant_id=tenant_id, owner_id=owner_id,
                                                             conversation_id=conversation_id)
            if not candidates:
                return {"status": "nothing_to_delete", "message": "没有找到对应的长期偏好项，请说明要删除哪一项。"}
            candidate = candidates[0]
            return {"status": "needs_confirmation", "candidates": [candidate.to_dict()],
                    "message": f"将删除偏好「{candidate.field}：{candidate.previous_value}」。请回复“确认”后执行。"}
        if command.kind in {"remember", "update"}:
            allow_new = command.kind == "remember"
            candidates = self._profiles.stage_candidates(
                text if command.kind == "remember" else f"{command.body}{command.value}",
                tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id,
                source_message_id=context.get("source_message_id"), allow_new=allow_new)
            if not candidates:
                return {"status": "no_profile_candidate",
                        "message": "没有识别到可保存的长期偏好；我不会从措辞猜测年龄、收入、资产或风险等级。"}
            context["profile_confirmation_ids"] = [item.candidate_id for item in candidates]
            previews = [f"- {FIELD_DISPLAY.get(item.field, item.field)}：{profile_value_label(item.field, item.previous_value)} → {profile_value_label(item.field, item.proposed_value)}"
                        if item.previous_value else f"- {FIELD_DISPLAY.get(item.field, item.field)}：{profile_value_label(item.field, item.proposed_value)}"
                        for item in candidates]
            return {"status": "needs_confirmation", "candidates": [item.to_dict() for item in candidates],
                    "message": "拟保存以下长期偏好：\n" + "\n".join(previews) + "\n\n回复“确认”保存，回复“取消”放弃。"}
        if command.kind == "session_tone":
            tone = detect_session_tone(text)
            if tone:
                field = "risk_preference" if tone in {"conservative", "aggressive", "neutral"} else "analysis_preference"
                context["session_preferences"] = {**(context.get("session_preferences") or {}), field: tone}
            return {"status": "session_tone", "message": "已按本会话的临时偏好处理；长期画像不会被覆盖。"}
        return None

    def _resolve_pending(self, context: dict[str, Any], *, tenant_id: str, owner_id: str,
                         conversation_id: str, accept: bool) -> dict[str, Any]:
        pending = [c for c in self._profiles.pending_candidates(tenant_id=tenant_id, owner_id=owner_id)
                   if c.conversation_id == conversation_id]
        previous = next((m for m in reversed(context.get("messages") or []) if m.get("role") == "assistant"), {})
        if not pending and accept and (previous.get("answer") or {}).get("status") in {"needs_confirmation", "profile_confirmed", "profile_already_confirmed"}:
            ids = context.get("profile_confirmation_ids") or []
            resolved = [self._store.get_profile_candidate(cid, tenant_id=tenant_id, owner_id=owner_id) for cid in ids]
            if resolved and all(c is not None and c.status == "confirmed" and c.conversation_id == conversation_id for c in resolved):
                active = self._profiles.active_profile(tenant_id=tenant_id, owner_id=owner_id)
                if all(c.op != "delete" and (active.get(c.field) or {}).get("value") == c.proposed_value for c in resolved):
                    return {"status": "profile_already_confirmed", "message": "这些研究偏好已保存（刚才已通过确认按钮处理），不需要重复确认。可以在左侧“研究偏好”查看或修改。"}
        if not pending:
            return {"status": "no_pending_confirmation",
                    "message": "当前没有待确认的偏好修改；需要保存时请直接说明。"}
        if not accept:
            for candidate in pending:
                self._profiles.reject_candidate(candidate.candidate_id, tenant_id=tenant_id,
                                                owner_id=owner_id,
                                                expected_version=candidate.version)
            return {"status": "profile_rejected", "message": "已放弃保存，长期画像没有变化。"}
        applied: list[str] = []
        deleted: list[str] = []
        for candidate in pending:
            if candidate.op == "delete":
                result = self._profiles.confirm_deletion(candidate.candidate_id, tenant_id=tenant_id,
                                                         owner_id=owner_id,
                                                         expected_version=candidate.version)
                if result.get("status") == "deleted":
                    deleted.append(candidate.field)
            else:
                result = self._profiles.confirm_candidate(candidate.candidate_id, tenant_id=tenant_id,
                                                          owner_id=owner_id,
                                                          expected_version=candidate.version)
                if result.get("status") == "confirmed":
                    applied.append(candidate.field)
        parts = []
        if applied:
            parts.append("已保存：" + "、".join(FIELD_DISPLAY.get(f, f) for f in applied))
        if deleted:
            parts.append("已删除：" + "、".join(FIELD_DISPLAY.get(f, f) for f in deleted))
        if not parts:
            return {"status": "confirmation_stale", "message": "待确认内容已过期或已被替换，请重新说明一次。"}
        return {"status": "profile_confirmed", "message": "；".join(parts) + "。这些是研究偏好，不是投资建议。",
                "applied_fields": applied, "deleted_fields": deleted}

    def _finish(self, result: dict[str, Any], *, context: dict[str, Any], conversation_id: str,
                user_text: str, request_id: str, tenant_id: str, owner_id: str,
                answer_mode: str) -> dict[str, Any]:
        """同事务写入一问一答；标题只在空标题时自动生成。"""
        message = str(result.get("message") or "本次请求未完成，请重试。")
        answer_payload = result.get("answer")
        if not isinstance(answer_payload, dict):
            answer_payload = {"status": result.get("status", "answered")}
        answer_payload = {**answer_payload, "answer_mode": answer_mode}
        try:
            completed = self._store.complete_turn(conversation_id, request_id,
                tenant_id=tenant_id, owner_id=owner_id, answer_text=message, answer=answer_payload,
                state={**context, "answer_mode": answer_mode})
        except ConversationStoreError:
            return {"status": "persist_failed", "message": "本轮结果未能保存，请重试。"}
        if completed is None:
            return {"status": "conversation_not_found", "message": "会话已删除或请求已中断，迟到结果未写入。"}
        user_message, assistant_message = completed
        self._store.apply_auto_title(conversation_id, build_session_titles(user_text), tenant_id=tenant_id, owner_id=owner_id)
        refreshed = self.load_context(conversation_id, tenant_id=tenant_id, owner_id=owner_id)
        state_version = refreshed.get("state_version", 0)
        summary_result = self.refresh_summary(refreshed, tenant_id=tenant_id, owner_id=owner_id,
                                              state_version=state_version)
        return {**result, "message": message, "answer": answer_payload, "conversation_id": conversation_id,
                "message_id": user_message.message_id, "assistant_message_id": assistant_message.message_id,
                "request_id": request_id, "state_version": state_version, "summary": summary_result.get("status"),
                "summary_degraded_reason": summary_result.get("reason"), "answer_mode": answer_mode}
