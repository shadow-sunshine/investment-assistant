from investment_assistant.chat_session import context_usage, new_context


def test_context_usage_is_only_a_history_estimate_and_counts_evidence():
    context = new_context()
    context["messages"] = [
        {"role": "user", "text": "腾讯年报收入", "answer": None},
        {"role": "assistant", "text": "资料见引用。", "answer": {"sources": [{"citation": "S1"}, {"citation": "S2"}]}},
    ]
    result = context_usage(context, answer_mode="official", budget_tokens=100)
    assert result["measurement"] == "estimated"
    assert result["message_count"] == 2
    assert result["evidence_count"] == 2
    assert result["estimated_tokens"] > 0
    assert result["reminder_threshold_tokens"] == 100
    assert result["answer_mode"] == "official"


def test_context_usage_warns_at_threshold_but_does_not_trim_messages():
    context = new_context()
    context["messages"] = [{"role": "user", "text": "问" * 190, "answer": None}]
    result = context_usage(context, budget_tokens=100)
    assert result["status"] == "critical"
    assert len(context["messages"]) == 1
