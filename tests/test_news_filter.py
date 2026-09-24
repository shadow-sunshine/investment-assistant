from investment_assistant.news_filter import REJECTION_REASON, filter_news_records, subject_aliases


def test_moutai_title_passes_deterministic_subject_filter():
    passed, audited = filter_news_records([{"title": "\u8d35\u5dde\u8305\u53f0\u53d1\u5e03\u5e74\u62a5", "summary": ""}], "600519.SS")
    assert len(passed) == 1
    assert audited[0]["filter"]["passed"] is True
    assert "\u8d35\u5dde\u8305\u53f0" in audited[0]["filter"]["matched_aliases"]


def test_munger_noise_title_is_rejected_with_explicit_reason():
    passed, audited = filter_news_records([{"title": "Charlie Munger Handed His Family Fortune Over to The Chinese Warren Buffett", "summary": ""}], "600519.SS")
    assert passed == []
    assert audited[0]["filter"] == {"passed": False, "reason": REJECTION_REASON, "matched_aliases": []}


def test_alias_dictionary_covers_aapl_tencent_and_moutai():
    assert "Apple" in subject_aliases("AAPL")
    assert "\u817e\u8baf" in subject_aliases("0700.HK")
    assert "Moutai" in subject_aliases("600519.SS")


def test_summary_alias_match_is_case_insensitive():
    passed, audited = filter_news_records([{"title": "Results update", "summary": "tEnCeNt reported quarterly results"}], "0700.HK")
    assert len(passed) == 1
    assert audited[0]["filter"]["matched_aliases"] == ["Tencent"]


def test_collect_real_data_audits_all_news_and_indexes_only_passed_items(monkeypatch):
    from investment_assistant import workflow

    class FakeRAG:
        indexed_news = None
        cleared_ticker = None
        def index_local_documents(self): return 0
        def clear_news(self, ticker):
            FakeRAG.cleared_ticker = ticker
            return 0
        def index_news(self, records):
            FakeRAG.indexed_news = records
            return len(records)

    monkeypatch.setattr(workflow, "fetch_market_snapshot", lambda ticker: {"ticker": ticker})
    monkeypatch.setattr(workflow, "fetch_financial_snapshot", lambda ticker: {"ticker": ticker})
    monkeypatch.setattr(workflow, "fetch_recent_news", lambda ticker: [
        {"id": "news-1", "ticker": ticker, "title": "\u8305\u53f0\u5e74\u62a5", "summary": ""},
        {"id": "news-2", "ticker": ticker, "title": "Charlie Munger update", "summary": ""},
    ])
    monkeypatch.setattr(workflow, "LocalResearchRAG", FakeRAG)
    result = workflow.collect_real_data({"ticker": "600519.SS"})
    assert FakeRAG.cleared_ticker == "600519.SS"
    assert [item["id"] for item in FakeRAG.indexed_news] == ["news-1"]
    assert len(result["raw_news"]) == 2
    assert result["raw_news"][0]["filter"]["passed"] is True
    assert result["raw_news"][1]["filter"]["reason"] == REJECTION_REASON
    assert result["filtered_news_available"] is True
