"""报告任务化（Phase A）：ReportJob 生命周期、异步执行与节点级进度。

设计约束（不破坏冻结边界）
--------------------------
* 不修改 ``rag.py`` / ``llm_generation.py`` / ``safety.py`` / ``workflow.py``；
* 进度在 LangGraph 编译图**外层**订阅 ``stream_mode=["updates", "values"]`` 采集，
  不改任何节点内部实现；
* 最终状态取自 ``values`` 流的最后一个快照（由框架产出），**不做手工合并**，
  因此与 ``run_research()`` 的 ``invoke()`` 结果保持一致。

本模块只回答两个业务问题：
  ① 「这份报告是谁发起的？」→ ``ReportJob.requested_by``
  ② 「当前生成到了哪一步？」→ ``ReportJob.status`` / ``current_step`` / ``steps``
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import JOB_DIR, REPORT_DIR
from .source_governance import SourceErrorCode, ToolCallError, tool_error
from .workflow import audit_json, create_research_workflow

# --- 状态枚举 ---------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

#: 与 ``workflow.create_research_workflow()`` 的节点名**逐字一致**；顺序即执行顺序。
WORKFLOW_STEPS: tuple[tuple[str, str], ...] = (
    ("collect_real_data", "拉取真实行情与财务数据"),
    ("retrieve_evidence", "检索官方年报证据"),
    ("model_market", "计算市场风险指标"),
    ("reason_scenarios", "生成情景推理"),
    ("controlled_generation", "生成受控叙述"),
    ("generate_report", "组装研究简报"),
    ("validate_risks", "执行安全校验"),
)

_STEP_LABELS: dict[str, str] = dict(WORKFLOW_STEPS)
_STEP_INDEX: dict[str, int] = {name: index for index, (name, _) in enumerate(WORKFLOW_STEPS)}

#: job_id 形状固定，既是存储文件名也是 API 路径参数；正则同时充当路径穿越防护。
JOB_ID_PATTERN = re.compile(r"^job_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")

_JOB_EXECUTOR_MAX_WORKERS = 2


class _JobCancelled(Exception):
    """内部信号：任务在节点边界被请求取消。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_ms(start_iso: str | None, end_iso: str) -> int | None:
    if not start_iso:
        return None
    try:
        delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    return int(delta.total_seconds() * 1000)


def _new_job_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"job_{stamp}_{uuid.uuid4().hex[:6]}"


def _safe_ticker(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9._-]", "_", str(ticker).upper().strip())


