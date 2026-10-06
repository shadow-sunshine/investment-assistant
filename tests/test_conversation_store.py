"""会话存储、画像与短期记忆的事务、隔离与边界测试（离线，不触网）。"""

import threading

import pytest

from investment_assistant.conversation_memory import (
    ConversationOrchestrator, assemble_context, build_summary, clear_mode_specific_state,
    effective_query_for, estimate_tokens, merge_turn_state, switch_company_state,
)
from investment_assistant.conversation_store import (
    ConversationStore, ConversationStoreError, DuplicateRequest,
)
from investment_assistant.user_profile import (
    ProfileManager, classify_profile_command, contains_sensitive_content,
    extract_profile_candidates, hard_constraint_conflict, is_preference_statement,
    render_profile_block,
)

TENANT = "desk-a"
OWNER = "alice"
OTHER_OWNER = "carol"
OTHER_TENANT = "desk-b"


@pytest.fixture()
def store(tmp_path):
    instance = ConversationStore(tmp_path / "conversation_memory.sqlite3")
    yield instance
    instance.close()


@pytest.fixture()
def profiles(store):
    return ProfileManager(store)


def make_conversation(store, *, tenant=TENANT, owner=OWNER, title=""):
    return store.create_conversation(tenant_id=tenant, owner_id=owner, title=title)


# --- 验收 1：会话生命周期 -------------------------------------------------------

def test_conversation_lifecycle_create_list_rename_delete(store):
    conversation = make_conversation(store)
    assert conversation.title == "" and conversation.title_source == "auto"
    listed = store.list_conversations(tenant_id=TENANT, owner_id=OWNER)
    assert [item.conversation_id for item in listed] == [conversation.conversation_id]
    renamed = store.rename_conversation(conversation.conversation_id, "腾讯收入研究",
                                       tenant_id=TENANT, owner_id=OWNER)
    assert renamed.title == "腾讯收入研究" and renamed.title_source == "manual"
    assert store.delete_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert store.get_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER) is None
    # 删除后不可重复删除。
    assert not store.delete_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)


def test_manual_title_is_not_overwritten_by_auto_title(store):
    conversation = make_conversation(store)
    store.rename_conversation(conversation.conversation_id, "我起的名字", tenant_id=TENANT, owner_id=OWNER)
    store.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                      user_text="腾讯 2025 年收入", request_id="req-title-0001", answer_text="好的")
    assert store.apply_auto_title(conversation.conversation_id, "自动标题", tenant_id=TENANT, owner_id=OWNER) is None
    current = store.get_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert current.title == "我起的名字" and current.title_source == "manual"


def test_auto_title_fills_empty_title_only(store):
    conversation = make_conversation(store)
    assert store.apply_auto_title(conversation.conversation_id, "腾讯 2025 年收入", tenant_id=TENANT,
                                  owner_id=OWNER) is not None
    assert store.get_conversation(conversation.conversation_id, tenant_id=TENANT,
                                  owner_id=OWNER).title == "腾讯 2025 年收入"


# --- 验收 2：刷新/重启后恢复 ---------------------------------------------------

def test_messages_and_state_survive_a_new_store_instance(tmp_path):
    path = tmp_path / "conversation_memory.sqlite3"
    first = ConversationStore(path)
    conversation = first.create_conversation(tenant_id=TENANT, owner_id=OWNER)
    first.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                      user_text="腾讯 2025 年收入", request_id="req-restart-01", answer_text="已核验")
    first.save_state(conversation.conversation_id, {"ticker": "0700.HK", "year": "2025"},
                     tenant_id=TENANT, owner_id=OWNER)
    first.close()

    # 模拟服务重启：全新连接，只有 ID 可用于恢复。
    second = ConversationStore(path)
    restored = second.get_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert restored is not None
    state = second.load_state(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert state["state"]["ticker"] == "0700.HK" and state["version"] >= 1
    messages = second.list_messages(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert [item.text for item in messages] == ["腾讯 2025 年收入", "已核验"]
    second.close()


# --- 验收 3/16：身份隔离与越权 -------------------------------------------------

def test_known_conversation_id_from_other_owner_cannot_be_read_or_written(store):
    conversation = make_conversation(store)
    for other in (OTHER_OWNER,):
        assert store.get_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=other) is None
        assert store.list_messages(conversation.conversation_id, tenant_id=TENANT, owner_id=other) == []
        assert store.load_state(conversation.conversation_id, tenant_id=TENANT, owner_id=other) is None
        assert not store.rename_conversation(conversation.conversation_id, "x", tenant_id=TENANT, owner_id=other)
        assert not store.delete_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=other)
    # 跨租户同样读不到。
    assert store.get_conversation(conversation.conversation_id, tenant_id=OTHER_TENANT, owner_id=OWNER) is None
    assert store.list_conversations(tenant_id=OTHER_TENANT, owner_id=OWNER) == []


