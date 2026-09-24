"""基于已生成报告的有证据边界问答。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ANSWERED = "answered"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
OUT_OF_SCOPE = "out_of_scope"
GENERATOR_FAILED = "generator_failed"

ERROR_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
ERROR_OUT_OF_SCOPE = "OUT_OF_SCOPE"
ERROR_GENERATOR_FAILED = "ANSWER_GENERATOR_FAILED"
ERROR_ANSWER_VALIDATION = "ANSWER_EVIDENCE_VALIDATION_FAILED"

_REPORT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_TICKER = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{1,5}(?:\.[A-Z]{1,3})?|[0-9]{1,5})(?![A-Za-z0-9])")
_MARKET_KEYS = {"latest_close", "latest_trading_date", "period_return_pct", "one_month_return_pct", "annualized_volatility_pct", "max_drawdown_pct", "volume_latest"}
_METRICS = (
    ("latest_close", "最新收盘价", ("最新收盘价", "收盘价", "股价", "价格")),
    ("latest_trading_date", "最新交易日", ("最新交易日", "交易日")),
    ("period_return_pct", "样本期收益率", ("样本期收益", "期间收益", "区间收益", "收益率")),
    ("one_month_return_pct", "近一月收益率", ("近一月收益", "一个月收益", "月收益")),
    ("annualized_volatility_pct", "年化波动率", ("年化波动率", "波动率")),
    ("max_drawdown_pct", "最大回撤", ("最大回撤", "回撤")),
    ("volume_latest", "最新成交量", ("成交量", "交易量")),
    ("revenue", "营收", ("营收", "收入", "营业收入")),
    ("revenue_period_end", "营收对应期间截止日", ("营收对应期间", "营收截止日")),
    ("net_income", "净利润", ("净利润", "盈利")),
    ("net_income_period_end", "净利润对应期间截止日", ("净利润对应期间", "净利润截止日")),
    ("free_cash_flow", "自由现金流", ("自由现金流", "现金流")),
    ("free_cash_flow_period_end", "自由现金流对应期间截止日", ("现金流对应期间", "现金流截止日")),
    ("trailing_pe", "市盈率 PE", ("市盈率", "PE", "pe")),
    ("price_to_book", "市净率 PB", ("市净率", "PB", "pb")),
    ("valuation_as_of", "估值快照时间", ("估值时间", "估值快照", "估值抓取")),
)


class AnswerGenerator(Protocol):
    """可注入的窄范围生成器；本期 API 默认不启用。"""

    def generate(self, context: "ReportEvidence", question: str) -> "Answer": ...


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    ref: str
    label: str
    file_name: str | None = None
    page: str | None = None
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "label": self.label, "file_name": self.file_name, "page": self.page, "value": self.value}


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    evidence_refs: tuple[EvidenceRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "evidence_refs": [item.to_dict() for item in self.evidence_refs]}


@dataclass
class Answer:
    status: str
    report_id: str
    ticker: str
    question: str
    answer: str
    claims: list[AnswerClaim] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    error_code: str | None = None
    requested_by: str = "anonymous"
    access_control: dict[str, str] = field(default_factory=lambda: {"mode": "mvp_attribution_only", "message": "requested_by 仅用于记录发起人，不等同于身份认证或报告授权。"})

    def __post_init__(self) -> None:
        if not self.evidence_refs:
            refs = [ref for claim in self.claims for ref in claim.evidence_refs]
            self.evidence_refs = list({(ref.kind, ref.ref): ref for ref in refs}.values())

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "error_code": self.error_code, "report_id": self.report_id, "ticker": self.ticker, "question": self.question, "answer": self.answer, "claims": [claim.to_dict() for claim in self.claims], "evidence_refs": [ref.to_dict() for ref in self.evidence_refs], "limitations": self.limitations, "requested_by": self.requested_by, "access_control": self.access_control}


@dataclass(frozen=True)
class ReportEvidence:
    report_id: str
    ticker: str
    audit: dict[str, Any]
    report: str


class ReportEvidenceError(ValueError):
    pass


def load_report_evidence(report_dir: Path, report_id: str) -> ReportEvidence:
    normalized = str(report_id).strip()
    if not normalized or not _REPORT_ID.fullmatch(normalized):
        raise FileNotFoundError(normalized)
    report_path = report_dir / f"{normalized}.md"
    audit_path = report_dir / f"{normalized}.json"
    if not report_path.is_file() or not audit_path.is_file():
        raise FileNotFoundError(normalized)
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportEvidenceError("报告审计 JSON 无法读取。") from exc
    if not isinstance(audit, dict) or not str(audit.get("ticker") or "").strip():
        raise ReportEvidenceError("报告审计 JSON 缺少 ticker。")
    try:
        report = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportEvidenceError("报告正文无法读取。") from exc
    return ReportEvidence(normalized, str(audit["ticker"]).strip().upper(), audit, report)


def _value(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _snapshot_ref(context: ReportEvidence, path: str, label: str | None = None) -> EvidenceRef | None:
    item = _value(context.audit, path)
    return None if item is None else EvidenceRef("snapshot", path, label or path, value=item)


def _source_map(context: ReportEvidence) -> dict[str, dict[str, Any]]:
    result = {}
    for source in context.audit.get("sources") or []:
        if not isinstance(source, dict):
            continue
        metadata = source.get("metadata") or {}
        citation = str(source.get("citation") or "").strip().upper()
        source_ticker = str(metadata.get("ticker") or source.get("ticker") or "").strip().upper()
        if citation and source_ticker == context.ticker:
            result[citation] = source
    return result


def _source_ref(context: ReportEvidence, citation: str) -> EvidenceRef | None:
    source = _source_map(context).get(citation.upper())
    if source is None:
        return None
    metadata = source.get("metadata") or {}
    page = metadata.get("page")
    return EvidenceRef("source", citation.upper(), f"{metadata.get('file_name') or '未命名来源'} 第 {page or '未提供'} 页", str(metadata.get("file_name") or "") or None, str(page) if page is not None else None, {"ticker": context.ticker, "content": str(source.get("content") or "")[:500]})


def validate_answer(answer: Answer, context: ReportEvidence) -> list[str]:
    """只校验引用结构和 ticker 边界，不证明文本与证据存在语义蕴含。"""
    errors = []
    source_map = _source_map(context)
    for index, claim in enumerate(answer.claims):
        if not claim.evidence_refs:
            errors.append(f"claim[{index}] 缺少证据引用")
        for ref in claim.evidence_refs:
            if ref.kind == "source" and source_map.get(ref.ref.upper()) is None:
                errors.append(f"claim[{index}] 引用了不存在或 ticker 不一致的 {ref.ref}")
            elif ref.kind == "snapshot" and _value(context.audit, ref.ref) is None:
                errors.append(f"claim[{index}] 引用了不存在的结构化字段 {ref.ref}")
            elif ref.kind not in {"source", "snapshot"}:
                errors.append(f"claim[{index}] 使用了不允许的证据类型 {ref.kind}")
    return errors


def _format(key: str, value: Any) -> str:
    if value is None or value == "" or value == "数据不可用":
        return "数据不可用"
    if key.endswith("_pct") and isinstance(value, (int, float)):
        return f"{value:,.2f}%"
    if isinstance(value, (int, float)):
        return f"{value:,.4f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _answer(context: ReportEvidence, question: str, requested_by: str, **kwargs: Any) -> Answer:
    return Answer(report_id=context.report_id, ticker=context.ticker, question=question, requested_by=requested_by or "anonymous", **kwargs)


def _out_of_scope(context: ReportEvidence, question: str, requested_by: str, text: str) -> Answer:
    return _answer(context, question, requested_by, status=OUT_OF_SCOPE, error_code=ERROR_OUT_OF_SCOPE, answer=text, limitations=["问答只处理当前 report_id 的结构化快照、已审计来源和报告状态，不提供开放式联网问答。"])


def _foreign_ticker(context: ReportEvidence, question: str) -> str | None:
    known = {"AAPL", "MSFT", "GOOG", "AMZN", "TSLA", "0700"}
    for token in _TICKER.findall(question.upper()):
        if token != context.ticker and ("." in token or token in known):
            return token
    return None


def _facts(context: ReportEvidence, question: str, requested_by: str) -> Answer | None:
    matches = []
    for key, label, keywords in _METRICS:
        if any(word in question for word in keywords):
            group = "market_snapshot" if key in _MARKET_KEYS else "financial_snapshot"
            matches.append((key, label, group))
    if not matches:
        return None

    # 同一问题的所有指标必须齐全；不可用快照中的旧值也不能作为回答。
    missing = [
        label for key, label, group in matches
        if _value(context.audit, f"{group}.data_available") is False
        or _value(context.audit, f"{group}.{key}") in (None, "", "数据不可用")
    ]
    if missing:
        return _answer(
            context, question, requested_by, status=INSUFFICIENT_EVIDENCE,
            error_code=ERROR_INSUFFICIENT_EVIDENCE,
            answer="报告中缺少回答全部所问指标所需的可用数据。",
            limitations=["缺失或不可用：" + "、".join(missing) + "。没有返回不完整指标组合，也没有使用失效快照中的旧值。"],
        )
    claims = []
    for key, label, group in matches:
        path = f"{group}.{key}"
        value = _value(context.audit, path)
        ref = _snapshot_ref(context, path, label)
        claims.append(AnswerClaim(f"{context.ticker} 报告中的{label}为 {_format(key, value)}。", (ref,)))
    return _answer(context, question, requested_by, status=ANSWERED, answer="\n".join(claim.text for claim in claims), claims=claims)


def _locations(context: ReportEvidence, question: str, requested_by: str) -> Answer:
    requested = list(dict.fromkeys(item.upper() for item in re.findall(r"(?<![A-Za-z0-9])S\d+(?![A-Za-z0-9])", question.upper())))
    sources = _source_map(context)
    missing = [citation for citation in requested if citation not in sources]
    if missing:
        return _answer(
            context, question, requested_by, status=INSUFFICIENT_EVIDENCE,
            error_code=ERROR_INSUFFICIENT_EVIDENCE,
            answer="部分指定引用在当前报告中不存在或 ticker 不一致，不能返回不完整的来源列表。",
            limitations=["不可用引用：" + "、".join(missing) + "。"],
        )
    citations = requested or list(sources)
    claims = []
    for citation in citations:
        ref = _source_ref(context, citation)
        if ref is not None:
            claims.append(AnswerClaim(f"[{citation}] 对应来源文件 {ref.file_name or '未命名'}，页码为 {ref.page or '未提供'}。", (ref,)))
    if not claims:
        return _answer(context, question, requested_by, status=INSUFFICIENT_EVIDENCE, error_code=ERROR_INSUFFICIENT_EVIDENCE, answer="报告没有可验证的来源引用或页码信息。", limitations=["系统不会根据文件名或页码格式猜测不存在的来源。"])
    return _answer(context, question, requested_by, status=ANSWERED, answer="\n".join(claim.text for claim in claims), claims=claims)


def _degradation(context: ReportEvidence, question: str, requested_by: str) -> Answer:
    claims = []
    llm = context.audit.get("llm_result") or {}
    if llm.get("used") is not None:
        ref = _snapshot_ref(context, "llm_result.used", "报告生成模式")
        if ref:
            claims.append(AnswerClaim(("使用了受控 LLM" if llm["used"] else "未采用 LLM，使用规则版") + "。", (ref,)))
    if llm.get("reason"):
        ref = _snapshot_ref(context, "llm_result.reason", "报告模式原因")
        if ref:
            claims.append(AnswerClaim(f"记录的原因是：{llm['reason']}", (ref,)))
    for path, label in (("evaluation.passed", "Safety 校验"), ("retrieval_evaluation.passed", "检索质量校验")):
        ref = _snapshot_ref(context, path, label)
        if ref:
            claims.append(AnswerClaim(f"{label}{'通过' if ref.value else '未通过'}。", (ref,)))
    for index, finding in enumerate((context.audit.get("evaluation") or {}).get("findings") or []):
        ref = _snapshot_ref(context, f"evaluation.findings.{index}", "Safety 校验发现")
        if ref:
            claims.append(AnswerClaim(f"Safety 校验记录：{finding}", (ref,)))
    if not claims:
        return _answer(context, question, requested_by, status=INSUFFICIENT_EVIDENCE, error_code=ERROR_INSUFFICIENT_EVIDENCE, answer="审计 JSON 没有记录足够的降级或校验原因。", limitations=["没有根据报告正文推断未记录的系统内部原因。"])
    return _answer(context, question, requested_by, status=ANSWERED, answer="\n".join(claim.text for claim in claims), claims=claims)


def _risks(context: ReportEvidence, question: str, requested_by: str) -> Answer:
    claims = []
    for index, flag in enumerate(context.audit.get("risk_flags") or []):
        ref = _snapshot_ref(context, f"risk_flags.{index}", "风险旗标")
        if ref:
            claims.append(AnswerClaim(f"风险旗标：{flag}", (ref,)))
    for group, label in (("market_snapshot", "行情数据"), ("financial_snapshot", "财务数据")):
        available = _snapshot_ref(context, f"{group}.data_available", label)
        if available and available.value is False:
            claims.append(AnswerClaim(f"{label}当前标记为不可用。", (available,)))
        for index, field_name in enumerate(_value(context.audit, f"{group}.unavailable_fields") or []):
            ref = _snapshot_ref(context, f"{group}.unavailable_fields.{index}", "缺失字段")
            if ref:
                claims.append(AnswerClaim(f"{label}缺失字段：{field_name}", (ref,)))
    if not claims:
        ref = _snapshot_ref(context, "risk_flags", "风险旗标")
        if ref is None:
            return _answer(context, question, requested_by, status=INSUFFICIENT_EVIDENCE, error_code=ERROR_INSUFFICIENT_EVIDENCE, answer="报告没有记录可验证的风险或数据缺失信息。", limitations=["系统不会把未记录的风险推断成已确认事实。"])
        claims.append(AnswerClaim("该报告的审计记录未标记风险旗标；这不等于不存在投资风险。", (ref,)))
    return _answer(context, question, requested_by, status=ANSWERED, answer="\n".join(claim.text for claim in claims), claims=claims)


def _rule(context: ReportEvidence, question: str, requested_by: str) -> Answer:
    foreign = _foreign_ticker(context, question)
    if foreign:
        return _out_of_scope(context, question, requested_by, f"问题包含其他标的 {foreign}，当前问答只允许讨论 {context.ticker} 报告。")
    if any(word in question for word in ("来源", "证据", "引用", "页码", "哪一页", "文件", "出处")):
        return _locations(context, question, requested_by)
    if any(word in question for word in ("降级", "规则版", "LLM", "校验", "检索质量", "为什么不是")):
        return _degradation(context, question, requested_by)
    if any(word in question for word in ("风险", "缺失", "不可用", "缺数据", "数据是否完整")):
        return _risks(context, question, requested_by)
    result = _facts(context, question, requested_by)
    return result if result is not None else _out_of_scope(context, question, requested_by, "这个问题超出当前报告的证据范围，系统没有足够的已审计证据回答。")


def answer_question(context: ReportEvidence, question: str, requested_by: str = "anonymous", generator: AnswerGenerator | None = None) -> Answer:
    """默认使用确定性抽取；注入生成器时只做引用结构校验，不宣称语义支持已验证。"""
    normalized = str(question or "").strip()
    if not normalized:
        return _out_of_scope(context, normalized, requested_by, "问题不能为空。")
    if generator is None:
        return _rule(context, normalized, requested_by)
    try:
        answer = generator.generate(context, normalized)
    except Exception:  # noqa: BLE001 - 生成器边界不应泄露内部实现细节
        return _answer(context, normalized, requested_by, status=GENERATOR_FAILED, error_code=ERROR_GENERATOR_FAILED, answer="回答生成器暂时不可用，未返回未经校验的内容。", limitations=["本次回答未使用外部生成结果；请稍后重试或使用规则抽取问题。"])
    errors = validate_answer(answer, context)
    if errors:
        return _answer(context, normalized, requested_by, status=INSUFFICIENT_EVIDENCE, error_code=ERROR_ANSWER_VALIDATION, answer="生成结果未通过证据引用校验，因此拒绝返回该结果。", limitations=["生成器只能引用当前报告中存在且 ticker 一致的 S 编号或结构化字段。"])
    answer.requested_by = requested_by or "anonymous"
    answer.question = normalized
    return answer
