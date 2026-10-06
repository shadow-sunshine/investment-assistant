"""研究聊天的离线意图分流、会话切换与 API 编排测试。"""

from investment_assistant.chat_session import (
    dispatch_message, new_context, record_error, route_message, select_job, select_report, select_knowledge_corpus, update_job,
)




def test_annual_report_request_is_separate_from_fact_question():
    latest = route_message("给我一份2025年的腾讯财报", new_context())
    assert latest[0] == "annual_report"
    assert latest[1]["ticker"] == "0700.HK"
    assert latest[1]["year"] == "2025"
    assert latest[1]["topic"] == "2025年年报摘要"
    assert route_message("腾讯2025年财报收入怎么样", new_context())[0] == "company"
    current = route_message("给我一份今年的腾讯财报", new_context())
    assert current[0] == "guidance"
    assert "2026" in current[1] and "2025" in current[1]


def test_annual_report_request_uses_job_path_and_exposes_intent_metadata():
    calls = []
    def create(payload):
        calls.append(payload)
        return {"job_id": "job_annual", "status": "queued"}
    state = dispatch_message(new_context(), "给我一份2025年的腾讯财报", create, lambda _: None, "demo")
    assert calls == [{"ticker": "0700.HK", "topic": "2025年年报摘要", "horizon": "中期", "requested_by": "demo"}]
    answer = state["messages"][-1]["answer"]
    assert state["job_id"] == "job_annual"
    assert answer["intent"] == "annual_report" and answer["intent_label"] == "年报摘要"
    assert answer["compliance"] == "通过" and isinstance(answer["latency_ms"], int)
    assert "官方原始 PDF" in state["messages"][-1]["text"]


def test_unknown_and_multi_company_annual_requests_fail_with_specific_guidance():
    unknown = route_message("给我一份2025年的阿里巴巴财报", new_context())
    assert unknown[0] == "guidance" and "暂未收录" in unknown[1]
    multi = route_message("给我一份2025年的腾讯和阿里巴巴财报", new_context())
    assert multi[0] == "guidance" and "一家公司" in multi[1]
    scoped = select_knowledge_corpus(new_context(), "0700.HK")
    same_company = route_message("给我一份2025年的财报", scoped)
    assert same_company[0] == "annual_report" and same_company[1]["ticker"] == "0700.HK"

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
    assert route_message("宁德时代货币资金是多少？", state) == ("knowledge", {"ticker": "300750.SZ", "question": "宁德时代货币资金是多少？"})
    assert route_message("x" * 501, state)[0] == "guidance"

    def knowledge_ask(payload):
        calls.append(payload)
        return {"status": "refused", "answer": "证据不足", "sources": []}

    updated = dispatch_message(state, "宁德时代货币资金是多少？", lambda _: None, lambda _: None,
                               "caller", knowledge_ask=knowledge_ask)
    assert calls == [{"ticker": "300750.SZ", "question": "宁德时代货币资金是多少？", "requested_by": "caller"}]
    assert updated["messages"][-1]["answer"]["status"] == "refused"
    assert select_knowledge_corpus(updated, "000001.SZ")["messages"] == []


def test_auto_route_official_annual_report_without_manual_corpus():
    calls = []

    def knowledge_ask(payload):
        calls.append(payload)
        return {"status": "refused", "answer": "当前证据不足", "sources": []}

    state = new_context()
    first = "宁德时代 2025 年营业收入是多少？"
    assert route_message(first, state) == ("knowledge", {"ticker": "300750.SZ", "question": first})
    state = dispatch_message(state, first, lambda _: None, lambda _: None, "caller", knowledge_ask=knowledge_ask)
    assert state["knowledge_ticker"] == "300750.SZ" and state["messages"][0]["text"] == first
    assert calls == [{"ticker": "300750.SZ", "question": first, "requested_by": "caller"}]
    state = dispatch_message(state, "那净利润呢？", lambda _: None, lambda _: None, "caller", knowledge_ask=knowledge_ask)
    assert calls[-1]["ticker"] == "300750.SZ" and len(state["messages"]) == 4

    next_question = "平安银行 2025 年净利润是多少？"
    state = dispatch_message(state, next_question, lambda _: None, lambda _: None, "caller", knowledge_ask=knowledge_ask)
    assert calls[-1]["ticker"] == "000001.SZ"
    assert state["knowledge_ticker"] == "000001.SZ" and len(state["messages"]) == 2
    assert state["messages"][0]["text"] == next_question


def test_auto_route_fails_closed_for_multiple_or_unsupported_tickers():
    state = select_knowledge_corpus(new_context(), "300750.SZ")
    for question in (
        "宁德时代和贵州茅台 2025 年营业收入是多少？",
        "300750.SZ 与 000001.SZ 2025 年净利润对比",
        "宁德时代和 123456.SZ 2025 年营业收入是多少？",
        "123456.SZ 2025 年营业收入是多少？",
    ):
        assert route_message(question, state)[0] == "guidance"
    assert route_message("000858.sz 2025 年营业收入是多少？", new_context()) == (
        "knowledge", {"ticker": "000858.SZ", "question": "000858.sz 2025 年营业收入是多少？"}
    )
    assert route_message("2025 年收入是多少？", new_context())[0] == "guidance"
    assert route_message("MSFT 2025 年营业收入是多少？", state)[0] == "company"
    assert route_message("宁德时代和 AAPL 2025 年收入对比", state)[0] == "guidance"
    hk_report = select_report(new_context(), {"id": "0700_1", "ticker": "0700.HK"})
    assert route_message("0700.HK 的现金流风险？", hk_report)[0] == "company"