def persist_report(result: dict[str, Any], report_dir: Path) -> str:
    """把工作流结果落盘为 ``<report_id>.md`` + ``<report_id>.json``，返回 report_id。

    与 ``api.generate_report`` 原先的落盘行为保持一致（同一命名、同一目录约定）。
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_id = f"{_safe_ticker(result.get('ticker') or 'UNKNOWN')}_{timestamp}_{uuid.uuid4().hex[:10]}"
    report_dir.mkdir(parents=True, exist_ok=True)
    # 唯一 ID + 排他创建，避免同秒跨租户报告互相覆盖。
    with (report_dir / f"{report_id}.md").open("x", encoding="utf-8") as handle:
        handle.write(result.get("report") or "")
    with (report_dir / f"{report_id}.json").open("x", encoding="utf-8") as handle:
        handle.write(audit_json(result))
    return report_id


# --- 数据模型 ---------------------------------------------------------------


@dataclass
class StepRecord:
    """单个工作流节点的执行记录。"""

    name: str
    label: str
    status: str = STEP_PENDING
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StepRecord:
        return cls(
            name=str(raw["name"]),
            label=str(raw.get("label") or _STEP_LABELS.get(str(raw["name"]), str(raw["name"]))),
            status=str(raw.get("status") or STEP_PENDING),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            duration_ms=raw.get("duration_ms"),
            error=raw.get("error"),
        )


def _default_steps() -> list[StepRecord]:
    return [StepRecord(name=name, label=label) for name, label in WORKFLOW_STEPS]


@dataclass
class ReportJob:
    """一份报告任务的完整生命周期记录。"""

    job_id: str
    ticker: str
    topic: str
    horizon: str
    requested_by: str
    tenant_id: str | None = None
    actor_id: str | None = None
    status: str = STATUS_QUEUED
    current_step: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    report_id: str | None = None
    error: str | None = None
    tool_error: dict[str, Any] | None = None
    source_errors: list[dict[str, Any]] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=_default_steps)

    @property
    def request_key(self) -> tuple[str | None, str, str, str]:
        return (self.tenant_id, self.ticker, self.topic, self.horizon)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def progress(self) -> int:
        """已完成节点占比（0–100）。"""
        if not self.steps:
            return 0
        done = sum(1 for step in self.steps if step.status == STEP_COMPLETED)
        return round(done / len(self.steps) * 100)

    def step(self, name: str) -> StepRecord | None:
        index = _STEP_INDEX.get(name)
        return self.steps[index] if index is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ticker": self.ticker,
            "topic": self.topic,
            "horizon": self.horizon,
            "requested_by": self.requested_by,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "status": self.status,
            "current_step": self.current_step,
            "progress": self.progress,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "report_id": self.report_id,
            "error": self.error,
            "tool_error": self.tool_error,
            "source_errors": self.source_errors,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReportJob:
        raw_steps = raw.get("steps") or []
        steps = [StepRecord.from_dict(item) for item in raw_steps] or _default_steps()
        return cls(
            job_id=str(raw["job_id"]),
            ticker=str(raw.get("ticker") or ""),
            topic=str(raw.get("topic") or ""),
            horizon=str(raw.get("horizon") or ""),
            requested_by=str(raw.get("requested_by") or "anonymous"),
            tenant_id=str(raw.get("tenant_id") or "") or None,
            actor_id=str(raw.get("actor_id") or "") or None,
            status=str(raw.get("status") or STATUS_QUEUED),
            current_step=raw.get("current_step"),
            created_at=str(raw.get("created_at") or _now()),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            report_id=raw.get("report_id"),
            error=raw.get("error"),
            tool_error=raw.get("tool_error"),
            source_errors=list(raw.get("source_errors") or []),
            steps=steps,
        )


# --- 持久化 -----------------------------------------------------------------


class JobStore:
    """把 ReportJob 以单个 JSON 文件持久化（与 ``data/reports/`` 同风格，零依赖）。"""

    def __init__(self, job_dir: Path | None = None) -> None:
        self._dir = Path(job_dir) if job_dir is not None else JOB_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        return self._dir

    def path(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.match(job_id):
            raise ValueError(f"非法 job_id：{job_id!r}")
        return self._dir / f"{job_id}.json"

    def save(self, job: ReportJob) -> None:
        target = self.path(job.job_id)
        payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2, default=str)
        with self._lock:
            target.write_text(payload, encoding="utf-8")

    def load(self, job_id: str) -> ReportJob | None:
        if not JOB_ID_PATTERN.match(job_id):
            return None
        target = self._dir / f"{job_id}.json"
        if not target.exists():
            return None
        with self._lock:
            raw = json.loads(target.read_text(encoding="utf-8"))
        return ReportJob.from_dict(raw)

    def list(self, limit: int = 30) -> list[ReportJob]:
        bounded = max(1, min(int(limit), 500))
        files = sorted(self._dir.glob("job_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        jobs: list[ReportJob] = []
        for path in files[:bounded]:
            try:
                jobs.append(ReportJob.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError):
                continue
        return jobs


# --- 执行器 -----------------------------------------------------------------


class JobExecutor(ABC):
    """任务执行后端抽象。Phase A 只有单机线程池实现。"""

    @abstractmethod
    def submit(self, job_id: str) -> None:
        """把任务交给执行后端。"""

    def shutdown(self) -> None:  # pragma: no cover - 生命周期钩子
        return None


class LocalThreadExecutor(JobExecutor):
    """单机 ``ThreadPoolExecutor`` 实现，适合 MVP 与演示。

    已知边界（诚实说明）：不跨进程、无持久队列，**服务重启后内存中的在执行任务会中断**。
    中断的任务由 :meth:`JobService.recover_stale_jobs` 标记为 ``failed``，不会永远停在 running。
    将来换 Celery / RQ 只需实现 :class:`JobExecutor`，API 层无需改动。
    """

    def __init__(self, runner: Callable[[str], None], max_workers: int = _JOB_EXECUTOR_MAX_WORKERS) -> None:
        self._runner = runner
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="report-job")
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    def submit(self, job_id: str) -> None:
        with self._lock:
            self._futures[job_id] = self._pool.submit(self._runner, job_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- 服务 -------------------------------------------------------------------


def _start_step(job: ReportJob, index: int) -> None:
    record = job.steps[index]
    record.status = STEP_RUNNING
    record.started_at = _now()
    job.current_step = record.name


def _finish_step(job: ReportJob, node_name: str) -> None:
    index = _STEP_INDEX.get(node_name)
    if index is None:
        return
    record = job.steps[index]
    finished_at = _now()
    record.status = STEP_COMPLETED
    record.completed_at = finished_at
    record.duration_ms = _elapsed_ms(record.started_at, finished_at)
    next_index = index + 1
    if next_index < len(job.steps):
        _start_step(job, next_index)
    else:
        job.current_step = None


def _fail_running_step(job: ReportJob, message: str) -> None:
    for record in job.steps:
        if record.status == STEP_RUNNING:
            record.status = STEP_FAILED
            record.error = message
            record.completed_at = _now()
            record.duration_ms = _elapsed_ms(record.started_at, record.completed_at)
            break


def _abandon_unfinished_steps(job: ReportJob, message: str) -> None:
    """取消时把未完成节点标为 skipped——它不是出错，是被放弃。已完成的节点保持 completed。"""
    for record in job.steps:
        if record.status in (STEP_RUNNING, STEP_PENDING):
            record.status = STEP_SKIPPED
            record.error = message
            record.completed_at = _now()
            if record.started_at:
                record.duration_ms = _elapsed_ms(record.started_at, record.completed_at)



def _source_errors_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """只从当前 job 的最终图状态提取来源错误，不查询进程级健康快照。"""
    errors: list[ToolCallError] = []
    operations = {"market_snapshot": "fetch_market_snapshot", "financial_snapshot": "fetch_financial_snapshot"}
    for key, operation in operations.items():
        value = state.get(key)
        if not isinstance(value, dict) or not value.get("error_code"):
            continue
        try:
            code = SourceErrorCode(str(value["error_code"]))
        except ValueError:
            continue
        errors.append(tool_error(str(value.get("source") or "Yahoo Finance via yfinance"), operation, code, attempts=int(value.get("attempts") or 1), message=str(value.get("error") or "来源数据不可用。")))
    news = state.get("raw_news")
    news_error = getattr(news, "fetch_error", None)
    if news_error is None and getattr(news, "fetch_status", None) == "failed":
        news_error = getattr(news, "error", None)
    if isinstance(news_error, ToolCallError):
        errors.append(news_error)
    elif isinstance(news_error, dict):
        try:
            errors.append(ToolCallError.from_dict(news_error))
        except (TypeError, ValueError):
            pass
    return [error.to_dict() for error in errors]


class JobService:
    """报告任务的创建、执行、查询与取消。

    ``graph_factory`` 可注入，便于测试用假图替换真实工作流（不触发网络与向量库）。
    """

    def __init__(
        self,
        store: JobStore,
        *,
        graph_factory: Callable[[], Any] = create_research_workflow,
        executor: JobExecutor | None = None,
    ) -> None:
        self._store = store
        self._graph_factory = graph_factory
        self._lock = threading.RLock()
        self._active: dict[tuple[str | None, str, str, str], str] = {}
        self._cancel_requested: set[str] = set()
        self._executor = executor or LocalThreadExecutor(self.run_job)

    @property
    def store(self) -> JobStore:
        return self._store

    # -- 对外接口 ----------------------------------------------------------

    def create(self, *, ticker: str, topic: str, horizon: str, requested_by: str = "anonymous", tenant_id: str | None = None, actor_id: str | None = None) -> tuple[ReportJob, bool]:
        """创建任务。返回 ``(job, created)``；``created=False`` 表示复用了执行中的同参任务。"""
        key = (tenant_id, ticker.upper().strip(), topic.strip(), horizon.strip())
        with self._lock:
            existing_id = self._active.get(key)
            if existing_id:
                existing = self._store.load(existing_id)
                if existing is not None and not existing.is_terminal:
                    return existing, False
                self._active.pop(key, None)
            job = ReportJob(
                job_id=_new_job_id(),
                ticker=key[1],
                topic=key[2],
                horizon=key[3],
                requested_by=(requested_by or "anonymous").strip() or "anonymous",
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            self._store.save(job)
            self._active[key] = job.job_id
        self._executor.submit(job.job_id)
        return job, True

    def get(self, job_id: str) -> ReportJob | None:
        return None if not JOB_ID_PATTERN.match(job_id) else self._store.load(job_id)

    def list(self, limit: int = 30) -> list[ReportJob]:
        return self._store.list(limit=limit)

    def cancel(self, job_id: str) -> ReportJob | None:
        """请求取消任务。

        取消是**协作式、在节点边界生效**：正在执行的节点会跑完，其后未开始的节点标为
        ``skipped``。任务已是终态时为空操作（幂等）。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if job.is_terminal:
            return job
        with self._lock:
            self._cancel_requested.add(job_id)
        if job.status == STATUS_QUEUED:
            _abandon_unfinished_steps(job, "任务在开始执行前被取消。")
            job.status = STATUS_CANCELLED
            job.current_step = None
            job.completed_at = _now()
            job.error = "任务在开始执行前被取消。"
            self._store.save(job)
            self._release(job.request_key)
        return job

    def recover_stale_jobs(self) -> list[str]:
        """把上次进程遗留的 queued/running 任务标记为 failed，避免永远卡在 running。"""
        recovered: list[str] = []
        for job in self._store.list(limit=500):
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.status = STATUS_FAILED
                job.error = "服务重启，任务已中断（LocalThreadExecutor 不跨进程恢复）。"
                job.completed_at = _now()
                job.current_step = None
                for record in job.steps:
                    if record.status == STEP_RUNNING:
                        record.status = STEP_FAILED
                        record.error = job.error
                        record.completed_at = job.completed_at
                self._store.save(job)
                recovered.append(job.job_id)
        return recovered

    def shutdown(self) -> None:  # pragma: no cover - 生命周期钩子
        self._executor.shutdown()

    # -- 内部 --------------------------------------------------------------

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    def _release(self, key: tuple[str | None, str, str, str]) -> None:
        with self._lock:
            current = self._active.get(key)
            if current:
                self._cancel_requested.discard(current)
                self._active.pop(key, None)

    def run_job(self, job_id: str) -> None:
        """任务执行入口，由 :class:`JobExecutor` 调用。

        同步执行整条工作流；进度在节点边界写回存储。异常一律兜住并落进任务记录，
        不向上抛出（否则线程池会静默吞掉失败原因）。
        """
        job = self._store.load(job_id)
        if job is None:
            return
        if job.status == STATUS_CANCELLED or self._is_cancelled(job_id):
            self._release(job.request_key)
            return

        key = job.request_key
        job.status = STATUS_RUNNING
        job.started_at = _now()
        _start_step(job, 0)
        self._store.save(job)

        initial_state = {
            "ticker": job.ticker,
            "topic": job.topic,
            "horizon": job.horizon,
            "report_mode": "auto",
        }
        final_state: dict[str, Any] = {}
        try:
            graph = self._graph_factory()
            for mode, payload in graph.stream(initial_state, stream_mode=["updates", "values"]):
                if self._is_cancelled(job_id):
                    raise _JobCancelled
                if mode == "updates" and isinstance(payload, dict):
                    for node_name in payload:
                        _finish_step(job, str(node_name))
                        self._store.save(job)
                elif mode == "values" and isinstance(payload, dict):
                    final_state = payload
        except _JobCancelled:
            _abandon_unfinished_steps(job, "任务在执行中被取消，该步骤未执行。")
            job.status = STATUS_CANCELLED
            job.current_step = None
            job.completed_at = _now()
            job.error = "任务在执行中被取消。"
            self._store.save(job)
            self._release(key)
            return
        except Exception as exc:  # noqa: BLE001 - 任务边界必须兜住任何节点异常
            message = f"{type(exc).__name__}: {exc}"
            structured_error = getattr(exc, "error", None)
            job.tool_error = structured_error.to_dict() if hasattr(structured_error, "to_dict") else None
            _fail_running_step(job, message)
            job.status = STATUS_FAILED
            job.error = message
            job.current_step = None
            job.completed_at = _now()
            self._store.save(job)
            self._release(key)
            return

        job.source_errors = _source_errors_from_state(final_state)
        try:
            final_state["tenant_id"] = job.tenant_id
            final_state["actor_id"] = job.actor_id
            job.report_id = persist_report(final_state, REPORT_DIR)
        except Exception as exc:  # noqa: BLE001 - 落盘失败也要落进任务记录
            job.status = STATUS_FAILED
            job.tool_error = None
            job.error = f"报告落盘失败：{type(exc).__name__}: {exc}"
            _fail_running_step(job, job.error)
        else:
            job.status = STATUS_COMPLETED
        job.current_step = None
        job.completed_at = _now()
        self._store.save(job)
        self._release(key)
