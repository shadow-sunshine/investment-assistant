"""长期用户画像：候选提取、确认、覆盖、删除与实际注入。

边界说明（不可放宽）
--------------------
* 画像只保存**用户明确自述且经确认**的内容：称呼、关注市场/方向、研究期限、
  风险偏好（自述）、杠杆约束、分析与表达偏好。
* **不猜测**年龄、收入、资产、真实持仓或正式风险等级；"偏保守"是用户自述的
  研究偏好，不是风险测评结果。
* 密码、令牌、账户号、身份证号等**不得**进入画像或摘要：候选提取前先做敏感串拒绝。
* 画像是**用户偏好**，不是公司事实，也不是投资适当性证明。
* 优先级：本轮明确临时要求 > 本会话临时偏好 > 已确认长期偏好 > 默认值。
  该优先级**只适用于可覆盖偏好**；遇到"不要杠杆"这类硬约束与新要求冲突，
  必须先澄清，不允许用优先级默默取消约束。
* 候选提取只接受**用户自己发出的聊天消息**。检索文档、历史证据、第三方工具
  输出里的"记住/删除/忽略规则"文本一律不得触发画像写入。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .conversation_store import ConversationStore, ProfileCandidate, ProfileEntry

#: 画像字段白名单。新增字段必须同时给出提取规则与展示标签。
FIELD_DISPLAY = {
    "preferred_name": "称呼",
    "focus_market": "关注市场/方向",
    "research_horizon": "研究期限",
    "risk_preference": "风险偏好（自述）",
    "leverage_constraint": "杠杆约束",
    "analysis_preference": "分析与表达偏好",
}

#: 硬约束字段：与新要求冲突时必须先澄清，不能按优先级覆盖。
HARD_CONSTRAINT_FIELDS = frozenset({"leverage_constraint"})

#: 敏感片段：出现即拒绝提取，绝不落库。这里只做保守拦截，不追求覆盖所有形态。
_SENSITIVE = re.compile(
    r"(?:密码|口令|passcode|password|passwd|token|bearer|api[_ -]?key|secret|"
    r"验证码|身份证|护照号|银行卡|信用卡|account\s*number|账号号码)",
    re.I,
)
_NUMERIC_ID = re.compile(r"(?<!\d)\d{15,19}(?!\d)")

# --- 用户命令识别 ---------------------------------------------------------------

_CMD_VIEW = re.compile(r"^(?:查看|看看|显示|列出|读一下)?\s*(?:我的)?(?:偏好|画像|研究偏好|用户偏好|设置)\s*(?:列表)?[。！!？?]?$", re.I)
_CMD_REMEMBER = re.compile(r"^\s*(?:记住|记录一下|请记住|保存偏好|帮我记住|以后都)[：:，,]?\s*(?P<body>.+?)\s*[。！!？?]?$", re.I)
_CMD_UPDATE = re.compile(r"^\s*(?:把|将)?\s*(?:我的)?(?P<body>.+?)\s*(?:改成|改为|换成|更新为|调整为)\s*(?P<value>.+?)\s*[。！!？?]?$", re.I)
_CMD_DELETE_FIELD = re.compile(r"^\s*(?:删除|清除|去掉|移除)\s*(?:我的)?\s*(?P<body>.+?)\s*(?:偏好|画像|设置)?\s*[。！!？?]?$", re.I)
_CMD_DELETE_ALL = re.compile(r"^\s*(?:删除|清除|移除)\s*(?:我的)?(?:全部|所有)\s*(?:偏好|画像|设置)\s*[。！!？?]?$", re.I)
_CMD_CONFIRM = re.compile(r"^\s*(?:确认|保存|是的|对|没错|可以|同意|就这个|确定)\s*[。！!？?]?$", re.I)
_CMD_REJECT = re.compile(r"^\s*(?:取消|不保存|不用了|不要|拒绝|否|算了)\s*[。！!？?]?$", re.I)
_CMD_DISABLE_PROFILE = re.compile(r"^\s*(?:本次|本轮|这次)?\s*(?:不使用|停用|先不用|忽略)\s*(?:我的)?(?:长期)?\s*(?:偏好|画像)\s*[。！!？?]?$", re.I)
_CMD_SESSION_ONLY = re.compile(r"^\s*(?:这次|本次|本轮)\s*(?:先)?\s*(?:激进|保守|中性|随意)一点\s*[。！!？?]?$", re.I)

_SESSION_TONE = {
    "激进": "aggressive",
    "保守": "conservative",
    "中性": "neutral",
    "随意": "relaxed",
}

# --- 自述偏好提取 ---------------------------------------------------------------

_MARKET_TERMS = (
    ("港股", "HK"), ("港股市场", "HK"), ("hkex", "HK"), ("香港", "HK"),
    ("a股", "A"), ("a 股", "A"), ("沪深", "A"),
    ("美股", "US"), ("纳斯达克", "US"), ("纽交所", "US"), ("sec", "US"),
)
_HORIZON_YEARS = re.compile(r"(?P<years>\d|[一二三四五六七八九十两])\s*年(?:内|以内|之内)?(?:的)?(?:研究|覆盖|关注)?(?:期限| horizon)?", re.I)
_HORIZON_TERMS = {
    "短期": "short_term", "中期": "mid_term", "长期": "long_term",
    "长线": "long_term", "短线": "short_term",
}
_RISK_TERMS = {
    "偏保守": "conservative", "保守": "conservative", "稳健": "conservative",
    "偏激进": "aggressive", "激进": "aggressive", "进取": "aggressive",
    "中性": "neutral", "中性偏保守": "conservative",
}
_PREFERENCE_TERMS = {
    "简洁": "concise", "简要": "concise", "简短": "concise",
    "详细": "detailed", "详细一点": "detailed", "展开讲": "detailed",
    "要结论": "conclusion_first", "先给结论": "conclusion_first",
    "多给表格": "tabular", "表格": "tabular",
}
# "不要用任何杠杆" / "不接受杠杆" / "避免杠杆操作" 都是同一类硬约束表达；
# 中间可能夹一个"用"字，所以必须显式允许，不能假设"不要"后紧跟"杠杆"。
_NO_LEVERAGE = re.compile(r"(?:不要|不使用|不用|不接受|不碰|避免|不能有|不得用)\s*(?:用|做|上)?\s*(?:任何)?\s*杠杆(?:融资|操作)?")
_LEVERAGE_LIMIT = re.compile(r"(?:杠杆(?:融资|)?\s*(?:不(?:得|能)超过|不超过|上限)\s*\d+(?:\.\d+)?\s*倍)|(最多\s*\d+(?:\.\d+)?\s*倍杠杆)")

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


class ProfileCommand:
    """一次用户输入的画像意图判定结果。"""

    def __init__(self, kind: str, *, body: str = "", value: str = "") -> None:
        self.kind = kind
        self.body = body
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"ProfileCommand({self.kind!r}, body={self.body!r}, value={self.value!r})"


def classify_profile_command(text: str) -> ProfileCommand:
    """只做**用户显式命令**判定；普通财务提问返回 ``none``，不会被误当成画像操作。

    另外单独识别"陈述式偏好"（"我偏保守，主要看港股"）：它没有"记住"这类动词，
    但按任务书仍要生成候选并请用户确认，因此返回 ``preference_statement``。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return ProfileCommand("none")
    if _CMD_DELETE_ALL.match(normalized):
        return ProfileCommand("delete_all")
    if _CMD_VIEW.match(normalized):
        return ProfileCommand("view")
    if _CMD_DISABLE_PROFILE.match(normalized):
        return ProfileCommand("disable_for_session")
    if _CMD_REJECT.match(normalized):
        return ProfileCommand("reject")
    if _CMD_CONFIRM.match(normalized):
        return ProfileCommand("confirm")
    update = _CMD_UPDATE.match(normalized)
    if update and re.search(r"偏好|画像|研究期限|风险|杠杆|关注市场|称呼|名字|姓名|表达|分析风格", update.group("body")):
        return ProfileCommand("update", body=update.group("body").strip(), value=update.group("value").strip())
    delete = _CMD_DELETE_FIELD.match(normalized)
    if delete:
        return ProfileCommand("delete_field", body=delete.group("body").strip())
    session = _CMD_SESSION_ONLY.match(normalized)
    if session:
        return ProfileCommand("session_tone", body=session.group(0).strip())
    remember = _CMD_REMEMBER.match(normalized)
    if remember:
        return ProfileCommand("remember", body=remember.group("body").strip())
    return ProfileCommand("none")