def test_auto_route_clears_selected_report_when_question_names_another_company():
    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    calls = []
    result = dispatch_message(
        state, "五粮液 2025 年营业收入是多少？", lambda _: None, lambda _: calls.append("report"),
        "caller", knowledge_ask=lambda payload: {"status": "refused", "answer": "证据不足", "sources": []},
    )
    assert calls == [] and result["report_id"] is None
    assert result["knowledge_ticker"] == "000858.SZ" and result["ticker"] == "000858.SZ"


def test_product_intents_answer_without_network_and_preserve_scope():
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("产品介绍不能调用研究或问答接口")

    for question in ("你是谁", "你是谁？", "你 是 谁？", "你能做什么？", "如何使用", "你好", "Hello!"):
        state = dispatch_message(new_context(), question, unexpected, unexpected, "caller", knowledge_ask=unexpected)
        assert state["messages"][0]["text"] == question
        assert "投研助手" in state["messages"][1]["text"] or "2025 年" in state["messages"][1]["text"]
        assert state["messages"][1]["answer"] is None
        assert state["report_id"] is None and state["knowledge_ticker"] is None

    report = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    state = dispatch_message(report, "你是谁？", unexpected, unexpected, "caller", knowledge_ask=unexpected)
    assert state["report_id"] == "AAPL_1" and len(state["messages"]) == 2
    running = select_job(new_context(), {"job_id": "job_1", "ticker": "AAPL", "status": "running"})
    state = dispatch_message(running, "你是谁？", unexpected, unexpected, "caller", knowledge_ask=unexpected)
    assert state["job_id"] == "job_1" and "投研助手" in state["messages"][-1]["text"]
    assert calls == []


def test_natural_self_introduction_is_not_misrouted_to_company_query():
    state = dispatch_message(new_context(), "我是小明", lambda *_: (_ for _ in ()).throw(AssertionError()),
                             lambda *_: (_ for _ in ()).throw(AssertionError()), "caller")
    assert state["messages"][1]["text"].startswith("你好，小明！")
    assert state["messages"][1]["answer"] is None


def test_market_first_conversation_keeps_context_for_company_name():
    calls = []

    def discover(query, market=None):
        calls.append((query, market))
        return {"status": "matched", "selected_ticker": "0700.HK",
                "message": "已识别腾讯港股候选",
                "candidates": [{"ticker": "0700.HK", "market": "HK", "verified": False}]}

    state = dispatch_message(new_context(), "我想看看港股", lambda *_: {}, lambda *_: {}, "caller",
                             company_discover=discover)
    assert state["pending_market"] == "HK"
    assert "港股" in state["messages"][1]["text"]
    state = dispatch_message(state, "腾讯", lambda *_: {}, lambda *_: {}, "caller",
                             company_discover=discover)
    assert calls == [("腾讯", "HK")]
    assert state["discovery_ticker"] == "0700.HK"


def test_product_intents_do_not_intercept_business_questions():
    state = new_context()
    for question in (
        "你是谁的营业收入数据来源？", "宁德时代 2025 年营业收入是多少，你是谁？",
        "分析 AAPL 的你是谁功能", "你能做什么来预测 AAPL 股价？",
    ):
        assert route_message(question, state)[0] != "product"
    assert route_message("宁德时代 2025 年营业收入是多少？", state)[0] == "knowledge"
    assert route_message("你是谁" * 101, state)[0] == "guidance"


def test_unknown_company_can_use_controlled_discovery_without_answering():
    calls = []

    def discover(query, market=None):
        calls.append((query, market))
        return {
            "status": "ambiguous",
            "selected_ticker": None,
            "message": "请确认市场",
            "candidates": [{"ticker": "9988.HK", "verified": False}],
        }

    def unexpected(_payload):
        raise AssertionError("候选发现不能直接调用财务问答")

    state = dispatch_message(
        new_context(), "阿里巴巴 2025 年收入怎么样", lambda _: unexpected(None), unexpected, "caller",
        company_ask=unexpected, company_discover=discover,
    )
    assert calls == [("阿里巴巴 2025 年收入怎么样", None)]
    assert state["report_id"] is None and state["job_id"] is None
    assert state["messages"][-1]["answer"]["status"] == "ambiguous"
    assert state["messages"][-1]["answer"]["candidates"][0]["verified"] is False
    assert "选择市场" in state["messages"][-1]["text"]


