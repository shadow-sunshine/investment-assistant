"""Phase A 报告任务化测试。

覆盖：JobStore 读写与路径穿越防护、进度计算、幂等提交、节点级进度推进、
失败归档、取消、陈旧任务回收，以及异步任务 API 端点。

全部用例不触网络、不触向量库——工作流由 :class:`_FakeGraph` 替身驱动。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api
from investment_assistant import report_jobs as rj

JOB_ID = "job_20260101_000000_abcdef"


# --- 测试替身 ---------------------------------------------------------------


class _InlineExecutor(rj.JobExecutor):
    """同步执行：``create()`` 返回时任务已跑完，测试无需等待也不引入竞态。"""

    def __init__(self, runner):
        self._runner = runner

    def submit(self, job_id: str) -> None:
        self._runner(job_id)


class _ManualExecutor(rj.JobExecutor):
    """只登记不执行，用于观察 ``queued`` 状态与手工驱动执行。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)


class _FakeGraph:
    """按 ``WORKFLOW_STEPS`` 顺序吐 ``updates``/``values``，模拟真实编译图的流式行为。"""

    def __init__(self, *, fail_at: str | None = None, cancel_at: str | None = None, on_cancel=None) -> None:
        self._fail_at = fail_at
        self._cancel_at = cancel_at
        self._on_cancel = on_cancel

    def stream(self, initial_state, stream_mode=None):
        state = dict(initial_state)
        yield ("values", dict(state))
        for name, _label in rj.WORKFLOW_STEPS:
            if name == self._cancel_at and self._on_cancel is not None:
                self._on_cancel()
            if name == self._fail_at:
                raise RuntimeError(f"节点失败：{name}")
            state = {**state, name: f"{name}-done"}
            if name == rj.WORKFLOW_STEPS[-1][0]:
                state = {**state, "report": "# 假报告\n\n## 4. 受控研究叙述\n内容 [S1]"}
            yield ("updates", {name: {name: f"{name}-done"}})
            yield ("values", dict(state))


def _make_service(tmp_path, *, graph_factory=None, manual: bool = False) -> rj.JobService:
    store = rj.JobStore(tmp_path / "jobs")
    factory = graph_factory or (lambda: _FakeGraph())
    bag: dict[str, rj.JobService] = {}
    if manual:
        executor: rj.JobExecutor = _ManualExecutor()
    else:
        executor = _InlineExecutor(lambda job_id: bag["service"].run_job(job_id))
    service = rj.JobService(store, graph_factory=factory, executor=executor)
    bag["service"] = service
    return service


# --- 数据模型与存储 ---------------------------------------------------------


def test_progress_tracks_completed_steps():
    job = rj.ReportJob(job_id=JOB_ID, ticker="AAPL", topic="服务业务", horizon="中期", requested_by="demo")
    assert job.progress == 0
    job.steps[0].status = rj.STEP_COMPLETED
    assert job.progress == round(1 / len(rj.WORKFLOW_STEPS) * 100)


def test_store_roundtrip_and_rejects_illegal_job_id(tmp_path):
    store = rj.JobStore(tmp_path / "jobs")
    job = rj.ReportJob(
        job_id=JOB_ID,
        ticker="AAPL",
        topic="服务业务",
        horizon="中期",
        requested_by="demo",
        tool_error={"error_code": "rate_limited", "http_status": 429},
    )
    store.save(job)

    loaded = store.load(JOB_ID)
    assert loaded is not None
    assert loaded.to_dict() == job.to_dict()
    assert [item.job_id for item in store.list()] == [JOB_ID]

    # job_id 同时是文件名，必须挡住路径穿越
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")
    assert store.load("../../etc/passwd") is None
    assert store.load("not-a-job-id") is None


def test_job_id_pattern_matches_generated_ids():
    assert rj.JOB_ID_PATTERN.match(rj._new_job_id())
    assert not rj.JOB_ID_PATTERN.match("job_x")


# --- 执行与进度 -------------------------------------------------------------


