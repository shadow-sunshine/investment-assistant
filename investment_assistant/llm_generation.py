"""受控 Qwen 叙述生成：模型只返回槽位模板，程序负责事实注入与数值溯源。"""

from __future__ import annotations

import json
import os
import re
import time
from http import HTTPStatus
from typing import Any

from dashscope import Generation

CITATION_PATTERN = re.compile(r"\[S(\d+)\]")
SLOT_PATTERN = re.compile(r"\{([a-z][a-z0-9_]*)\}")
BRACED_TOKEN_PATTERN = re.compile(r"\{[^{}]*\}")
NUMBER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?%?")
QUESTION_MARK_RUN_PATTERN = re.compile(r"\?{3,}")

SLOT_LABELS = {
    "latest_close": "最新收盘价",
    "latest_trading_date": "最新交易日",
    "period_return_pct": "样本期收益率",
    "one_month_return_pct": "近一月收益率",
    "annualized_volatility_pct": "年化波动率",
    "max_drawdown_pct": "样本期最大回撤",
    "revenue": "最近财年营收",
    "revenue_period_end": "营收报表期末",
    "net_income": "最近财年净利润",
    "net_income_period_end": "净利润报表期末",
    "free_cash_flow": "最近财年自由现金流",
    "free_cash_flow_period_end": "自由现金流报表期末",
    "trailing_pe": "滚动市盈率",
    "price_to_book": "市净率",
    "valuation_as_of": "估值抓取时间",
}


def _redact_numbers(text: str) -> str:
    """LLM 只需理解证据语义，数值由槽位注入，避免其复述原始数字。"""
    return NUMBER_TOKEN_PATTERN.sub("<数值>", text)


def _compact_source(source: dict[str, Any]) -> dict[str, Any]:
    metadata = source.get("metadata", {})
    return {
        "citation": source.get("citation"),
        "content": _redact_numbers(str(source.get("content", ""))),
        "source_type": metadata.get("source_type"),
    }


def _format_number(value: Any) -> str:
    if value is None:
        return "数据不可用"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.0f}"
    return str(value)


def _format_decimal(value: Any, suffix: str = "") -> str:
    if value is None:
        return "数据不可用"
    if isinstance(value, (int, float)):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def build_narrative_slots(market_snapshot: dict[str, Any], financial_snapshot: dict[str, Any]) -> dict[str, str]:
    """将结构化真实数据转换为唯一允许注入叙述的事实槽位。"""
    return {
        "latest_close": _format_decimal(market_snapshot.get("latest_close")),
        "latest_trading_date": str(market_snapshot.get("latest_trading_date") or "数据不可用"),
        "period_return_pct": _format_decimal(market_snapshot.get("period_return_pct"), "%"),
        "one_month_return_pct": _format_decimal(market_snapshot.get("one_month_return_pct"), "%"),
        "annualized_volatility_pct": _format_decimal(market_snapshot.get("annualized_volatility_pct"), "%"),
        "max_drawdown_pct": _format_decimal(market_snapshot.get("max_drawdown_pct"), "%"),
        "revenue": _format_number(financial_snapshot.get("revenue")),
        "revenue_period_end": str(financial_snapshot.get("revenue_period_end") or "数据不可用"),
        "net_income": _format_number(financial_snapshot.get("net_income")),
        "net_income_period_end": str(financial_snapshot.get("net_income_period_end") or "数据不可用"),
        "free_cash_flow": _format_number(financial_snapshot.get("free_cash_flow")),
        "free_cash_flow_period_end": str(financial_snapshot.get("free_cash_flow_period_end") or "数据不可用"),
        "trailing_pe": _format_decimal(financial_snapshot.get("trailing_pe")),
        "price_to_book": _format_decimal(financial_snapshot.get("price_to_book")),
        "valuation_as_of": str(financial_snapshot.get("valuation_as_of") or "数据不可用"),
    }


