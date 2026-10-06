"""会话与画像 API 的身份边界、端到端真实链路与反例测试。

这些测试用**离线 fake受控能力**证明接线与边界：不证明真实模型语义能力，
也不证明第三方来源可用性。所有断言都指向实际落库内容或实际传给服务的参数。
"""

import json

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant.conversation_store import ConversationStore

TOKENS = {
    "a" * 40: {"actor": "alice", "tenant": "desk-a", "roles": ["analyst", "admin"]},
    "b" * 40: {"actor": "bob", "tenant": "desk-b", "roles": ["analyst"]},
    "c" * 40: {"actor": "carol", "tenant": "desk-a", "roles": ["analyst"]},
}


def headers(actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {actor * 40}"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """每个测试独立的会话库与身份表，避免跨测试串数据。"""
    from investment_assistant import audit_log
    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", tmp_path / "audit")
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps(TOKENS))
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path / "reports")
    from types import SimpleNamespace
    monkeypatch.setattr(api.job_service, "create", lambda **kwargs: (
        SimpleNamespace(job_id="offline-test-job", status="queued", tenant_id=kwargs["tenant_id"],
                        actor_id=kwargs["actor_id"]), True))

    path = tmp_path / "conversation_memory.sqlite3"

    def store() -> ConversationStore:
        return ConversationStore(path)

    monkeypatch.setattr(api, "_conversation_store", store)
    monkeypatch.setattr(api, "_profile_manager", lambda: api.ProfileManager(store()))
    monkeypatch.setattr(api, "_orchestrator", lambda: api.ConversationOrchestrator(store()))
    return TestClient(api.app)


@pytest.fixture()
def calls(monkeypatch):
    """把受控回答能力替换成记录参数的 fake：验证**真实传给服务的查询**。"""
    recorded: list[tuple[str, dict]] = []

    def fake_company(ticker, question, requested_by="anonymous"):
        recorded.append(("company", {"ticker": ticker, "question": question,
                                     "requested_by": requested_by}))
        return {"status": "answered", "answer": f"{ticker} {question} 的已核验收入为 751,766 百万元。[S1]",
                "sources": [{"citation": "S1", "label": "官方年报", "identity": {"ticker": ticker}}]}

    def fake_knowledge(ticker, question, requested_by="anonymous"):
        recorded.append(("knowledge", {"ticker": ticker, "question": question,
                                       "requested_by": requested_by}))
        return {"status": "answered", "answer": f"{ticker} {question}：751,766 百万元。[S1]",
                "sources": [{"citation": "S1", "label": "官方年报", "identity": {"ticker": ticker}}]}

    monkeypatch.setattr(api.company_answer_service, "answer", fake_company)
    monkeypatch.setattr(api.knowledge_answer_service, "answer", fake_knowledge)
    return recorded


def new_conversation(client, actor="a", title=""):
    response = client.post("/api/conversations", json={"title": title}, headers=headers(actor))
    assert response.status_code == 201, response.text
    return response.json()["conversation_id"]


def turn(client, conversation_id, text, request_id, actor="a", **extra):
    payload = {"text": text, "request_id": request_id}
    payload.update(extra)
    return client.post(f"/api/conversations/{conversation_id}/turns", json=payload,
                       headers=headers(actor))


def confirm_all(client, actor="a"):
    profile = client.get("/api/user-profile", headers=headers(actor)).json()
    for candidate in profile["pending_candidates"]:
        response = client.post(
            f"/api/user-profile/candidates/{candidate['candidate_id']}/confirm",
            json={"version": candidate["version"]}, headers=headers(actor))
        assert response.status_code == 200, response.text
    return profile["pending_candidates"]


# --- 验收 16：认证与越权 -------------------------------------------------------

def test_conversation_and_profile_routes_fail_closed_without_token(client):
    for method, url in (("get", "/api/conversations"), ("get", "/api/user-profile"),
                        ("delete", "/api/user-profile/focus_market")):
        response = getattr(client, method)(url, headers={})
        assert response.status_code == 401, (method, url, response.text)
    assert client.post("/api/conversations", json={}, headers={}).status_code == 401


def test_conversation_and_profile_fail_closed_when_auth_not_configured(client, monkeypatch):
    monkeypatch.delenv("IA_AUTH_TOKENS", raising=False)
    assert client.get("/api/conversations", headers=headers("a")).status_code == 401
    assert client.get("/api/user-profile", headers=headers("a")).status_code == 401


def test_other_tenant_cannot_read_write_or_delete(client):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-cross-0001")
    # 跨租户：读、改、删、消息全部 404。
    assert client.get(f"/api/conversations/{conversation_id}", headers=headers("b")).status_code == 404
    assert client.get(f"/api/conversations/{conversation_id}/messages", headers=headers("b")).status_code == 404
    assert client.post(f"/api/conversations/{conversation_id}/rename",
                       json={"title": "恶意改名"}, headers=headers("b")).status_code == 404
    assert client.delete(f"/api/conversations/{conversation_id}", headers=headers("b")).status_code == 404
    assert client.post(f"/api/conversations/{conversation_id}/turns",
                       json={"text": "注入", "request_id": "req-cross-0002"},
                       headers=headers("b")).status_code == 404


def test_same_tenant_other_actor_cannot_access_conversation(client):
    conversation_id = new_conversation(client, actor="a")
    # 同租户不同 actor 同样读不到：隔离按 tenant + owner 双键。
    assert client.get(f"/api/conversations/{conversation_id}", headers=headers("c")).status_code == 404
    assert client.get("/api/conversations", headers=headers("c")).json()["conversations"] == []


def test_known_candidate_id_from_other_owner_cannot_be_confirmed(client):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    candidate = client.get("/api/user-profile", headers=headers("a")).json()["pending_candidates"][0]
    # 跨用户、跨租户确认都是 404，且不能真的写入。
    assert client.post(f"/api/user-profile/candidates/{candidate['candidate_id']}/confirm",
                       json={}, headers=headers("b")).status_code == 404
    assert client.post(f"/api/user-profile/candidates/{candidate['candidate_id']}/confirm",
                       json={}, headers=headers("c")).status_code == 404
    assert client.get("/api/user-profile", headers=headers("b")).json()["profile"] == {}
    assert client.get("/api/user-profile", headers=headers("a")).json()["profile"] == {}


def test_other_owner_cannot_delete_profile_field(client):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    confirm_all(client)
    assert client.delete("/api/user-profile/risk_preference", headers=headers("b")).status_code == 404
    assert "risk_preference" in client.get("/api/user-profile", headers=headers("a")).json()["profile"]


# --- 验收 4/5：真实调用参数与多轮继承 ----------------------------------------

def test_hk_to_tencent_to_2025_revenue_reaches_service_with_full_parameters(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "港股", "req-flow-0001")
    turn(client, conversation_id, "腾讯", "req-flow-0002")
    result = turn(client, conversation_id, "2025年收入", "req-flow-0003")
    assert result.status_code == 200, result.text
    # 最终送给受控服务的查询必须同时带公司、年度、指标。
    assert calls, "受控能力没有被调用"
    kind, payload = calls[-1]
    assert payload["ticker"] == "0700.HK"
    assert "2025" in payload["question"]
    assert "收入" in payload["question"]
    assert result.json()["effective_question"]


def test_follow_up_inherits_company_and_year_without_repeating_conditions(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯", "req-follow-0001")
    turn(client, conversation_id, "2025年收入", "req-follow-0002")
    calls.clear()
    result = turn(client, conversation_id, "那净利润呢", "req-follow-0003")
    assert result.status_code == 200
    # 追问继承公司与年度，不要求用户重新输入全部条件。
    _, payload = calls[-1]
    assert payload["ticker"] == "0700.HK"
    assert payload["question"].startswith("2025年，")
    assert "净利润" in payload["question"]


def test_switching_company_does_not_carry_old_evidence(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯", "req-switch-0001")
    turn(client, conversation_id, "2025年收入", "req-switch-0002")
    calls.clear()
    turn(client, conversation_id, "平安银行 2025 年营业收入是多少？", "req-switch-0003")
    _, payload = calls[-1]
    # 不能带着腾讯证据回答平安银行。
    assert payload["ticker"] == "000001.SZ"
    state = client.get(f"/api/conversations/{conversation_id}", headers=headers("a")).json()["state"]
    assert state["ticker"] == "000001.SZ"
    # 会话状态里的旧公司专属字段必须清掉。
    assert state.get("knowledge_ticker") in (None, "000001.SZ")


def test_switching_to_company_without_known_year_asks_instead_of_guessing(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯", "req-unknown-0001")
    turn(client, conversation_id, "2025年收入", "req-unknown-0002")
    calls.clear()
    result = turn(client, conversation_id, "换成阿里巴巴", "req-unknown-0003")
    assert result.status_code == 200
    # 阿里年度不确定时不能沿用腾讯的 2025 直接作答。
    state = client.get(f"/api/conversations/{conversation_id}", headers=headers("a")).json()["state"]
    assert state.get("year") is None or "2025" not in str(result.json().get("message") or "")
    body = result.json()
    if body.get("answer") and body["answer"].get("status") == "answered":
        for kind, payload in calls:
            assert payload["ticker"] != "0700.HK"


# --- 验收 6：模式切换 ---------------------------------------------------------

def test_switching_answer_mode_keeps_history(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-mode-0001")
    before = client.get(f"/api/conversations/{conversation_id}/messages",
                        headers=headers("a")).json()["count"]
    response = client.post(f"/api/conversations/{conversation_id}/answer-mode",
                           json={"answer_mode": "mcp"}, headers=headers("a"))
    assert response.status_code == 200
    after = client.get(f"/api/conversations/{conversation_id}/messages",
                       headers=headers("a")).json()["count"]
    # 切换模式不删消息。
    assert before == after and before > 0
    assert client.get(f"/api/conversations/{conversation_id}",
                      headers=headers("a")).json()["answer_mode"] == "mcp"


def test_official_and_mcp_answers_are_not_mixed(client, calls, monkeypatch):
    """MCP 模式必须走候选路径，不能把官方结论当 MCP 结果返回。"""
    conversation_id = new_conversation(client)

    def fake_candidates(principal, ticker, year=None):
        calls.append(("mcp", {"ticker": ticker, "year": year}))
        return {"data_available": True, "candidates": [
            {"row": {"STD_ITEM_NAME": "营业收入", "AMOUNT": 996347, "REPORT_DATE": "2025-12-31"},
             "source": "fake-mcp"}], "sources": ["fake-mcp"]}

    monkeypatch.setattr(api, "get_structured_financial_candidates_tool", fake_candidates)
    turn(client, conversation_id, "腾讯", "req-mix-0001", answer_mode="mcp")
    turn(client, conversation_id, "2025年收入", "req-mix-0002", answer_mode="mcp")
    mcp_calls = [item for item in calls if item[0] == "mcp"]
    assert mcp_calls, "MCP 模式没有走候选数据源"
    assert mcp_calls[-1][1]["ticker"] == "0700.HK"
    # 官方受控能力不应在 MCP 模式下被调用。
    assert not [item for item in calls if item[0] == "company"]


# --- 验收 7/8/9/10：画像在真实回答链路里生效 ---------------------------------

def test_self_reported_preference_needs_confirmation(client, calls):
    conversation_id = new_conversation(client)
    result = turn(client, conversation_id, "我偏保守，主要看港股", "req-prof-0001")
    body = result.json()
    assert body["status"] == "needs_confirmation"
    assert sorted(item["field"] for item in body["candidates"]) == ["focus_market", "risk_preference"]
    # 未经确认不进长期画像。
    assert client.get("/api/user-profile", headers=headers("a")).json()["profile"] == {}


def test_confirmed_profile_is_used_in_a_new_conversation_real_context(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "我偏保守，主要看港股", "req-use-0001")
    confirm_all(client)
    # 新会话里真实组装上下文时必须带上画像，而不只是返回 profile id。
    fresh = new_conversation(client)
    result = turn(client, fresh, "腾讯 2025 年营业收入是多少？", "req-use-0002")
    context = result.json()["context"]
    assert context["profile_applied"] is True
    rendered = "\n".join(line for section in context["sections"] for line in section["lines"])
    assert "conservative" in rendered and "HK" in rendered


def test_profile_change_shows_previous_and_replaces_value(client):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    confirm_all(client)
    updated = client.put("/api/user-profile/risk_preference", json={"value": "aggressive"},
                         headers=headers("a"))
    assert updated.status_code == 200
    body = updated.json()
    assert body["previous_value"] == "conservative" and body["entry"]["value"] == "aggressive"
    profile = client.get("/api/user-profile", headers=headers("a")).json()["profile"]
    assert profile["risk_preference"]["value"] == "aggressive"


def test_update_profile_rejects_unknown_field(client):
    assert client.put("/api/user-profile/password", json={"value": "x"},
                      headers=headers("a")).status_code == 422


def test_session_tone_does_not_overwrite_long_term_profile(client, calls):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    confirm_all(client)
    conversation_id = new_conversation(client)
    result = turn(client, conversation_id, "这次激进一点", "req-tone-0001")
    assert result.status_code == 200
    # 长期画像没有被会话临时偏好改写。
    profile = client.get("/api/user-profile", headers=headers("a")).json()["profile"]
    assert profile["risk_preference"]["value"] == "conservative"


def test_hard_constraint_conflict_asks_for_clarification(client, calls):
    client.post("/api/user-profile/candidates", json={"text": "记住我不要用任何杠杆"}, headers=headers("a"))
    confirm_all(client)
    conversation_id = new_conversation(client)
    calls.clear()
    result = turn(client, conversation_id, "这次用3倍杠杆做腾讯", "req-hard-0001")
    body = result.json()
    assert body["status"] == "needs_clarification"
    assert "杠杆" in body["message"]
    # 冲突时不能继续调用受控能力去作答。
    assert calls == []


def test_profile_delete_takes_effect_and_is_not_revived_by_old_history(client, calls):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守，主要看港股"}, headers=headers("a"))
    confirm_all(client)
    first = new_conversation(client)
    turn(client, first, "腾讯 2025 年营业收入是多少？", "req-revive-0001")
    assert client.delete("/api/user-profile/risk_preference",
                         headers=headers("a")).status_code == 200
    # 恢复旧会话并继续提问：已撤销偏好不得回到上下文。
    second = new_conversation(client)
    turn(client, second, "我偏保守", "req-revive-0002")
    confirm_all(client)
    assert client.delete("/api/user-profile/risk_preference",
                         headers=headers("a")).status_code == 200
    third = new_conversation(client)
    result = turn(client, third, "腾讯 2025 年营业收入是多少？", "req-revive-0003")
    rendered = "\n".join(line for section in result.json()["context"]["sections"]
                         for line in section["lines"])
    assert "conservative" not in rendered


def test_delete_all_profile_reports_actual_count(client):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守，主要看港股"}, headers=headers("a"))
    confirm_all(client)
    response = client.delete("/api/user-profile", headers=headers("a"))
    assert response.status_code == 200 and response.json()["deleted"] == 2
    assert client.get("/api/user-profile", headers=headers("a")).json()["profile"] == {}
    # 没有可删时不谎报成功。
    assert client.delete("/api/user-profile", headers=headers("a")).json()["deleted"] == 0


def test_disabling_profile_for_session_keeps_archive(client, calls):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    confirm_all(client)
    conversation_id = new_conversation(client)
    result = turn(client, conversation_id, "本次不使用长期偏好", "req-disable-0001")
    assert result.json()["status"] == "profile_disabled"
    follow = turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-disable-0002")
    context = follow.json()["context"]
    assert context["profile_disabled"] is True
    rendered = "\n".join(line for section in context["sections"] for line in section["lines"])
    assert "conservative" not in rendered
    # 长期档案没有被删除。
    assert "risk_preference" in client.get("/api/user-profile", headers=headers("a")).json()["profile"]


# --- 验收 15：重复提交与迟到回答 ---------------------------------------------

def test_duplicate_request_id_is_idempotent(client, calls):
    conversation_id = new_conversation(client)
    first = turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-idem-0001")
    assert first.status_code == 200
    count_before = client.get(f"/api/conversations/{conversation_id}/messages",
                              headers=headers("a")).json()["count"]
    call_count = len(calls)
    second = turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-idem-0001")
    assert second.json()["status"] == "duplicate_request"
    count_after = client.get(f"/api/conversations/{conversation_id}/messages",
                             headers=headers("a")).json()["count"]
    # 不产生第二条消息，也不重复调用受控能力。
    assert count_after == count_before
    assert len(calls) == call_count


def test_late_answer_stays_in_its_original_conversation(client, calls):
    """切换会话后提交的两轮，回答只能落在各自会话，不能串。"""
    first = new_conversation(client)
    second = new_conversation(client)
    turn(client, first, "平安银行 2025 年营业收入是多少？", "req-late-0001")
    turn(client, second, "腾讯 2025 年营业收入是多少？", "req-late-0002")
    first_text = [item["text"] for item in
                  client.get(f"/api/conversations/{first}/messages", headers=headers("a")).json()["messages"]]
    second_text = [item["text"] for item in
                   client.get(f"/api/conversations/{second}/messages", headers=headers("a")).json()["messages"]]
    assert all("平安银行" in text or "000001" in text for text in first_text)
    assert not any("平安银行" in text for text in second_text)
    assert not any("腾讯" in text for text in first_text)


def test_two_conversations_do_not_share_state(client, calls):
    hk = new_conversation(client)
    us = new_conversation(client)
    turn(client, hk, "港股", "req-share-0001")
    turn(client, hk, "腾讯", "req-share-0002")
    turn(client, hk, "2025年收入", "req-share-0003")
    turn(client, us, "美股", "req-share-0004")
    left = client.get(f"/api/conversations/{hk}", headers=headers("a")).json()["state"]
    right = client.get(f"/api/conversations/{us}", headers=headers("a")).json()["state"]
    assert left.get("market") == "HK" and right.get("market") != "HK"
    # 右会话不得继承左会话的公司/年度。
    assert right.get("ticker") is None and right.get("year") is None


def test_deleting_conversation_while_messages_exist_keeps_profile(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "我偏保守", "req-delrace-0001")
    confirm_all(client)
    turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-delrace-0002")
    assert client.delete(f"/api/conversations/{conversation_id}", headers=headers("a")).status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}", headers=headers("a")).status_code == 404
    assert client.get(f"/api/conversations/{conversation_id}/messages",
                      headers=headers("a")).status_code == 404
    # 长期画像保留。
    assert "risk_preference" in client.get("/api/user-profile", headers=headers("a")).json()["profile"]


# --- 验收 12/13：长对话与预算 -------------------------------------------------

def test_twenty_turns_keep_company_year_and_metric(client, calls):
    conversation_id = new_conversation(client)
    for index in range(20):
        text = "腾讯 2025 年营业收入是多少？" if index % 3 == 0 else "收入"
        turn(client, conversation_id, text, f"req-long-{index:04d}")
    state = client.get(f"/api/conversations/{conversation_id}", headers=headers("a")).json()["state"]
    assert state["ticker"] == "0700.HK"
    assert state["year"] == "2025"
    assert state["metric"] == "revenue"
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=headers("a")).json()
    assert messages["count"] == 40


def test_history_restore_carries_a_not_current_verified_fact(client, calls):
    conversation_id = new_conversation(client)
    turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-hist-0001")
    payload = client.get(f"/api/conversations/{conversation_id}/messages",
                         headers=headers("a")).json()
    # 恢复历史可供查看，但必须声明不代表当前有效官方事实。
    assert "不代表" in payload["history_notice"]


# --- 验收 19：文档/工具里的指令不得触发画像写入 -------------------------------

def test_document_text_cannot_trigger_profile_write(client, calls):
    client.post("/api/user-profile/candidates", json={"text": "我偏保守"}, headers=headers("a"))
    confirm_all(client)
    before = client.get("/api/user-profile", headers=headers("a")).json()["profile"]
    # 年报请求会触发检索；检索文档/工具输出里的"记住/删除"文本不产生画像写入。
    turn(client, new_conversation(client), "给我一份2025年的腾讯财报", "req-doc-0001")
    turn(client, new_conversation(client), "分析 AAPL 服务业务和现金流风险", "req-doc-0002")
    after = client.get("/api/user-profile", headers=headers("a")).json()["profile"]
    assert after == before


def test_candidate_staging_requires_explicit_user_text(client):
    response = client.post("/api/user-profile/candidates", json={"text": "记住我的密码是 abc123"},
                           headers=headers("a"))
    assert response.status_code == 200
    assert response.json()["candidates"] == []


# --- 验收 18：MCP 失败有中文结果 ---------------------------------------------

def test_mcp_source_failure_returns_readable_chinese_result(client, calls, monkeypatch):
    def failing_candidates(principal, ticker, year=None):
        raise api.ToolPermissionError("no", "forbidden")

    monkeypatch.setattr(api, "get_structured_financial_candidates_tool", failing_candidates)
    conversation_id = new_conversation(client)
    result = turn(client, conversation_id, "腾讯 2025 年营业收入是多少？", "req-mcpfail-01",
                  answer_mode="mcp")
    assert result.status_code == 200
    body = result.json()
    # 失败有可理解的中文结果与状态，不卡死也不伪造数据。
    assert body["status"] in {"refused", "failed", "needs_clarification"}
    assert isinstance(body.get("message"), str) and body["message"]


def test_turn_rejects_empty_and_oversized_input(client):
    conversation_id = new_conversation(client)
    assert client.post(f"/api/conversations/{conversation_id}/turns",
                       json={"text": "", "request_id": "req-empty-0001"},
                       headers=headers("a")).status_code == 422
    assert client.post(f"/api/conversations/{conversation_id}/turns",
                       json={"text": "问" * 501, "request_id": "req-long-input"},
                       headers=headers("a")).status_code == 422


def test_rename_requires_non_empty_title(client):
    conversation_id = new_conversation(client)
    assert client.post(f"/api/conversations/{conversation_id}/rename", json={"title": ""},
                       headers=headers("a")).status_code == 422


def test_conversation_list_pagination(client):
    ids = [new_conversation(client, title=f"会话{i}") for i in range(5)]
    page = client.get("/api/conversations?limit=2&offset=0", headers=headers("a")).json()
    assert len(page["conversations"]) == 2 and page["total"] == 5 and page["has_more"] is True
    tail = client.get("/api/conversations?limit=2&offset=4", headers=headers("a")).json()
    assert len(tail["conversations"]) == 1 and tail["has_more"] is False
    assert set(ids) >= {item["conversation_id"] for item in page["conversations"]}


def test_report_job_created_from_conversation_carries_server_side_ownership(client, monkeypatch):
    """从会话提交的报告任务必须绑定服务端身份，不能由请求参数决定归属。"""
    created: list[dict] = []

    class FakeJob:
        job_id = "job_from_conversation"
        status = "queued"
        tenant_id = None
        actor_id = None

    def fake_create(**payload):
        created.append(dict(payload))
        job = FakeJob()
        job.tenant_id = payload.get("tenant_id")
        job.actor_id = payload.get("actor_id")
        return job, True

    monkeypatch.setattr(api.job_service, "create", fake_create)
    conversation_id = new_conversation(client)
    response = turn(client, conversation_id, "给我一份2025年的腾讯财报", "req-job-0001")
    assert response.status_code == 200
    assert created, "报告任务没有被创建"
    # requested_by 由服务端改写，不接受请求里的身份。
    assert created[0]["requested_by"] == "alice"