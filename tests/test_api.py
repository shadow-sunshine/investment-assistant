from yfinance.exceptions import YFPricesMissingError

from fastapi.testclient import TestClient

from investment_assistant import api


def _result() -> dict:
    return {
        "report": "# AAPL report\n\n## 4. \u53d7\u63a7\u7814\u7a76\u53d9\u8ff0\n\n\u7814\u7a76\u89c2\u5bdf\u3002[S1]",
        "ticker": "AAPL",
        "topic": "\u6d4b\u8bd5\u4e3b\u9898",
        "horizon": "\u4e2d\u671f",
        "created_at": "2026-09-08T00:00:00+00:00",
        "llm_result": {"used": True, "model": "qwen-plus"},
        "market_snapshot": {"data_available": True, "latest_close": 1.0},
        "financial_snapshot": {"data_available": True, "revenue": 2},
        "sources": [{"citation": "S1", "content": "evidence", "metadata": {"file_name": "a.pdf", "page": 1}}],
        "risk_flags": ["risk"],
        "evaluation": {"passed": True},
        "retrieval_evaluation": {"passed": True},
    }


def test_generate_list_and_get_report(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(api, "_ticker_exists", lambda _: True)
    monkeypatch.setattr(api, "run_research", lambda **_: _result())
    client = TestClient(api.app)

    created = client.post("/api/reports", json={"ticker": "aapl", "topic": "\u6d4b\u8bd5\u4e3b\u9898", "horizon": "\u4e2d\u671f"})
    assert created.status_code == 200
    assert created.json()["mode"]["label"] == "\u53d7\u63a7 LLM \u7248"

    listed = client.get("/api/reports")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    fetched = client.get(f"/api/reports/{created.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["sources"][0]["metadata"]["page"] == 1


def test_get_missing_report_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    response = TestClient(api.app).get("/api/reports/missing")
    assert response.status_code == 404


def test_invalid_ticker_stops_before_workflow_or_report_write(monkeypatch, tmp_path):
    calls = {"workflow": 0}

    def unexpected_workflow(**_):
        calls["workflow"] += 1
        raise AssertionError("workflow must not run for an invalid ticker")

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(api, "_ticker_exists", lambda _: False)
    monkeypatch.setattr(api, "run_research", unexpected_workflow)

    response = TestClient(api.app).post("/api/reports", json={"ticker": "123456", "topic": "测试主题", "horizon": "中期"})

    assert response.status_code == 422
    assert response.json()["detail"] == "未找到股票代码 123456，请检查输入"
    assert calls["workflow"] == 0
    assert list(tmp_path.iterdir()) == []


def test_blank_topic_stops_before_ticker_lookup_workflow_or_report_write(monkeypatch, tmp_path):
    calls = {"lookup": 0, "workflow": 0}

    def unexpected_lookup(_):
        calls["lookup"] += 1
        raise AssertionError("ticker lookup must not run for a blank topic")

    def unexpected_workflow(**_):
        calls["workflow"] += 1
        raise AssertionError("workflow must not run for a blank topic")

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(api, "_ticker_exists", unexpected_lookup)
    monkeypatch.setattr(api, "run_research", unexpected_workflow)

    response = TestClient(api.app).post("/api/reports", json={"ticker": "AAPL", "topic": "   ", "horizon": "中期"})

    assert response.status_code == 422
    assert response.json()["detail"] == "研究主题不能为空，请检查输入。"
    assert calls == {"lookup": 0, "workflow": 0}
    assert list(tmp_path.iterdir()) == []


def test_missing_ticker_exception_is_reported_as_invalid_input(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)

    def missing_ticker(_):
        raise YFPricesMissingError("123456", "possibly delisted; no price data found")

    monkeypatch.setattr(api, "_ticker_exists", missing_ticker)
    monkeypatch.setattr(api, "run_research", lambda **_: (_ for _ in ()).throw(AssertionError("workflow must not run")))

    response = TestClient(api.app).post("/api/reports", json={"ticker": "123456", "topic": "\u6d4b\u8bd5\u4e3b\u9898", "horizon": "\u4e2d\u671f"})

    assert response.status_code == 422
    assert response.json()["detail"] == "\u672a\u627e\u5230\u80a1\u7968\u4ee3\u7801 123456\uff0c\u8bf7\u68c0\u67e5\u8f93\u5165"
    assert list(tmp_path.iterdir()) == []


def test_upstream_ticker_validation_error_keeps_valid_request_on_workflow_path(monkeypatch, tmp_path):
    calls = {"workflow": 0}

    def unavailable_lookup(_):
        raise ConnectionError("upstream unavailable")

    def workflow(**kwargs):
        calls["workflow"] += 1
        assert kwargs["ticker"] == "AAPL"
        return _result()

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(api, "_ticker_exists", unavailable_lookup)
    monkeypatch.setattr(api, "run_research", workflow)

    response = TestClient(api.app).post("/api/reports", json={"ticker": "AAPL", "topic": "\u6d4b\u8bd5\u4e3b\u9898", "horizon": "\u4e2d\u671f"})

    assert response.status_code == 200
    assert calls["workflow"] == 1
    assert len(list(tmp_path.glob("*.md"))) == 1