STRUCTURED_AVAILABILITY_FIELDS = (
    ("revenue", "\u8425\u6536", ("\u8425\u6536", "\u603b\u8425\u6536", "\u8425\u4e1a\u6536\u5165")),
    ("net_income", "\u51c0\u5229\u6da6", ("\u51c0\u5229\u6da6",)),
    ("free_cash_flow", "\u81ea\u7531\u73b0\u91d1\u6d41", ("\u81ea\u7531\u73b0\u91d1\u6d41", "FCF")),
    ("trailing_pe", "\u6eda\u52a8\u5e02\u76c8\u7387\uff08PE\uff09", ("\u6eda\u52a8\u5e02\u76c8\u7387", "\u5e02\u76c8\u7387", "PE")),
    ("price_to_book", "\u5e02\u51c0\u7387\uff08PB\uff09", ("\u5e02\u51c0\u7387", "PB")),
)


def build_data_availability_checklist(market_snapshot: dict[str, Any], financial_snapshot: dict[str, Any]) -> dict[str, list[str]]:
    """Expose whether structured facts exist, without exposing their values to the model."""
    available = []
    missing = []
    for field, label, _ in STRUCTURED_AVAILABILITY_FIELDS:
        (available if financial_snapshot.get(field) is not None else missing).append(label)
    return {
        "structured_snapshot_available": available,
        "structured_snapshot_missing": missing,
        "not_in_structured_snapshot": [
            "\u670d\u52a1\u4e1a\u52a1\u5206\u90e8\u6536\u5165",
            "\u7814\u53d1\u8d39\u7528",
            "\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41",
            "\u4e2d\u56fd\u5730\u533a\u51c0\u9500\u552e\u989d",
            "\u5b9e\u9645\u6240\u5f97\u7a0e\u7387",
        ],
    }