def test_same_tenant_different_actor_is_still_isolated(store):
    """同一 demo 凭证代表同一身份；隔离测试必须用不同 actor/tenant。"""
    alice = make_conversation(store, owner="alice")
    carol = make_conversation(store, owner="carol")
    assert store.get_conversation(alice.conversation_id, tenant_id=TENANT, owner_id="carol") is None
    assert store.get_conversation(carol.conversation_id, tenant_id=TENANT, owner_id="alice") is None
    assert [item.conversation_id for item in store.list_conversations(tenant_id=TENANT, owner_id="alice")] == [
        alice.conversation_id]


# --- 验收 15：重复 request_id 与并发 -------------------------------------------

def test_duplicate_request_id_does_not_create_a_second_message(store):
    conversation = make_conversation(store)
    store.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                      user_text="腾讯 2025 年收入", request_id="req-dupe-0001", answer_text="第一次")
    with pytest.raises(DuplicateRequest):
        store.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                          user_text="腾讯 2025 年收入", request_id="req-dupe-0001", answer_text="第二次")
    messages = store.list_messages(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert [item.text for item in messages] == ["腾讯 2025 年收入", "第一次"]
    assert store.find_by_request_id("req-dupe-0001", tenant_id=TENANT, owner_id=OWNER).text == "腾讯 2025 年收入"


def test_duplicate_request_id_is_scoped_per_identity(store):
    alice = make_conversation(store, owner="alice")
    carol = make_conversation(store, owner="carol")
    store.append_turn(tenant_id=TENANT, owner_id="alice", conversation_id=alice.conversation_id,
                      user_text="问题", request_id="req-shared-01", answer_text="A")
    # 同 request_id 但不同身份：不算重复，能各自落库。
    store.append_turn(tenant_id=TENANT, owner_id="carol", conversation_id=carol.conversation_id,
                      user_text="问题", request_id="req-shared-01", answer_text="C")
    assert store.find_by_request_id("req-shared-01", tenant_id=TENANT, owner_id="carol").text == "问题"


def test_concurrent_turns_do_not_lose_messages_or_duplicate_seq(store):
    conversation = make_conversation(store)
    errors: list[Exception] = []

    def submit(index: int) -> None:
        try:
            store.append_turn(tenant_id=TENANT, owner_id=OWNER,
                              conversation_id=conversation.conversation_id,
                              user_text=f"问题{index}", request_id=f"req-race-{index:04d}",
                              answer_text=f"回答{index}")
        except Exception as exc:  # noqa: BLE001 - 测试要看到真实失败
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    messages = store.list_messages(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert len(messages) == 16
    # seq 必须连续且不重复，否则恢复历史会乱序。
    assert [item.seq for item in messages] == list(range(1, 17))


# --- 验收 8：删除会话的原子性 ---------------------------------------------------

def test_delete_conversation_removes_messages_state_summary_and_pending(store, profiles):
    conversation = make_conversation(store)
    store.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                      user_text="腾讯 2025 年收入", request_id="req-del-0001", answer_text="回答")
    store.save_state(conversation.conversation_id, {"ticker": "0700.HK"}, tenant_id=TENANT, owner_id=OWNER)
    store.save_summary(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER,
                       covered_through_seq=2, state_version=1,
                       summary={"company": "0700.HK", "summary_type": "structured_extractive"},
                       summary_type="structured_extractive")
    profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER,
                              conversation_id=conversation.conversation_id)
    assert store.delete_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert store.list_messages(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER) == []
    assert store.load_state(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER) is None
    assert store.load_summary(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER) is None
    assert profiles.pending_candidates(tenant_id=TENANT, owner_id=OWNER) == []


