"""中文官方年报 API 与服务边界：身份、拒答、污染和文件版本漂移。"""

import hashlib
import json

from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant import chinese_qa_service as qa


class FakeModel:
    def encode(self, texts, **_):
        return [[1.0, 0.0] for _ in texts]


def _source(ticker="300750.SZ", value="423,701,834", *, page=11):
    return {
        "content": "项目 2025年 2024年\n单位：千元\n营业收入 " + value + " 362,012,554",
        "lines": ["项目 2025年 2024年", "单位：千元", "营业收入 " + value + " 362,012,554"],
        "metadata": {
            "ticker": ticker, "file_name": "300750_2025_annual_report.pdf",
            "source_sha256": "a" * 64, "page": page,
        },
        "excerpt": "营业收入 " + value + " 362,012,554",
        "lexical_score": 30.0,
    }


def _service(monkeypatch, tmp_path):
    source = _source()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    manifest = tmp_path / "materials_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(qa, "MANIFEST_PATH", manifest)
    service = qa.ChineseKnowledgeAnswerService()
    service._loaded = True
    service._model = FakeModel()
    service._materials = {
        "300750.SZ": {
            "ticker": "300750.SZ",
            "file_name": source["metadata"]["file_name"],
            "sha256": "a" * 64,
            "page_count": 232,
            "path": pdf,
        }
    }
    service._pages_by_ticker = {"300750.SZ": [source]}
    service._corpus_version = qa._sha256(manifest)
    # 此桩只替代 PDF 哈希，不替代清单漂移检查。
    monkeypatch.setattr(qa, "_sha256", lambda path: service._corpus_version if path == manifest else "a" * 64)
    return service, source, pdf, manifest


def test_knowledge_api_returns_answer_with_bound_identity(monkeypatch, tmp_path):
    service, _, _, _ = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    response = TestClient(api.app).post("/api/knowledge-answers", json={
        "ticker": "300750.sz", "question": "宁德时代2025年营业收入是多少？",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["answer"] == "营业收入为 423,701,834。"
    assert payload["evidence"]["identity"] == {
        "ticker": "300750.SZ", "file_name": "300750_2025_annual_report.pdf",
        "source_sha256": "a" * 64, "page": 11,
    }
    assert payload["actor_id"] == "test-actor"
    assert payload["access_control"]["mode"] == "server_token"


def test_refuses_cross_ticker_and_wrong_period_without_retrieval(monkeypatch, tmp_path):
    service, _, _, _ = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    client = TestClient(api.app)
    cross = client.post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代和五粮液2025年营业收入谁更高？",
    })
    assert cross.status_code == 200
    assert cross.json()["status"] == "refused"
    assert cross.json()["error_code"] == "CROSS_TICKER_QUERY"
    assert cross.json()["evidence"] is None

    wrong_year = client.post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代2024年营业收入是多少？",
    })
    assert wrong_year.status_code == 200
    assert wrong_year.json()["status"] == "refused"
    assert wrong_year.json()["error_code"] == "REPORT_PERIOD_UNSUPPORTED"
    assert wrong_year.json()["sources"] == []


def test_refuses_unknown_field_and_missing_column_binding(monkeypatch, tmp_path):
    service, source, _, _ = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    client = TestClient(api.app)
    unsupported = client.post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代2025年研发人才结构如何？",
    })
    assert unsupported.json()["status"] == "refused"
    assert unsupported.json()["evidence"] is None

    source["lines"] = ["营业收入 423,701,834 362,012,554"]
    source["content"] = source["lines"][0]
    response = client.post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代2025年营业收入是多少？",
    })
    assert response.json()["status"] == "refused"
    assert response.json()["evidence"] is None


def test_rejects_polluted_source_before_exposing_it(monkeypatch, tmp_path):
    service, source, _, _ = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    polluted = {**source, "metadata": {**source["metadata"], "ticker": "000858.SZ"}}
    monkeypatch.setattr(service, "_retrieve", lambda *_: ([polluted], [polluted]))
    response = TestClient(api.app).post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代2025年营业收入是多少？",
    })
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "KNOWLEDGE_CORPUS_UNAVAILABLE"
    assert "000858" not in json.dumps(response.json())


def test_invalid_input_and_unknown_ticker(monkeypatch, tmp_path):
    service, _, _, _ = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    client = TestClient(api.app)
    assert client.post("/api/knowledge-answers", json={"ticker": "300750.SZ", "question": "   "}).status_code == 422
    assert client.post("/api/knowledge-answers", json={"ticker": "AAPL", "question": "2025年营业收入？"}).status_code == 422
    assert client.post("/api/knowledge-answers", json={"ticker": "600000.SS", "question": "2025年营业收入？"}).status_code == 404


def test_manifest_change_fails_closed(monkeypatch, tmp_path):
    service, _, _, manifest = _service(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "knowledge_answer_service", service)
    manifest.write_text('{"changed": true}', encoding="utf-8")
    original = qa._sha256
    monkeypatch.setattr(qa, "_sha256", lambda path: hashlib.sha256(path.read_bytes()).hexdigest() if path == manifest else original(path))
    response = TestClient(api.app).post("/api/knowledge-answers", json={
        "ticker": "300750.SZ", "question": "宁德时代2025年营业收入是多少？",
    })
    assert response.status_code == 503


def test_existing_report_answer_route_is_preserved():
    assert any(route.path == "/api/answers" and "POST" in route.methods for route in api.app.routes)
    assert any(route.path == "/api/knowledge-answers" and "POST" in route.methods for route in api.app.routes)
