"""真实聊天端点的用户行为验收；本地官方资料真实读取，外部/付费调用禁止。"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from investment_assistant import api, audit_log
from investment_assistant.conversation_store import ConversationStore
from investment_assistant.conversation_memory import ConversationOrchestrator


@pytest.fixture
def flow(monkeypatch, tmp_path):
    store = ConversationStore(tmp_path / "behavior.sqlite3")
    monkeypatch.setattr(api, "_conversation_store", lambda: store)
    monkeypatch.setattr(api, "_orchestrator", lambda: ConversationOrchestrator(store))
    monkeypatch.setattr(api, "_profile_manager", lambda: api.ProfileManager(store))
    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", tmp_path / "audit")
    token = "behavior-tests-" + "x" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "behavior", "tenant": "isolated", "roles": ["analyst"]}}))
    def forbidden(*args, **kwargs):
        raise AssertionError("行为验收禁止付费模型与外部数据源调用")
    monkeypatch.setattr(api, "get_structured_financial_candidates_tool", forbidden)
    monkeypatch.setattr(api.job_service, "create", forbidden)
    client = TestClient(api.app)
    headers = {"Authorization": "Bearer " + token}
    cid = client.post("/api/conversations", json={}, headers=headers).json()["conversation_id"]
    def ask(text):
        response = client.post(f"/api/conversations/{cid}/turns", headers=headers,
                               json={"text": text, "request_id": "behavior-" + uuid.uuid4().hex})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("message"), body
        return body
    ask.store, ask.client, ask.cid, ask.headers = store, client, cid, headers
    yield ask
    store.close()


@pytest.mark.parametrize("text", ["腾讯最新官方资料有哪些？", "腾讯最新公告有哪些", "腾讯有哪些已收录的年报", "腾讯的官方披露清单", "腾讯官方资料目录"])
def test_document_inventory_does_not_require_year_or_metric(flow, text):
    result = flow(text)
    assert result["status"] == "documents_listed"
    assert (result.get("answer") or {}).get("documents")
    assert "Next Day Disclosure Return" in result["message"] or "ANNUAL REPORT" in result["message"]
    assert "想看哪一年" not in result["message"]


@pytest.mark.parametrize("text", ["我想查看港股公司的最新公告", "我想了解最近的港股", "我想看港股市场", "美股最近怎么样", "我想看看A股"])
def test_general_market_request_not_attached_to_previous_company(flow, text):
    flow("腾讯2025年收入")
    result = flow(text)
    assert (result.get("answer") or {}).get("error_code") != "COMPANY_SCOPE_UNVERIFIED"
    assert result["status"] == "needs_clarification"
    assert "公司" in result["message"] or "市场" in result["message"]
    assert not result.get("effective_question", "").startswith("2025")


def test_multi_metric_query_reports_each_requested_metric(flow):
    result = flow("腾讯 2025 年收入和净利润怎么样？")
    answer = result.get("answer") or {}
    outcomes = answer.get("metric_results") or []
    assert {item["metric"] for item in outcomes} == {"营业收入", "净利润"}
    assert "净利润" in result["message"] and "751,766" in result["message"]
    assert result["status"] in {"partial", "answered"}


def test_all_selection_after_metric_question_is_not_company_scope_error(flow):
    flow("腾讯")
    flow("2025")
    result = flow("都要")
    assert (result.get("answer") or {}).get("error_code") != "COMPANY_SCOPE_UNVERIFIED"
    assert len((result.get("answer") or {}).get("metric_results") or []) == 3


@pytest.mark.parametrize("text", ["你好", "你是谁", "你能做什么", "如何查询已接入的官方年报？", "怎么使用这个助手"])
def test_product_questions_not_sent_to_financial_service(flow, text):
    flow("腾讯2025年收入")
    result = flow(text)
    assert result["status"] in {"product", "answered"}
    assert (result.get("answer") or {}).get("error_code") != "COMPANY_SCOPE_UNVERIFIED"
    assert not result.get("effective_question", "").startswith("2025")


def test_latest_question_does_not_inherit_old_year(flow):
    flow("腾讯2025年收入")
    result = flow("腾讯最新官方资料有哪些？")
    assert result["status"] == "documents_listed"
    assert "2026" in result["message"]
    assert "本地" in result["message"] or "已入库" in result["message"]


def test_ui_confirmation_then_typed_confirmation_explains_already_saved(flow):
    staged = flow("我偏保守，主要看港股")
    for candidate in staged["candidates"]:
        response = flow.client.post(f"/api/user-profile/candidates/{candidate['candidate_id']}/confirm", headers=flow.headers,
                                    json={"version": candidate["version"]})
        assert response.status_code == 200
    result = flow("确认")
    assert "已保存" in result["message"]
    assert "当前没有待确认" not in result["message"]


def test_cancelled_profile_not_presented_as_confirmed_strategy(flow):
    flow("我偏保守，主要看港股")
    flow("取消")
    result = flow("腾讯2025年收入")
    assert "按你的保守" not in result["message"]
    assert flow.client.get("/api/user-profile", headers=flow.headers).json()["profile"] == {}


def test_confirmed_profile_pause_removes_long_term_tone(flow):
    flow("我偏保守，主要看港股")
    flow("确认")
    flow("本次不使用长期偏好")
    result = flow("腾讯2025年收入")
    assert "按你的保守" not in result["message"]


def test_generic_latest_documents_request_inherits_task_not_last_financial_year(flow):
    flow("腾讯2025年收入")
    result = flow("它的最新公告呢")
    assert result["status"] == "documents_listed"


def test_no_false_revenue_answer_for_profit_only(flow):
    result = flow("腾讯2025年净利润是多少")
    assert "全年收入为人民币" not in result["message"]


def test_snapshot_full_conversation(flow):
    sequence = ["我想查看港股公司的最新公告", "腾讯最新官方资料有哪些？", "腾讯 2025 年收入和净利润怎么样？", "我偏保守，主要看港股", "确认", "我想了解最近的港股"]
    answers = [flow(q) for q in sequence]
    assert not any((a.get("answer") or {}).get("error_code") == "COMPANY_SCOPE_UNVERIFIED" for a in answers)
    assert answers[1]["status"] == "documents_listed"
    assert answers[4]["status"] == "profile_confirmed"


@pytest.mark.parametrize("text", ["什么是市盈率", "什么是毛利率", "解释一下现金流", "同比是什么意思"])
def test_concept_after_company_question_does_not_inherit_company_year(flow, text):
    flow("腾讯2025年收入")
    result = flow(text)
    assert result["status"] == "answered"
    assert (result.get("answer") or {}).get("error_code") != "COMPANY_SCOPE_UNVERIFIED"
    assert "2025年，" not in result["message"]


def test_company_switch_not_classified_as_profile_update(flow):
    flow("腾讯2025年收入")
    result = flow("换成阿里巴巴")
    assert result["status"] != "no_profile_candidate"
    state = flow.client.get(f"/api/conversations/{flow.cid}", headers=flow.headers).json()["state"]
    assert state.get("ticker") != "0700.HK"
    assert not state.get("year")


def test_followup_year_keeps_revenue_metric_but_not_confuse_with_unknown_company(flow):
    flow("腾讯2025年收入")
    result = flow("2024年呢")
    assert (result.get("answer") or {}).get("error_code") not in {"COMPANY_SCOPE_UNVERIFIED", "COMPANY_NOT_FOUND"}


def test_newer_request_does_not_rewrite_latest_to_old_knowledge_year(flow):
    flow("腾讯2025年收入")
    result = flow("腾讯最新收入怎么样")
    assert "2025年，" not in result.get("effective_question", "")
    assert "2025 年全年收入为" not in result["message"]


def test_all_selection_keeps_original_user_message(flow):
    flow("腾讯")
    flow("2025")
    flow("都要")
    messages = flow.client.get(f"/api/conversations/{flow.cid}/messages", headers=flow.headers).json()["messages"]
    assert [m for m in messages if m["role"] == "user"][-1]["text"] == "都要"


def test_market_announcement_request_then_company_completes_inventory(flow):
    flow("我想查看港股公司的最新公告")
    result = flow("腾讯")
    assert result["status"] == "documents_listed"
    assert "想看哪一年" not in result["message"]


@pytest.mark.parametrize("text", ["收入和利润", "营业收入和净利润", "收入、净利润和现金流", "归母净利润和研发费用"])
def test_multiple_metric_parser_covers_all_requested_fields(text):
    from investment_assistant.question_intents import requested_metrics
    assert len(requested_metrics(text)) >= 2


def test_detailed_metrics_not_replaced_by_total_revenue(flow):
    result = flow("腾讯2025年利息收入和净利润是多少")
    assert "利息收入" in result["message"]
    assert "营业收入（已核验）" not in result["message"]


def test_unknown_company_in_multi_metric_query_does_not_get_tencent_facts(flow):
    flow("腾讯2025年收入")
    result = flow("小米2025年收入和净利润是多少")
    assert "751,766" not in result["message"]


def test_mcp_inventory_does_not_silently_switch_data_mode(flow):
    flow.client.post(f"/api/conversations/{flow.cid}/answer-mode", headers=flow.headers, json={"answer_mode": "mcp"})
    result = flow("腾讯最新公告有哪些")
    assert result["status"] == "needs_clarification"
    assert "官方资料模式" in result["message"]


def test_ui_click_real_backend_confirm_and_follow_up(flow, monkeypatch):
    """不是固定回答的 FakeAPI：Streamlit 发出请求，由真实 FastAPI 编排和官方本地服务处理。"""
    from pathlib import Path
    import requests
    from urllib.parse import urlsplit
    from streamlit.testing.v1 import AppTest

    def request_adapter(method):
        def send(url, **kwargs):
            parts = urlsplit(url)
            path = parts.path + ("?" + parts.query if parts.query else "")
            result = flow.client.request(method, path, headers=kwargs.get("headers"), json=kwargs.get("json"))
            response = requests.Response()
            response.status_code = result.status_code
            response._content = result.content
            response.url = url
            response.encoding = "utf-8"
            return response
        return send

    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(requests, method, request_adapter(method.upper()))
    app = Path(__file__).resolve().parent.parent / "investment_assistant" / "web_app.py"
    at = AppTest.from_file(str(app), default_timeout=60)
    at.session_state["auth_token"] = flow.headers["Authorization"][7:]
    at.session_state["identity"] = {"actor_id": "behavior", "tenant_id": "isolated", "roles": ["analyst"]}
    at.run()
    assert not at.exception
    cid = at.session_state["conversation_id"]
    at.button(key="quick_question_0").click().run()
    assert not at.exception
    history = flow.client.get(f"/api/conversations/{cid}/messages", headers=flow.headers).json()["messages"]
    assert history[-1]["answer"]["status"] == "documents_listed"
    assert "Next Day Disclosure Return" in history[-1]["text"]
    at.chat_input[0].set_value("我偏保守，主要看港股").run()
    assert not at.exception
    at.button(key="profile_confirm").click().run()
    assert not at.exception
    at.chat_input[0].set_value("确认").run()
    assert not at.exception
    history = flow.client.get(f"/api/conversations/{cid}/messages", headers=flow.headers).json()["messages"]
    assert "已保存" in history[-1]["text"]
    at.chat_input[0].set_value("我想查看港股公司的最新公告").run()
    assert not at.exception
    history = flow.client.get(f"/api/conversations/{cid}/messages", headers=flow.headers).json()["messages"]
    assert history[-1]["answer"]["status"] == "needs_clarification"
    at.chat_input[0].set_value("腾讯").run()
    assert not at.exception
    history = flow.client.get(f"/api/conversations/{cid}/messages", headers=flow.headers).json()["messages"]
    assert history[-1]["answer"]["status"] == "documents_listed"


def test_document_task_failure_also_writes_readable_response(flow, monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("内部敏感路径不显示")
    monkeypatch.setattr(api.official_document_answer_service, "inventory", fail)
    result = flow("腾讯最新公告有哪些")
    assert result["status"] == "failed"
    assert "重试" in result["message"]
    assert "敏感路径" not in result["message"]
    messages = flow.client.get(f"/api/conversations/{flow.cid}/messages", headers=flow.headers).json()["messages"]
    assert messages[-1]["role"] == "assistant"


def test_scope_strip_deterministic_for_overlapping_grammar_words():
    from investment_assistant.company_qa import catalog, unverified_company_subject
    records = catalog()
    for text in ["腾讯的官方披露清单", "腾讯官方资料目录", "腾讯2025年利息收入和净利润是多少", "2025年营业收入和净利润和经营现金流是多少？"]:
        assert not unverified_company_subject(text, records, include_non_fact=True), text
    assert unverified_company_subject("腾讯和小米2025年收入", records, include_non_fact=True)


@pytest.mark.parametrize("metric", ["利息收入", "服务收入", "投资收入", "净收入", "净利润", "归母净利润"])
def test_other_metrics_never_use_total_revenue_anchor(flow, metric):
    result = flow(f"腾讯2025年{metric}是多少")
    assert "751,766" not in result["message"] or result["status"] == "evidence_retrieved"
    assert "2025 年全年收入为人民币" not in result["message"]
    assert result["status"] != "answered"



def test_year_after_document_inventory_filters_documents_not_old_revenue(flow):
    flow("腾讯2025年收入")
    flow("腾讯最新官方资料有哪些")
    result = flow("2025年")
    assert result["status"] == "documents_listed"
    assert all(d["report_date"].startswith("2025") for d in result["answer"]["documents"])
    assert "751,766" not in result["message"]


def test_unrelated_yes_does_not_explain_old_profile_as_current_action(flow):
    flow("我偏保守，主要看港股")
    flow("确认")
    flow("你好")
    result = flow("确认")
    assert result["status"] == "no_pending_confirmation"


@pytest.mark.parametrize("field", ["全年营业收入", "全年收入", "总收入"])
def test_total_revenue_synonyms_still_use_verified_total_anchor(flow, field):
    result = flow(f"腾讯2025年{field}是多少")
    assert result["status"] == "answered"
    assert "751,766" in result["message"]
    assert result["answer"]["sources"]
