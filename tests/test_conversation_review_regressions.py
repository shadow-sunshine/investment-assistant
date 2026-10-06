"""Codex 独立反例：真实接线、确认范围与持久化边界。"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant.chat_session import dispatch_message
from investment_assistant.conversation_memory import ConversationOrchestrator, assemble_context
from investment_assistant.conversation_store import ConversationStore
from investment_assistant.user_profile import ProfileManager


@pytest.fixture
def setup(tmp_path):
    store = ConversationStore(tmp_path / "review.sqlite3")
    profile = ProfileManager(store)
    orchestrator = ConversationOrchestrator(store)
    conv = store.create_conversation(tenant_id="t", owner_id="u")
    yield store, profile, orchestrator, conv.conversation_id
    store.close()


def run(setup, text, rid, cid=None, dispatch=None):
    store, profile, orchestrator, conversation_id = setup
    return orchestrator.handle_turn(cid or conversation_id, text, tenant_id="t", owner_id="u",
                                    request_id=rid, dispatch=dispatch or (lambda ctx, q, **kw: {
                                        **ctx, "messages": [{"role": "assistant", "text": "收到"}]}))


def test_confirmation_does_not_apply_another_conversations_candidate(setup):
    store, profiles, _, cid = setup
    other = store.create_conversation(tenant_id="t", owner_id="u").conversation_id
    run(setup, "我偏保守", "scope-stage", cid=cid)
    run(setup, "确认", "scope-confirm", cid=other)
    assert profiles.profile_values(tenant_id="t", owner_id="u") == {}


def test_replaced_candidate_cannot_replay_old_preference(setup):
    store, profiles, _, cid = setup
    old = profiles.stage_candidates("我偏保守", tenant_id="t", owner_id="u", conversation_id=cid)[0]
    profiles.stage_candidates("我偏激进", tenant_id="t", owner_id="u", conversation_id=cid)
    assert profiles.confirm_candidate(old.candidate_id, tenant_id="t", owner_id="u")["status"] != "confirmed"
    assert profiles.profile_values(tenant_id="t", owner_id="u") == {}


def test_delete_all_chat_command_is_confirmable(setup):
    _, profiles, _, _ = setup
    profiles.set_field("risk_preference", "conservative", tenant_id="t", owner_id="u")
    staged = run(setup, "删除全部画像", "all-stage")
    assert staged["candidates"]
    run(setup, "确认", "all-confirm")
    assert profiles.profile_values(tenant_id="t", owner_id="u") == {}


def test_dispatch_error_is_persisted_as_readable_assistant_reply(setup):
    def fail(*args, **kwargs):
        raise TimeoutError("不可向用户暴露的地址或凭证")
    result = run(setup, "腾讯2025年收入", "timeout-req", dispatch=fail)
    store, _, _, cid = setup
    messages = store.list_messages(cid, tenant_id="t", owner_id="u")
    assert result["status"] == "failed"
    assert messages[-1].role == "assistant"
    assert "重试" in messages[-1].text
    assert "凭证" not in messages[-1].text


def test_multi_slot_dialogue_only_calls_data_when_ready(setup):
    calls = []
    def company(payload):
        calls.append(payload)
        return {"status": "answered", "answer": "已核验收入", "sources": []}
    def dispatch(ctx, q, **kwargs):
        kwargs.pop("conversation_memory", None)
        return dispatch_message(ctx, q, lambda _: {}, lambda _: {}, "u", company_ask=company,
                                company_discover=lambda query, market=None: {
                                    "status": "onboarded", "selected_ticker": "0700.HK",
                                    "candidates": [{"ticker": "0700.HK", "market": "HK", "verified": True}]})
    for i, q in enumerate(["港股", "腾讯", "2025年", "收入"]):
        run(setup, q, f"slots-{i}", dispatch=dispatch)
    assert len(calls) == 1
    assert calls[0]["ticker"] == "0700.HK"
    assert "2025" in calls[0]["question"] and "收入" in calls[0]["question"]


def test_assembled_budget_includes_system_rules_and_history_labels():
    result = assemble_context(profile={}, context={}, summary=None,
                              messages=[{"role": "user", "text": "x" * 600}],
                              current_question="收入", budget_tokens=400)
    assert result["estimated_tokens"] <= result["budget_tokens"]


def test_nested_database_parent_is_created(tmp_path):
    store = ConversationStore(tmp_path / "new" / "path" / "x.sqlite3")
    store.close()


def test_profile_used_in_real_answer_organization(setup):
    _, profile, _, _ = setup
    profile.set_field("risk_preference", "conservative", tenant_id="t", owner_id="u")
    captured = []
    def dispatch(ctx, q, **kwargs):
        captured.append(kwargs.get("conversation_memory"))
        return {**ctx, "messages": [{"role": "assistant", "text": "有来源的回答"}]}
    result = run(setup, "腾讯2025年收入", "actual-context", dispatch=dispatch)
    assert captured[0] and "conservative" in captured[0]["rendered"]
    assert "偏好" in result["message"]


def test_same_request_does_not_execute_dispatch_twice(setup):
    entered, release = Event(), Event()
    calls = []
    def dispatch(ctx, q, **kwargs):
        calls.append(q)
        entered.set()
        assert release.wait(5)
        return {**ctx, "messages": [{"role": "assistant", "text": "完成"}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(run, setup, "腾讯2025年收入", "same-request", dispatch=dispatch)
        assert entered.wait(5)
        second = run(setup, "腾讯2025年收入", "same-request", dispatch=lambda *a, **k: calls.append("duplicate") or {})
        release.set()
        future.result()
    assert len(calls) == 1
    assert second["status"] in {"request_in_progress", "duplicate_request"}


def test_temporary_preference_cannot_bypass_leverage_constraint(setup):
    _, profile, _, _ = setup
    profile.set_field("leverage_constraint", "none", tenant_id="t", owner_id="u")
    result = run(setup, "这次激进一点，用3倍杠杆", "hard-conflict")
    assert result["status"] == "needs_clarification"
    assert "不会" in result["message"]


def test_expired_profile_confirmation_never_writes(setup):
    store, profile, _, cid = setup
    item = profile.stage_candidates("我偏保守", tenant_id="t", owner_id="u", conversation_id=cid)[0]
    with store._transaction() as conn:
        conn.execute("UPDATE profile_candidates SET created_at='2020-01-01T00:00:00+00:00' WHERE candidate_id=?", (item.candidate_id,))
    assert profile.confirm_candidate(item.candidate_id, tenant_id="t", owner_id="u")["status"] == "expired"
    assert profile.profile_values(tenant_id="t", owner_id="u") == {}


def test_confirm_and_reject_race_has_only_one_effect(setup):
    store, profile, _, cid = setup
    item = profile.stage_candidates("我偏保守", tenant_id="t", owner_id="u", conversation_id=cid)[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: profile.confirm_candidate(item.candidate_id,
                            tenant_id="t", owner_id="u", expected_version=item.version), range(8)))
    assert sum(r["status"] == "confirmed" for r in results) == 1
    assert store.get_profile_entry("risk_preference", tenant_id="t", owner_id="u").version == 1


def test_profile_deletion_invalidates_old_pending_candidates_and_state(setup):
    store, profile, orch, cid = setup
    profile.set_field("risk_preference", "conservative", tenant_id="t", owner_id="u")
    item = profile.stage_candidates("我偏激进", tenant_id="t", owner_id="u", conversation_id=cid)[0]
    store.save_state(cid, {"session_preferences": {"risk_preference": "conservative"}}, tenant_id="t", owner_id="u")
    assert profile.delete_field("risk_preference", tenant_id="t", owner_id="u")
    assert not orch.load_context(cid, tenant_id="t", owner_id="u")["session_preferences"]
    assert profile.confirm_candidate(item.candidate_id, tenant_id="t", owner_id="u")["status"] != "confirmed"


def test_pending_confirmation_does_not_survive_unrelated_question(setup):
    _, profile, _, _ = setup
    run(setup, "我偏保守", "new-pending")
    run(setup, "你好", "unrelated")
    run(setup, "确认", "unrelated-confirm")
    assert profile.profile_values(tenant_id="t", owner_id="u") == {}


def test_independent_store_connections_cannot_dispatch_duplicate(setup):
    store, _, _, cid = setup
    other_store = ConversationStore(store.path)
    other = (other_store, ProfileManager(other_store), ConversationOrchestrator(other_store), cid)
    entered, release = Event(), Event()
    calls = []
    def dispatch(ctx, q, **kwargs):
        calls.append(q)
        entered.set()
        assert release.wait(5)
        return {**ctx, "messages": [{"role": "assistant", "text": "完成"}]}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(run, setup, "腾讯2025年收入", "cross-connection", dispatch=dispatch)
            assert entered.wait(5)
            result = run(other, "腾讯2025年收入", "cross-connection", dispatch=lambda *a, **k: calls.append("bad") or {})
            release.set()
            future.result()
        assert result["status"] == "request_in_progress" and len(calls) == 1
    finally:
        other_store.close()


def test_late_response_after_deletion_cannot_resurrect_conversation(setup):
    store, _, _, cid = setup
    def dispatch(ctx, q, **kwargs):
        assert store.delete_conversation(cid, tenant_id="t", owner_id="u")
        return {**ctx, "messages": [{"role": "assistant", "text": "迟到回复"}]}
    result = run(setup, "腾讯2025年收入", "delete-while-processing", dispatch=dispatch)
    assert result["status"] == "conversation_not_found"
    assert not store.list_messages(cid, tenant_id="t", owner_id="u")


def test_sensitive_input_not_in_storage_or_tool_calls(setup):
    store, profile, _, cid = setup
    called = []
    result = run(setup, "记住我的密码是 test-secret-123", "sensitive-live", dispatch=lambda *a, **k: called.append(a))
    assert result["status"] == "refused" and not called
    assert "test-secret" not in json.dumps([m.to_dict() for m in store.list_messages(cid, tenant_id="t", owner_id="u")])


def test_same_request_id_cannot_be_reused_in_another_conversation(setup):
    store, _, _, _ = setup
    run(setup, "你好", "cross-session-id")
    other = store.create_conversation(tenant_id="t", owner_id="u").conversation_id
    assert run(setup, "你好", "cross-session-id", cid=other)["status"] == "request_conflict"
    assert not store.list_messages(other, tenant_id="t", owner_id="u")


def test_summary_trigger_uses_accumulated_history(setup, monkeypatch):
    from investment_assistant import conversation_memory
    monkeypatch.setattr(conversation_memory, "CONTEXT_BUDGET_TOKENS", 400)
    store, _, _, cid = setup
    for i in range(12):
        run(setup, "你好，我还想继续了解研究流程。" * 3, f"summary-{i}")
    summary = store.load_summary(cid, tenant_id="t", owner_id="u")
    assert summary and summary["covered_through_seq"] == 24


def test_budget_caps_recent_history_window():
    result = assemble_context(profile={}, context={}, summary=None, current_question="收入",
        messages=[{"role": "user", "text": "无关聊天"} for _ in range(50)], recent_turns=3)
    assert result["retained_messages"] <= 6


def test_pause_long_term_profile_keeps_temporary_preferences(setup):
    _, profile, orch, cid = setup
    profile.set_field("risk_preference", "conservative", tenant_id="t", owner_id="u")
    run(setup, "本次不使用长期偏好", "pause-profile")
    run(setup, "这次激进一点", "temporary-after-pause")
    result = run(setup, "腾讯2025年收入", "ask-after-pause")
    assert "进取" in result["message"] and "保守" not in result["message"]
    assert profile.profile_values(tenant_id="t", owner_id="u")["risk_preference"] == "conservative"


def test_profile_update_via_chat_changes_confirmed_value(setup):
    _, profile, _, _ = setup
    profile.set_field("research_horizon", "1y", tenant_id="t", owner_id="u")
    run(setup, "把我的研究期限改成三年", "update-horizon")
    run(setup, "确认", "confirm-horizon")
    assert profile.profile_values(tenant_id="t", owner_id="u")["research_horizon"] == "3y"


def test_readonly_user_cannot_use_conversation_as_tool_bypass(setup, monkeypatch, tmp_path):
    from investment_assistant import audit_log
    store, _, _, _ = setup
    monkeypatch.setattr(api, "_conversation_store", lambda: store)
    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", tmp_path / "audit")
    token = "read-only-test-" + "x" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "u", "tenant": "t", "roles": ["reviewer"]}}))
    client = TestClient(api.app)
    headers = {"Authorization": "Bearer " + token}
    conv = client.post("/api/conversations", json={}, headers=headers).json()["conversation_id"]
    response = client.post(f"/api/conversations/{conv}/turns", headers=headers,
                           json={"text": "腾讯2025年收入", "request_id": "readonly-bypass"})
    assert response.status_code == 403


def test_report_adapter_checks_publication_and_owner(monkeypatch):
    from fastapi import HTTPException
    from investment_assistant.access_control import Principal
    checked = []
    def deny(report_id, principal):
        checked.append((report_id, principal.actor_id))
        raise HTTPException(status_code=404)
    monkeypatch.setattr(api, "_report_access", deny)
    principal = Principal("u", "t", frozenset({"analyst"}))
    with pytest.raises(HTTPException):
        api._conversation_report_ask_factory(principal)({"report_id": "other-private", "question": "收入", "requested_by": "u"})
    assert checked == [("other-private", "u")]


def test_new_conversation_path_keeps_official_index_search(monkeypatch):
    from investment_assistant.access_control import Principal
    monkeypatch.setattr(api.company_answer_service, "answer", lambda *a, **k: {"status": "refused", "error_code": "FIELD_NOT_VERIFIED", "answer": "字段缺口"})
    calls = []
    def search(ticker, question, **kwargs):
        calls.append((ticker, question))
        return {"status": "evidence_retrieved", "answer": "待核验摘录"}
    monkeypatch.setattr(api.official_document_answer_service, "answer", search)
    answer = api._conversation_company_ask_factory(Principal("u", "t", frozenset({"analyst"})))(
        {"ticker": "0700.HK", "question": "2025年研发费用"})
    assert answer["status"] == "evidence_retrieved" and calls