def test_unknown_ticker_can_use_controlled_discovery():
    kind, detail = route_message(
        "9988.HK 2025 年收入", new_context(), lambda _query: {}
    )
    assert kind == "discovery"
    assert detail == {"query": "9988.HK 2025 年收入"}


def test_discovery_market_selection_reuses_original_question_and_binds_ticker():
    calls = []

    def discover(query, market=None):
        calls.append((query, market))
        if market == "HK":
            return {
                "status": "matched", "selected_ticker": "9988.HK",
                "message": "已识别候选公司，但官方年报证据尚未接入。",
                "candidates": [{"ticker": "9988.HK", "market": "HK", "verified": False}],
            }
        return {
            "status": "ambiguous", "selected_ticker": None, "message": "请选择市场",
            "candidates": [{"ticker": "9988.HK", "market": "HK", "verified": False}, {"ticker": "BABA", "market": "US", "verified": False}],
        }

    state = dispatch_message(
        new_context(), "阿里巴巴 2025 年收入怎么样", lambda _: {}, lambda _: {}, "caller",
        company_ask=lambda payload: {"status": "refused", "answer": "未接入官方资料", "error_code": "MATERIAL_NOT_ONBOARDED"},
        company_discover=discover,
    )
    state = dispatch_message(
        state, "港股", lambda _: {}, lambda _: {}, "caller",
        company_ask=lambda payload: {"status": "refused", "answer": "未接入官方资料", "error_code": "MATERIAL_NOT_ONBOARDED"},
        company_discover=discover,
    )
    assert calls == [("阿里巴巴 2025 年收入怎么样", None), ("阿里巴巴 2025 年收入怎么样", "HK")]
    assert state["discovery_ticker"] == "9988.HK"
    assert state["messages"][-1]["answer"]["status"] == "matched"

    state = dispatch_message(
        state, "收入怎么样", lambda _: {}, lambda _: {}, "caller",
        company_ask=lambda payload: {"status": "refused", "answer": "未接入官方资料", "error_code": "MATERIAL_NOT_ONBOARDED", **payload},
        company_discover=discover,
    )
    assert state["messages"][-1]["answer"]["ticker"] == "9988.HK"
    assert state["messages"][-1]["answer"]["question"].startswith("2025年")


def test_generic_unonboarded_company_data_request_enters_discovery():
    kind, detail = route_message("给我一份阿里巴巴的数据", new_context(), lambda *_: {})
    assert kind == "discovery"
    assert detail["query"] == "给我一份阿里巴巴的数据"


def test_mcp_mode_answers_candidate_without_calling_official_service():
    calls = []

    def forbidden(_payload):
        raise AssertionError("第三方模式不得调用官方问答或报告服务")

    def discover(query, market=None):
        calls.append((query, market))
        if market is None:
            return {"status": "ambiguous", "selected_ticker": None,
                    "candidates": [{"ticker": "9988.HK", "market": "HK", "verified": False},
                                   {"ticker": "BABA", "market": "US", "verified": False}]}
        return {"status": "matched", "selected_ticker": "9988.HK",
                "candidates": [{"ticker": "9988.HK", "market": "HK", "verified": False}]}

    def candidate(payload):
        assert payload == {"ticker": "9988.HK", "year": "2025"}
        return {"data_available": True, "verified": False,
                "candidates": [{"source": "akshare_mcp", "row": {"STD_ITEM_NAME": "营业额",
                                "AMOUNT": 996347000000, "REPORT_DATE": "2025-03-31"}}]}

    state = dispatch_message(new_context(), "阿里巴巴2025年收入", forbidden, forbidden, "test",
                             candidate_ask=candidate, company_ask=forbidden,
                             company_discover=discover, answer_mode="mcp")
    assert state["messages"][-1]["answer"]["status"] == "ambiguous"
    state = dispatch_message(state, "港股", forbidden, forbidden, "test", candidate_ask=candidate,
                             company_ask=forbidden, company_discover=discover, answer_mode="mcp")
    answer = state["messages"][-1]["answer"]
    assert calls == [("阿里巴巴2025年收入", None), ("阿里巴巴2025年收入", "HK")]
    assert answer["status"] == "candidate" and answer["verified"] is False
    assert "996,347,000,000" in answer["answer"] and "未确认币种" in answer["answer"]
    state = dispatch_message(state, "收入怎么样", forbidden, forbidden, "test", candidate_ask=candidate,
                             company_ask=forbidden, company_discover=discover, answer_mode="mcp")
    assert state["messages"][-1]["answer"]["status"] == "candidate"


def test_official_mode_keeps_unonboarded_company_refusal():
    state = {**new_context(), "ticker": "9988.HK", "discovery_ticker": "9988.HK",
             "discovery_query": "阿里巴巴2025年收入"}
    result = dispatch_message(state, "收入怎么样", lambda _: {}, lambda _: {}, "test",
                              company_ask=lambda _: {"status": "refused", "error_code": "MATERIAL_NOT_ONBOARDED",
                                                     "answer": "官方资料未接入"}, answer_mode="official")
    assert result["messages"][-1]["answer"]["status"] == "refused"
    assert "996,347" not in result["messages"][-1]["text"]