def test_delete_conversation_keeps_long_term_profile(store, profiles):
    conversation = make_conversation(store)
    for candidate in profiles.stage_candidates("我偏保守，主要看港股", tenant_id=TENANT, owner_id=OWNER,
                                              conversation_id=conversation.conversation_id):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    store.delete_conversation(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    # 长期画像不随便删除。
    assert sorted(profiles.profile_values(tenant_id=TENANT, owner_id=OWNER)) == ["focus_market", "risk_preference"]


# --- 验收 7/8/10：画像候选、确认、删除 ----------------------------------------

def test_self_reported_preference_creates_candidate_not_profile(store, profiles):
    candidates = profiles.stage_candidates("我偏保守，主要看港股", tenant_id=TENANT, owner_id=OWNER)
    assert sorted(item.field for item in candidates) == ["focus_market", "risk_preference"]
    # 未经确认绝不进长期画像。
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {}


def test_confirm_is_bound_to_candidate_and_cannot_be_replayed(store, profiles):
    candidate = profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER)[0]
    first = profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER,
                                       expected_version=candidate.version)
    assert first["status"] == "confirmed"
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {"risk_preference": "conservative"}
    # 重放同一候选不再改档案。
    replay = profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER,
                                        expected_version=candidate.version)
    assert replay["status"] == "already_resolved"


def test_candidate_confirmation_from_other_owner_is_not_found(store, profiles):
    candidate = profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER)[0]
    assert profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT,
                                      owner_id=OTHER_OWNER)["status"] == "not_found"
    assert profiles.confirm_candidate(candidate.candidate_id, tenant_id=OTHER_TENANT,
                                      owner_id=OWNER)["status"] == "not_found"
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {}


def test_stale_candidate_version_is_rejected(store, profiles):
    candidate = profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER)[0]
    assert profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER,
                                      expected_version=candidate.version + 5)["status"] == "version_mismatch"
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {}


def test_profile_update_delete_and_delete_all_take_effect(store, profiles):
    for candidate in profiles.stage_candidates("我偏保守，主要看港股", tenant_id=TENANT, owner_id=OWNER):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    updated = profiles.set_field("research_horizon", "3y", tenant_id=TENANT, owner_id=OWNER)
    assert updated.value == "3y" and updated.version == 1
    replaced = profiles.set_field("research_horizon", "5y", tenant_id=TENANT, owner_id=OWNER)
    assert replaced.value == "5y" and replaced.version == 2
    assert profiles.delete_field("research_horizon", tenant_id=TENANT, owner_id=OWNER)
    assert "research_horizon" not in profiles.profile_values(tenant_id=TENANT, owner_id=OWNER)
    # 已删除项不再出现在有效画像里。
    assert all(entry["field"] != "research_horizon"
               for entry in profiles.active_profile(tenant_id=TENANT, owner_id=OWNER).values())
    assert profiles.delete_all(tenant_id=TENANT, owner_id=OWNER) == 2
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {}


def test_profile_deletion_candidate_requires_confirmation(store, profiles):
    for candidate in profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    staged = profiles.stage_field_deletion("删除我的风险偏好", tenant_id=TENANT, owner_id=OWNER)
    assert len(staged) == 1 and staged[0].op == "delete"
    # 只是候选，档案未变。
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {"risk_preference": "conservative"}
    assert profiles.confirm_deletion(staged[0].candidate_id, tenant_id=TENANT,
                                    owner_id=OWNER)["status"] == "deleted"
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {}


def test_profile_view_command_needs_no_company_or_year():
    assert classify_profile_command("查看我的偏好").kind == "view"
    assert classify_profile_command("我的偏好").kind == "view"
    # 普通财务问题绝不能被当成画像命令。
    for question in ("腾讯 2025 年收入是多少？", "宁德时代货币资金", "分析 AAPL 现金流风险"):
        assert classify_profile_command(question).kind == "none"
        assert not is_preference_statement(question)


