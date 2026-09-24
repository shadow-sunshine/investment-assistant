import json
from pathlib import Path

import pytest

from investment_assistant.fetch_materials import (
    CNINFO_ANNOUNCEMENT_URL,
    CNINFO_SOURCE,
    CNINFO_TOP_SEARCH_URL,
    HKEX_PREFIX_URL,
    HKEX_SOURCE,
    HKEX_TITLE_SEARCH_URL,
    MaterialFetchError,
    OfficialMaterialFetcher,
    route_source,
)
from investment_assistant.rag import infer_ticker_from_filename


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FailingSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise OSError("network down")


def test_ticker_routes_to_expected_official_source():
    assert route_source("MSFT") == "SEC EDGAR"
    assert route_source("600519.SS") == "\u5de8\u6f6e\u8d44\u8baf"
    assert route_source("0700.HK") == "\u62ab\u9732\u6613"


def test_download_filename_is_recognized_as_msft_material():
    assert infer_ticker_from_filename(Path("MSFT_SEC_10-K_2026-06-30_report.pdf")) == "MSFT"
    assert infer_ticker_from_filename(Path("Microsoft_SEC_10-K_2026-06-30_report.pdf")) == "MSFT"


def test_corrupt_pdf_is_rejected_before_knowledge_base_entry(tmp_path):
    corrupt = tmp_path / "MSFT_SEC_10-K_2026-06-30.pdf.part"
    corrupt.write_bytes(b"not a pdf")
    fetcher = OfficialMaterialFetcher()

    with pytest.raises(MaterialFetchError, match="PDF\\s*\\u6821\\u9a8c\\u5931\\u8d25"):
        fetcher._validate_pdf(corrupt)

    assert corrupt.exists()


def test_network_failure_is_explicit_and_retried_once():
    session = FailingSession()
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=lambda: 0.0)

    with pytest.raises(MaterialFetchError, match="SEC EDGAR\\s*\\u8bf7\\u6c42\\u5931\\u8d25") as exc_info:
        fetcher._get("https://example.invalid", "SEC EDGAR", "www.sec.gov")

    assert session.calls == 2
    assert "network down" in str(exc_info.value)


def test_missing_official_pdf_selects_transparent_html_conversion_path(monkeypatch):
    fetcher = OfficialMaterialFetcher()
    monkeypatch.setattr(fetcher, "_get_json", lambda *args, **kwargs: {"directory": {"item": [{"name": "filing.htm"}]}})

    assert fetcher._official_pdf_name(789019, "0001193125-26-323660") is None



class RecordingSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, headers, params=None, timeout=30):
        self.get_calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return self.get_responses.pop(0)

    def post(self, url, headers, data, timeout=30):
        self.post_calls.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return self.post_responses.pop(0)


def test_cninfo_uses_post_and_code_before_org_id():
    session = RecordingSession(
        post_responses=[
            FakeResponse(payload={"keyBoardList": [{"code": "600519", "orgId": "gssh0600519"}]}),
            FakeResponse(payload={"announcements": [{"adjunctUrl": "finalpage/2026-03-01/123.pdf"}]}),
        ]
    )
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=lambda: 0.0)

    org_id = fetcher._cninfo_org_id("600519")
    announcement = fetcher._cninfo_annual_announcement("600519", org_id)

    assert announcement["adjunctUrl"].endswith("123.pdf")
    assert session.post_calls[0]["url"] == CNINFO_TOP_SEARCH_URL
    assert session.post_calls[0]["data"] == {"keyWord": "600519", "maxNum": "10"}
    assert session.post_calls[1]["url"] == CNINFO_ANNOUNCEMENT_URL
    assert session.post_calls[1]["data"]["stock"] == "600519,gssh0600519"
    assert session.post_calls[1]["data"]["column"] == "sse"
    assert "User-Agent" in session.post_calls[0]["headers"]
    assert "Referer" in session.post_calls[0]["headers"]


def test_hkex_parses_stock_id_from_jsonp_and_uses_required_parameters():
    session = RecordingSession(get_responses=[FakeResponse(content=b'callback([{"stockId":"7609"}]);')])
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=lambda: 0.0)

    stock_id = fetcher._hkex_stock_id("0700.HK")

    assert stock_id == "7609"
    call = session.get_calls[0]
    assert call["url"] == HKEX_PREFIX_URL
    assert call["params"] == {"callback": "callback", "lang": "EN", "type": "A", "name": "Tencent Holdings", "market": "SEHK"}
    assert "User-Agent" in call["headers"]
    assert "Referer" in call["headers"]


def test_hkex_title_search_parses_nested_result_and_uses_stock_id():
    expected = {"FILE_LINK": "/listedco/listconews/sehk/2026/0301/2026030100010.pdf", "TITLE": "Annual Report 2025"}
    session = RecordingSession(get_responses=[FakeResponse(payload={"result": json.dumps([expected])})])
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=lambda: 0.0)

    result = fetcher._hkex_annual_result("7609")

    assert result == expected
    call = session.get_calls[0]
    assert call["url"] == HKEX_TITLE_SEARCH_URL
    assert call["params"]["stockId"] == "7609"
    assert call["params"]["title"] == "Annual Report"
    assert call["params"]["rowRange"] == "100"


@pytest.mark.parametrize(
    ("source", "url", "host", "method"),
    [
        (CNINFO_SOURCE, CNINFO_TOP_SEARCH_URL, "www.cninfo.com.cn", "post"),
        (HKEX_SOURCE, HKEX_PREFIX_URL, "www1.hkexnews.hk", "get"),
    ],
)
def test_html_response_is_explicit_anti_bot_failure(source, url, host, method):
    response = FakeResponse(content=b"<html>blocked</html>")
    session = RecordingSession(
        get_responses=[response, response] if method == "get" else [],
        post_responses=[response, response] if method == "post" else [],
    )
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=lambda: 0.0)

    with pytest.raises(MaterialFetchError) as exc_info:
        if method == "post":
            fetcher._post_json(url, source, host, {"keyWord": "600519"})
        else:
            fetcher._hkex_stock_id("0700.HK")
    assert source in str(exc_info.value)
    assert "\u7591\u4f3c\u88ab\u53cd\u722c\u62e6\u622a" in str(exc_info.value)
