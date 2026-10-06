"""公司候选解析的离线契约测试：不联网、不写正式清单、候选永不 verified。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api, company_discovery as cd
from investment_assistant.market_data_sources import (
    STATUS_CANDIDATE,
    STATUS_EMPTY,
    STATUS_UNAVAILABLE,
    MarketDataSourceRegistry,
    SourceObservation,
)
from investment_assistant.research_tools import (
    ToolInputError,
    ToolPermissionError,
    get_market_snapshot_tool,
    get_source_health_tool,
    get_structured_financial_candidates_tool,
    invoke_tool,
    request_official_material_onboarding_tool,
    resolve_company_candidates_tool,
    tool_catalog,
)

ANALYST = api.default_auth_service  # 仅用于确认单例存在；身份由测试环境变量提供


def _principal(roles: tuple[str, ...] = ("analyst", "admin")):
    from investment_assistant.access_control import Principal

    return Principal(actor_id="tester", tenant_id="t", roles=frozenset(roles))


class FakeAdapter:
    """离线 fake 数据源；记录被调用的市场，证明只按需探测。"""

    def __init__(self, source: str, rows: list[dict] | None = None, status: str = STATUS_CANDIDATE,
                 error: dict | None = None) -> None:
        self.source = source
        self.rows = rows or []
        self.status = status
        self.error = error
        self.calls: list[str | None] = []

    def resolve_company(self, query: str, *, market: str | None = None) -> SourceObservation:
        self.calls.append(market)
        return SourceObservation(
            source=self.source, operation="resolve_company", status=self.status,
            retrieved_at="2026-10-05T00:00:00+00:00", company=query, data=self.rows,
            provenance={"provider": self.source, "endpoint": "fake"}, error=self.error,
        )

    def fetch_market_snapshot(self, ticker: str, *, market: str | None = None) -> SourceObservation:
        self.calls.append(f"snapshot:{ticker}:{market}")
        return SourceObservation(
            source=self.source, operation="fetch_market_snapshot", status=STATUS_CANDIDATE,
            retrieved_at="2026-10-05T00:00:00+00:00", ticker=ticker,
            data=[{"date": "2025-01-02", "close": 10.0}],
            provenance={"provider": self.source, "endpoint": "fake"},
        )

    def fetch_structured_financials(self, ticker: str, *, year: str | None = None) -> SourceObservation:
        self.calls.append(f"financials:{ticker}:{year}")
        return SourceObservation(
            source=self.source, operation="fetch_structured_financials", status=STATUS_CANDIDATE,
            retrieved_at="2026-10-05T00:00:00+00:00", ticker=ticker,
            data=[{"revenue": 1, "unit": "unknown"}],
            provenance={"provider": self.source, "endpoint": "fake"},
        )


def _registry(*adapters: FakeAdapter) -> MarketDataSourceRegistry:
    registry = MarketDataSourceRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


# --- 正向：本地清单优先 ---------------------------------------------------------


@pytest.mark.parametrize("query, ticker", [
    ("腾讯", "0700.HK"), ("Tencent", "0700.HK"), ("0700.HK", "0700.HK"),
    ("平安银行", "000001.SZ"), ("000001.SZ", "000001.SZ"),
    ("宁德时代", "300750.SZ"), ("300750.SZ", "300750.SZ"),
    ("贵州茅台", "600519.SS"), ("600519.SS", "600519.SS"),
    ("网易", "9999.HK"),
])
def test_onboarded_companies_resolve_from_local_manifest_without_external_source(query, ticker):
    exploding = FakeAdapter("exploding", rows=[{"ticker": "9999.SZ", "company": "错误公司", "market": "A"}])
    result = cd.resolve_company_candidates(query, registry=_registry(exploding))
    assert result.status == cd.STATUS_ONBOARDED
    assert result.selected_ticker == ticker
    assert result.candidates[0].verified is True
    assert result.source_observations == ()
    assert exploding.calls == [], "已收录公司不得调用外部源"
    assert result.external_source_used is False


def test_local_manifest_hit_marks_evidence_state_per_anchor():
    # 腾讯/平安银行/宁德时代/网易已有核验锚点；贵州茅台只有资料，字段未锚定。
    assert cd.resolve_company_candidates("腾讯2025年收入").evidence_state == cd.EVIDENCE_FIELD_VERIFIED
    assert cd.resolve_company_candidates("贵州茅台2025年收入").evidence_state == cd.EVIDENCE_MATERIAL_ONLY


def test_bare_six_digit_code_is_not_guessed_to_an_exchange():
    result = cd.resolve_company_candidates("000001")
    assert result.status == cd.STATUS_ONBOARDED and result.selected_ticker == "000001.SZ"
    # 未知裸代码保持歧义，而不是随便挑一个交易所。
    unknown = cd.resolve_company_candidates("654321")
    assert unknown.selected_ticker is None
    assert all(c.verified is False for c in unknown.candidates)


@pytest.mark.parametrize("raw, expected", [
    ("0700.HK", "0700.HK"), ("700.HK", "0700.HK"), ("sz.000001", "000001.SZ"),
    ("sh.600519", "600519.SS"), ("600519.SH", "600519.SS"), ("000858.sz", "000858.SZ"),
    ("baba", "BABA"), ("9988", "9988.HK"),
])
def test_ticker_normalization_preserves_market_meaning(raw, expected):
    assert cd.normalize_ticker(raw)["ticker"] == expected


def test_ticker_normalization_rejects_arbitrary_url():
    with pytest.raises(cd.CompanyDiscoveryInputError):
        cd.normalize_ticker("https://evil.example.com/report.pdf")
    with pytest.raises(cd.CompanyDiscoveryInputError):
        cd.resolve_company_candidates("https://evil.example.com/x.pdf")


# --- 阿里巴巴：识别但不冒充证据 ---------------------------------------------------


def test_alibaba_without_market_is_ambiguous_and_never_verified():
    result = cd.resolve_company_candidates("阿里巴巴")
    assert result.status == cd.STATUS_AMBIGUOUS
    assert result.selected_ticker is None
    assert result.error_code == cd.ERROR_COMPANY_AMBIGUOUS_MARKET
    markets = {c.market for c in result.candidates}
    assert {"HK", "US"} <= markets
    assert all(c.verified is False for c in result.candidates)
    assert result.evidence_state == cd.EVIDENCE_NOT_ONBOARDED
    payload = result.to_dict()
    assert payload["answerable"] is False
    assert payload["evidence_state"] == cd.EVIDENCE_NOT_ONBOARDED
    for candidate in payload["candidates"]:
        assert candidate["verified"] is False
        assert candidate["evidence_level"] == "candidate"


def test_alibaba_with_explicit_market_is_matched_but_still_requires_official_material():
    hk = cd.resolve_company_candidates("阿里巴巴", market="HK")
    assert hk.status == cd.STATUS_MATCHED and hk.selected_ticker == "9988.HK"
    assert hk.error_code == cd.ERROR_OFFICIAL_MATERIAL_REQUIRED
    assert hk.to_dict()["answerable"] is False
    us = cd.resolve_company_candidates("Alibaba", market="US")
    assert us.selected_ticker == "BABA" and us.status == cd.STATUS_MATCHED
    # 英文名与中文名解析到同一候选。
    assert {c.ticker for c in us.candidates} == {c.ticker for c in cd.resolve_company_candidates("阿里巴巴", market="US").candidates}


def test_alibaba_fake_external_candidate_stays_candidate_with_provenance():
    fake = FakeAdapter("akshare", rows=[
        {"ticker": "9988", "company": "阿里巴巴-W", "market": "HK"},
    ])
    result = cd.resolve_company_candidates("阿里巴巴", market="HK", registry=_registry(fake))
    assert result.status == cd.STATUS_MATCHED
    assert result.selected_ticker == "9988.HK"
    assert result.candidates[0].verified is False
    assert "local_routing_hint" in result.candidates[0].sources
    assert result.candidates[0].provenance.get("provider") == "akshare"
    assert cd.WARNING_CANDIDATE_NOT_EVIDENCE in result.warnings


def test_unresolved_company_reports_not_found_not_ambiguous():
    result = cd.resolve_company_candidates("完全不存在的公司xyz")
    assert result.status == cd.STATUS_UNRESOLVED
    assert result.error_code == cd.ERROR_COMPANY_NOT_FOUND
    assert result.candidates == ()


def test_external_source_failure_is_structured_and_not_replaced_by_other_company():
    failing = FakeAdapter("akshare", status=STATUS_UNAVAILABLE, error={
        "source": "akshare", "operation": "resolve_company", "error_code": "timeout",
        "retryable": True, "message": "来源请求超时，已按策略停止。", "attempts": 2,
    })
    result = cd.resolve_company_candidates("某家从未听过的公司", registry=_registry(failing))
    assert result.status == cd.STATUS_SOURCE_UNAVAILABLE
    assert result.error_code == cd.ERROR_SOURCE_UNAVAILABLE
    assert result.candidates == (), "外部源失败时不得混入任何其他公司的缓存"
    assert result.source_observations[0].error["error_code"] == "timeout"


def test_registry_exception_becomes_observation_not_unhandled():
    class Exploding:
        adapters = {"akshare": object()}

        def resolve_company(self, query, *, market=None):
            raise RuntimeError("boom")

    result = cd.resolve_company_candidates("某未知公司xyz", registry=Exploding())
    assert result.status in {cd.STATUS_UNRESOLVED, cd.STATUS_SOURCE_UNAVAILABLE}
    assert all(isinstance(o, SourceObservation) for o in result.source_observations)


def test_empty_external_response_does_not_invent_candidates():
    empty = FakeAdapter("akshare", status=STATUS_EMPTY, rows=[])
    result = cd.resolve_company_candidates("某空白公司xyz", registry=_registry(empty))
    assert result.selected_ticker is None
    assert all(c.verified is False for c in result.candidates)
    # 源可达但无匹配 ≠ 源不可用：状态与文案必须区分。
    assert result.status == cd.STATUS_UNRESOLVED
    assert result.error_code == cd.ERROR_COMPANY_NOT_FOUND
    assert "没有该公司记录" in result.message


def test_field_drift_rows_are_dropped_not_guessed():
    drifting = FakeAdapter("akshare", rows=[
        {"ticker": "9988", "company": "无市场标注冲突", "market": "US"},
        {"ticker": "not-a-ticker", "company": "格式错误"},
        {"company": "缺代码"},
    ])
    result = cd.resolve_company_candidates("阿里巴巴", market="HK", registry=_registry(drifting))
    # 上游市场标注与代码后缀冲突的行被丢弃；只保留路由提示里的 HK 候选。
    assert all(c.market == "HK" for c in result.candidates)
    assert all(c.company != "格式错误" for c in result.candidates)


def test_multi_market_candidates_never_auto_select():
    class TwoMarkets:
        adapters = {"akshare": object()}

        def resolve_company(self, query, *, market=None):
            return [SourceObservation(
                source="akshare", operation="resolve_company", status=STATUS_CANDIDATE,
                retrieved_at="2026-10-05T00:00:00+00:00", company=query,
                data=[{"ticker": "1234.HK" if market == "HK" else "ABCD", "company": query}],
                provenance={"provider": "akshare", "endpoint": "fake"},
            )]

    result = cd.resolve_company_candidates("某双市场公司", registry=TwoMarkets())
    assert result.status == cd.STATUS_AMBIGUOUS
    assert result.selected_ticker is None
    assert {c.market for c in result.candidates} == {"HK", "US"}


def test_market_filter_never_leaks_other_market_candidate():
    hk = cd.resolve_company_candidates("阿里巴巴", market="HK")
    assert {c.market for c in hk.candidates} == {"HK"}
    assert all(c.market == "HK" for c in hk.candidates)


def test_invalid_market_and_empty_query_are_rejected():
    with pytest.raises(cd.CompanyDiscoveryInputError):
        cd.resolve_company_candidates("阿里巴巴", market="MARS")
    with pytest.raises(cd.CompanyDiscoveryInputError):
        cd.resolve_company_candidates("   ")


# --- 工具边界 -------------------------------------------------------------------


def test_discovery_tool_requires_read_role_and_blocks_url_fields():
    with pytest.raises(ToolPermissionError):
        resolve_company_candidates_tool(principal=_principal(("reviewer",)), query="阿里巴巴")
    with pytest.raises(ToolPermissionError):
        resolve_company_candidates_tool(principal=None, query="阿里巴巴")
    with pytest.raises(ToolInputError):
        invoke_tool("resolve_company_candidates", principal=_principal(), payload={"query": "阿里巴巴", "url": "https://x/y"})


def test_discovery_tool_does_not_call_external_by_default():
    fake = FakeAdapter("akshare", rows=[{"ticker": "9988", "company": "阿里巴巴", "market": "HK"}])
    payload = resolve_company_candidates_tool(
        principal=_principal(), query="阿里巴巴", market="HK", registry=_registry(fake),
    )
    assert fake.calls == [], "默认不允许外部源；必须显式开启"
    with pytest.raises(ToolInputError):
        invoke_tool("resolve_company_candidates", principal=_principal(), payload={"query": "x", "unknown_field": 1})


def test_financial_candidate_tool_never_marks_verified():
    fake = FakeAdapter("akshare")
    payload = get_structured_financial_candidates_tool(
        principal=_principal(), ticker="9988.HK", year="2025", registry=_registry(fake),
    )
    assert payload["verified"] is False
    assert payload["requires_official_verification"] is True
    assert payload["candidates"][0]["row"]["revenue"] == 1


def test_market_snapshot_tool_is_not_annual_report_evidence():
    fake = FakeAdapter("baostock")
    payload = get_market_snapshot_tool(principal=_principal(), ticker="000001.SZ", registry=_registry(fake))
    assert payload["data_available"] is True
    assert payload["answerable"] is False
    assert payload["evidence_level"] == "market_data_not_official_annual_report"


def test_onboarding_request_tool_needs_admin_and_does_not_write_manifest(tmp_path, monkeypatch):
    from investment_assistant import company_onboarding as onboarding

    requests_path = tmp_path / "requests.json"
    manifest = tmp_path / "manifest.json"
    onboarded = tmp_path / "onboarded.json"
    manifest.write_text("{}", encoding="utf-8")
    onboarded.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(onboarding, "REQUESTS_PATH", requests_path)
    monkeypatch.setattr(onboarding, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(onboarding, "ONBOARDED_PATH", onboarded)
    with pytest.raises(ToolPermissionError):
        request_official_material_onboarding_tool(principal=_principal(("analyst",)), ticker="9988.HK", year="2025")
    payload = request_official_material_onboarding_tool(principal=_principal(), ticker="9988.HK", year="2025")
    assert payload["official_channel"] == "HKEX"
    assert payload["network_started"] is False
    assert json.loads(manifest.read_text(encoding="utf-8")) == {}, "不得写正式资料清单"
    assert json.loads(onboarded.read_text(encoding="utf-8")) == {}, "不得写已接入清单"
    assert json.loads(requests_path.read_text(encoding="utf-8"))["9988.HK:2025"]["status"] == "awaiting_preapproval"


def test_tool_catalog_exposes_schema_roles_and_no_manifest_writes():
    specs = {spec["name"]: spec for spec in tool_catalog()}
    assert set(specs) == {
        "resolve_company_candidates", "get_market_snapshot", "get_structured_financial_candidates",
        "get_source_health", "request_official_material_onboarding",
    }
    for spec in specs.values():
        assert spec["input_schema"] and spec["output_schema"]
        assert spec["required_roles"] and spec["timeout_seconds"] > 0
        assert spec["writes_formal_manifest"] is False
    assert specs["request_official_material_onboarding"]["required_roles"] == ["admin"]


def test_source_health_tool_never_fabricates_availability():
    payload = get_source_health_tool(principal=_principal())
    sources = {row["source"]: row for row in payload["sources"]}
    assert {"baostock", "akshare"} <= set(sources)
    for row in sources.values():
        assert row["status"] in {"unknown", "available", "degraded", "unavailable"}


# --- API 与聊天路由 --------------------------------------------------------------


def test_company_discovery_api_returns_structured_status_for_alibaba():
    client = TestClient(api.app)
    body = client.post("/api/company-discovery", json={"query": "阿里巴巴"}).json()
    assert body["status"] == cd.STATUS_AMBIGUOUS
    assert body["selected_ticker"] is None
    assert body["status_label"] == "待确认市场"
    assert body["error_code"] == cd.ERROR_COMPANY_AMBIGUOUS_MARKET
    assert body["answerable"] is False
    assert all(candidate["verified"] is False for candidate in body["candidates"])
    assert body["actor_id"] == "test-actor"


def test_company_discovery_api_rejects_url_and_requires_auth(monkeypatch):
    client = TestClient(api.app)
    assert client.post("/api/company-discovery", json={"query": "https://evil.example.com/a.pdf"}).status_code in {401, 422}
    assert client.post("/api/company-discovery", json={"query": "阿里巴巴"}, headers={"Authorization": ""}).status_code == 401


def test_company_discovery_api_requires_analyst_role(monkeypatch):
    token = "viewer-" + "y" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "viewer", "tenant": "t", "roles": ["reviewer"]}}))
    client = TestClient(api.app)
    assert client.post("/api/company-discovery", json={"query": "阿里巴巴"},
                       headers={"Authorization": "Bearer " + token}).status_code == 403


def test_alibaba_chat_question_is_identified_not_vaguely_refused():
    from investment_assistant.chat_session import new_context, route_message

    kind, detail = route_message("阿里巴巴2025年的收入怎么样", new_context())
    assert kind == "guidance"
    assert "已识别" in detail and "阿里巴巴" in detail
    assert "9988.HK" in detail and "BABA" in detail
    assert "不会自行选择" in detail


def test_tencent_chat_question_still_uses_onboarded_path():
    from investment_assistant.chat_session import new_context, route_message

    assert route_message("腾讯2025年财报收入怎么样", new_context())[0] == "company"
    assert route_message("给我一份2025年的腾讯财报", new_context())[0] == "annual_report"


def test_cross_ticker_question_is_refused():
    from investment_assistant.chat_session import new_context, route_message

    kind, detail = route_message("阿里巴巴和腾讯2025年收入对比", new_context())
    assert kind == "guidance"
    assert "一家公司" in detail or "不会混入" in detail


def test_unknown_company_still_blocks_old_report_reuse():
    from investment_assistant.chat_session import new_context, route_message, select_report

    state = select_report(new_context(), {"id": "AAPL_1", "ticker": "AAPL"})
    for question, expected in (("阿里巴巴2025年收入是多少", "已识别"), ("某不存在公司xyz2025年收入", "暂未收录")):
        kind, detail = route_message(question, state)
        assert kind == "guidance"
        assert expected in detail
    # 无论哪种情况都不能把旧报告当作当前问题的答案范围。
    assert route_message("阿里巴巴2025年收入是多少", state)[0] != "answer"


def test_evidence_state_api_reports_stable_error_codes():
    client = TestClient(api.app)
    alibaba = client.get("/api/company-evidence-state", params={"question": "阿里巴巴2025年收入"}).json()
    assert alibaba["error_code"] == cd.ERROR_COMPANY_AMBIGUOUS_MARKET
    unknown = client.get("/api/company-evidence-state", params={"question": "某不存在公司xyz的收入"}).json()
    assert unknown["error_code"] in {cd.ERROR_COMPANY_NOT_FOUND, cd.ERROR_COMPANY_NOT_ONBOARDED}
    codes = {row["ticker"] for row in alibaba["companies"]}
    assert {"0700.HK", "300750.SZ"} <= codes


def test_research_tool_catalog_endpoint_declares_mcp_deferred():
    body = TestClient(api.app).get("/api/research-tools").json()
    assert body["mcp_exposed"] is False
    assert "MCP" in body["mcp_deferred_reason"]
    assert {tool["name"] for tool in body["tools"]} >= {"resolve_company_candidates", "get_source_health"}


def test_project_imports_and_starts_without_optional_dependencies():
    """未安装 baostock/akshare 时仍必须可导入、可解析（只是没有外部候选）。"""
    import importlib

    for name in ("baostock", "akshare"):
        with pytest.raises(ImportError):
            importlib.import_module(name)
    result = cd.resolve_company_candidates("阿里巴巴")
    assert result.status == cd.STATUS_AMBIGUOUS
    assert importlib.import_module("investment_assistant.api") is api
