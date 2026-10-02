"""R4 数据源/工具治理测试。全部离线可重复，不访问真实网络。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import requests
from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant.fetch_materials import (
    CNINFO_SOURCE,
    CNINFO_TOP_SEARCH_URL,
    MAX_RETRIES,
    MaterialFetchError,
    OfficialMaterialFetcher,
    fetch_materials,
)
from investment_assistant.source_governance import (
    ANTI_BOT_OR_BLOCKED,
    DEFAULT_MAX_DATA_AGE_SECONDS,
    DEPENDENCY_ERROR,
    HEALTH_AVAILABLE,
    HEALTH_DEGRADED,
    HEALTH_UNAVAILABLE,
    HEALTH_UNKNOWN,
    HTTP_ERROR,
    PARSE_ERROR,
    RATE_LIMITED,
    RESULT_FAILED,
    RESULT_SUCCESS,
    TIMEOUT,
    VALIDATION_ERROR,
    KNOWN_SOURCES,
    SourceCallFailure,
    SourceCallPolicy,
    SourceErrorCode,
    ToolCallAuditRecord,
    ToolCallError,
    ToolCallLedger,
    build_degradation,
    call_fingerprint,
    classify_exception,
    classify_http_status,
    classify_material_fetch_error,
    execute_source_call,
    freshness_state,
    get_default_health_registry,
    get_default_tool_call_ledger,
    impact_from_error,
    sanitize_detail,
    tool_error,
)


# --- 桩 ------------------------------------------------------------------------


class StubResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubSession:
    """按脚本返回响应；记录每次调用的 method/url。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, **kwargs):
        self.calls.append(("post", url))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class AdvancingClock:
    """手动推进的单调钟；sleeper 借它模拟真实等待消耗。"""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _fetcher(session, clock=None):
    clock = clock or AdvancingClock()
    sleeper = clock.advance
    return OfficialMaterialFetcher(session=session, sleeper=sleeper, clock=clock)


# --- 1. 错误映射 -----------------------------------------------------------------


def test_error_contract_covers_all_required_codes():
    expected = {
        "invalid_input",
        "unsupported_ticker",
        "timeout",
        "rate_limited",
        "http_error",
        "anti_bot_or_blocked",
        "empty_response",
        "parse_error",
        "validation_error",
        "dependency_error",
        "unknown",
    }
    assert {code.value for code in SourceErrorCode} == expected


def test_exception_mapping_covers_timeout_dependency_parse_and_unknown():
    timeout_error = classify_exception(requests.exceptions.ConnectTimeout("timed out"), "SEC EDGAR", "probe")
    assert timeout_error.error_code == TIMEOUT
    assert timeout_error.retryable is True

    dependency_error = classify_exception(OSError("network down"), "SEC EDGAR", "probe")
    assert dependency_error.error_code == DEPENDENCY_ERROR
    assert dependency_error.retryable is True

    parse_error = classify_exception(ValueError("invalid json"), "SEC EDGAR", "probe")
    assert parse_error.error_code == PARSE_ERROR
    assert parse_error.retryable is False

    unknown_error = classify_exception(RuntimeError("mystery"), "SEC EDGAR", "probe")
    assert unknown_error.error_code == SourceErrorCode.UNKNOWN
    assert unknown_error.retryable is False


def test_http_status_mapping_covers_429_5xx_and_4xx():
    rate_limited = classify_http_status(429, "SEC EDGAR", "probe", retry_after_seconds=7)
    assert rate_limited.error_code == RATE_LIMITED
    assert rate_limited.retryable is True
    assert rate_limited.retry_after_ms == 7000

    server_error = classify_http_status(503, "SEC EDGAR", "probe")
    assert server_error.error_code == HTTP_ERROR
    assert server_error.retryable is True

    client_error = classify_http_status(404, "SEC EDGAR", "probe")
    assert client_error.error_code == HTTP_ERROR
    assert client_error.retryable is False