def test_structured_source_error_survives_job_storage_and_api(tmp_path, monkeypatch):
    from investment_assistant.fetch_materials import MaterialFetchError
    from investment_assistant.source_governance import RATE_LIMITED, tool_error

    structured = tool_error(
        "SEC EDGAR", "fetch_annual_report", RATE_LIMITED, http_status=429, retry_after_ms=2000
    )

    class _FailingGraph:
        def stream(self, initial_state, stream_mode=None):
            raise MaterialFetchError("HTTP 429", error=structured)
            yield  # 使该方法成为生成器

    service = _make_service(tmp_path, graph_factory=_FailingGraph)
    job, _created = service.create(ticker="AAPL", topic="服务业务", horizon="中期", requested_by="demo")

    stored = service.get(job.job_id)
    assert stored is not None
    assert stored.tool_error == structured.to_dict()

    monkeypatch.setattr(api, "job_service", service)
    response = TestClient(api.app).get(f"/api/report-jobs/{job.job_id}")
    assert response.status_code == 200
    degradation = response.json()["degradation"]
    assert degradation["status"] == "source_unavailable"
    assert any("SEC EDGAR" in reason for reason in degradation["degradation_reasons"])


def test_create_runs_to_completion_and_persists_report(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    monkeypatch.setattr(rj, "REPORT_DIR", report_dir)
    service = _make_service(tmp_path)

    job, created = service.create(ticker="aapl", topic="  服务业务  ", horizon="中期", requested_by="demo-user")

    assert created is True
    final = service.get(job.job_id)
    assert final is not None
    assert final.status == rj.STATUS_COMPLETED
    assert final.current_step is None
    assert final.progress == 100
    assert [record.status for record in final.steps] == [rj.STEP_COMPLETED] * len(rj.WORKFLOW_STEPS)
    assert [record.name for record in final.steps] == [name for name, _ in rj.WORKFLOW_STEPS]
    # 入参被规范化；发起人被保留（问题①）
    assert final.ticker == "AAPL" and final.topic == "服务业务"
    assert final.requested_by == "demo-user"
    # 报告落盘
    assert final.report_id is not None
    assert (report_dir / f"{final.report_id}.json").exists()
    assert (report_dir / f"{final.report_id}.md").exists()


def test_each_step_records_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(rj, "REPORT_DIR", tmp_path / "reports")
    service = _make_service(tmp_path)
    job, _ = service.create(ticker="AAPL", topic="t", horizon="中期")

    final = service.get(job.job_id)
    assert all(record.duration_ms is not None for record in final.steps)
    assert all(record.started_at and record.completed_at for record in final.steps)


def test_duplicate_submission_reuses_active_job(tmp_path):
    service = _make_service(tmp_path, manual=True)
    first, created_first = service.create(ticker="AAPL", topic="服务业务", horizon="中期")
    second, created_second = service.create(ticker="AAPL", topic="服务业务", horizon="中期")

    assert created_first is True
    assert created_second is False
    assert first.job_id == second.job_id


def test_streamed_final_state_equals_invoke_on_real_graph(monkeypatch):
    """钉死 Phase A 的核心假设。

    用 ``stream_mode=["updates", "values"]`` 订阅**真实**编译图，最终状态必须与
    ``invoke()`` 逐键一致——这正是「不用改 workflow.py 也能拿到权威最终状态」的依据。
    节点被替换为离线桩，测试不触网络与向量库。
    """
    from investment_assistant import workflow

    def _stub(name):
        def _node(_state):
            return {name: f"{name}-done", "report": "# 报告", "ticker": "AAPL"}

        return _node

    for name, _label in rj.WORKFLOW_STEPS:
        monkeypatch.setattr(workflow, name, _stub(name))

    graph = workflow.create_research_workflow()
    initial = {"ticker": "AAPL", "topic": "服务业务", "horizon": "中期", "report_mode": "auto"}

    invoked = graph.invoke(dict(initial))

    streamed_final: dict = {}
    seen_nodes: list[str] = []
    for mode, payload in graph.stream(dict(initial), stream_mode=["updates", "values"]):
        if mode == "updates":
            seen_nodes.extend(payload.keys())
        elif mode == "values":
            streamed_final = payload

    assert streamed_final == invoked
    # 顺序与 WORKFLOW_STEPS 一致，进度条才不会错位
    assert seen_nodes == [name for name, _ in rj.WORKFLOW_STEPS]


def test_failed_node_marks_step_failed_and_archives_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(rj, "REPORT_DIR", tmp_path / "reports")
    service = _make_service(tmp_path, graph_factory=lambda: _FakeGraph(fail_at="model_market"))

    job, _ = service.create(ticker="AAPL", topic="t", horizon="中期")
    final = service.get(job.job_id)

    assert final.status == rj.STATUS_FAILED
    assert "节点失败：model_market" in (final.error or "")
    assert [record.name for record in final.steps if record.status == rj.STEP_FAILED] == ["model_market"]
    # 失败前已完成的节点保持 completed，失败原因不会只留在终端
    assert final.steps[0].status == rj.STEP_COMPLETED
    assert final.report_id is None


def test_cancel_queued_job(tmp_path):
    service = _make_service(tmp_path, manual=True)
    job, _ = service.create(ticker="AAPL", topic="t", horizon="中期")
    assert service.get(job.job_id).status == rj.STATUS_QUEUED

    cancelled = service.cancel(job.job_id)

    assert cancelled.status == rj.STATUS_CANCELLED
    final = service.get(job.job_id)
    assert final.completed_at is not None
    # 未执行的节点标为 skipped（不是 failed——它没有出错）
    assert {record.status for record in final.steps} == {rj.STEP_SKIPPED}


def test_cancel_during_run_stops_before_that_step_completes(tmp_path, monkeypatch):
    """取消在**节点边界**生效：在途节点跑完才停，其后节点一律 skipped。"""
    monkeypatch.setattr(rj, "REPORT_DIR", tmp_path / "reports")
    box: dict[str, object] = {}

    def factory():
        return _FakeGraph(cancel_at="model_market", on_cancel=lambda: box["service"].cancel(box["job_id"]))

    service = _make_service(tmp_path, graph_factory=factory, manual=True)
    box["service"] = service
    job, _ = service.create(ticker="AAPL", topic="t", horizon="中期")
    box["job_id"] = job.job_id

    service.run_job(job.job_id)

    final = service.get(job.job_id)
    assert final.status == rj.STATUS_CANCELLED
    assert final.report_id is None
    completed = [record.name for record in final.steps if record.status == rj.STEP_COMPLETED]
    assert completed == ["collect_real_data", "retrieve_evidence"]
    # 被取消打断的那一步既没完成、也没被判为失败
    assert final.step("model_market").status == rj.STEP_SKIPPED
    assert final.step("validate_risks").status == rj.STEP_SKIPPED


def test_recover_stale_jobs_marks_leftovers_failed(tmp_path):
    service = _make_service(tmp_path, manual=True)
    job, _ = service.create(ticker="AAPL", topic="t", horizon="中期")

    recovered = service.recover_stale_jobs()

    assert recovered == [job.job_id]
    final = service.get(job.job_id)
    assert final.status == rj.STATUS_FAILED
    assert "服务重启" in (final.error or "")


def test_run_job_on_missing_id_is_noop(tmp_path):
    service = _make_service(tmp_path, manual=True)
    service.run_job(JOB_ID)  # 不应抛异常
    assert service.get(JOB_ID) is None


# --- API 端点 ---------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(rj, "REPORT_DIR", tmp_path / "reports")
    service = _make_service(tmp_path)
    monkeypatch.setattr(api, "job_service", service)
    return TestClient(api.app), service


def test_api_submit_then_poll_then_fetch_report(api_client):
    http, _service = api_client

    created = http.post(
        "/api/report-jobs",
        json={"ticker": "AAPL", "topic": "服务业务", "horizon": "中期", "requested_by": "demo"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == rj.STATUS_QUEUED  # 提交即返回，此刻尚未执行
    job_id = body["job_id"]

    status = http.get(f"/api/report-jobs/{job_id}")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == rj.STATUS_COMPLETED
    assert payload["requested_by"] == "test-actor"
    assert payload["progress"] == 100
    assert len(payload["steps"]) == len(rj.WORKFLOW_STEPS)
    assert payload["steps"][0]["label"]

    assert payload["delivery"]["release_status"] == "needs_review"
    report = http.get(f"/api/report-jobs/{job_id}/report")
    assert report.status_code == 409
    assert report.json()["detail"]["delivery"]["release_status"] == "needs_review"
    assert "report" not in report.json()["detail"]


def test_api_report_returns_409_before_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(api, "job_service", _make_service(tmp_path, manual=True))
    http = TestClient(api.app)

    job_id = http.post("/api/report-jobs", json={"ticker": "AAPL", "topic": "t", "horizon": "中期"}).json()["job_id"]

    response = http.get(f"/api/report-jobs/{job_id}/report")
    assert response.status_code == 409
    assert response.json()["detail"]["job_id"] == job_id


def test_api_unknown_or_malformed_job_id_returns_404(api_client):
    http, _service = api_client
    assert http.get("/api/report-jobs/job_20260101_000000_abcdef").status_code == 404
    assert http.get("/api/report-jobs/not-a-job-id").status_code == 404
    assert http.post("/api/report-jobs/not-a-job-id/cancel").status_code == 404


def test_api_blank_topic_rejected_without_creating_job(api_client):
    http, service = api_client
    response = http.post("/api/report-jobs", json={"ticker": "AAPL", "topic": "   ", "horizon": "中期"})

    assert response.status_code == 422
    assert response.json()["detail"] == "研究主题不能为空，请检查输入。"
    assert service.list(limit=100) == []


def test_api_duplicate_submission_returns_409_with_existing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "job_service", _make_service(tmp_path, manual=True))
    http = TestClient(api.app)
    payload = {"ticker": "AAPL", "topic": "服务业务", "horizon": "中期"}

    first = http.post("/api/report-jobs", json=payload)
    second = http.post("/api/report-jobs", json=payload)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"]["job_id"] == first.json()["job_id"]


def test_api_lists_jobs(api_client):
    http, _service = api_client
    http.post("/api/report-jobs", json={"ticker": "AAPL", "topic": "服务业务", "horizon": "中期"})

    listed = http.get("/api/report-jobs")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["ticker"] == "AAPL"


def test_completed_graph_with_source_error_returns_job_scoped_degradation(tmp_path, monkeypatch):
    class SourceGapGraph(_FakeGraph):
        def stream(self, initial_state, stream_mode=None):
            for mode, payload in super().stream(initial_state, stream_mode):
                if mode == "values":
                    payload = {**payload, "market_snapshot": {
                        "source": "Yahoo Finance via yfinance", "error_code": "timeout",
                        "error": "行情源请求超时。", "data_available": False,
                    }}
                yield mode, payload

    report_dir = tmp_path / "reports"
    monkeypatch.setattr(api, "REPORT_DIR", report_dir)
    monkeypatch.setattr(rj, "REPORT_DIR", report_dir)
    service = _make_service(tmp_path, graph_factory=SourceGapGraph)
    monkeypatch.setattr(api, "job_service", service)
    http = TestClient(api.app)

    created = http.post("/api/report-jobs", json={"ticker": "AAPL", "topic": "服务业务", "horizon": "中期"})
    job_id = created.json()["job_id"]
    payload = http.get(f"/api/report-jobs/{job_id}").json()

    assert payload["status"] == rj.STATUS_COMPLETED
    assert payload["degradation"]["status"] == "source_unavailable"
    assert payload["degradation_reasons"] and "Yahoo Finance" in payload["degradation_reasons"][0]
    assert payload["source_errors"][0]["error_code"] == "timeout"


def test_completed_graph_without_source_fault_has_empty_degradation_reasons(api_client):
    http, _service = api_client
    created = http.post("/api/report-jobs", json={"ticker": "AAPL", "topic": "服务业务", "horizon": "中期"})
    payload = http.get(f"/api/report-jobs/{created.json()['job_id']}").json()
    assert payload["status"] == rj.STATUS_COMPLETED
    assert payload["degradation"] == {"status": "ok", "degradation_reasons": []}
    assert payload["degradation_reasons"] == []