def test_profile_command_detection_covers_documented_phrases():
    assert classify_profile_command("记住我主要看港股").kind == "remember"
    assert classify_profile_command("把我的研究期限改成三年").kind == "update"
    assert classify_profile_command("删除我的风险偏好").kind == "delete_field"
    assert classify_profile_command("删除全部画像").kind == "delete_all"
    assert classify_profile_command("本次不使用长期偏好").kind == "disable_for_session"
    assert classify_profile_command("这次激进一点").kind == "session_tone"
    assert classify_profile_command("确认").kind == "confirm"
    assert classify_profile_command("取消").kind == "reject"
    # 陈述式偏好没有命令动词，但也要能被识别成候选来源。
    assert is_preference_statement("我偏保守，主要看港股")
    assert is_preference_statement("我主要看港股")


# --- 验收：敏感信息不得进入画像 -------------------------------------------------

@pytest.mark.parametrize("text", [
    "记住我的密码是 abc123",
    "请记住我的 API key 是 sk-abcdefghijklmnop",
    "我的身份证号 440101199001011234",
    "记住我的银行卡号 6222021234567890123",
])
def test_sensitive_content_never_enters_profile(text):
    assert contains_sensitive_content(text)
    assert extract_profile_candidates(text) == []


def test_sensitive_message_produces_no_candidate(store, profiles):
    assert profiles.stage_candidates("记住我的密码是 abc123", tenant_id=TENANT, owner_id=OWNER) == []
    assert profiles.pending_candidates(tenant_id=TENANT, owner_id=OWNER) == []


# --- 验收 8/11：画像实际注入与撤销后不复活 -----------------------------------

def test_profile_is_rendered_into_context_not_just_returned_as_id():
    profile = {"risk_preference": {"field": "risk_preference", "value": "conservative", "confirmed": True}}
    lines = render_profile_block(profile)
    assert any("conservative" in line for line in lines)
    assembled = assemble_context(profile=profile, context={"ticker": "0700.HK"}, messages=[],
                                 summary=None, current_question="收入")
    assert assembled["profile_applied"] is True
    assert any("conservative" in line for section in assembled["sections"] for line in section["lines"])


def test_disabling_profile_for_session_does_not_delete_archive(store, profiles):
    for candidate in profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    # 停用只影响本轮注入。
    assert render_profile_block(profiles.active_profile(tenant_id=TENANT, owner_id=OWNER), disabled=True) == []
    assert profiles.profile_values(tenant_id=TENANT, owner_id=OWNER) == {"risk_preference": "conservative"}


def test_session_preference_overrides_long_term_but_hard_constraint_survives():
    profile = {"risk_preference": {"field": "risk_preference", "value": "conservative", "confirmed": True},
               "leverage_constraint": {"field": "leverage_constraint", "value": "none", "confirmed": True}}
    lines = render_profile_block(profile, session_preferences={"risk_preference": "aggressive"})
    assert any("aggressive" in line for line in lines)
    assert not any("conservative" in line for line in lines)
    # 硬约束不受会话临时偏好覆盖。
    assert any("硬约束" in line for line in lines)


def test_hard_constraint_conflict_requires_clarification_instead_of_silent_override():
    profile = {"leverage_constraint": {"field": "leverage_constraint", "value": "none", "confirmed": True}}
    assert hard_constraint_conflict(profile, "这次可以加杠杆")
    assert hard_constraint_conflict(profile, "这次用3倍杠杆做腾讯")
    # 不涉及杠杆的请求不触发冲突。
    assert hard_constraint_conflict(profile, "腾讯 2025 年收入是多少？") is None
    limited = {"leverage_constraint": {"field": "leverage_constraint", "value": "最多2倍杠杆", "confirmed": True}}
    assert hard_constraint_conflict(limited, "这次用3倍杠杆")
    assert hard_constraint_conflict(limited, "这次用1倍杠杆") is None


def test_revoked_field_is_not_reintroduced_by_old_summary(store, profiles):
    for candidate in profiles.stage_candidates("我偏保守", tenant_id=TENANT, owner_id=OWNER):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    profiles.delete_field("risk_preference", tenant_id=TENANT, owner_id=OWNER)
    revoked = store.revoked_profile_fields(tenant_id=TENANT, owner_id=OWNER)
    assert revoked == {"risk_preference"}
    # 旧摘要里若仍带该偏好，重建时必须剔除。
    context = {"session_preferences": {"risk_preference": "conservative"}, "ticker": "0700.HK"}
    summary = build_summary([{"role": "user", "text": "腾讯"}], context, revoked_fields=revoked)
    assert "risk_preference" not in summary["session_preferences"]