def test_material_fetch_error_text_mapping():
    rules = [
        ("SEC EDGAR 疑似被反爬拦截：返回 HTML", ANTI_BOT_OR_BLOCKED),
        ("PDF 校验失败：pypdf broken", VALIDATION_ERROR),
        ("下载文件为空或不存在。", VALIDATION_ERROR),
        ("SEC EDGAR 返回的不是有效 JSON：…", PARSE_ERROR),
        ("SEC EDGAR 未找到 ticker AAPL 的 CIK。", SourceErrorCode.EMPTY_RESPONSE),
        ("无法识别 ticker ?? 的官方信源路由。", SourceErrorCode.UNSUPPORTED_TICKER),
    ]
    for text, code in rules:
        error = classify_material_fetch_error(text, source="SEC EDGAR", operation="probe")
        assert error.error_code == code, text
        assert error.retryable is False


def test_error_record_serialization_round_trip():
    error = tool_error(
        "SEC EDGAR", "probe", RATE_LIMITED, retry_after_ms=2000, http_status=429, detail="HTTP 429"
    )
    revived = ToolCallError.from_dict(error.to_dict())
    assert revived == error


# --- 2. 重试边界与总预算 -----------------------------------------------------------


def test_retryable_error_is_retried_up_to_policy_limit():
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        raise OSError("network down")

    policy = SourceCallPolicy(max_attempts=3, backoff_seconds=0.0, total_budget_s=60.0)
    with pytest.raises(SourceCallFailure) as exc_info:
        execute_source_call(
            attempt, source="SEC EDGAR", operation="probe", policy=policy, clock=AdvancingClock(), sleeper=lambda _: None
        )
    assert calls["n"] == 3
    assert exc_info.value.error.error_code == DEPENDENCY_ERROR
    assert exc_info.value.error.attempts == 3


def test_non_retryable_error_fails_immediately_without_retry():
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        raise ValueError("bad payload")

    policy = SourceCallPolicy(max_attempts=3, backoff_seconds=0.0, total_budget_s=60.0)
    with pytest.raises(SourceCallFailure) as exc_info:
        execute_source_call(
            attempt, source="SEC EDGAR", operation="probe", policy=policy, clock=AdvancingClock(), sleeper=lambda _: None
        )
    assert calls["n"] == 1
    assert exc_info.value.error.error_code == PARSE_ERROR
    assert exc_info.value.error.attempts == 1


def test_total_budget_fail_closed_stops_network_before_max_attempts():
    clock = AdvancingClock()
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        raise OSError("network down")

    policy = SourceCallPolicy(max_attempts=5, backoff_seconds=10.0, total_budget_s=5.0)
    with pytest.raises(SourceCallFailure) as exc_info:
        execute_source_call(attempt, source="SEC EDGAR", operation="probe", policy=policy, clock=clock, sleeper=clock.advance)
    assert calls["n"] == 1
    assert exc_info.value.error.error_code == TIMEOUT
    assert exc_info.value.error.retryable is False
    assert "预算" in exc_info.value.error.message


def test_rate_limit_respects_retry_after_header_when_retrying():
    sleeps: list[float] = []
    responses = [
        StubResponse(status_code=429, headers={"Retry-After": "3"}),
        StubResponse(payload={"ok": True}),
    ]
    session = StubSession(responses)
    clock = AdvancingClock()
    fetcher = OfficialMaterialFetcher(session=session, sleeper=sleeps.append, clock=clock)

    response = fetcher._post(CNINFO_TOP_SEARCH_URL, CNINFO_SOURCE, "www.cninfo.com.cn", {"keyWord": "600519"})

    assert response.json() == {"ok": True}
    assert len(session.calls) == 2
    # 第一次退避尊重 Retry-After=3s；第二次请求前仍保留 2s 礼貌间隔（既有行为）。
    assert sleeps == [3.0, 2.0]