#: 陈述式偏好：主语是"我"且带偏好/约束信号。
#: 必须排除财务提问（"我的研究期限改成三年"里含"收入"就不该当画像命令），
#: 也不能匹配纯事实问题，否则会把财务问答误判成画像操作。
_FACT_QUERY = re.compile(r"收入|营收|营业额|利润|净利润|现金流|资产负债|年报|财报|股价|收盘价|市值|多少|怎么样|如何|对比")
_STATEMENT_PREFERENCE = re.compile(
    r"^\s*我(?:主要|重点|只|一般|比较|偏|倾向|习惯|更)?\s*"
    r"(?:关注|看|研究|投|做)?\s*(?:保守|激进|稳健|进取|中性|港股|a\s*股|美股|沪深|香港|"
    r"行业|短线|长线|短期|中期|长期|杠杆|估值|现金流|成长|价值)"
    r".{0,30}$",
    re.I,
)


def is_preference_statement(text: str) -> bool:
    """判断是否是"我这样/我偏 X"式的偏好陈述（而非财务提问或闲聊）。"""
    normalized = str(text or "").strip()
    if not normalized or len(normalized) > 160:
        return False
    if _FACT_QUERY.search(normalized):
        return False
    return bool(_STATEMENT_PREFERENCE.match(normalized) or re.fullmatch(
        r"(?:我叫|我的名字是)[一-龥A-Za-z0-9 _-]{1,20}[。!?！]?|我的(?:风险偏好|研究期限|分析偏好|关注市场)(?:是|为|：|:).{1,60}", normalized))


