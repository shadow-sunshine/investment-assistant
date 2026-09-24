"""有证据边界问答的领域与 API 测试。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant.qa import (
    ANSWERED,
    GENERATOR_FAILED,
    INSUFFICIENT_EVIDENCE,
    OUT_OF_SCOPE,
    Answer,
    AnswerClaim,
    EvidenceRef,
    answer_question,
    load_report_evidence,
)


def _audit(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "market_snapshot": {"data_available": True, "latest_close": 326.57, "period_return_pct": 42.49},
        "financial_snapshot": {"data_available": True, "revenue": 416161000000.0, "unavailable_fields": []},
        "sources": [{"citation": "S1", "content": "Cash flow evidence", "metadata": {"ticker": ticker, "file_name": "annual.pdf", "page": "36"}}],
        "llm_result": {"used": False, "reason": "LLM 完整报告未通过 safety 校验。"},
        "evaluation": {"passed": False, "findings": ["存在未支持的断言"]},
        "retrieval_evaluation": {"passed": True},
        "risk_flags": ["估值存在不确定性"],
    }


def _context(tmp_path, ticker: str = "AAPL"):
    report_id = f"{ticker}_20260923_120000"
    (tmp_path / f"{report_id}.json").write_text(json.dumps(_audit(ticker), ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"{report_id}.md").write_text(f"# {ticker} report\n[S1]", encoding="utf-8")
    return load_report_evidence(tmp_path, report_id)


def test_metric_fact_has_structured_evidence(tmp_path):
    result = answer_question(_context(tmp_path), "这份报告的最新收盘价和营收是多少？")
    assert result.status == ANSWERED
    assert {ref["ref"] for ref in result.to_dict()["evidence_refs"]} == {"market_snapshot.latest_close", "financial_snapshot.revenue"}
    assert all(claim.evidence_refs for claim in result.claims)


def test_source_question_without_spaces_selects_only_requested_citation(tmp_path):
    result = answer_question(_context(tmp_path), "S1来自哪一页？")
    assert result.status == ANSWERED
    assert [ref.ref for ref in result.evidence_refs] == ["S1"]
    assert len(result.claims) == 1


def test_foreign_ticker_adjacent_to_chinese_is_rejected(tmp_path):
    result = answer_question(_context(tmp_path), "MSFT最新收盘价是多少？")
    assert result.status == OUT_OF_SCOPE
    assert result.error_code == "OUT_OF_SCOPE"


def test_source_and_page_question_returns_existing_citation(tmp_path):
    result = answer_question(_context(tmp_path), "S1 的来源文件和页码是什么？")
    assert result.status == ANSWERED
    assert result.evidence_refs[0].ref == "S1"
    assert result.evidence_refs[0].page == "36"


def test_degradation_question_uses_audit_fields(tmp_path):
    result = answer_question(_context(tmp_path), "报告为什么降级为规则版，校验结果如何？")
    assert result.status == ANSWERED
    assert "规则版" in result.answer
    assert "未通过" in result.answer


def test_unknown_question_is_explicitly_out_of_scope(tmp_path):
    result = answer_question(_context(tmp_path), "现在应该买入吗？")
    assert result.status == OUT_OF_SCOPE
    assert result.error_code == "OUT_OF_SCOPE"


def test_missing_metric_returns_insufficient_evidence_without_guessing(tmp_path):
    context = _context(tmp_path)
    audit = dict(context.audit)
    audit["financial_snapshot"] = {"data_available": False, "unavailable_fields": ["net_income"]}
    context = type(context)(context.report_id, context.ticker, audit, context.report)
    result = answer_question(context, "净利润是多少？")
    assert result.status == INSUFFICIENT_EVIDENCE
    assert result.error_code == "INSUFFICIENT_EVIDENCE"


def test_source_ticker_mismatch_cannot_be_referenced(tmp_path):
    context = _context(tmp_path)
    audit = dict(context.audit)
    audit["sources"] = [{"citation": "S1", "content": "foreign", "metadata": {"ticker": "MSFT", "file_name": "other.pdf", "page": 1}}]
    context = type(context)(context.report_id, context.ticker, audit, context.report)
    result = answer_question(context, "S1 在哪一页？")
    assert result.status == INSUFFICIENT_EVIDENCE


class _BadGenerator:
    def generate(self, context, question):
        raise RuntimeError("SECRET_INTERNAL_STACK")


class _InvalidGenerator:
    def generate(self, context, question):
        return Answer(
            status=ANSWERED,
            report_id=context.report_id,
            ticker=context.ticker,
            question=question,
            answer="未经证据支持的回答",
            claims=[AnswerClaim("未经证据支持的回答", (EvidenceRef("source", "S99", "fake"),))],
        )


def test_generator_exception_does_not_leak_stack(tmp_path):
    result = answer_question(_context(tmp_path), "问题", generator=_BadGenerator())
    assert result.status == GENERATOR_FAILED
    assert "SECRET_INTERNAL_STACK" not in result.answer
    assert "SECRET_INTERNAL_STACK" not in str(result.to_dict())


def test_generator_with_out_of_bounds_evidence_is_rejected(tmp_path):
    result = answer_question(_context(tmp_path), "问题", generator=_InvalidGenerator())
    assert result.status == INSUFFICIENT_EVIDENCE
    assert result.error_code == "ANSWER_EVIDENCE_VALIDATION_FAILED"


def test_api_answer_contract_and_error_boundaries(monkeypatch, tmp_path):
    context = _context(tmp_path)
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    client = TestClient(api.app)

    answered = client.post("/api/answers", json={"report_id": context.report_id, "question": "最新收盘价是多少？", "requested_by": "analyst"})
    assert answered.status_code == 200
    assert answered.json()["status"] == ANSWERED
    assert answered.json()["access_control"]["mode"] == "mvp_attribution_only"

    empty = client.post("/api/answers", json={"report_id": context.report_id, "question": "   "})
    assert empty.status_code == 422
    assert empty.json()["detail"]["error_code"] == "QUESTION_EMPTY"

    missing = client.post("/api/answers", json={"report_id": "missing_report", "question": "最新收盘价是多少？"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "REPORT_NOT_FOUND"

    mismatch = client.post("/api/answers", json={"report_id": context.report_id, "ticker": "MSFT", "question": "最新收盘价是多少？"})
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"]["error_code"] == "TICKER_MISMATCH"

    outside = client.post("/api/answers", json={"report_id": context.report_id, "question": "现在应该买入吗？"})
    assert outside.status_code == 422
    assert outside.json()["error_code"] == "OUT_OF_SCOPE"


def test_multi_metric_question_refuses_stale_or_partial_snapshot(tmp_path):
    context = _context(tmp_path)
    audit = dict(context.audit)
    audit["market_snapshot"] = {"data_available": False, "latest_close": 999.0}
    context = type(context)(context.report_id, context.ticker, audit, context.report)
    result = answer_question(context, "最新收盘价和营收是多少？")
    assert result.status == INSUFFICIENT_EVIDENCE
    assert "最新收盘价" in result.limitations[0]
    assert not result.claims


def test_multiple_source_refs_require_every_requested_ref(tmp_path):
    result = answer_question(_context(tmp_path), "S1 和 S2 分别来自哪一页？")
    assert result.status == INSUFFICIENT_EVIDENCE
    assert result.error_code == "INSUFFICIENT_EVIDENCE"
    assert "S2" in result.limitations[0]


def test_zero_metric_is_rendered_as_zero(tmp_path):
    context = _context(tmp_path)
    audit = dict(context.audit)
    audit["market_snapshot"] = {"data_available": True, "latest_close": 0}
    context = type(context)(context.report_id, context.ticker, audit, context.report)
    result = answer_question(context, "最新收盘价是多少？")
    assert result.status == ANSWERED
    assert "为 0。" in result.answer