def test_fetcher_retries_transient_network_error_once():
    session = StubSession([])
    for _ in range(MAX_RETRIES + 1):
        session.responses.append(OSError("network down"))
    fetcher = _fetcher(session)

    with pytest.raises(MaterialFetchError, match="SEC EDGAR\\s*请求失败") as exc_info:
        fetcher._get("https://example.invalid", "SEC EDGAR", "www.sec.gov")

    assert len(session.calls) == MAX_RETRIES + 1
    assert "network down" in str(exc_info.value)


def test_fetcher_does_not_retry_client_error_4xx():
    session = StubSession([StubResponse(status_code=404)])
    fetcher = _fetcher(session)

    with pytest.raises(MaterialFetchError):
        fetcher._get("https://example.invalid", "SEC EDGAR", "www.sec.gov")

    assert len(session.calls) == 1


def test_fetcher_does_not_retry_anti_bot_html_response():
    session = StubSession([StubResponse(content=b"<html>blocked</html>")])
    fetcher = _fetcher(session)

    with pytest.raises(MaterialFetchError, match="疑似被反爬拦截"):
        fetcher._post(CNINFO_TOP_SEARCH_URL, CNINFO_SOURCE, "www.cninfo.com.cn", {"keyWord": "600519"})

    assert len(session.calls) == 1


# --- 3. 脱敏 -------------------------------------------------------------------


def test_sanitize_detail_strips_query_params_and_redacts_credentials():
    raw = "GET https://api.example.com/v1/data?api_key=sk-abc123&q=AAPL 请求失败; password=hunter2"
    sanitized = sanitize_detail(raw)
    assert "sk-abc123" not in sanitized
    assert "hunter2" not in sanitized
    assert "?..." in sanitized


def test_user_visible_message_and_detail_never_leak_credentials_or_query():
    def attempt():
        raise RuntimeError("POST https://x.example.com/p?token=tok123&x=1 failed")

    policy = SourceCallPolicy(max_attempts=1, total_budget_s=60.0)
    with pytest.raises(SourceCallFailure) as exc_info:
        execute_source_call(
            attempt, source="S", operation="probe", policy=policy, clock=AdvancingClock(), sleeper=lambda _: None
        )
    error = exc_info.value.error
    blob = f"{error.message}|{error.detail or ''}"
    assert "tok123" not in blob
    assert "Traceback" not in blob
    assert error.message  # 用户可见 message 非空


# --- 4. 健康状态转换与 retry_after -------------------------------------------------


def test_health_registry_unknown_by_default_then_transitions():
    registry = get_default_health_registry()
    registry.reset()
    snapshot = registry.snapshot("SEC EDGAR")
    assert snapshot["status"] == HEALTH_UNKNOWN

    registry.record_failure(
        "SEC EDGAR", "probe", tool_error("SEC EDGAR", "probe", RATE_LIMITED, retry_after_ms=3000)
    )
    snapshot = registry.snapshot("SEC EDGAR")
    assert snapshot["status"] == HEALTH_DEGRADED
    assert snapshot["retry_after_ms"] == 3000
    assert snapshot["last_error_code"] == RATE_LIMITED.value

    registry.record_success("SEC EDGAR", "probe", latency_ms=120, data_fetched_at=datetime.now(UTC).isoformat())
    snapshot = registry.snapshot("SEC EDGAR")
    assert snapshot["status"] == HEALTH_AVAILABLE
    assert snapshot["freshness"]["status"] == "fresh"


def test_health_registry_hard_failure_marks_unavailable():
    registry = get_default_health_registry()
    registry.reset()
    registry.record_failure(
        "巨潮资讯", "probe", tool_error("巨潮资讯", "probe", ANTI_BOT_OR_BLOCKED)
    )
    snapshot = registry.snapshot("巨潮资讯")
    assert snapshot["status"] == HEALTH_UNAVAILABLE
    assert snapshot["last_error_code"] == ANTI_BOT_OR_BLOCKED.value


def test_health_snapshot_all_lists_known_sources_as_unknown():
    registry = get_default_health_registry()
    registry.reset()
    snapshots = registry.snapshot_all()
    statuses = {item["source"]: item["status"] for item in snapshots}
    assert set(statuses) == set(KNOWN_SOURCES)
    assert set(statuses.values()) == {HEALTH_UNKNOWN}


