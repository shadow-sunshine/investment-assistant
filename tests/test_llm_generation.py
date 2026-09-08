from investment_assistant.llm_generation import (
    render_narrative_template,
    validate_narrative_template,
    validate_numeric_provenance,
    validate_narrative_snapshot_consistency,
    validate_narrative_source_attribution,
    normalize_narrative_source_attribution,
    validate_rag_claim_attribution,
    validate_narrative_text_integrity,
)
from investment_assistant.safety import REQUIRED_DISCLAIMER, validate_report

SOURCES = [{"citation": "S1", "content": "Apple reported operating cash flow of 111,482 in 2025."}]
SLOTS = {"operating_cash_flow": "111,482", "latest_trading_date": "2026-09-04"}


def test_llm_template_rejects_unknown_citation():
    result = validate_narrative_template("现金流为 {operating_cash_flow}。[S2]", SOURCES, SLOTS)
    assert not result["passed"]
    assert result["invalid_citations"] == ["S2"]


def test_llm_template_accepts_known_slot_and_citation():
    result = validate_narrative_template("现金流为 {operating_cash_flow}，仍需核验质量。[S1]", SOURCES, SLOTS)
    assert result["passed"]
    assert result["used_slots"] == ["operating_cash_flow"]


def test_llm_template_rejects_unknown_slot():
    result = validate_narrative_template("现金流为 {made_up}。[S1]", SOURCES, SLOTS)
    assert not result["passed"]
    assert result["unknown_slots"] == ["made_up"]


def test_llm_template_rejects_bare_number():
    result = validate_narrative_template("现金流为 123。[S1]", SOURCES, SLOTS)
    assert not result["passed"]
    assert "裸数字或日期" in result["findings"][0]


def test_numeric_provenance_accepts_program_injected_value():
    rendered = render_narrative_template("现金流为 {operating_cash_flow}。[S1]", SLOTS)
    result = validate_numeric_provenance(rendered, SLOTS, SOURCES)
    assert result["passed"]
    assert "111,482" in result["narrative_numbers"]


def test_numeric_provenance_rejects_unsupported_number():
    result = validate_numeric_provenance("现金流为 999。[S1]", SLOTS, SOURCES)
    assert not result["passed"]
    assert result["unsupported_numbers"] == ["999"]


def test_llm_template_rejects_financial_metric_mislabeling():
    slots = {"revenue": "416,161,000,000", "free_cash_flow": "98,767,000,000"}
    revenue_result = validate_narrative_template("服务业务收入为 {revenue}。[S1]", SOURCES, slots)
    cash_flow_result = validate_narrative_template("经营现金流为 {free_cash_flow}。[S1]", SOURCES, slots)
    assert not revenue_result["passed"]
    assert "总营收槽位不得表述为服务业务收入。" in revenue_result["semantic_conflicts"]
    assert not cash_flow_result["passed"]
    assert "自由现金流槽位不得表述为经营活动产生的现金或经营现金流。" in cash_flow_result["semantic_conflicts"]


def test_llm_template_accepts_qualitative_narrative_with_valid_citation():
    result = validate_narrative_template("Evidence remains qualitative and requires further verification.[S1]", SOURCES, SLOTS)
    assert result["passed"]
    assert result["used_slots"] == []


def test_llm_template_keeps_explicit_required_slot_support():
    result = validate_narrative_template("??????[S1] {operating_cash_flow}", SOURCES, SLOTS, {"latest_trading_date"})
    assert not result["passed"]
    assert result["missing_required_slots"] == ["latest_trading_date"]


def test_complete_report_rejects_unknown_citation():
    report = f"""## 2. 财报与估值快照
数据不可用
## 4. 受控研究叙述
研究观察。[S9]
{REQUIRED_DISCLAIMER}"""
    outcome = validate_report(report, [{"citation": "S1"}], {"data_available": False})
    assert not outcome["passed"]
    assert outcome["invalid_citations"] == ["S9"]



def test_snapshot_consistency_rejects_missing_claim_for_available_metric():
    financial_snapshot = {"free_cash_flow": 100, "trailing_pe": 20, "price_to_book": 3}
    narrative = "\u73b0\u6709\u8d44\u6599\u672a\u63d0\u4f9b\u81ea\u7531\u73b0\u91d1\u6d41\u548c PE/PB \u6570\u636e\u3002[S1]"
    outcome = validate_narrative_snapshot_consistency(narrative, financial_snapshot)
    assert not outcome["passed"]
    assert len(outcome["contradictions"]) == 3


def test_snapshot_availability_uses_structured_source_without_rag_citation():
    invalid = "\u81ea\u7531\u73b0\u91d1\u6d41\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b[S1]\u3002"
    missing_label = "\u81ea\u7531\u73b0\u91d1\u6d41\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b\u3002"
    valid = "\u81ea\u7531\u73b0\u91d1\u6d41\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\u3002"
    assert not validate_narrative_source_attribution(invalid)["passed"]
    assert not validate_narrative_source_attribution(missing_label)["passed"]
    assert validate_narrative_source_attribution(valid)["passed"]


def test_normalizer_labels_snapshot_clause_and_removes_its_rag_citation():
    template = "\u81ea\u7531\u73b0\u91d1\u6d41\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b[S1]\uff1b\u9700\u7ed3\u5408\u539f\u59cb\u8bc1\u636e\u6838\u9a8c[S2]\u3002"
    normalized = normalize_narrative_source_attribution(template)
    assert "\u81ea\u7531\u73b0\u91d1\u6d41\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\uff1b" in normalized
    assert "\u9700\u7ed3\u5408\u539f\u59cb\u8bc1\u636e\u6838\u9a8c[S2]" in normalized
    assert validate_narrative_source_attribution(normalized)["passed"]


def test_rag_claim_requires_citation_and_cannot_share_snapshot_clause():
    mixed = "\u8bc1\u636e\u4e0d\u8db3\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\u3002"
    uncited = "\u9700\u7ed3\u5408\u9644\u6ce8\u8fdb\u4e00\u6b65\u6838\u9a8c\u3002"
    cited = "\u9700\u7ed3\u5408\u9644\u6ce8\u8fdb\u4e00\u6b65\u6838\u9a8c[S1]\u3002"
    assert not validate_rag_claim_attribution(mixed)["passed"]
    assert not validate_rag_claim_attribution(uncited)["passed"]
    assert validate_rag_claim_attribution(cited)["passed"]


def test_source_attribution_normalized_template_keeps_rag_sentence_cited():
    template = "\u8425\u6536\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b[S1]\u3002\u9700\u7ed3\u5408\u539f\u59cb\u8bc1\u636e\u6838\u9a8c[S2]\u3002"
    normalized = normalize_narrative_source_attribution(template)
    assert "\u8425\u6536\u5df2\u5728\u7ed3\u6784\u5316\u5feb\u7167\u4e2d\u63d0\u4f9b\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011\u3002" in normalized
    assert validate_narrative_source_attribution(normalized)["passed"]
    assert validate_rag_claim_attribution(normalized)["passed"]


def test_narrative_text_integrity_rejects_three_or_more_question_marks():
    outcome = validate_narrative_text_integrity("文本损坏?????[S1]")
    assert not outcome["passed"]
    assert "连续三个及以上问号" in outcome["findings"][0]


def test_narrative_text_integrity_accepts_normal_text():
    assert validate_narrative_text_integrity("需结合附注进一步核验[S1]。")["passed"]