def contains_sensitive_content(text: str) -> bool:
    """候选提取前的硬拦截：敏感串一律不进入画像，也不进入摘要。"""
    value = str(text or "")
    return bool(_SENSITIVE.search(value)) or bool(_NUMERIC_ID.search(value))


def _normalize_horizon(raw: str) -> str | None:
    text = str(raw or "").strip()
    for term, code in _HORIZON_TERMS.items():
        if term in text:
            return code
    match = _HORIZON_YEARS.search(text)
    if match:
        token = match.group("years")
        years = int(token) if token.isdigit() else _CN_DIGITS.get(token)
        if years:
            return f"{years}y"
    return None


def extract_profile_candidates(text: str, *, existing: dict[str, str] | None = None,
                                allow_new: bool = True) -> list[dict[str, str]]:
    """从**用户自述消息**提取候选；返回 ``[{field, value, op, previous_value}]``。

    * ``allow_new=False`` 时只做覆盖（"改成三年"），不新增字段。
    * 敏感内容直接返回空列表，且**不留任何部分结果**。
    * 每条候选都带 ``previous_value``，供确认时展示"修改前后"。
    """
    normalized = str(text or "").strip()
    if not normalized or contains_sensitive_content(normalized):
        return []
    current = dict(existing or {})
    found: list[dict[str, str]] = []

    def add(field: str, value: str) -> None:
        if not allow_new and field not in current:
            return
        if current.get(field) == value:
            # 与现值相同：不产生"修改"候选，避免无意义确认。
            return
        found.append({"field": field, "value": value, "op": "update" if field in current else "create",
                      "previous_value": current.get(field)})

    for term, code in _MARKET_TERMS:
        if term in normalized.lower() and re.search(r"(?:主要|重点|只|专注|关注|看|研究)", normalized):
            add("focus_market", code)
            break
    horizon = _normalize_horizon(normalized)
    if horizon:
        add("research_horizon", horizon)
    for term, code in _RISK_TERMS.items():
        if term in normalized and not re.search(r"(?:不要|不|不接受|避免)\s*" + re.escape(term), normalized):
            add("risk_preference", code)
            break
    for term, code in _PREFERENCE_TERMS.items():
        if term in normalized:
            add("analysis_preference", code)
            break
    if _NO_LEVERAGE.search(normalized):
        add("leverage_constraint", "none")
    else:
        limit = _LEVERAGE_LIMIT.search(normalized)
        if limit:
            add("leverage_constraint", (limit.group(1) or limit.group(0)).strip())
    name = re.match(r"^\s*(?:我叫|我是)\s*([一-龥A-Za-z][一-龥A-Za-z0-9 _-]{0,19})[。！？!?]?$", normalized)
    if name:
        add("preferred_name", name.group(1).strip(" _-"))
    return found