# --- 5. 新鲜度与 stale_data -------------------------------------------------------


def test_freshness_states_fresh_stale_and_unknown():
    now = datetime.now(UTC)
    fresh = freshness_state((now - timedelta(hours=1)).isoformat(), now=now)
    assert fresh["status"] == "fresh"

    stale = freshness_state((now - timedelta(seconds=DEFAULT_MAX_DATA_AGE_SECONDS + 10)).isoformat(), now=now)
    assert stale["status"] == "stale"

    unknown = freshness_state(None, now=now)
    assert unknown["status"] == "unknown"


def test_degradation_reports_stale_data_and_prioritizes_impacts():
    now = datetime.now(UTC)
    degradation = build_degradation(
        [],
        freshness_by_source={"SEC EDGAR": freshness_state((now - timedelta(days=30)).isoformat(), now=now)},
    )
    assert degradation["status"] == "stale_data"
    assert any("stale_data" in reason for reason in degradation["degradation_reasons"])

    review = build_degradation(
        [tool_error("SEC EDGAR", "probe", VALIDATION_ERROR)],
    )
    assert review["status"] == "needs_review"

    unavailable = build_degradation(
        [tool_error("SEC EDGAR", "probe", VALIDATION_ERROR), tool_error("披露易", "probe", TIMEOUT)]
    )
    # needs_review 需要人工复核，优先级高于 source_unavailable
    assert unavailable["status"] == "needs_review"
    assert any("披露易" in reason and "source_unavailable" in reason for reason in unavailable["degradation_reasons"])


def test_impact_mapping_covers_unavailable_partial_and_review():
    assert impact_from_error(tool_error("S", "op", TIMEOUT)) == "source_unavailable"
    assert impact_from_error(tool_error("S", "op", SourceErrorCode.EMPTY_RESPONSE)) == "partial_evidence"
    assert impact_from_error(tool_error("S", "op", PARSE_ERROR)) == "needs_review"
    assert impact_from_error(tool_error("S", "op", VALIDATION_ERROR)) == "needs_review"


# --- 6. fingerprint 幂等与审计 -----------------------------------------------------


def _audit_record(fingerprint: str, job_id: str = "job_20260927_120000_abc123") -> ToolCallAuditRecord:
    return ToolCallAuditRecord(
        job_id=job_id,
        requested_by="tester",
        source="SEC EDGAR",
        operation="fetch_annual_report",
        ticker="AAPL",
        fingerprint=fingerprint,
        attempts=2,
        started_at="2026-09-27T12:00:00+00:00",
        finished_at="2026-09-27T12:00:01+00:00",
        result_status=RESULT_SUCCESS,
    )


def test_call_fingerprint_is_stable_and_input_sensitive():
    base = dict(source="SEC EDGAR", operation="fetch_annual_report", ticker="aapl", period="FY2025")
    first = call_fingerprint(**base)
    assert first == call_fingerprint(**base)  # 规范化后（大小写/空白）幂等
    assert first == call_fingerprint(**{**base, "ticker": "  AAPL "})

    assert call_fingerprint(**{**base, "period": "FY2024"}) != first
    assert call_fingerprint(**{**base, "extra_inputs": {"form": "10-K"}}) != first


def test_ledger_registers_each_fingerprint_once_and_keeps_audit_fields():
    ledger = ToolCallLedger()
    fingerprint = call_fingerprint(source="SEC EDGAR", operation="fetch_annual_report", ticker="AAPL")
    assert ledger.register(_audit_record(fingerprint)) is True
    assert ledger.register(_audit_record(fingerprint)) is False  # 同一任务边界内不重复登记

    records = ledger.records()
    assert len(records) == 1
    record = records[0].to_dict()
    for field_name in (
        "job_id",
        "requested_by",
        "source",
        "operation",
        "attempts",
        "started_at",
        "finished_at",
        "result_status",
    ):
        assert field_name in record

    failed = replace(
        records[0],
        fingerprint=call_fingerprint(source="披露易", operation="fetch_annual_report", ticker="0700.HK"),
        result_status=RESULT_FAILED,
        error_code=ANTI_BOT_OR_BLOCKED,
    )
    assert ledger.register(failed) is True
    assert ledger.records()[1].result_status == RESULT_FAILED