def test_deleted_profile_is_not_injected_into_next_turn(store, profiles):
    for candidate in profiles.stage_candidates("我偏保守，主要看港股", tenant_id=TENANT, owner_id=OWNER):
        profiles.confirm_candidate(candidate.candidate_id, tenant_id=TENANT, owner_id=OWNER)
    profiles.delete_field("risk_preference", tenant_id=TENANT, owner_id=OWNER)
    assembled = assemble_context(profile=profiles.active_profile(tenant_id=TENANT, owner_id=OWNER),
                                 context={"ticker": "0700.HK"}, messages=[], summary=None,
                                 current_question="收入")
    rendered = "\n".join(line for section in assembled["sections"] for line in section["lines"])
    assert "conservative" not in rendered
    # 另一个字段仍然生效，说明不是把整份画像清空。
    assert "HK" in rendered


# --- 验收 3/5：短期状态合并与跨公司隔离 ---------------------------------------

def test_merge_turn_state_records_explicit_year_metric_and_negation():
    context = merge_turn_state({}, "腾讯 2025 年营业收入是多少？不要杠杆")
    assert context["year"] == "2025" and context["metric"] == "revenue"
    assert "不要杠杆" in context["negations"]


def test_switch_company_clears_inapplicable_state_but_keeps_messages_and_preferences():
    context = {"ticker": "0700.HK", "year": "2025", "evidence_scope": "official",
               "knowledge_ticker": "0700.HK", "knowledge_year": "2025",
               "messages": [{"role": "user", "text": "腾讯收入"}],
               "session_preferences": {"risk_preference": "aggressive"}}
    switched = switch_company_state(context, "9988.HK")
    assert switched["ticker"] == "9988.HK"
    # 换公司必须清掉不适用的公司/年度/证据状态。
    assert switched["year"] is None and switched["knowledge_ticker"] is None
    assert switched["evidence_scope"] is None
    # 但保留本会话原始消息与适用偏好。
    assert len(switched["messages"]) == 1
    assert switched["session_preferences"] == {"risk_preference": "aggressive"}


def test_switch_company_to_same_ticker_is_a_no_op():
    context = {"ticker": "0700.HK", "year": "2025"}
    assert switch_company_state(context, "0700.HK") is context


def test_mode_switch_clears_evidence_but_keeps_state():
    context = {"ticker": "0700.HK", "year": "2025", "evidence_scope": "mcp_candidate",
               "messages": [{"role": "user", "text": "腾讯收入"}]}
    switched = clear_mode_specific_state(context, "official")
    assert switched["evidence_scope"] is None and switched["answer_mode"] == "official"
    # 消息不删。
    assert len(switched["messages"]) == 1
    assert switched["year"] == "2025"