def _candidate_preview(field: str, value: str, previous: str | None) -> str:
    label = FIELD_DISPLAY.get(field, field)
    if previous:
        return f"{label}：{previous} → {value}"
    return f"{label}：{value}"


def profile_value_label(field: str, value: str | None) -> str:
    labels = {"HK": "港股", "US": "美股", "A": "A股", "conservative": "保守", "aggressive": "进取", "neutral": "中性", "none": "不使用杠杆", "long_term": "长期", "short_term": "短期", "mid_term": "中期", "long": "长期", "short": "短期", "medium": "中期", "detailed": "详细", "concise": "简洁", "tabular": "表格", "conclusion_first": "结论优先"}
    return labels.get(str(value), str(value or "未设置"))


class ProfileManager:
    """画像候选与已确认档案的业务封装；所有操作都带服务端归属。"""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store

    # --- 读 ---------------------------------------------------------------

    def active_profile(self, *, tenant_id: str, owner_id: str) -> dict[str, dict[str, Any]]:
        """只返回**未撤销**的已确认画像，供上下文组装使用。"""
        return {entry.field: entry.to_dict()
                for entry in self._store.list_profile_entries(tenant_id=tenant_id, owner_id=owner_id)
                if entry.confirmed}

    def profile_values(self, *, tenant_id: str, owner_id: str) -> dict[str, str]:
        return {field: entry["value"] for field, entry in self.active_profile(tenant_id=tenant_id, owner_id=owner_id).items()}

    def pending_candidates(self, *, tenant_id: str, owner_id: str) -> list[ProfileCandidate]:
        return self._store.list_profile_candidates(tenant_id=tenant_id, owner_id=owner_id, status="pending")

    # --- 写 ---------------------------------------------------------------

    def stage_candidates(self, text: str, *, tenant_id: str, owner_id: str,
                         conversation_id: str | None = None,
                         source_message_id: str | None = None,
                         allow_new: bool = True) -> list[ProfileCandidate]:
        """生成**待确认候选**；不写画像。

        已撤销字段**允许用户主动重新声明**（删除后再说要重新生效是合法操作），
        但仍然必须走确认。撤销标记真正拦住的是"旧摘要自动复活偏好"这条路径，
        那由 :func:`conversation_memory.build_summary` 的 ``revoked_fields`` 负责。
        """
        extracted = extract_profile_candidates(
            text, existing=self.profile_values(tenant_id=tenant_id, owner_id=owner_id),
            allow_new=allow_new)
        return [self._store.create_profile_candidate(
            tenant_id=tenant_id, owner_id=owner_id, field=item["field"],
            proposed_value=item["value"], previous_value=item["previous_value"], op=item["op"],
            conversation_id=conversation_id, source_message_id=source_message_id,
        ) for item in extracted]

    def confirm_candidate(self, candidate_id: str, *, tenant_id: str, owner_id: str,
                          expected_version: int | None = None) -> dict[str, Any]:
        """确认必须绑定 candidate_id（可选 version）；已处理/跨用户的候选一律拒绝。"""
        candidate = self._store.get_profile_candidate(candidate_id, tenant_id=tenant_id, owner_id=owner_id)
        if candidate is None:
            return {"status": "not_found"}
        if candidate.status != "pending":
            return {"status": "already_resolved", "candidate": candidate.to_dict()}
        if expected_version is not None and int(candidate.version) != int(expected_version):
            return {"status": "version_mismatch", "candidate": candidate.to_dict()}
        if candidate.field not in FIELD_DISPLAY:
            return {"status": "unknown_field", "candidate": candidate.to_dict()}
        return self._store.apply_profile_candidate(candidate_id, tenant_id=tenant_id,
                                                   owner_id=owner_id, expected_version=expected_version)

    def reject_candidate(self, candidate_id: str, *, tenant_id: str, owner_id: str,
                         expected_version: int | None = None) -> dict[str, Any]:
        if self._store.get_profile_candidate(candidate_id, tenant_id=tenant_id, owner_id=owner_id) is None:
            return {"status": "not_found"}
        resolved = self._store.resolve_profile_candidate(candidate_id, tenant_id=tenant_id,
                                                        owner_id=owner_id, status="rejected",
                                                        expected_version=expected_version)
        return {"status": "rejected" if resolved else "already_resolved"}

    def set_field(self, field: str, value: str, *, tenant_id: str, owner_id: str,
                  source_message_id: str | None = None) -> ProfileEntry | None:
        """直接改值（API 用），展示前后由调用方负责。"""
        if field not in FIELD_DISPLAY or not str(value or "").strip():
            return None
        return self._store.upsert_profile_entry(
            tenant_id=tenant_id, owner_id=owner_id, field=field, value=str(value).strip(),
            source_message_id=source_message_id, confirmed=True)

    def delete_field(self, field: str, *, tenant_id: str, owner_id: str) -> bool:
        """单项删除；撤销标记让旧摘要/旧历史无法复活该偏好。"""
        return self._store.revoke_profile_field(field, tenant_id=tenant_id, owner_id=owner_id)

    def delete_all(self, *, tenant_id: str, owner_id: str) -> int:
        return self._store.revoke_all_profile_fields(tenant_id=tenant_id, owner_id=owner_id)

    def stage_field_deletion(self, text: str, *, tenant_id: str, owner_id: str,
                             conversation_id: str | None = None) -> list[ProfileCandidate]:
        """把"删除我的风险偏好"这类请求变成**待确认候选**，避免一句模糊话直接删档案。"""
        command = classify_profile_command(text)
        if command.kind != "delete_field":
            return []
        field = self._match_field_by_words(command.body)
        if field is None:
            return []
        current = self.profile_values(tenant_id=tenant_id, owner_id=owner_id)
        if field not in current:
            return []
        return [self._store.create_profile_candidate(
            tenant_id=tenant_id, owner_id=owner_id, field=field,
            proposed_value="", previous_value=current[field], op="delete",
            conversation_id=conversation_id,
        )]

    def confirm_deletion(self, candidate_id: str, *, tenant_id: str, owner_id: str,
                         expected_version: int | None = None) -> dict[str, Any]:
        """执行删除候选；同样绑定版本，不可重放。"""
        candidate = self._store.get_profile_candidate(candidate_id, tenant_id=tenant_id, owner_id=owner_id)
        if candidate is None:
            return {"status": "not_found"}
        if candidate.status != "pending":
            return {"status": "already_resolved"}
        if expected_version is not None and int(candidate.version) != int(expected_version):
            return {"status": "version_mismatch"}
        if candidate.op != "delete":
            return {"status": "not_a_deletion"}
        return self._store.apply_profile_candidate(candidate_id, tenant_id=tenant_id,
                                                   owner_id=owner_id, expected_version=expected_version)

    def _match_field_by_words(self, text: str) -> str | None:
        """把"风险偏好/杠杆/期限/市场/称呼/表达"等自然词映射到白名单字段。"""
        value = str(text or "")
        if not value:
            return None
        table = (
            (("杠杆",), "leverage_constraint"),
            (("风险", "保守", "激进", "稳健"), "risk_preference"),
            (("期限", "年限", "多久", "几年"), "research_horizon"),
            (("市场", "港股", "a股", "a 股", "美股", "方向", "行业"), "focus_market"),
            (("称呼", "名字", "姓名"), "preferred_name"),
            (("表达", "分析", "风格", "详细", "简洁", "表格"), "analysis_preference"),
        )
        for keywords, field in table:
            if any(keyword in value.lower() for keyword in keywords):
                return field
        return None