def test_ledger_allows_same_fingerprint_in_distinct_job_boundaries():
    ledger = ToolCallLedger()
    fingerprint = call_fingerprint(source="SEC EDGAR", operation="fetch_annual_report", ticker="AAPL")
    assert ledger.register(_audit_record(fingerprint, job_id="job-1")) is True
    assert ledger.register(_audit_record(fingerprint, job_id="job-2")) is True
    assert len(ledger.records()) == 2
    assert ledger.has_fingerprint(fingerprint, job_id="job-1") is True
    assert ledger.has_fingerprint(fingerprint, job_id="job-2") is True
# --- 7. API 错误状态与健康端点 ------------------------------------------------------


def test_source_health_endpoint_reports_unknown_without_faking_health():
    get_default_health_registry().reset()
    client = TestClient(api.app)
    response = client.get("/api/source-health")
    assert response.status_code == 200
    statuses = {item["source"]: item["status"] for item in response.json()["sources"]}
    assert statuses["SEC EDGAR"] == HEALTH_UNKNOWN
    assert HEALTH_AVAILABLE not in set(statuses.values())


def test_failed_job_response_carries_stable_degradation(monkeypatch, tmp_path):
    from investment_assistant.report_jobs import ReportJob, STATUS_FAILED, _default_steps

    failed_job = ReportJob(
        job_id="job_20260927_120000_abc123",
        ticker="AAPL",
        topic="测试",
        horizon="中期",
        requested_by="tester",
        status=STATUS_FAILED,
        error="MaterialFetchError: SEC EDGAR 疑似被反爬拦截：返回 HTML",
        steps=_default_steps(),
    )
    monkeypatch.setattr(api.job_service, "get", lambda job_id: failed_job)

    client = TestClient(api.app)
    response = client.get("/api/report-jobs/job_20260927_120000_abc123")
    assert response.status_code == 200
    payload = response.json()
    assert payload["degradation"]["status"] == "source_unavailable"
    assert payload["degradation"]["degradation_reasons"]
    # 脱敏：不泄露凭证、查询参数、堆栈
    blob = str(payload["degradation"])
    assert "token=" not in blob
    assert "Traceback" not in blob


def test_completed_job_response_has_no_degradation(monkeypatch):
    from investment_assistant.report_jobs import ReportJob, STATUS_COMPLETED, _default_steps

    done_job = ReportJob(
        job_id="job_20260927_120001_def456",
        ticker="AAPL",
        topic="测试",
        horizon="中期",
        requested_by="tester",
        status=STATUS_COMPLETED,
        steps=_default_steps(),
    )
    monkeypatch.setattr(api.job_service, "get", lambda job_id: done_job)

    client = TestClient(api.app)
    response = client.get("/api/report-jobs/job_20260927_120001_def456")
    assert response.status_code == 200
    assert response.json()["degradation"] == {"status": "ok", "degradation_reasons": []}
    assert response.json()["degradation_reasons"] == []


# --- 8. fetch_materials 外层健康登记 ------------------------------------------------


def test_fetch_materials_records_real_failure_into_health_registry(monkeypatch):
    registry = get_default_health_registry()
    registry.reset()

    class BlockedFetcher:
        def fetch_sec_10k_pdf(self, ticker):
            raise MaterialFetchError("SEC EDGAR 疑似被反爬拦截：返回 HTML")

    monkeypatch.setattr("investment_assistant.fetch_materials.OfficialMaterialFetcher", BlockedFetcher)

    with pytest.raises(MaterialFetchError):
        from investment_assistant.fetch_materials import fetch_materials

        fetch_materials("MSFT")

    snapshot = registry.snapshot("SEC EDGAR")
    assert snapshot["status"] == HEALTH_UNAVAILABLE
    assert snapshot["last_error_code"] == ANTI_BOT_OR_BLOCKED.value