def test_two_conversations_never_share_state(store):
    orchestrator = ConversationOrchestrator(store)
    first = make_conversation(store)
    second = make_conversation(store)
    orchestrator.persist({**orchestrator.load_context(first.conversation_id, tenant_id=TENANT, owner_id=OWNER),
                          "ticker": "0700.HK", "year": "2025", "market": "HK"},
                         tenant_id=TENANT, owner_id=OWNER)
    left = orchestrator.load_context(first.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    right = orchestrator.load_context(second.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert left["ticker"] == "0700.HK" and left["market"] == "HK"
    # 另一会话必须是干净的空状态，不能继承公司/年度/市场。
    assert right["ticker"] is None and right["year"] is None and right["market"] is None


# --- 验收 13/14：预算裁剪与摘要降级 -------------------------------------------

def test_small_budget_trims_history_but_keeps_question_and_constraints():
    profile = {"risk_preference": {"field": "risk_preference", "value": "conservative", "confirmed": True}}
    context = {"ticker": "0700.HK", "negations": ["不要杠杆"], "open_questions": ["需要补充年份"]}
    messages = [{"role": "user", "text": "很长的历史内容" * 200} for _ in range(5)]
    assembled = assemble_context(profile=profile, context=context, messages=messages, summary=None,
                                 current_question="那净利润呢", budget_tokens=400)
    assert assembled["trimmed"] is True and assembled["dropped_messages"] > 0
    assert assembled["measurement"] == "estimated"
    # 本轮问题、否定条件、待澄清项都不能丢。
    rendered = "\n".join(line for section in assembled["sections"] for line in section["lines"])
    assert "那净利润呢" in rendered
    assert "不要杠杆" in rendered and "需要补充年份" in rendered


def test_budget_is_labeled_as_estimate_not_model_quota():
    assembled = assemble_context(profile={}, context={}, messages=[], summary=None, current_question="收入")
    assert assembled["measurement"] == "estimated"
    assert "估算" in assembled["note"]
    # 32,000 只是产品提醒阈值，不是模型真实额度。
    assert assembled["reminder_threshold_tokens"] == 32_000
    assert estimate_tokens("中文文本") > 0


def test_summary_failure_keeps_previous_valid_summary(store):
    orchestrator = ConversationOrchestrator(store)
    conversation = make_conversation(store)
    store.save_summary(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER,
                       covered_through_seq=4, state_version=1,
                       summary={"company": "0700.HK", "summary_type": "structured_extractive"},
                       summary_type="structured_extractive")
    # 空消息 + 空状态无法提取任何约束，必须降级而不是覆盖。
    result = orchestrator.refresh_summary({"conversation_id": conversation.conversation_id, "messages": []},
                                         tenant_id=TENANT, owner_id=OWNER, state_version=1, force=True)
    assert result["status"] == "degraded" and result["reason"]
    preserved = store.load_summary(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER)
    assert preserved["summary"]["company"] == "0700.HK"
    assert preserved["summary_type"] == "structured_extractive"


def test_summary_records_coverage_and_never_invents_financial_facts(store):
    orchestrator = ConversationOrchestrator(store)
    conversation = make_conversation(store)
    store.append_turn(tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation.conversation_id,
                      user_text="腾讯 2025 年收入", request_id="req-sum-0001", answer_text="已核验")
    context = {**orchestrator.load_context(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER),
               "ticker": "0700.HK", "year": "2025", "metric": "revenue",
               "negations": ["不要杠杆"], "open_questions": ["需要补充口径"]}
    result = orchestrator.refresh_summary(context, tenant_id=TENANT, owner_id=OWNER,
                                         state_version=1, force=True)
    assert result["status"] == "ok"
    stored = result["summary"]
    assert stored["summary_type"] == "structured_extractive"
    assert stored["covered_through_seq"] >= 2 and stored["state_version"] == 1
    assert stored["summary"]["company"] == "0700.HK" and stored["summary"]["year"] == "2025"
    # 摘要是派生内容，不是事实证据：不得包含财务数字结论。
    assert "751" not in str(stored["summary"])


def test_summary_type_must_be_extractive(store):
    conversation = make_conversation(store)
    with pytest.raises(ConversationStoreError):
        store.save_summary(conversation.conversation_id, tenant_id=TENANT, owner_id=OWNER,
                           covered_through_seq=1, state_version=1, summary={}, summary_type="generated")


# --- 验收 4/5：有效查询构造 -----------------------------------------------------

def test_effective_query_inherits_year_for_follow_up():
    context = {"ticker": "0700.HK", "year": "2025"}
    assert effective_query_for(context, "那净利润呢") == "2025年，0700.HK，那净利润呢"
    # 本轮已写明年度时不重复注入。
    assert effective_query_for(context, "2024年收入呢") == "0700.HK，2024年收入呢"
    assert effective_query_for({"ticker": "0700.HK"}, "那净利润呢") == "0700.HK，那净利润呢"


def test_named_other_subject_detection_switches_evidence_scope():
    """换主体检测：提到别家公司要清证据，普通追问不能误触发。"""
    from investment_assistant.company_qa import catalog
    from investment_assistant.conversation_memory import _names_other_subject

    records = catalog()
    should_switch = ["换成阿里巴巴", "换成阿里巴巴的收入", "换成平安银行", "换成茅台",
                     "换成宁德时代", "看一下小米", "改成MSFT"]
    for text in should_switch:
        assert _names_other_subject(text, "0700.HK", records) is True, text
    should_not_switch = ["那净利润呢", "收入呢", "看一下腾讯的利润", "那个成本呢", "换成港股"]
    for text in should_not_switch:
        assert _names_other_subject(text, "0700.HK", records) is False, text


def test_estimate_tokens_is_conservative_and_zero_for_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") >= 1
    assert estimate_tokens("中文" * 100) > estimate_tokens("中文" * 10)