# --- 上下文注入 -----------------------------------------------------------------

DEFAULT_PROFILE_LINES: tuple[str, ...] = ()

#: 偏好 → 注入到上下文的可读说明。措辞刻意保持"用户偏好"，不得写成投资结论。
_PROFILE_CONTEXT_TEMPLATE = {
    "preferred_name": "称呼偏好：{value}",
    "focus_market": "关注市场/方向：{value}（用户自述偏好，不代表覆盖其他市场）",
    "research_horizon": "研究期限偏好：{value}",
    "risk_preference": "风险偏好（用户自述，非正式风险测评）：{value}",
    "leverage_constraint": "硬约束：{value}；与新要求冲突时必须先澄清，不得默默取消",
    "analysis_preference": "分析与表达偏好：{value}",
}

#: 会话临时偏好（只在本会话生效，不写长期画像）。
SESSION_PREFERENCE_FIELDS = frozenset({"risk_preference", "analysis_preference", "focus_market"})


def render_profile_block(profile: dict[str, dict[str, Any]], *,
                         session_preferences: dict[str, str] | None = None,
                         disabled: bool = False) -> list[str]:
    """把画像与会话临时偏好渲染成上下文行。

    ``disabled=True``（"本次不使用长期偏好"）时返回空列表：**不删除长期档案**，
    只是这一轮不注入。
    """
    if disabled:
        profile = {}
    lines: list[str] = []
    # 可覆盖偏好按优先级合并：会话临时 > 长期。
    merged: dict[str, str] = {}
    for field, entry in (profile or {}).items():
        if field in HARD_CONSTRAINT_FIELDS or field not in _PROFILE_CONTEXT_TEMPLATE:
            continue
        merged[field] = str(entry.get("value") or "")
    for field, value in (session_preferences or {}).items():
        if field in SESSION_PREFERENCE_FIELDS and field not in HARD_CONSTRAINT_FIELDS:
            merged[field] = str(value)
    for field in sorted(merged):
        template = _PROFILE_CONTEXT_TEMPLATE.get(field)
        if template and merged[field]:
            lines.append(template.format(value=merged[field]))
    # 硬约束单独放在最后，避免被普通偏好淹没。
    for field in sorted(HARD_CONSTRAINT_FIELDS):
        entry = (profile or {}).get(field)
        if entry and str(entry.get("value") or ""):
            lines.append(_PROFILE_CONTEXT_TEMPLATE[field].format(value=entry["value"]))
    return lines