# --- R4 外层结构化错误、路由健康与真实审计 -------------------------------


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_attempts"),
    [(404, HTTP_ERROR, 1), (429, RATE_LIMITED, 2)],
)
def test_fetch_materials_preserves_http_error_code_and_records_health(
    monkeypatch, status, expected_code, expected_attempts
):
    from investment_assistant import fetch_materials as fm

    registry = get_default_health_registry()
    registry.reset()
    get_default_tool_call_ledger().reset()
    session = StubSession([StubResponse(status_code=status), StubResponse(status_code=status)])
    fetcher = OfficialMaterialFetcher(session=session, sleeper=lambda _: None, clock=AdvancingClock())
    monkeypatch.setattr(fm, "OfficialMaterialFetcher", lambda: fetcher)

    with pytest.raises(MaterialFetchError) as exc_info:
        fetch_materials("AAPL", job_id="job-http", requested_by="reviewer")

    assert exc_info.value.error.error_code == expected_code
    assert exc_info.value.error.http_status == status
    assert registry.snapshot("SEC EDGAR")["last_error_code"] == expected_code.value
    audit = get_default_tool_call_ledger().records()[-1]
    assert audit.result_status == RESULT_FAILED
    assert audit.error_code == expected_code.value
    assert audit.attempts == expected_attempts


def test_unsupported_ticker_records_health_and_audit(monkeypatch):
    from investment_assistant import fetch_materials as fm

    registry = get_default_health_registry()
    registry.reset()
    ledger = get_default_tool_call_ledger()
    ledger.reset()

    with pytest.raises(MaterialFetchError) as exc_info:
        fetch_materials("???", job_id="job-unsupported", requested_by="demo")

    assert exc_info.value.error.error_code == SourceErrorCode.UNSUPPORTED_TICKER
    assert registry.snapshot(fm.SOURCE_ROUTER)["last_error_code"] == "unsupported_ticker"
    record = ledger.records()[-1]
    assert record.source == fm.SOURCE_ROUTER
    assert record.job_id == "job-unsupported"
    assert record.requested_by == "demo"
    assert record.error_code == "unsupported_ticker"


def test_report_job_api_degradation_prefers_persisted_structured_error(monkeypatch):
    from investment_assistant.report_jobs import ReportJob, STATUS_FAILED, _default_steps

    structured = tool_error(
        "SEC EDGAR", "fetch_annual_report", RATE_LIMITED, http_status=429, retry_after_ms=3000
    )
    job = ReportJob(
        job_id="job_20260927_120010_abc123",
        ticker="AAPL",
        topic="测试",
        horizon="中期",
        requested_by="tester",
        status=STATUS_FAILED,
        error="opaque text with no useful classification",
        tool_error=structured.to_dict(),
        steps=_default_steps(),
    )
    monkeypatch.setattr(api.job_service, "get", lambda _job_id: job)

    response = TestClient(api.app).get(f"/api/report-jobs/{job.job_id}")

    assert response.status_code == 200
    degradation = response.json()["degradation"]
    assert degradation["status"] == "source_unavailable"
    assert any("SEC EDGAR" in reason for reason in degradation["degradation_reasons"])


def test_ledger_without_job_id_keeps_each_real_invocation():
    ledger = ToolCallLedger()
    fingerprint = call_fingerprint(source="Yahoo Finance via yfinance", operation="fetch_recent_news", ticker="AAPL")
    first = replace(_audit_record(fingerprint), job_id="")
    second = replace(first, result_status=RESULT_FAILED, error_code="timeout")
    assert ledger.register(first) is True
    assert ledger.register(second) is True
    assert [record.result_status for record in ledger.records()] == [RESULT_SUCCESS, RESULT_FAILED]
