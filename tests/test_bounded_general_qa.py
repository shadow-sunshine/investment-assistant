"""网易官方年报与行业观察的路由、身份和来源失效回归。"""
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api, bounded_general_qa as sources
from investment_assistant.chat_session import dispatch_message, new_context, route_message, select_report


def test_user_phrases_and_scoped_switching():
    calls = []
    def issuer(payload):
        calls.append(("issuer", payload))
        return sources.IssuerAnswerService().answer(payload["ticker"], payload["question"], requested_by="actor")
    def industry(payload):
        calls.append(("industry", payload))
        return sources.IndustryObservationService().answer(payload["question"], requested_by="actor", today=date(2026, 10, 4))
    def forbidden(*args):
        raise AssertionError("不得错误调用报告/旧中文年报路径")

    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    state = dispatch_message(state, "我要查网易公司的股份", forbidden, forbidden, "actor", knowledge_ask=forbidden, issuer_ask=issuer, industry_ask=industry)
    assert state["report_id"] is None and state["general_scope"] == "issuer"
    assert state["messages"][-1]["answer"]["status"] == "needs_clarification"
    state = dispatch_message(state, "网易公司的2025年的收入", forbidden, forbidden, "actor", knowledge_ask=forbidden, issuer_ask=issuer, industry_ask=industry)
    result = state["messages"][-1]["answer"]
    assert result["status"] == "answered" and "1,126 亿元" in result["answer"]
    assert all(ref["excerpt"] in __import__("pypdf").PdfReader(str(sources.KNOWLEDGE_DIR / ref["identity"]["file_name"])).pages[ref["identity"]["page"] - 1].extract_text() for ref in result["sources"])
    state = dispatch_message(state, "今年可以关注什么行业", forbidden, forbidden, "actor", knowledge_ask=forbidden, issuer_ask=issuer, industry_ask=industry)
    assert state["general_scope"] == "industry" and len(state["messages"]) == 2
    assert state["messages"][-1]["answer"]["status"] == "answered"
    assert [kind for kind, _ in calls] == ["issuer", "issuer", "industry"]


def test_cross_company_and_time_boundaries():
    state = new_context()
    for q in ["网易和宁德时代收入对比", "网易与 MSFT 收入", "网易和 123456.SZ 2025 年收入", "网易公司哪些行业值得关注"]:
        assert route_message(q, state)[0] == "guidance"
    assert route_message("宁德时代 2025 年收入", state)[0] == "knowledge"
    issuer = sources.IssuerAnswerService()
    assert issuer.answer("NTES", "网易公司2026年的收入", requested_by="a")["status"] == "refused"
    assert issuer.answer("NTES", "网易现在股价多少", requested_by="a")["status"] == "refused"
    assert issuer.answer("NTES", "AAPL 2025 年收入", requested_by="a")["status"] == "refused"
    assert sources.IndustryObservationService().answer("网易 2026 年关注什么行业", requested_by="a", today=date(2026, 10, 4))["status"] == "refused"
    assert sources.IndustryObservationService().answer("2025 年关注什么行业", requested_by="a", today=date(2026, 10, 4))["status"] == "refused"


def test_source_excerpt_is_verbatim_and_staleness():
    result = sources.IndustryObservationService().answer("今年关注什么行业", requested_by="actor", today=date(2026, 10, 4))
    for ref in result["sources"]:
        assert ref["excerpt"] in (sources.INDUSTRY_DIR / ref["identity"]["file_name"]).read_text(encoding="utf-8")
    with pytest.raises(sources.BoundedSourceUnavailable, match="industry_source_stale"):
        sources.IndustryObservationService().answer("今年关注什么行业", requested_by="actor", today=date(2026, 11, 20))


def test_issuer_manifest_page_or_hash_drift_fails_closed(tmp_path, monkeypatch):
    payload = json.loads(sources.ISSUER_MANIFEST.read_text(encoding="utf-8"))
    target = tmp_path / "issuer.json"
    monkeypatch.setattr(sources, "ISSUER_MANIFEST", target)
    for change in ({"verified_pages": {**payload["NETEASE_2025"]["verified_pages"], "revenue_summary": 107}},
                   {"validation": {**payload["NETEASE_2025"]["validation"], "sha256": "0" * 64}},
                   {"validation": "corrupt"}):
        variant = json.loads(json.dumps(payload))
        variant["NETEASE_2025"].update(change)
        target.write_text(json.dumps(variant), encoding="utf-8")
        with pytest.raises(sources.BoundedSourceUnavailable):
            sources.IssuerAnswerService().answer("NTES", "2025 年收入", requested_by="actor")


def test_industry_manifest_and_text_drift_fails_closed(tmp_path, monkeypatch):
    payload = json.loads(sources.INDUSTRY_MANIFEST.read_text(encoding="utf-8"))
    target = tmp_path / "industry.json"
    monkeypatch.setattr(sources, "INDUSTRY_MANIFEST", target)
    payload["sources"][0]["text_sha256"] = "0" * 64
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sources.BoundedSourceUnavailable):
        sources.IndustryObservationService().answer("今年关注什么行业", requested_by="actor", today=date(2026, 10, 4))
    monkeypatch.setattr(sources, "INDUSTRY_MANIFEST", sources.DATA_DIR / "industry_sources.json")
    monkeypatch.setattr(sources, "INDUSTRY_DIR", tmp_path)
    with pytest.raises(sources.BoundedSourceUnavailable):
        sources.IndustryObservationService().answer("今年关注什么行业", requested_by="actor", today=date(2026, 10, 4))


def test_api_auth_role_identity_and_source_fail_closed(monkeypatch):
    client = TestClient(api.app)
    path = "/api/issuer-answers"
    request = {"ticker": "NTES", "question": "网易公司 2025 年收入", "requested_by": "fake-actor"}
    response = client.post(path, json=request)
    assert response.status_code == 200
    assert response.json()["actor_id"] == "test-actor" and response.json()["requested_by"] == "test-actor"
    assert client.post(path, json={"ticker": "BAD", "question": "收入"}).status_code == 422
    assert client.post("/api/industry-observations", json={"question": "2026 年关注什么行业"}).status_code == 200
    token = "viewer-" + "x" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "viewer", "tenant": "t", "roles": ["reviewer"]}}))
    assert client.post(path, json=request, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.post("/api/industry-observations", json={"question": "行业"}, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.post(path, json=request, headers={"Authorization": ""}).status_code == 401


def test_api_source_unavailable_is_503(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "ISSUER_MANIFEST", tmp_path / "not-there.json")
    response = TestClient(api.app).post("/api/issuer-answers", json={"ticker": "NTES", "question": "网易 2025 年收入"})
    assert response.status_code == 503 and response.json()["detail"]["error_code"] == "ISSUER_SOURCE_UNAVAILABLE"
