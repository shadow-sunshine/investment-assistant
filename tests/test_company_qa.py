"""公司问答的真实锚点、身份边界及受控接入负面测试。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api, company_qa, company_onboarding
from investment_assistant.chat_session import dispatch_message, new_context, route_message, select_report, record_error


def test_catalog_from_manifests_and_explicit_aliases():
    records = company_qa.catalog()
    assert records["0700.HK"]["year"] == "2025"
    assert records["300750.SZ"]["engine"] == "chinese"
    assert company_qa.references("腾讯 2025 年收入")[0] == {"0700.HK"}
    assert company_qa.references("网易 2025 年收入")[0] == {"9999.HK"}
    assert company_qa.references("宁德时代 2025 年营收")[0] == {"300750.SZ"}


def test_pingan_natural_revenue_question_routes_to_verified_annual_comparison():
    question = "平安银行2025年的收入怎么样"
    assert route_message(question, new_context()) == (
        "knowledge", {"ticker": "000001.SZ", "question": question}
    )
    assert route_message(question, select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"}))[0] == "knowledge"
    result = company_qa.CompanyAnswerService().answer("000001.SZ", question, requested_by="actor")
    assert result["status"] == "answered"
    assert "131,442 百万元" in result["answer"]
    assert "146,695 百万元" in result["answer"]
    assert "下降 10.4%" in result["answer"]
    assert result["sources"][0]["identity"]["page"] == 17
    assert result["sources"][0]["identity"]["source_sha256"] == company_qa.catalog()["000001.SZ"]["entry"]["validation"]["sha256"]
    assert "人民币百万元" in result["sources"][0]["excerpt"]
    state = dispatch_message(new_context(), question, lambda _: None, lambda _: None,
                             "actor", company_ask=lambda _: result)
    assert state["messages"][-1]["answer"]["status"] == "answered"
    assert state["knowledge_ticker"] == "000001.SZ"
    response = TestClient(api.app).post("/api/company-answers",
                                        json={"ticker": "000001.SZ", "question": question})
    assert response.status_code == 200
    assert response.json()["status"] == "answered"
    assert "下降 10.4%" in response.json()["answer"]


def test_pingan_revenue_scope_and_anchor_fail_closed(tmp_path, monkeypatch):
    service = company_qa.CompanyAnswerService()
    for question in ("平安银行和小米2025年收入怎么样", "平安银行和腾讯2025年收入怎么样",
                     "平安银行2025年Q4收入怎么样", "平安银行2026年收入怎么样"):
        assert service.answer("000001.SZ", question, requested_by="actor")["status"] == "refused"
    for question in ("平安银行2025年利息收入怎么样", "平安银行2025年净收入怎么样"):
        assert not company_qa._annual_revenue_intent(question, company_qa.catalog()["000001.SZ"])
    original = json.loads(company_qa.ANCHORS_PATH.read_text(encoding="utf-8"))
    target = tmp_path / "anchors.json"
    monkeypatch.setattr(company_qa, "ANCHORS_PATH", target)
    for change in ({"page": 18}, {"value": "146,695"}, {"unit": "千元"}, {"source_sha256": "0" * 64}):
        payload = copy.deepcopy(original)
        payload["000001.SZ"]["revenue"].update(change)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(company_qa.CompanySourceUnavailable):
            service.answer("000001.SZ", "平安银行2025年的收入怎么样", requested_by="actor")


def test_unsupported_period_reports_current_official_coverage_instead_of_only_old_year():
    result = company_qa.CompanyAnswerService().answer("0700.HK", "腾讯2026年收入怎么样", requested_by="actor")
    assert result["status"] == "refused"
    assert result["error_code"] == "OFFICIAL_DOCUMENT_FIELD_UNVERIFIED"
    coverage = result["official_coverage"]
    assert "2026" in coverage["indexed_years"]
    assert "2025" in coverage["indexed_years"]
    assert "不会用旧年度替代当前期间" in result["answer"]


def test_latest_official_document_query_lists_metadata_not_keyword_search(monkeypatch):
    from investment_assistant.official_document_answers import OfficialDocumentAnswerService

    class Store:
        def list_documents(self, *, ticker=None):
            return [
                {"document_id": "old", "ticker": ticker, "report_date": "2025", "filing_date": "09/04/2026", "title": "Annual report"},
                {"document_id": "new", "ticker": ticker, "report_date": "2026", "filing_date": "05/10/2026", "title": "Disclosure return"},
            ]

    def forbidden_search(*args, **kwargs):
        raise AssertionError("资料目录不应依赖全文是否包含‘有哪些’")

    monkeypatch.setattr("investment_assistant.official_document_answers.search_official_documents", forbidden_search)
    result = OfficialDocumentAnswerService(store=Store()).answer("0700.HK", "腾讯最新官方资料", requested_by="actor")
    assert result["status"] == "documents_listed"
    assert [d["document_id"] for d in result["documents"]] == ["new", "old"]
    assert result["freshness"]["checked_online"] is False


def test_tencent_annual_revenue_is_pinned_to_real_annual_statement():
    answer = company_qa.CompanyAnswerService().answer("0700.HK", "腾讯2025年收入是多少", requested_by="actor")
    assert answer["status"] == "answered"
    assert "751,766 百万元" in answer["answer"] and "660,257" in answer["answer"]
    assert "194,371" not in answer["answer"]
    ref = answer["sources"][0]
    assert ref["identity"]["page"] == 130
    assert ref["identity"]["source_sha256"] == "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98"
    assert ref["identity"]["source_url"].startswith("https://www1.hkexnews.hk/")
    assert "For the year ended 31 December 2025" in ref["excerpt"]
    assert "RMB" in ref["excerpt"] and "751,766" in ref["excerpt"]
    for question in ("腾讯2025年Q4收入", "腾讯2025年第四季度收入", "腾讯2026年收入", "腾讯2025和2024收入比较"):
        assert company_qa.CompanyAnswerService().answer("0700.HK", question, requested_by="actor")["status"] == "refused"


def test_netease_and_catl_requested_questions_answer_and_switch_report_scope():
    service = company_qa.CompanyAnswerService()
    n = service.answer("9999.HK", "网易2025年收入", requested_by="a")
    assert n["status"] == "answered" and n["sources"][0]["identity"]["page"] == 108
    c = service.answer("300750.SZ", "宁德时代2025年营收", requested_by="a")
    assert c["status"] == "answered"
    assert "人民币 423,701,834 千元" in c["answer"]
    assert c["evidence"]["identity"]["page"] == 11
    calls = []
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    assert route_message("腾讯2025年收入是多少", state)[0] == "company"
    state = dispatch_message(state, "腾讯2025年收入是多少", lambda _: None,
                             lambda _: calls.append("old_report"), "a",
                             company_ask=lambda payload: service.answer(payload["ticker"], payload["question"], requested_by="a"))
    assert not calls and state["report_id"] is None and state["knowledge_ticker"] == "0700.HK"
    assert state["messages"][-1]["answer"]["status"] == "answered"
    assert route_message("那2025年收入呢", state)[1]["ticker"] == "0700.HK"
    state = dispatch_message(state, "网易2025年收入", lambda _: None,
                             lambda _: calls.append("old_report"), "a",
                             company_ask=lambda payload: service.answer(payload["ticker"], payload["question"], requested_by="a"),
                             issuer_ask=lambda _: None)
    assert not calls and len(state["messages"]) == 2
    assert state["messages"][-1]["answer"]["ticker"] == "9999.HK"


def test_manifest_anchor_and_pdf_drift_refuse_delivery(tmp_path, monkeypatch):
    service = company_qa.CompanyAnswerService()
    data = json.loads(company_qa.ANCHORS_PATH.read_text(encoding="utf-8"))
    anchor_path = tmp_path / "anchors.json"
    monkeypatch.setattr(company_qa, "ANCHORS_PATH", anchor_path)
    for patch in ({"page": 12}, {"page": 13}, {"value": "194,371"}, {"source_sha256": "0" * 64}):
        changed = copy.deepcopy(data)
        changed["0700.HK"]["revenue"].update(patch)
        anchor_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(company_qa.CompanySourceUnavailable):
            service.answer("0700.HK", "腾讯2025收入", requested_by="a")
    anchor_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    monkeypatch.setattr(company_qa, "KNOWLEDGE_DIR", pdf_dir)
    (pdf_dir / data["0700.HK"]["revenue"]["file_name"]).write_bytes(b"%PDF- forged")
    with pytest.raises(company_qa.CompanySourceUnavailable):
        service.answer("0700.HK", "腾讯2025收入", requested_by="a")


def test_cross_company_missing_source_and_role(monkeypatch):
    service = company_qa.CompanyAnswerService()
    for question in ("腾讯与宁德时代2025收入", "腾讯与AAPL 2025收入"):
        assert service.answer("0700.HK", question, requested_by="a")["status"] == "refused"
    assert service.answer("TSLA", "TSLA 2025收入", requested_by="a")["error_code"] == "MATERIAL_NOT_ONBOARDED"
    client = TestClient(api.app)
    path = "/api/company-answers"
    assert client.post(path, json={"ticker": "0700.HK", "question": "腾讯2025收入", "requested_by": "forged"}).json()["actor_id"] == "test-actor"
    token = "reviewer-" + "x" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "reviewer", "tenant": "t", "roles": ["reviewer"]}}))
    assert client.post(path, json={"ticker": "0700.HK", "question": "腾讯2025收入"}, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.post("/api/company-material-requests", json={"ticker": "1234.HK", "year": "2025"}, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.post(path, json={"ticker": "0700.HK", "question": "腾讯2025收入"}, headers={"Authorization": ""}).status_code == 401


def test_unapproved_onboarding_never_opens_network(monkeypatch, tmp_path):
    approval = tmp_path / "approval.json"
    approval.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(company_onboarding, "APPROVAL_PATH", approval)
    class NoNetwork:
        def get(self, *args, **kwargs):
            raise AssertionError("未预审不能下载")
    result = company_onboarding.provision_approved("1234.HK", "2025", session=NoNetwork())
    assert result["status"] == "not_approved"
    assert TestClient(api.app).post("/api/company-material-requests", json={"ticker": "1234.HK", "year": "2025"}).json()["status"] == "not_approved"
    approval.write_text(json.dumps({"1234.HK:2025": {"market": "HKEX", "approved": True,
                        "source_url": "http://example.com/report.pdf", "sha256": "0" * 64,
                        "page_count": 1, "report_date": "2025", "company": "example"}}), encoding="utf-8")
    with pytest.raises(company_qa.CompanySourceUnavailable):
        company_onboarding.provision_approved("1234.HK", "2025", session=NoNetwork())


def test_issuer_catalog_cannot_inherit_material_loop_name(tmp_path, monkeypatch):
    original = json.loads(company_qa.bounded.ISSUER_MANIFEST.read_text(encoding="utf-8"))
    target = tmp_path / "issuer.json"
    monkeypatch.setattr(company_qa.bounded, "ISSUER_MANIFEST", target)
    target.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    assert company_qa.catalog()["9999.HK"]["display_name"] == "网易"
    original["NETEASE_2025"]["aliases"].append("腾讯")
    target.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(company_qa.CompanySourceUnavailable, match="issuer_identity_invalid"):
        company_qa.catalog()


def test_onboarding_preapproved_snapshot_validates_and_does_not_touch_base_manifest(tmp_path, monkeypatch):
    source = Path("data/knowledge_base/0700HK_annual_report_2025.pdf")
    base = tmp_path / "base.json"
    approved = tmp_path / "approved.json"
    attached = tmp_path / "attached.json"
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    base.write_text("{}", encoding="utf-8")
    attached.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(company_onboarding, "MANIFEST_PATH", base)
    monkeypatch.setattr(company_onboarding, "APPROVAL_PATH", approved)
    monkeypatch.setattr(company_onboarding, "ONBOARDED_PATH", attached)
    monkeypatch.setattr(company_onboarding, "KNOWLEDGE_DIR", pdf_dir)
    url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.pdf"
    entry = {"market": "HKEX", "source_url": url, "sha256": "0" * 64,
             "page_count": 282, "report_date": "2025", "company": "Tencent Holdings Limited", "approved": True}
    approved.write_text(json.dumps({"0700.HK:2025": entry}), encoding="utf-8")
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/pdf"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size):
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(chunk_size), b""):
                    yield chunk
    class Session:
        def get(self, requested, **kwargs):
            assert requested == url and kwargs["allow_redirects"] is False
            return Response()
    with pytest.raises(company_qa.CompanySourceUnavailable, match="approved_sha256_mismatch"):
        company_onboarding.provision_approved("0700.HK", "2025", session=Session())
    assert json.loads(base.read_text(encoding="utf-8")) == {}
    assert json.loads(attached.read_text(encoding="utf-8")) == {}
    entry["sha256"] = "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98"
    approved.write_text(json.dumps({"0700.HK:2025": entry}), encoding="utf-8")
    result = company_onboarding.provision_approved("0700.HK", "2025", session=Session())
    assert result["status"] == "material_onboarded"
    assert json.loads(base.read_text(encoding="utf-8")) == {}
    onboarded = json.loads(attached.read_text(encoding="utf-8"))["0700.HK"]
    assert onboarded["validation"]["sha256"] == entry["sha256"] and onboarded["report_date"] == "2025"
    assert company_onboarding.provision_approved("0700.HK", "2025", session=Session())["status"] == "already_onboarded"


def test_unknown_named_company_does_not_leak_old_report():
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    assert route_message("阿里巴巴2025年收入是多少", state)[0] == "guidance"
    assert route_message("腾讯2025年收入是多少", state)[0] == "company"


def test_failed_company_request_keeps_new_scope_not_old_report():
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    result = record_error(state, "腾讯2025年收入", "官方来源不可用")
    assert result["report_id"] is None and result["knowledge_ticker"] == "0700.HK"
    assert result["messages"][-1]["text"] == "官方来源不可用"


def test_catalog_rejects_wrong_ticker_and_spoofed_alias(tmp_path, monkeypatch):
    data = json.loads(company_qa.MANIFEST_PATH.read_text(encoding="utf-8"))
    target = tmp_path / "materials.json"
    monkeypatch.setattr(company_qa, "MANIFEST_PATH", target)
    wrong = copy.deepcopy(data)
    wrong["0700.HK"]["ticker"] = "AAPL"
    target.write_text(json.dumps(wrong, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(company_qa.CompanySourceUnavailable, match="catalog_entry_invalid"):
        company_qa.catalog()
    spoofed = copy.deepcopy(data)
    spoofed["300750.SZ"]["company"] = "腾讯"
    target.write_text(json.dumps(spoofed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(company_qa.CompanySourceUnavailable, match="catalog_company_name_drift"):
        company_qa.catalog()
    spoofed["300750.SZ"]["company"] = "阿里巴巴"
    target.write_text(json.dumps(spoofed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(company_qa.CompanySourceUnavailable, match="catalog_company_name_drift"):
        company_qa.catalog()


def test_implicit_year_is_only_inherited_from_answered_same_company():
    calls = []
    def ask(payload):
        calls.append(payload)
        return {"status": "answered", "answer": "有据回答", "sources": []}
    state = dispatch_message(new_context(), "腾讯2025年收入", lambda _: None, lambda _: None, "a", company_ask=ask)
    assert state["knowledge_year"] == "2025"
    state = dispatch_message(state, "那收入呢", lambda _: None, lambda _: None, "a", company_ask=ask)
    assert calls[-1]["question"] == "2025年，那收入呢"
    state = dispatch_message(state, "宁德时代2025年营收", lambda _: None, lambda _: None, "a", company_ask=ask)
    assert state["knowledge_ticker"] == "300750.SZ" and state["knowledge_year"] == "2025"
    state = dispatch_message(state, "2026年的营收", lambda _: None, lambda _: None, "a", company_ask=ask)
    assert calls[-1]["question"] == "2026年的营收"


def test_unlisted_chinese_issuer_is_blocked_at_ui_and_final_service():
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    unexpected = []
    for question in ("阿里巴巴收入是多少", "小米收入是多少", "阿里巴巴2025年收入", "腾讯和阿里巴巴2025年收入", "这个报告的阿里巴巴收入是多少"):
        assert route_message(question, state)[0] == "guidance"
        next_state = dispatch_message(state, question, lambda _: unexpected.append("job"),
                                      lambda _: unexpected.append("old_report"), "actor",
                                      company_ask=lambda _: unexpected.append("company"))
        assert not unexpected
        assert next_state["messages"][-1]["answer"] is None
        assert "不会沿用" in next_state["messages"][-1]["text"] or "公司" in next_state["messages"][-1]["text"]
    for question in ("这条风险来自哪里？", "现金流来自哪一页？", "这个报告的收入是多少？", "为什么它的营收下降？"):
        assert route_message(question, state) == ("answer", question)
    service = company_qa.CompanyAnswerService()
    for question in ("阿里巴巴2025年收入", "小米2025年收入", "腾讯与阿里巴巴2025年收入", "阿里巴巴2025年营业额", "Alibaba 2025 revenue", "阿里巴巴公司"):
        result = service.answer("0700.HK", question, requested_by="actor")
        assert result["status"] == "refused" and result["sources"] == []
        assert result["error_code"] == "COMPANY_SCOPE_UNVERIFIED"
    client = TestClient(api.app)
    payload = client.post("/api/company-answers", json={"ticker": "0700.HK", "question": "阿里巴巴2025年收入"})
    assert payload.status_code == 200
    assert payload.json()["status"] == "refused" and payload.json()["sources"] == []
    assert "751,766" not in payload.json()["answer"]
    assert service.answer("0700.HK", "腾讯2025年收入", requested_by="actor")["status"] == "answered"


def test_malformed_issuer_validation_fails_as_catalog_unavailable(tmp_path, monkeypatch):
    from investment_assistant import bounded_general_qa as bounded
    payload = json.loads(bounded.ISSUER_MANIFEST.read_text(encoding="utf-8"))
    payload["NETEASE_2025"]["validation"] = "not-a-validation-object"
    target = tmp_path / "issuer.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bounded, "ISSUER_MANIFEST", target)
    with pytest.raises(company_qa.CompanySourceUnavailable, match="issuer_identity_invalid"):
        company_qa.catalog()
    response = TestClient(api.app).get("/api/company-coverage")
    assert response.status_code == 503


def test_anchored_pdf_is_parsed_from_verified_bytes(monkeypatch, tmp_path):
    """在验完文件后替换路径，不能让解析器重新从路径读取新内容。"""
    original = company_qa.PdfReader
    loaded = []
    def reader(source):
        loaded.append(source)
        assert hasattr(source, "read") and not isinstance(source, (str, Path))
        return original(source)
    monkeypatch.setattr(company_qa, "PdfReader", reader)
    answer = company_qa.CompanyAnswerService().answer("0700.HK", "腾讯2025年收入", requested_by="audit")
    assert answer["status"] == "answered" and loaded