def hard_constraint_conflict(profile: dict[str, dict[str, Any]], requested_text: str, *,
                             session_preferences: dict[str, str] | None = None) -> str | None:
    """检测新要求是否与硬约束冲突；冲突时返回澄清文案，必须由上层先澄清。"""
    text = str(requested_text or "")
    if not text:
        return None
    constraint = str((profile.get("leverage_constraint") or {}).get("value") or
                     (session_preferences or {}).get("leverage_constraint") or "")
    if not constraint:
        return None
    # 三种表达都要算作"要求用杠杆"：直接说杠杆（"可以加杠杆"）、
    # 带倍数的说法（"用3倍杠杆"）、以及"上杠杆"。漏掉任一都会让硬约束被绕过。
    wants_leverage = bool(
        re.search(r"(?:用|加|上|做|接受|可以|允许|想)\s*(?:[\d.]+\s*倍)?\s*杠杆", text)
        or re.search(r"杠杆(?:融资|操作|仓位)", text)
    )
    if not wants_leverage:
        return None
    if constraint == "none":
        return ("你之前明确说过不使用杠杆，这一条我不会默默取消；请确认这次是要保留该硬约束，"
                "还是明确修改它。")
    match = re.search(r"(\d+(?:\.\d+)?)\s*倍", constraint)
    wanted = re.search(r"(\d+(?:\.\d+)?)\s*倍", text)
    if match and wanted and float(wanted.group(1)) > float(match.group(1)):
        return (f"你之前的杠杆上限是 {match.group(1)} 倍，本次提到 {wanted.group(1)} 倍；"
                "请确认是否要修改这个硬约束，我不会自动放宽。")
    return None


def summarize_tone(value: str) -> str:
    """把"激进一点"映射到规范化偏好值，供会话临时偏好使用。"""
    return _SESSION_TONE.get(str(value or "").strip(), "")


def detect_session_tone(text: str) -> str | None:
    """识别"这次激进一点"这类只影响本会话的临时偏好。"""
    normalized = str(text or "").strip()
    if not _CMD_SESSION_ONLY.match(normalized):
        return None
    for term, code in _SESSION_TONE.items():
        if term in normalized:
            return code
    return None