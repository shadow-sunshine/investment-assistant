"""研究聊天的离线意图分流、会话切换与 API 编排测试。"""

from investment_assistant.chat_session import (
    dispatch_message, new_context, record_error, route_message, select_job, select_report, select_knowledge_corpus, update_job,
)


def test_research_instruction_requires_ticker_and_topic():
    state = new_context()
    kind, payload = route_message("分析 AAPL 服务业务和现金流风险", state)
    assert kind == "research"
    assert payload == {"ticker": "AAPL", "topic": "服务业务和现金流风险"}
    assert route_message("分析服务业务", state)[0] == "guidance"
    assert route_message("生成报告 AAPL", state)[0] == "guidance"
    assert route_message("分析 600519.SS 现金流", state) == ("research", {"ticker": "600519.SS", "topic": "现金流"})


def test_follow_up_is_not_research_and_no_report_needs_selection():
    state = new_context()
    assert route_message("最新收盘价是多少？", state)[0] == "guidance"
    state = select_report(state, {"id": "AAPL_1", "ticker": "AAPL"})
    assert route_message("这条风险来自哪里？", state) == ("answer", "这条风险来自哪里？")
    # 明确研究任务但信息不足时不悄悄复用报告标的。
    assert route_message("分析现金流", state)[0] == "guidance"


def test_dispatch_only_explicit_research_uses_job_api():
    calls = []

    def create(payload):
        calls.append(("job", payload))
        return {"job_id": "job_1", "status": "queued"}

    def ask(payload):
        calls.append(("answer", payload))
        return {"status": "insufficient_evidence", "answer": "证据不足", "limitations": ["缺少年报"]}

    state = dispatch_message(new_context(), "这份报告的风险？", create, ask, "demo")
    assert calls == []
    state = dispatch_message(state, "分析 AAPL 现金流风险", create, ask, "demo")
    assert calls == [("job", {"ticker": "AAPL", "topic": "现金流风险", "horizon": "中期", "requested_by": "demo"})]
    assert state["job_id"] == "job_1" and state["report_id"] is None
    assert len(state["messages"]) == 2
    blocked = dispatch_message(state, "分析 AAPL 服务业务", create, ask, "demo")
    assert len(calls) == 1 and "仍在执行" in blocked["messages"][-1]["text"]
    state = update_job(state, {"job_id": "job_1", "status": "completed", "report_id": "AAPL_1", "ticker": "AAPL"})
    state = dispatch_message(state, "现金流来自哪一页？", create, ask, "demo")
    assert calls[-1] == ("answer", {"report_id": "AAPL_1", "ticker": "AAPL", "question": "现金流来自哪一页？", "requested_by": "demo"})
    assert state["messages"][-1]["answer"]["status"] == "insufficient_evidence"


def test_context_switch_clears_old_messages_and_bound_report():
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    state = record_error(state, "问题", "证据不足")
    assert select_report(state, {"id": "AAPL_1", "ticker": "AAPL"}) == state
    other = select_report(state, {"id": "MSFT_2", "ticker": "MSFT"})
    assert other["messages"] == [] and other["ticker"] == "MSFT" and other["report_id"] == "MSFT_2"
    task = select_job(other, {"job_id": "job_2", "ticker": "AAPL", "status": "running"})
    assert task["report_id"] is None and task["messages"] == []
    assert update_job(task, {"job_id": "other", "status": "completed", "report_id": "MSFT_2"}) == task


def test_terminal_transition_only_once_and_failure_does_not_bind_report():
    state = select_job(new_context(), {"job_id": "job_1", "ticker": "AAPL", "status": "running"})
    failed = {"job_id": "job_1", "status": "failed", "ticker": "AAPL", "error": "上游不可用"}
    state = update_job(state, failed)
    assert state["report_id"] is None and "上游不可用" in state["messages"][-1]["text"]
    assert update_job(state, failed) == state


def test_switching_to_new_research_discards_old_answer():
    state = select_report(new_context(), {"id": "MSFT_1", "ticker": "MSFT"})
    state = record_error(state, "净利润是多少？", "旧回答")
    started = dispatch_message(state, "分析 AAPL 现金流", lambda _: {"job_id": "job_new"}, lambda _: {}, "demo")
    assert started["report_id"] is None and started["ticker"] == "AAPL"
    assert all("旧回答" not in item["text"] for item in started["messages"])



def test_chinese_corpus_chat_is_scoped_and_clears_previous_context():
    calls = []
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    state = record_error(state, "先前问题", "旧回答")
    state = select_knowledge_corpus(state, "300750.SZ")
    assert state["report_id"] is None and state["messages"] == []
    assert route_message("宁德时代货币资金是多少？", state) == ("knowledge", "宁德时代货币资金是多少？")
    assert route_message("x" * 501, state)[0] == "guidance"

    def knowledge_ask(payload):
        calls.append(payload)
        return {"status": "refused", "answer": "证据不足", "sources": []}

    updated = dispatch_message(state, "宁德时代货币资金是多少？", lambda _: None, lambda _: None,
                               "caller", knowledge_ask=knowledge_ask)
    assert calls == [{"ticker": "300750.SZ", "question": "宁德时代货币资金是多少？", "requested_by": "caller"}]
    assert updated["messages"][-1]["answer"]["status"] == "refused"
    assert select_knowledge_corpus(updated, "000001.SZ")["messages"] == []
