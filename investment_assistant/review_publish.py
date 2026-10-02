"""R6 人工审核与发布门禁：版本绑定的审核记录、发布状态与职责分离。

核心规则
--------
* 支持状态、生成状态、审核状态、发布状态是四个不同维度；``completed`` ≠ ``released``。
* 审核基于当前报告 Markdown + JSON 的字节 SHA、claim 集版本、tenant、report_id；
  任何字节变化后旧审核失效（binding mismatch）。
* 职责分离：创建人不能审核自己的报告，审核人不能发布，发布人不能是创建人。
* 无完整 claim 覆盖（无机读 claims 或存在未通过锚点）时不能发布；
  人工逐条审阅记录 reviewer、时间、决定、理由和证据版本，不把系统校验误称为零幻觉。
* 拒绝、撤回、重审与并发重复动作按确定性规则处理（同参数幂等，冲突显式 409）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

REVIEW_DIR = DATA_DIR / "review_records"
PUBLISH_DIR = DATA_DIR / "publish_state"

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"

PUBLISH_NOT_PUBLISHED = "not_published"
PUBLISH_PUBLISHED = "published"
PUBLISH_WITHDRAWN = "withdrawn"

_lock = threading.Lock()

_REPORT_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


# --- 版本绑定 -------------------------------------------------------------------


def report_binding(
    report_dir: Path,
    report_id: str,
    *,
    audit: dict[str, Any],
    material_index: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """计算当前报告版本绑定：md/json 字节 SHA + claim 集版本 + 报告引用资料 SHA。"""
    report_dir = Path(report_dir)
    md_bytes = (report_dir / f"{report_id}.md").read_bytes()
    json_bytes = (report_dir / f"{report_id}.json").read_bytes()
    claims = audit.get("claims") if isinstance(audit.get("claims"), list) else []
    claim_set_digest = hashlib.sha256(
        json.dumps(claims, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    material_shas: dict[str, str] = {}
    for source in audit.get("sources") or []:
        metadata = (source.get("metadata") or {}) if isinstance(source, dict) else {}
        file_name = str(metadata.get("file_name") or "").strip()
        if not file_name:
            continue
        entry = (material_index or {}).get(file_name)
        if not entry:
            raise ValueError("报告引用资料未在白名单中。")
        path = Path(entry.get("path") or "")
        if not path.is_file():
            raise ValueError("报告引用资料缺失。")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            raise ValueError("资料实际字节与白名单 SHA 不一致。")
        material_shas[file_name] = digest
    return {
        "tenant_id": audit.get("tenant_id"),
        "report_id": report_id,
        "report_md_sha256": hashlib.sha256(md_bytes).hexdigest(),
        "report_json_sha256": hashlib.sha256(json_bytes).hexdigest(),
        "claim_set_sha256": claim_set_digest,
        "claims_total": len(claims),
        "material_shas": material_shas,
    }


def binding_matches(binding: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = ("report_md_sha256", "report_json_sha256", "claim_set_sha256", "claims_total", "material_shas", "tenant_id", "report_id")
    return all(binding.get(key) == current.get(key) for key in keys)


# --- 审核记录 -------------------------------------------------------------------


@dataclass
class ReviewRecord:
    review_id: str
    report_id: str
    tenant_id: str
    reviewer_id: str
    decision: str
    reason: str
    reviewed_at: str
    binding: dict[str, Any] = field(default_factory=dict)
    claim_decisions: dict[str, str] = field(default_factory=dict)
    coverage_attested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "report_id": self.report_id,
            "tenant_id": self.tenant_id,
            "reviewer_id": self.reviewer_id,
            "decision": self.decision,
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
            "binding": self.binding,
            "claim_decisions": self.claim_decisions,
            "coverage_attested": self.coverage_attested,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReviewRecord:
        return cls(
            review_id=str(raw.get("review_id") or ""),
            report_id=str(raw.get("report_id") or ""),
            tenant_id=str(raw.get("tenant_id") or ""),
            reviewer_id=str(raw.get("reviewer_id") or ""),
            decision=str(raw.get("decision") or ""),
            reason=str(raw.get("reason") or ""),
            reviewed_at=str(raw.get("reviewed_at") or ""),
            binding=dict(raw.get("binding") or {}),
            claim_decisions=dict(raw.get("claim_decisions") or {}),
            coverage_attested=raw.get("coverage_attested") is True,
        )


def _review_path(report_id: str) -> Path:
    if not _REPORT_ID.match(report_id):
        raise ValueError(f"非法 report_id：{report_id!r}")
    return REVIEW_DIR / f"{report_id}.json"


def load_review_records(report_id: str) -> list[ReviewRecord]:
    path = _review_path(report_id)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("审核记录不可读取。") from exc
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("审核记录格式损坏。")
    return [ReviewRecord.from_dict(item) for item in raw if isinstance(item, dict)]


def save_review_record(record: ReviewRecord) -> ReviewRecord:
    """保存审核记录；同 reviewer + 同决定 + 同 binding 视为幂等重复，返回既有记录。"""
    with _lock:
        existing = load_review_records(record.report_id)
        publication = load_publish_record(record.report_id)
        for item in existing:
            if (
                item.reviewer_id == record.reviewer_id
                and item.decision == record.decision
                and binding_matches(record.binding, item.binding)
                and item.claim_decisions == record.claim_decisions
                and item.coverage_attested == record.coverage_attested
                and item.reason == record.reason
                and existing[-1].review_id == item.review_id
                and not (publication and publication.withdrawn_at and publication.review_id == item.review_id)
            ):
                return item
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        records = [item.to_dict() for item in existing]
        records.append(record.to_dict())
        target = _review_path(record.report_id)
        temporary = target.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    return record


def effective_review(report_id: str, current_binding: dict[str, Any]) -> ReviewRecord | None:
    """返回仍绑定当前报告版本的最新 approved 审核；版本漂移后返回 None。"""
    for record in reversed(load_review_records(report_id)):
        if binding_matches(record.binding, current_binding):
            return record if record.decision == REVIEW_APPROVED and record.coverage_attested else None
    return None


# --- 发布状态 -------------------------------------------------------------------


@dataclass
class PublishRecord:
    report_id: str
    tenant_id: str
    publisher_id: str
    published_at: str
    withdrawn_at: str | None = None
    binding: dict[str, Any] = field(default_factory=dict)
    review_id: str | None = None

    @property
    def status(self) -> str:
        return PUBLISH_WITHDRAWN if self.withdrawn_at else PUBLISH_PUBLISHED

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "tenant_id": self.tenant_id,
            "publisher_id": self.publisher_id,
            "published_at": self.published_at,
            "withdrawn_at": self.withdrawn_at,
            "publish_status": self.status,
            "binding": self.binding,
            "review_id": self.review_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PublishRecord:
        return cls(
            report_id=str(raw.get("report_id") or ""),
            tenant_id=str(raw.get("tenant_id") or ""),
            publisher_id=str(raw.get("publisher_id") or ""),
            published_at=str(raw.get("published_at") or ""),
            withdrawn_at=raw.get("withdrawn_at"),
            binding=dict(raw.get("binding") or {}),
            review_id=raw.get("review_id"),
        )


def _publish_path(report_id: str) -> Path:
    if not _REPORT_ID.match(report_id):
        raise ValueError(f"非法 report_id：{report_id!r}")
    return PUBLISH_DIR / f"{report_id}.json"


def load_publish_record(report_id: str) -> PublishRecord | None:
    path = _publish_path(report_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("发布状态不可读取。") from exc
    if not isinstance(raw, dict):
        raise ValueError("发布状态格式损坏。")
    return PublishRecord.from_dict(raw)


def _write_publish_record(record: PublishRecord) -> PublishRecord:
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    target = _publish_path(record.report_id)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    return record


def publish_report(
    *,
    report_id: str,
    tenant_id: str,
    publisher_id: str,
    binding: dict[str, Any],
    review_id: str,
) -> tuple[PublishRecord, bool]:
    """发布报告。确定性处理：
    * 已发布且 binding 未变 → 幂等返回既有记录（created=False）；
    * 已撤回或 binding 变化（旧发布失效）→ 写入新发布记录。
    """
    with _lock:
        existing = load_publish_record(report_id)
        if (
            existing is not None
            and existing.status == PUBLISH_PUBLISHED
            and binding_matches(existing.binding, binding)
            and existing.review_id == review_id
            and existing.tenant_id == tenant_id
        ):
            return existing, False
        record = PublishRecord(
            report_id=report_id,
            tenant_id=tenant_id,
            publisher_id=publisher_id,
            published_at=_now_iso(),
            withdrawn_at=None,
            binding=dict(binding),
            review_id=review_id,
        )
        _write_publish_record(record)
        return record, True


def withdraw_report(report_id: str, *, publisher_id: str) -> PublishRecord | None:
    """撤回发布。未发布/已撤回为幂等空操作；返回当前状态记录。"""
    with _lock:
        existing = load_publish_record(report_id)
        if existing is None or existing.status == PUBLISH_WITHDRAWN:
            return existing
        existing.withdrawn_at = _now_iso()
        existing.publisher_id = publisher_id or existing.publisher_id
        _write_publish_record(existing)
        return existing


def publish_is_effective(
    report_dir: Path,
    report_id: str,
    *,
    audit: dict[str, Any],
    material_index: dict[str, dict[str, str]] | None = None,
) -> tuple[PublishRecord | None, str | None]:
    """返回 ``(生效发布记录, 失效原因)``。

    发布记录存在、未撤回、且 binding 与当前报告字节一致才算仍发布；
    报告任何字节变化后旧发布自动失效。
    """
    record = load_publish_record(report_id)
    if record is None:
        return None, "not_published"
    if record.status == PUBLISH_WITHDRAWN:
        return None, "withdrawn"
    current = report_binding(report_dir, report_id, audit=audit, material_index=material_index)
    if not binding_matches(record.binding, current):
        return None, "version_drift"
    return record, None
