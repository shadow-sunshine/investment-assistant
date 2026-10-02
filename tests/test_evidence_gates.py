"""R5 证据与交付门禁测试。全部离线、可重复，不访问真实网络。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant.evidence_gates import (
    ANCHOR_SOURCE,
    ANCHOR_SNAPSHOT,
    CONFLICT,
    NEEDS_REVIEW,
    PARTIAL,
    RELEASE_BLOCKED,
    RELEASE_NEEDS_REVIEW,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    SUPPORTED,
    UNSUPPORTED,
    Claim,
    EvidenceAnchor,
    EvidenceErrorCode,
    build_claim_fixture,
    claims_from_answer,
    claims_from_audit,
    evaluate_delivery,
    load_report_context,
    render_delivery,
    validate_claim,
)
from investment_assistant.qa import ANSWERED, INSUFFICIENT_EVIDENCE, answer_question, load_report_evidence

REPORT_ID = "AAPL_20260927_120000"

SOURCE_ID = "pdf-dd52a5964d6d25ab4032"
SOURCE_CONTENT = "Net income 112,010 million for fiscal 2025. Total shareholders equity ending balances 93,568."
SOURCE_FILE = "Apple_2025_Form_10-K.pdf"


def _audit(tmp_path=None) -> dict:
    material = _material_file(tmp_path) if tmp_path is not None else None
    digest = hashlib.sha256(Path(material).read_bytes()).hexdigest() if material else ""
    audit = {
        "ticker": "AAPL",
        "market_snapshot": {"data_available": True, "latest_close": 338.3401, "observations": 252, "currency": "USD"},
        "financial_snapshot": {
            "data_available": True,
            "currency": "USD",
            "revenue": 416161000000.0,
            "revenue_period_end": "2025-09-30",
            "net_income": 112010000000.0,
            "net_income_period_end": "2025-09-30",
        },
        "sources": [
            {
                "citation": "S1",
                "content": SOURCE_CONTENT,
                "metadata": {
                    "ticker": "AAPL",
                    "file_name": SOURCE_FILE,
                    "page": "36",
                    "source_id": SOURCE_ID,
                    "source_sha256": digest,
                    "path": material,
                },
            }
        ],
    }
    return audit


def _material_file(tmp_path, content: bytes | None = None) -> str:
    target = tmp_path / SOURCE_FILE
    if content is not None:
        target.write_bytes(content)
    else:
        document = canvas.Canvas(str(target), invariant=1, pageCompression=0)
        for page in range(1, 37):
            if page == 36:
                document.drawString(40, 730, SOURCE_CONTENT)
            document.showPage()
        document.save()
    return str(target)


def _write_manifest(tmp_path, sha256: str, material_path: str | None = None):
    entry = {"file_name": SOURCE_FILE, "validation": {"sha256": sha256}}
    if material_path:
        entry["path"] = material_path
    manifest_path = tmp_path / "materials_manifest.json"
    manifest_path.write_text(json.dumps({"MSFT": entry}, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def _write_report(tmp_path, audit: dict) -> str:
    (tmp_path / f"{REPORT_ID}.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"{REPORT_ID}.md").write_text("# AAPL report\n[S1]", encoding="utf-8")
    return load_report_evidence(tmp_path, REPORT_ID).report_id


def _context(tmp_path, audit: dict | None = None):
    audit = audit or _audit(tmp_path)
    context = load_report_context(tmp_path, _write_report(tmp_path, audit))
    digest = hashlib.sha256((tmp_path / SOURCE_FILE).read_bytes()).hexdigest()
    return replace(context, material_index={SOURCE_FILE: {"sha256": digest, "path": str(tmp_path / SOURCE_FILE)}})


def _context_with_manifest(tmp_path, audit: dict, manifest_path):
    _write_report(tmp_path, audit)
    from investment_assistant import evidence_gates

    # 测试 manifest 位于 pytest 临时目录；生产默认仅接受 data/knowledge_base 内资料。
    with patch.object(evidence_gates, "ALLOWED_MATERIAL_DIRS", (tmp_path,)):
        return load_report_context(tmp_path, REPORT_ID, manifest_path=manifest_path)


def _snapshot_claim(**overrides) -> Claim:
    anchor_fields = dict(
        kind=ANCHOR_SNAPSHOT,
        field_path="financial_snapshot.revenue",
        value=416161000000.0,
        period="2025-09-30",
        unit="USD",
    )
    anchor_fields.update(overrides)
    return Claim(
        claim_id="c-revenue",
        claim_text="AAPL 最近财年营收为 416161000000 USD。",
        ticker="AAPL",
        report_id=REPORT_ID,
        anchors=(EvidenceAnchor(**anchor_fields),),
    )


def _source_claim(**overrides) -> Claim:
    anchor_fields = dict(
        kind=ANCHOR_SOURCE,
        citation="S1",
        source_id=SOURCE_ID,
        file_name=SOURCE_FILE,
        page="36",
        evidence_excerpt="Net income 112,010 million for fiscal 2025.",
    )
    anchor_fields.update(overrides)
    return Claim(
        claim_id="c-source",
        claim_text="净利润数据来自年报第 36 页。",
        ticker="AAPL",
        report_id=REPORT_ID,
        anchors=(EvidenceAnchor(**anchor_fields),),
    )


# --- 1. supported 样例：同 ticker、同 source_id、同 page、同 period、同 unit、同 excerpt ----


def test_supported_claim_passes_all_bindings(tmp_path):
    context = _context(tmp_path)
    result = validate_claim(_snapshot_claim(), context)
    assert result.support_status == SUPPORTED
    assert result.validation_errors == ()

    source_result = validate_claim(_source_claim(), context)
    assert source_result.support_status == SUPPORTED

    delivery = evaluate_delivery([_snapshot_claim(), _source_claim()], context)
    assert delivery["release_status"] == RELEASE_RELEASED
    assert delivery["blocking_reasons"] == []
    assert delivery["report_version"] == REPORT_ID
    assert delivery["ticker"] == "AAPL"


def test_snapshot_claim_without_value_anchor_is_needs_review(tmp_path):
    context = _context(tmp_path)
    claim = _snapshot_claim(value=None)
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.EXCERPT_UNVERIFIABLE in error for error in result.validation_errors)


# --- 2. ticker 不一致必须阻断 ---------------------------------------------------------


def test_ticker_mismatch_is_unsupported_and_blocks_delivery(tmp_path):
    context = _context(tmp_path)
    claim = _snapshot_claim()
    bad = Claim(
        claim_id=claim.claim_id,
        claim_text=claim.claim_text,
        ticker="MSFT",
        report_id=claim.report_id,
        anchors=claim.anchors,
    )
    result = validate_claim(bad, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.CLAIM_TICKER_MISMATCH in error for error in result.validation_errors)
    assert evaluate_delivery([bad], context)["release_status"] == RELEASE_BLOCKED


# --- 3. 不同 PDF 相同 page 号必须阻断（不得只用 page 判定命中） -----------------------------


def test_same_page_from_different_pdf_is_conflict(tmp_path):
    context = _context(tmp_path)
    claim = _source_claim(source_id="pdf-other-file-id", file_name="Other_Company_Report.pdf", page="36", evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == CONFLICT
    assert any(EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT in error for error in result.validation_errors)
    assert evaluate_delivery([claim], context)["release_status"] == RELEASE_BLOCKED


def test_wrong_page_number_is_blocked(tmp_path):
    context = _context(tmp_path)
    claim = _source_claim(page="99", evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.PAGE_MISMATCH in error for error in result.validation_errors)


def test_citation_with_foreign_ticker_source_is_not_found(tmp_path):
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["ticker"] = "MSFT"
    context = _context(tmp_path, audit)
    claim = _source_claim(evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.SOURCE_NOT_FOUND in error for error in result.validation_errors)


# --- R5 复核修复 1：source 锚点不能只凭 citation 判定 supported ---------------------------


def test_citation_only_source_anchor_is_unsupported(tmp_path):
    """攻击路径：只有 citation（S1）就宣称来源已验证。"""
    context = _context(tmp_path)
    claim = _source_claim(source_id=None, file_name=None, page=None, evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE in error for error in result.validation_errors)
    delivery = evaluate_delivery([claim], context)
    assert delivery["release_status"] == RELEASE_BLOCKED


def test_source_anchor_missing_source_id_is_unsupported(tmp_path):
    """file_name + page 也在，但缺 source_id：来源身份仍不完整，不得判 supported。"""
    context = _context(tmp_path)
    claim = _source_claim(source_id=None)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE in error for error in result.validation_errors)


def test_source_anchor_without_excerpt_is_needs_review_not_supported(tmp_path):
    """身份齐全但缺少 evidence_excerpt：无法核验原文，只能 needs_review。"""
    context = _context(tmp_path)
    claim = _source_claim(evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.EXCERPT_UNVERIFIABLE in error for error in result.validation_errors)
    delivery = evaluate_delivery([claim], context)
    assert delivery["release_status"] == RELEASE_NEEDS_REVIEW
    assert delivery["release_status"] != RELEASE_RELEASED


# --- 4. report/source 版本或 SHA 不匹配必须阻断 -------------------------------------------


def test_report_id_mismatch_is_blocked(tmp_path):
    context = _context(tmp_path)
    claim = Claim(
        claim_id="c-wrong-report",
        claim_text="x",
        ticker="AAPL",
        report_id="AAPL_other_version",
        anchors=_snapshot_claim().anchors,
    )
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.REPORT_ID_MISMATCH in error for error in result.validation_errors)


def test_source_sha_mismatch_is_conflict_and_matching_sha_is_supported(tmp_path):
    material = _material_file(tmp_path)
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["path"] = material
    actual = hashlib.sha256((tmp_path / SOURCE_FILE).read_bytes()).hexdigest()
    manifest_path = _write_manifest(tmp_path, actual, material)
    context = _context_with_manifest(tmp_path, audit, manifest_path)

    drifted = _source_claim(source_sha256="0" * 64)
    assert validate_claim(drifted, context).support_status == CONFLICT

    ok = _source_claim(source_sha256=actual)
    assert validate_claim(ok, context).support_status == SUPPORTED


def test_missing_manifest_material_file_is_needs_review_not_supported(tmp_path):
    audit = _audit(tmp_path)
    missing_material = tmp_path / "missing" / SOURCE_FILE
    audit["sources"][0]["metadata"]["path"] = str(missing_material)
    manifest_path = _write_manifest(tmp_path, "a" * 64, str(missing_material))
    context = _context_with_manifest(tmp_path, audit, manifest_path)
    claim = _source_claim()
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.SOURCE_FILE_UNREADABLE in error for error in result.validation_errors)


def test_sha_uses_manifest_record_not_forged_metadata_path(tmp_path):
    """攻击路径：metadata.path 指向攻击者文件且其哈希与 claim 一致，但 manifest canonical SHA 不同。"""
    forged = tmp_path / "forged.pdf"
    forged.write_bytes(b"forged-bytes")
    forged_sha = hashlib.sha256(forged.read_bytes()).hexdigest()

    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["path"] = str(forged)  # 被伪造的路径
    canonical = _material_file(tmp_path)
    canonical_sha = hashlib.sha256((tmp_path / SOURCE_FILE).read_bytes()).hexdigest()
    manifest_path = _write_manifest(tmp_path, canonical_sha, canonical)
    context = _context_with_manifest(tmp_path, audit, manifest_path)

    claim = _source_claim(source_sha256=forged_sha)
    result = validate_claim(claim, context)
    assert result.support_status == CONFLICT
    assert any(EvidenceErrorCode.SOURCE_SHA_MISMATCH in error for error in result.validation_errors)


def test_untrusted_metadata_path_outside_material_dirs_is_needs_review(tmp_path):
    """攻击路径：审计 JSON 自带任意绝对路径（目录外）冒充资料位置，不能据此核验 SHA。"""
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"attacker-file")
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["path"] = str(outside)
    context = _context_with_manifest(tmp_path, audit, tmp_path / "missing_manifest.json")

    claim = _source_claim(evidence_excerpt=None)
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.SOURCE_PATH_UNTRUSTED in error for error in result.validation_errors)


def test_manifest_sha_drift_is_needs_review(tmp_path):
    material = _material_file(tmp_path)
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["path"] = material
    # manifest 记录的 SHA 与磁盘文件实际内容不一致 → 资料版本漂移，无法确认
    manifest_path = _write_manifest(tmp_path, "b" * 64, material)
    context = _context_with_manifest(tmp_path, audit, manifest_path)
    claim = _source_claim()
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.SOURCE_MANIFEST_DRIFT in error for error in result.validation_errors)


def test_load_material_index_parses_and_fails_closed(tmp_path):
    from investment_assistant.config import KNOWLEDGE_DIR
    from investment_assistant.evidence_gates import load_material_index

    assert load_material_index(tmp_path / "missing.json") == {}
    corrupt = tmp_path / "manifest_corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    assert load_material_index(corrupt) == {}
    ok = tmp_path / "manifest_ok.json"
    ok.write_text(json.dumps({"MSFT": {"file_name": "a.pdf", "validation": {"sha256": "c" * 64}}}), encoding="utf-8")
    index = load_material_index(ok)
    assert index == {"a.pdf": {"sha256": "c" * 64, "path": str(KNOWLEDGE_DIR / "a.pdf")}}


# --- 5. excerpt/字段不存在必须阻断 -----------------------------------------------------


def test_excerpt_not_in_source_content_is_blocked(tmp_path):
    context = _context(tmp_path)
    claim = _source_claim(evidence_excerpt="这段话根本不在来源里")
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.EXCERPT_NOT_FOUND in error for error in result.validation_errors)


def test_unknown_field_path_is_blocked(tmp_path):
    context = _context(tmp_path)
    claim = _snapshot_claim(field_path="financial_snapshot.nonexistent")
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.FIELD_NOT_FOUND in error for error in result.validation_errors)


def test_unavailable_snapshot_field_is_not_supported(tmp_path):
    audit = _audit(tmp_path)
    audit["financial_snapshot"]["revenue"] = None
    context = _context(tmp_path, audit)
    result = validate_claim(_snapshot_claim(), context)
    assert result.support_status == UNSUPPORTED


# --- 6. period/unit/数值不一致必须阻断 --------------------------------------------------


def test_period_mismatch_is_blocked(tmp_path):
    context = _context(tmp_path)
    result = validate_claim(_snapshot_claim(period="2024-09-28"), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.PERIOD_MISMATCH in error for error in result.validation_errors)


def test_unit_mismatch_is_blocked(tmp_path):
    context = _context(tmp_path)
    result = validate_claim(_snapshot_claim(unit="%"), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.UNIT_MISMATCH in error for error in result.validation_errors)


def test_unit_without_canonical_unit_is_needs_review(tmp_path):
    """R5 复核修复 3：字段没有登记 canonical 单位时，声明了 unit 就不得判 supported。"""
    context = _context(tmp_path)
    claim = _snapshot_claim(field_path="market_snapshot.observations", value=252, unit="倍")
    result = validate_claim(claim, context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.UNIT_UNVERIFIABLE in error for error in result.validation_errors)
    delivery = evaluate_delivery([claim], context)
    assert delivery["release_status"] == RELEASE_NEEDS_REVIEW
    assert delivery["release_status"] != RELEASE_RELEASED


def test_value_mismatch_is_blocked(tmp_path):
    context = _context(tmp_path)
    result = validate_claim(_snapshot_claim(value=999.0), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.VALUE_MISMATCH in error for error in result.validation_errors)


def test_period_without_period_end_in_audit_is_needs_review(tmp_path):
    audit = _audit(tmp_path)
    del audit["financial_snapshot"]["revenue_period_end"]
    context = _context(tmp_path, audit)
    result = validate_claim(_snapshot_claim(), context)
    assert result.support_status == NEEDS_REVIEW


# --- 7. conflict / needs_review 不能误报 released ---------------------------------------


def test_conflict_and_needs_review_are_never_released(tmp_path):
    context = _context(tmp_path)
    conflict = _source_claim(source_id="pdf-other-file-id", file_name="Other.pdf", page="36", evidence_excerpt=None)
    conflict_delivery = evaluate_delivery([conflict], context)
    assert conflict_delivery["release_status"] == RELEASE_BLOCKED
    assert conflict_delivery["release_status"] != RELEASE_RELEASED

    audit = _audit(tmp_path)
    audit["sources"][0]["content"] = ""  # 来源内容为空 → 片段无法确认，只能 needs_review
    empty_context = _context(tmp_path, audit)
    unverifiable = _source_claim()
    review_delivery = evaluate_delivery([unverifiable], empty_context)
    assert review_delivery["release_status"] == RELEASE_NEEDS_REVIEW
    assert review_delivery["release_status"] != RELEASE_RELEASED


def test_claim_without_anchors_is_unsupported(tmp_path):
    context = _context(tmp_path)
    claim = Claim(claim_id="c-empty", claim_text="无锚点", ticker="AAPL", report_id=REPORT_ID)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.ANCHOR_MISSING in error for error in result.validation_errors)


# --- 8. partial 交付必须显式带缺口 ------------------------------------------------------


def test_partial_delivery_carries_gap_reasons(tmp_path):
    context = _context(tmp_path)
    claim = Claim(
        claim_id="c-partial",
        claim_text="一条锚点可验证、一条不可验证的结论。",
        ticker="AAPL",
        report_id=REPORT_ID,
        anchors=(
            EvidenceAnchor(kind=ANCHOR_SNAPSHOT, field_path="financial_snapshot.revenue", value=416161000000.0, period="2025-09-30", unit="USD"),
            EvidenceAnchor(kind=ANCHOR_SNAPSHOT, field_path="financial_snapshot.revenue", value=1.0),
        ),
    )
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    delivery = evaluate_delivery([claim], context)
    assert delivery["release_status"] == RELEASE_BLOCKED
    assert delivery["blocking_reasons"]
    assert delivery["release_status"] != RELEASE_RELEASED


def test_no_claims_reports_needs_review_with_explicit_reason(tmp_path):
    context = _context(tmp_path)
    delivery = evaluate_delivery([], context)
    assert delivery["release_status"] == RELEASE_NEEDS_REVIEW
    assert any("尚未接入自动 claim 抽取" in reason for reason in delivery["blocking_reasons"])


# --- claim 抽取入口与 fixture --------------------------------------------------------


def test_claims_from_audit_parses_machine_readable_claims_and_flags_malformed(tmp_path):
    audit = _audit(tmp_path)
    audit["claims"] = [
        {
            "claim_id": "c1",
            "claim_text": "营收锚定",
            "anchors": [{"kind": ANCHOR_SNAPSHOT, "field_path": "financial_snapshot.revenue", "value": 416161000000.0, "period": "2025-09-30", "unit": "USD"}],
        },
        "not-a-dict",
    ]
    claims = claims_from_audit(audit, REPORT_ID)
    assert len(claims) == 2
    assert claims[0].claim_id == "c1"
    assert claims[1].support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.CLAIM_MALFORMED in error for error in claims[1].validation_errors)


def test_claims_from_answer_converts_qa_evidence_refs(tmp_path):
    context = _context(tmp_path)
    evidence = load_report_evidence(tmp_path, REPORT_ID)
    result = answer_question(evidence, "这份报告的最新收盘价是多少？")
    claims = claims_from_answer(result)
    assert claims
    validated = [validate_claim(claim, context) for claim in claims]
    assert all(item.support_status == SUPPORTED for item in validated)
    assert evaluate_delivery(claims, context)["release_status"] == RELEASE_RELEASED


def test_build_claim_fixture_is_deterministic_and_gate_blocks_unsupported():
    supported, unsupported = build_claim_fixture()
    assert supported.claim_id == "fixture-supported-revenue"
    assert unsupported.claim_id == "fixture-unsupported-missing-source"
    assert build_claim_fixture() == (supported, unsupported)
    assert unsupported.support_status == NEEDS_REVIEW  # 校验前默认 fail-closed，不预判 supported


def test_render_delivery_is_deterministic_markdown(tmp_path):
    context = _context(tmp_path)
    delivery = evaluate_delivery([_snapshot_claim()], context)
    rendered = render_delivery(delivery)
    assert rendered == render_delivery(evaluate_delivery([_snapshot_claim()], context))
    assert "**released**" in rendered
    assert "c-revenue" in rendered


# --- 9. API / 任务序列化回归 -----------------------------------------------------------


def test_report_delivery_endpoint_needs_review_without_full_claim_coverage(monkeypatch, tmp_path):
    audit = _audit(tmp_path)
    audit["claims"] = [
        {
            "claim_id": "c1",
            "claim_text": "营收锚定",
            "anchors": [{"kind": ANCHOR_SNAPSHOT, "field_path": "financial_snapshot.revenue", "value": 416161000000.0, "period": "2025-09-30", "unit": "USD"}],
        }
    ]
    _write_report(tmp_path, audit)
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)

    response = TestClient(api.app).get(f"/api/reports/{REPORT_ID}/delivery")
    assert response.status_code == 409
    body = response.json()
    assert body["release_status"] == RELEASE_NEEDS_REVIEW
    assert body["claims_total"] == 1
    assert body["report_version"] == REPORT_ID
    assert any("完整关键结论覆盖" in reason for reason in body["blocking_reasons"])


def test_report_delivery_endpoint_needs_review_without_machine_claims(monkeypatch, tmp_path):
    _write_report(tmp_path, _audit(tmp_path))
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)

    response = TestClient(api.app).get(f"/api/reports/{REPORT_ID}/delivery")
    assert response.status_code == 409
    body = response.json()
    assert body["release_status"] == RELEASE_NEEDS_REVIEW
    assert any("尚未接入自动 claim 抽取" in reason for reason in body["blocking_reasons"])


def test_report_delivery_endpoint_error_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    missing = TestClient(api.app).get("/api/reports/missing_report/delivery")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "REPORT_NOT_FOUND"

    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "broken.md").write_text("x", encoding="utf-8")
    invalid = TestClient(api.app).get("/api/reports/broken/delivery")
    assert invalid.status_code == 404
    assert invalid.json()["detail"]["error_code"] == "REPORT_NOT_FOUND"


def test_answer_api_payload_carries_delivery(monkeypatch, tmp_path):
    _write_report(tmp_path, _audit(tmp_path))
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(api, "_effective_publication", lambda *_: (object(), None))
    client = TestClient(api.app)

    answered = client.post("/api/answers", json={"report_id": REPORT_ID, "question": "最新收盘价是多少？", "requested_by": "analyst"})
    assert answered.status_code == 200
    delivery = answered.json()["delivery"]
    assert delivery["release_status"] == RELEASE_RELEASED
    assert delivery["claims_total"] >= 1

    insufficient_audit = _audit(tmp_path)
    insufficient_audit["financial_snapshot"] = {"data_available": False, "unavailable_fields": ["net_income"]}
    _write_report(tmp_path, insufficient_audit)
    short = client.post("/api/answers", json={"report_id": REPORT_ID, "question": "净利润是多少？"})
    assert short.status_code == 422
    assert short.json()["status"] == INSUFFICIENT_EVIDENCE
    assert short.json()["answer"] == "该回答未通过证据交付门禁，系统未返回未经验证的结论。"
    assert short.json()["claims"] == []
    assert short.json()["evidence_refs"] == []
    assert short.json()["delivery"]["release_status"] == RELEASE_BLOCKED
    assert short.json()["delivery"]["blocking_reasons"]


def test_completed_job_response_carries_delivery_for_its_report(monkeypatch, tmp_path):
    from investment_assistant.report_jobs import ReportJob, STATUS_COMPLETED, _default_steps

    audit = _audit(tmp_path)
    audit["claims"] = [
        {
            "claim_id": "c1",
            "claim_text": "营收锚定",
            "anchors": [{"kind": ANCHOR_SNAPSHOT, "field_path": "financial_snapshot.revenue", "value": 416161000000.0, "period": "2025-09-30", "unit": "USD"}],
        }
    ]
    _write_report(tmp_path, audit)
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    job = ReportJob(
        job_id="job_20260927_120002_cafe01",
        ticker="AAPL",
        topic="测试",
        horizon="中期",
        requested_by="tester",
        status=STATUS_COMPLETED,
        report_id=REPORT_ID,
        steps=_default_steps(),
    )
    monkeypatch.setattr(api.job_service, "get", lambda _job_id: job)

    response = TestClient(api.app).get(f"/api/report-jobs/{job.job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["delivery"]["release_status"] == RELEASE_NEEDS_REVIEW
    assert body["degradation"]["status"] == "ok"


def test_completed_job_without_report_id_has_no_delivery(monkeypatch):
    from investment_assistant.report_jobs import ReportJob, STATUS_COMPLETED, _default_steps

    job = ReportJob(
        job_id="job_20260927_120003_cafe02",
        ticker="AAPL",
        topic="测试",
        horizon="中期",
        requested_by="tester",
        status=STATUS_COMPLETED,
        steps=_default_steps(),
    )
    monkeypatch.setattr(api.job_service, "get", lambda _job_id: job)

    response = TestClient(api.app).get(f"/api/report-jobs/{job.job_id}")
    assert response.status_code == 200
    assert "delivery" not in response.json()


# --- 10. 冻结测试 / R0-R3 评测回归由全量测试覆盖（见最终报告命令） ---------------------------


# --- R5 复核修复 5：数值、来源元数据、数据可用性和报告读取必须继续 fail-closed ---


def test_source_numeric_anchor_does_not_match_as_substring(tmp_path):
    context = _context(tmp_path)
    claim = _source_claim(value=10)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.VALUE_MISMATCH in error for error in result.validation_errors)


def test_source_period_and_unit_mismatch_are_blocked(tmp_path):
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"]["period"] = "2025"
    audit["sources"][0]["metadata"]["unit"] = "USD"
    context = _context(tmp_path, audit)
    claim = _source_claim(period="2024", unit="CNY")
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.PERIOD_MISMATCH in error for error in result.validation_errors)
    assert any(EvidenceErrorCode.UNIT_MISMATCH in error for error in result.validation_errors)


def test_source_record_without_source_id_cannot_accept_claim_identity(tmp_path):
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"].pop("source_id")
    context = _context(tmp_path, audit)
    result = validate_claim(_source_claim(), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE in error for error in result.validation_errors)


def test_unavailable_snapshot_value_is_not_supported(tmp_path):
    audit = _audit(tmp_path)
    audit["financial_snapshot"]["data_available"] = False
    context = _context(tmp_path, audit)
    result = validate_claim(_snapshot_claim(), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.DATA_UNAVAILABLE in error for error in result.validation_errors)


def test_empty_claim_identity_is_not_supported(tmp_path):
    context = _context(tmp_path)
    claim = Claim(claim_id="", claim_text="", ticker="AAPL", report_id=REPORT_ID, anchors=_snapshot_claim().anchors)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.CLAIM_MALFORMED in error for error in result.validation_errors)


def test_report_body_is_not_returned_when_delivery_needs_review(monkeypatch, tmp_path):
    _write_report(tmp_path, _audit(tmp_path))
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    response = TestClient(api.app).get(f"/api/reports/{REPORT_ID}")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["error_code"] == "EVIDENCE_GATE_BLOCKED"
    assert "report" not in body["detail"]


def test_original_pdf_page_must_contain_claim_excerpt(tmp_path):
    audit = _audit(tmp_path)
    forged = "Invented revenue 999999 on this page."
    audit["sources"][0]["content"] = forged
    context = _context(tmp_path, audit)
    result = validate_claim(_source_claim(evidence_excerpt=forged), context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.EXCERPT_NOT_FOUND in error for error in result.validation_errors)


def test_source_sha_missing_from_report_is_needs_review_even_if_claim_uses_current_sha(tmp_path):
    audit = _audit(tmp_path)
    audit["sources"][0]["metadata"].pop("source_sha256")
    context = _context(tmp_path, audit)
    actual = hashlib.sha256((tmp_path / SOURCE_FILE).read_bytes()).hexdigest()
    result = validate_claim(_source_claim(source_sha256=actual), context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.SOURCE_MANIFEST_DRIFT in error for error in result.validation_errors)


def test_source_sha_drift_cannot_be_skipped_by_omitting_claim_sha(tmp_path):
    audit = _audit(tmp_path)
    context = _context(tmp_path, audit)
    (tmp_path / SOURCE_FILE).write_bytes(b"mutated-original")
    result = validate_claim(_source_claim(source_sha256=None), context)
    assert result.support_status == NEEDS_REVIEW
    assert any(EvidenceErrorCode.SOURCE_MANIFEST_DRIFT in error for error in result.validation_errors)


def test_duplicate_citation_is_conflict(tmp_path):
    audit = _audit(tmp_path)
    audit["sources"].append({**audit["sources"][0], "metadata": {**audit["sources"][0]["metadata"], "source_id": "other"}})
    result = validate_claim(_source_claim(), _context(tmp_path, audit))
    assert result.support_status == CONFLICT


def test_snapshot_cannot_use_claims_as_its_own_evidence(tmp_path):
    audit = _audit(tmp_path)
    audit["claims"] = [{"claim_text": "invented"}]
    context = _context(tmp_path, audit)
    claim = _snapshot_claim(field_path="claims.0.claim_text", value="invented", period=None, unit=None)
    result = validate_claim(claim, context)
    assert result.support_status == UNSUPPORTED
    assert any(EvidenceErrorCode.SNAPSHOT_FIELD_UNTRUSTED in error for error in result.validation_errors)


def test_nonobject_anchor_and_nonlist_claims_fail_closed(tmp_path):
    audit = _audit(tmp_path)
    audit["claims"] = [{"claim_id": "x", "claim_text": "revenue", "anchors": [_snapshot_claim().anchors[0].to_dict(), "invalid"]}]
    context = _context(tmp_path, audit)
    assert evaluate_delivery(claims_from_audit(audit, REPORT_ID), context)["release_status"] == RELEASE_BLOCKED
    audit["claims"] = {"claim_id": "x"}
    assert evaluate_delivery(claims_from_audit(audit, REPORT_ID), context)["release_status"] == RELEASE_BLOCKED


def test_good_and_bad_anchors_cannot_be_partial_release(tmp_path):
    context = _context(tmp_path)
    claim = Claim("both", "revenue assertion", "AAPL", REPORT_ID, (_snapshot_claim().anchors[0], EvidenceAnchor(kind=ANCHOR_SNAPSHOT, field_path="financial_snapshot.revenue", value=0)))
    delivery = evaluate_delivery([claim], context)
    assert delivery["release_status"] == RELEASE_BLOCKED
    assert delivery["blocking_reasons"]


def test_answer_source_requires_current_pdf_version_and_does_not_leak_if_blocked(monkeypatch, tmp_path):
    audit = _audit(tmp_path)
    _write_report(tmp_path, audit)
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    material = tmp_path / SOURCE_FILE
    digest = hashlib.sha256(material.read_bytes()).hexdigest()
    monkeypatch.setattr(api, "load_material_index", lambda: {SOURCE_FILE: {"sha256": digest, "path": str(material)}})
    monkeypatch.setattr(api, "_effective_publication", lambda *_: (object(), None))
    client = TestClient(api.app)
    good = client.post("/api/answers", json={"report_id": REPORT_ID, "question": "S1 来源在哪里？"})
    assert good.status_code == 200
    assert good.json()["delivery"]["release_status"] == RELEASE_RELEASED
    audit["sources"][0]["metadata"].pop("source_sha256")
    _write_report(tmp_path, audit)
    rejected = client.post("/api/answers", json={"report_id": REPORT_ID, "question": "S1 来源在哪里？"})
    assert rejected.status_code == 422
    body = rejected.json()
    assert body["delivery"]["release_status"] == RELEASE_NEEDS_REVIEW
    assert body["answer"] == "该回答未通过证据交付门禁，系统未返回未经验证的结论。"
    assert body["claims"] == [] and body["evidence_refs"] == []
    assert all("claim_text" not in item and "anchors" not in item for item in body["delivery"]["claim_results"])


def test_fixture_claims_do_not_release_report_and_pure_partial_has_gap(tmp_path, monkeypatch):
    audit = _audit(tmp_path)
    audit["claims"] = [{
        "claim_id": "approved-fixture", "claim_text": "营收结构化锚点",
        "anchors": [{"kind": ANCHOR_SNAPSHOT, "field_path": "financial_snapshot.revenue", "value": 416161000000.0, "period": "2025-09-30", "unit": "USD"}],
    }]
    _write_report(tmp_path, audit)
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    response = TestClient(api.app).get(f"/api/reports/{REPORT_ID}")
    assert response.status_code == 409
    assert response.json()["detail"]["delivery"]["release_status"] == RELEASE_NEEDS_REVIEW
    assert "report" not in response.json()["detail"]
    context = _context(tmp_path, audit)
    delivery = evaluate_delivery(claims_from_audit(audit, REPORT_ID), context, ["非关键来源暂不可用"])
    assert delivery["release_status"] == RELEASE_PARTIAL
    assert delivery["degradation_reasons"] == ["非关键来源暂不可用"]


def test_manifest_path_outside_allowed_material_dir_is_not_indexed(tmp_path):
    from investment_assistant.evidence_gates import load_material_index
    external = tmp_path / SOURCE_FILE
    external.write_bytes(b"external")
    manifest = _write_manifest(tmp_path, hashlib.sha256(external.read_bytes()).hexdigest(), str(external))
    assert SOURCE_FILE not in load_material_index(manifest)
