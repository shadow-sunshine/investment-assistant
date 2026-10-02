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


def test_alias_file_missing_or_corrupt_is_structured_and_audited(tmp_path):
    import json

    import pytest

    from investment_assistant.news_filter import NewsFilterError, load_ticker_aliases
    from investment_assistant.source_governance import get_default_health_registry, get_default_tool_call_ledger

    get_default_health_registry().reset()
    get_default_tool_call_ledger().reset()
    missing = tmp_path / "missing-aliases.json"
    with pytest.raises(NewsFilterError) as missing_error:
        load_ticker_aliases(missing)
    assert missing_error.value.error.error_code.value == "dependency_error"

    corrupt = tmp_path / "broken-aliases.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    with pytest.raises(NewsFilterError) as corrupt_error:
        load_ticker_aliases(corrupt)
    assert corrupt_error.value.error.error_code.value == "parse_error"

    malformed = tmp_path / "malformed-aliases.json"
    malformed.write_text('{"AAPL": "Apple"}', encoding="utf-8")
    with pytest.raises(NewsFilterError) as schema_error:
        load_ticker_aliases(malformed)
    assert schema_error.value.error.error_code.value == "parse_error"

    records = get_default_tool_call_ledger().records()
    assert [record.operation for record in records] == ["load_ticker_aliases"] * 3
    assert all(record.result_status == "failed" for record in records)
    assert all(record.source == "local_news_filter" for record in records)
    assert get_default_health_registry().snapshot("Yahoo Finance via yfinance")["status"] == "unknown"


def test_local_no_match_is_not_registered_as_network_failure():
    from investment_assistant.source_governance import get_default_health_registry, get_default_tool_call_ledger

    get_default_health_registry().reset()
    get_default_tool_call_ledger().reset()
    passed, audited = filter_news_records([{"title": "Unrelated market story", "summary": "No issuer mention"}], "AAPL")
    assert passed == [] and audited[0]["filter"]["passed"] is False
    assert get_default_tool_call_ledger().records() == ()
    assert get_default_health_registry().snapshot("Yahoo Finance via yfinance")["status"] == "unknown"


def test_news_fetch_failure_metadata_survives_list_compatible_local_filter():
    from investment_assistant.market_data import NewsRecords
    from investment_assistant.source_governance import SourceErrorCode, tool_error

    failure = tool_error("Yahoo Finance via yfinance", "fetch_recent_news", SourceErrorCode.TIMEOUT)
    fetched = NewsRecords(error=failure)
    passed, audited = filter_news_records(fetched, "AAPL")

    assert passed == [] and audited == []
    assert isinstance(audited, list)
    assert audited.fetch_status == "failed"
    assert audited.fetch_error.error_code == SourceErrorCode.TIMEOUT