def validate_narrative_snapshot_consistency(narrative: str, financial_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reject claims that an available structured financial metric was not provided or is missing."""
    absence = r"(?:\u672a(?:\u63d0\u4f9b|\u62ab\u9732|\u7ed9\u51fa|\u663e\u793a)|\u7f3a\u4e4f|\u6ca1\u6709|\u6682\u65e0|\u672a\u80fd\u83b7\u53d6)"
    contradictions: list[str] = []
    for field, label, aliases in STRUCTURED_AVAILABILITY_FIELDS:
        if financial_snapshot.get(field) is None:
            continue
        alias_group = "(?:" + "|".join(re.escape(alias) for alias in aliases) + ")"
        prefix_claim = re.search(absence + r"[^\u3002\uff1b\uff0c]{0,18}" + alias_group, narrative, flags=re.IGNORECASE)
        suffix_claim = re.search(alias_group + r"[^\u3002\uff1b\uff0c]{0,18}" + absence, narrative, flags=re.IGNORECASE)
        if prefix_claim or suffix_claim:
            contradictions.append(f"\u53d9\u8ff0\u58f0\u79f0{label}\u672a\u63d0\u4f9b\u6216\u7f3a\u5931\uff0c\u4f46\u7ed3\u6784\u5316\u5feb\u7167\u5df2\u5305\u542b\u8be5\u6307\u6807\u3002")
    return {"passed": not contradictions, "contradictions": contradictions, "findings": contradictions}


SNAPSHOT_SOURCE_LABEL = "\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011"
SNAPSHOT_AVAILABILITY_TERMS = (
    "\u7ed3\u6784\u5316\u5feb\u7167",
    "\u5feb\u7167\u5b57\u6bb5",
    "\u7ed3\u6784\u5316\u6570\u636e\u652f\u6301",
    "\u7ed3\u6784\u5316\u6570\u636e\u4e2d\u63d0\u4f9b",
    "\u5f53\u524d\u5feb\u7167\u4e2d\u63d0\u4f9b",
)



def normalize_narrative_source_attribution(template: str) -> str:
    """Programmatically attach the source label to structured-availability clauses before validation."""
    parts = re.split(r"([\u3002\uff01\uff1f\uff1b])", template)
    normalized: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index]
        punctuation = parts[index + 1] if index + 1 < len(parts) else ""
        if any(term in clause for term in SNAPSHOT_AVAILABILITY_TERMS):
            clause = CITATION_PATTERN.sub("", clause).rstrip()
            if SNAPSHOT_SOURCE_LABEL not in clause:
                clause += SNAPSHOT_SOURCE_LABEL
        normalized.append(clause + punctuation)
    return "".join(normalized)


def validate_narrative_source_attribution(narrative: str) -> dict[str, Any]:
    """Separate structured-snapshot availability statements from RAG citations."""
    findings: list[str] = []
    clauses = re.split(r"(?<=[\u3002\uff01\uff1f\uff1b])", narrative)
    for clause in clauses:
        if not clause.strip() or not any(term in clause for term in SNAPSHOT_AVAILABILITY_TERMS):
            continue
        if SNAPSHOT_SOURCE_LABEL not in clause:
            findings.append("\u7ed3\u6784\u5316\u5feb\u7167\u6570\u636e\u53ef\u5f97\u6027\u9648\u8ff0\u7f3a\u5c11\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\u6807\u6ce8\u3002")
        if CITATION_PATTERN.search(clause):
            findings.append("\u7ed3\u6784\u5316\u5feb\u7167\u6570\u636e\u53ef\u5f97\u6027\u9648\u8ff0\u4e0d\u5f97\u6302 [Sx] \u5f15\u7528\u3002")
    return {"passed": not findings, "findings": findings}


RAG_CLAIM_TERMS = ("\u8bc1\u636e", "\u9644\u6ce8", "\u5ba1\u8ba1\u62a5\u544a", "\u539f\u59cb\u62ab\u9732", "\u539f\u6587")


def validate_rag_claim_attribution(narrative: str) -> dict[str, Any]:
    """Require citations for RAG-derived claims and keep them out of snapshot-only clauses."""
    findings: list[str] = []
    clauses = re.split(r"(?<=[\u3002\uff01\uff1f\uff1b])", narrative)
    for clause in clauses:
        if not clause.strip() or not any(term in clause for term in RAG_CLAIM_TERMS):
            continue
        if SNAPSHOT_SOURCE_LABEL in clause:
            findings.append("RAG \u8bc1\u636e\u5224\u65ad\u4e0d\u5f97\u4e0e\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\u540c\u53e5\u6df7\u7528\u3002")
        if not CITATION_PATTERN.search(clause):
            findings.append("RAG \u8bc1\u636e\u5224\u65ad\u7f3a\u5c11 [Sx] \u5f15\u7528\u3002")
    return {"passed": not findings, "findings": findings}


def validate_narrative_text_integrity(narrative: str) -> dict[str, Any]:
    """校验 LLM 叙述是否含有疑似编码损坏的连续问号。"""
    findings: list[str] = []
    if QUESTION_MARK_RUN_PATTERN.search(narrative):
        findings.append("LLM 叙述包含连续三个及以上问号，疑似编码损坏。")
    return {"passed": not findings, "findings": findings}


def _parse_json_payload(content: str) -> dict[str, Any]:
    clean = content.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
    payload = json.loads(clean)
    if not isinstance(payload, dict) or not isinstance(payload.get("narrative_template"), str):
        raise ValueError("模型返回未包含 narrative_template 字段的 JSON 对象")
    return payload


def validate_narrative_template(
    template: str,
    sources: list[dict[str, Any]],
    slot_values: dict[str, str],
    required_slots: set[str] | None = None,
) -> dict[str, Any]:
    """校验模型只使用允许的槽位、已检索引用和非数值叙述框架。"""
    allowed_citations = {str(source.get("citation", "")).removeprefix("S") for source in sources}
    used_citations = CITATION_PATTERN.findall(template)
    invalid_citations = sorted(set(used_citations) - allowed_citations)
    used_slots = SLOT_PATTERN.findall(template)
    unknown_slots = sorted(set(used_slots) - set(slot_values))
    required_slots = required_slots or set()
    missing_required_slots = sorted(required_slots - set(used_slots))
    semantic_conflicts: list[str] = []
    # 结构化快照只提供总营收与自由现金流，禁止模型把它们误写成不同财务口径。
    if "revenue" in used_slots and re.search(r"服务(?:业务)?收入|服务收入", template):
        semantic_conflicts.append("总营收槽位不得表述为服务业务收入。")
    if "free_cash_flow" in used_slots and re.search(r"经营活动产生的现金|经营现金流", template):
        semantic_conflicts.append("自由现金流槽位不得表述为经营活动产生的现金或经营现金流。")

    findings: list[str] = []
    remaining = CITATION_PATTERN.sub("", template)
    remaining = BRACED_TOKEN_PATTERN.sub("", remaining)
    if re.search(r"\d", remaining):
        findings.append("LLM 槽位模板包含未注入的裸数字或日期。")
    malformed_slots = BRACED_TOKEN_PATTERN.findall(template)
    if any(not SLOT_PATTERN.fullmatch(token) for token in malformed_slots):
        findings.append("LLM 槽位模板包含格式不合法的占位符。")
    if missing_required_slots:
        findings.append("LLM 槽位模板缺少必需占位符：" + "、".join(f"{{{item}}}" for item in missing_required_slots) + "。")
    findings.extend(semantic_conflicts)
    if not used_citations:
        findings.append("LLM 槽位模板缺少 [Sx] 引用。")
    if invalid_citations:
        findings.append("LLM 槽位模板包含不存在的引用编号：" + "、".join(f"S{item}" for item in invalid_citations) + "。")
    if unknown_slots:
        findings.append("LLM 槽位模板包含未授权占位符：" + "、".join(f"{{{item}}}" for item in unknown_slots) + "。")
    return {
        "passed": not findings,
        "used_citations": [f"S{item}" for item in used_citations],
        "invalid_citations": [f"S{item}" for item in invalid_citations],
        "used_slots": used_slots,
        "unknown_slots": unknown_slots,
        "missing_required_slots": missing_required_slots,
        "semantic_conflicts": semantic_conflicts,
        "findings": findings,
    }


def validate_narrative_citations(narrative: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """保留旧函数名供既有调用使用；其语义已升级为槽位模板校验。"""
    return validate_narrative_template(narrative, sources, {name: "已注入" for name in SLOT_LABELS})


def render_narrative_template(template: str, slot_values: dict[str, str]) -> str:
    """在模板校验通过后进行唯一一次事实注入，避免 LLM 直接接触真实数值。"""
    return SLOT_PATTERN.sub(lambda match: slot_values[match.group(1)], template)


def _numeric_tokens(text: str) -> set[str]:
    return set(NUMBER_TOKEN_PATTERN.findall(CITATION_PATTERN.sub("", text)))


def validate_numeric_provenance(rendered_narrative: str, slot_values: dict[str, str], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """验证渲染后叙述的每个数字均来自程序槽位或原始证据文本。"""
    narrative_numbers = _numeric_tokens(rendered_narrative)
    injected_numbers = set().union(*(_numeric_tokens(value) for value in slot_values.values())) if slot_values else set()
    evidence_numbers = set().union(*(_numeric_tokens(str(source.get("content", ""))) for source in sources)) if sources else set()
    unsupported = sorted(narrative_numbers - injected_numbers - evidence_numbers)
    findings = []
    if unsupported:
        findings.append("渲染后叙述存在无法追溯到程序注入或原始证据的数字：" + "、".join(unsupported) + "。")
    return {
        "passed": not findings,
        "narrative_numbers": sorted(narrative_numbers),
        "injected_numbers": sorted(injected_numbers),
        "evidence_numbers": sorted(evidence_numbers),
        "unsupported_numbers": unsupported,
        "findings": findings,
    }


def generate_controlled_narrative(
    ticker: str,
    topic: str,
    horizon: str,
    market_snapshot: dict[str, Any],
    financial_snapshot: dict[str, Any],
    sources: list[dict[str, Any]],
    model: str | None = None,
) -> dict[str, Any]:
    """受控 Qwen 叙述生成：模型只返回槽位模板，程序负责事实注入与数值溯源。"""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    model_name = model or os.getenv("DASHSCOPE_MODEL", "qwen-plus")
    if not api_key:
        return {"used": False, "reason": "未设置 DASHSCOPE_API_KEY。"}
    if not sources:
        return {"used": False, "reason": "没有 RAG 证据，拒绝调用 LLM 生成叙述。"}

    slot_values = build_narrative_slots(market_snapshot, financial_snapshot)
    # 不设置全局必填槽位：模板可使用相关授权槽位，或输出无数字的定性叙述。
    required_slots: set[str] = set()
    slot_catalog = [{"slot": f"{{{name}}}", "meaning": label} for name, label in SLOT_LABELS.items()]
    evidence = [_compact_source(source) for source in sources]
    availability = build_data_availability_checklist(market_snapshot, financial_snapshot)
    system_prompt = """You are a controlled narrative-template generator for an investment research report.
Write one to three conservative sentences in Chinese based only on the provided topic, allowed slot meanings, redacted evidence, and structured_data_availability.
Do not add facts, company events, forecasts, price targets, trading instructions, or return promises.
Never output bare Arabic digits, dates, amounts, percentages, or financial values. If a numeric fact is needed, use an exact allowed {slot_name} placeholder. You may instead write a qualitative narrative containing no numeric facts.
The structured_data_availability list is authoritative: never state that a metric is unavailable, missing, or not provided when it appears in structured_snapshot_available. You may describe an item as unavailable only when it is in structured_snapshot_missing or not_in_structured_snapshot.
Any statement about structured snapshot availability or missing fields must include the exact label 【来源：结构化快照】 and must not include any [Sx] citation in the same sentence or clause. Only judgments derived from RAG evidence may use [Sx] citations.
Do not invent slots. Do not call total revenue services revenue. Do not call free cash flow operating cash flow or cash generated by operating activities.
Every RAG-based key judgment must include at least one valid [Sx] citation from the evidence. Do not place RAG terms such as evidence, notes, audit report, original disclosure, or source text in the same sentence or clause as 【来源：结构化快照】; write them as a separate RAG-cited sentence. Do not use headings, lists, sequence numbers, or explanatory parentheticals.
If evidence is insufficient, state in Chinese that the evidence is insufficient and further verification is needed, with a valid citation.
Valid pattern example: a structured-availability sentence ends with 【来源：结构化快照】 and has no [Sx]; a separate evidence sentence ends with [Sx]. Never invent a slot such as {services_revenue} when that metric is not listed in allowed_slots.
Return JSON only: {"narrative_template":"Chinese Markdown narrative template under 300 characters"}."""
    request = {
        "ticker": ticker,
        "topic": _redact_numbers(topic),
        "horizon": _redact_numbers(horizon),
        "allowed_slots": slot_catalog,
        "structured_data_availability": availability,
        "evidence": evidence,
    }
    last_failure: dict[str, Any] | None = None
    validation_feedback = ""
    for attempt in range(1, 4):
        try:
            request_with_feedback = {**request, "validation_feedback": validation_feedback} if validation_feedback else request
            response = Generation.call(
                model=model_name,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(request_with_feedback, ensure_ascii=False)}],
                result_format="message",
                temperature=0.1,
                max_tokens=700,
            )
            if response.status_code != HTTPStatus.OK:
                last_failure = {"used": False, "reason": f"模型调用失败：{response.code}: {response.message}", "model": model_name, "attempts": attempt}
            else:
                payload = _parse_json_payload(response.output.choices[0].message.content)
                raw_template = payload["narrative_template"]
                template = normalize_narrative_source_attribution(raw_template)
                template_check = validate_narrative_template(template, sources, slot_values, required_slots)
                if template_check["passed"]:
                    narrative = render_narrative_template(template, slot_values)
                    numeric_check = validate_numeric_provenance(narrative, slot_values, sources)
                    consistency_check = validate_narrative_snapshot_consistency(narrative, financial_snapshot)
                    attribution_check = validate_narrative_source_attribution(narrative)
                    rag_attribution_check = validate_rag_claim_attribution(narrative)
                    text_integrity_check = validate_narrative_text_integrity(narrative)
                    if numeric_check["passed"] and consistency_check["passed"] and attribution_check["passed"] and rag_attribution_check["passed"] and text_integrity_check["passed"]:
                        return {
                            "used": True,
                            "model": model_name,
                            "template": template,
                            "raw_template": raw_template,
                            "narrative": narrative,
                            "template_check": template_check,
                            "numeric_check": numeric_check,
                            "consistency_check": consistency_check,
                            "attribution_check": attribution_check,
                            "rag_attribution_check": rag_attribution_check,
                            "text_integrity_check": text_integrity_check,
                            "availability": availability,
                            "usage": getattr(response, "usage", None),
                            "attempts": attempt,
                        }
                    if not numeric_check["passed"]:
                        failure_name, failure_findings = "LLM numeric provenance validation failed: ", numeric_check["findings"]
                    elif not consistency_check["passed"]:
                        failure_name, failure_findings = "LLM narrative-snapshot consistency validation failed: ", consistency_check["findings"]
                    elif not attribution_check["passed"]:
                        failure_name, failure_findings = "LLM narrative-source attribution validation failed: ", attribution_check["findings"]
                    elif not rag_attribution_check["passed"]:
                        failure_name, failure_findings = "LLM RAG attribution validation failed: ", rag_attribution_check["findings"]
                    else:
                        failure_name, failure_findings = "LLM narrative text integrity validation failed: ", text_integrity_check["findings"]
                    last_failure = {
                        "used": False,
                        "reason": failure_name + " ".join(failure_findings),
                        "model": model_name,
                        "template": template,
                        "raw_template": raw_template,
                        "template_check": template_check,
                        "numeric_check": numeric_check,
                        "consistency_check": consistency_check,
                        "attribution_check": attribution_check,
                        "rag_attribution_check": rag_attribution_check,
                        "text_integrity_check": text_integrity_check,
                        "availability": availability,
                        "attempts": attempt,
                    }
                else:
                    last_failure = {
                        "used": False,
                        "reason": "LLM 槽位模板校验失败：" + " ".join(template_check["findings"]),
                        "model": model_name,
                        "template": template,
                        "raw_template": raw_template,
                        "template_check": template_check,
                        "attempts": attempt,
                    }
        except Exception as exc:
            last_failure = {"used": False, "reason": f"模型调用或解析异常：{type(exc).__name__}: {exc}", "model": model_name, "attempts": attempt}
        if last_failure:
            validation_feedback = (
                "Previous candidate was rejected by program validation. Return a corrected template only. "
                "Keep structured snapshot availability statements separate from RAG claims; use 【来源：结构化快照】 without [Sx] for the former, "
                "and use [Sx] for the latter. Do not use bare digits or unlisted slots. "
                f"Validation findings: {' '.join(last_failure.get('template_check', {}).get('findings', []) + last_failure.get('numeric_check', {}).get('findings', []) + last_failure.get('consistency_check', {}).get('findings', []) + last_failure.get('attribution_check', {}).get('findings', []) + last_failure.get('rag_attribution_check', {}).get('findings', []) + last_failure.get('text_integrity_check', {}).get('findings', []))}"
            )
        if attempt < 3:
            time.sleep(1)
    return last_failure or {"used": False, "reason": "模型生成未返回结果。", "model": model_name}
