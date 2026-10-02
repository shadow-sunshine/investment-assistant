"""R7 版本监控与受控研究记忆：watchlist、有限期可撤销记忆与同标的版本对比。

边界
----
* watchlist 只针对白名单本地资料 manifest（``materials_manifest.json``）里的文件做
  显式触发（手动/受控任务）检查；不做后台自动轮询，不访问真实网络。
* SHA 漂移或资料不可用 → ``needs_review`` / ``source_unavailable``，绝不补成肯定的
  “结论变化”。
* 研究记忆仅保存可撤销、有限期、版本绑定的摘要；每条有 tenant、owner、report_id、
  绑定版本与到期时间。过期、撤回、跨租户、报告版本漂移时拒绝注入问答。
* 结论对比只在同 ticker 且两个仍发布的版本之间运行；期间/单位不一致时给出无法比较
  原因，不自动给出买卖建议。
* R0/R3 的离线检索实验结果与生产监控效果无关。
"""

from __future__ import annotations

import json
import os
import math
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

WATCHLIST_PATH = DATA_DIR / "watchlists.json"
MEMORY_PATH = DATA_DIR / "research_memories.json"

WATCH_UNCHANGED = "unchanged"
WATCH_NEEDS_REVIEW = "needs_review"
WATCH_SOURCE_UNAVAILABLE = "source_unavailable"

MEMORY_ACTIVE = "active"
MEMORY_EXPIRED = "expired"
MEMORY_REPORT_GONE = "report_unavailable"
MEMORY_VERSION_DRIFT = "version_drift"
MEMORY_WITHDRAWN = "report_withdrawn"

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _valid_expiry(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > datetime.now(UTC)


# --- JSON 存储基元 ---------------------------------------------------------------


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("研究状态存储不可读取。") from exc
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("研究状态存储格式损坏。")
    return raw


def _write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


# --- Watchlist -------------------------------------------------------------------


@dataclass
class WatchEntry:
    watch_id: str
    tenant_id: str
    owner_id: str
    name: str
    file_names: list[str]
    baseline: dict[str, str]
    created_at: str
    last_check: dict[str, Any] | None = None
    report_baseline: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WatchEntry:
        return cls(
            watch_id=str(raw.get("watch_id") or ""),
            tenant_id=str(raw.get("tenant_id") or ""),
            owner_id=str(raw.get("owner_id") or ""),
            name=str(raw.get("name") or ""),
            file_names=[str(item) for item in raw.get("file_names") or []],
            baseline={str(k): str(v) for k, v in (raw.get("baseline") or {}).items()},
            created_at=str(raw.get("created_at") or ""),
            last_check=raw.get("last_check") if isinstance(raw.get("last_check"), dict) else None,
            report_baseline=dict(raw.get("report_baseline") or {}),
        )


def create_watch_entry(
    *,
    tenant_id: str,
    owner_id: str,
    name: str,
    file_names: list[str],
    material_index: dict[str, dict[str, str]],
    report_bindings: dict[str, dict[str, Any]] | None = None,
) -> tuple[WatchEntry, bool]:
    """创建 watchlist 条目；file_names 必须都在白名单 manifest 内，baseline 取 manifest SHA。"""
    normalized = list(dict.fromkeys(str(item).strip() for item in file_names if str(item).strip()))
    unknown = [item for item in normalized if item not in material_index]
    if (not normalized and not report_bindings) or unknown:
        raise ValueError(f"watchlist 只允许白名单本地资料文件，未知文件：{'、'.join(unknown) if unknown else '(空)'}")
    baseline = {}
    for item in normalized:
        actual = hashlib_file_sha256(Path(material_index[item]["path"]))
        if actual is None or actual != material_index[item]["sha256"]:
            raise ValueError("白名单资料缺失或实际 SHA 漂移，不能创建可信基线。")
        baseline[item] = actual
    entry = WatchEntry(
        watch_id=_new_id("watch"),
        tenant_id=tenant_id,
        owner_id=owner_id,
        name=str(name or "").strip() or "未命名关注清单",
        file_names=normalized,
        baseline=baseline,
        created_at=_now_iso(),
        report_baseline=dict(report_bindings or {}),
    )
    with _lock:
        rows = _load_json(WATCHLIST_PATH)
        rows.append(entry.to_dict())
        _write_json(WATCHLIST_PATH, rows)
    return entry, True


def list_watch_entries(*, tenant_id: str, owner_id: str | None = None) -> list[WatchEntry]:
    rows = [WatchEntry.from_dict(item) for item in _load_json(WATCHLIST_PATH)]
    return [
        item
        for item in rows
        if item.tenant_id == tenant_id and (owner_id is None or item.owner_id == owner_id)
    ]


def get_watch_entry(watch_id: str, *, tenant_id: str) -> WatchEntry | None:
    if not _ID.match(watch_id or ""):
        return None
    for item in list_watch_entries(tenant_id=tenant_id):
        if item.watch_id == watch_id:
            return item
    return None


def delete_watch_entry(watch_id: str, *, tenant_id: str, owner_id: str) -> bool:
    with _lock:
        rows = _load_json(WATCHLIST_PATH)
        kept = []
        deleted = False
        for item in rows:
            entry = WatchEntry.from_dict(item)
            if entry.watch_id == watch_id and entry.tenant_id == tenant_id and entry.owner_id == owner_id:
                deleted = True
                continue
            kept.append(item)
        if deleted:
            _write_json(WATCHLIST_PATH, kept)
        return deleted


def check_watch_entry(entry: WatchEntry, material_index: dict[str, dict[str, str]],
                      report_bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """显式触发一次版本检查；不更新 baseline（需要人工确认后另行重置）。

    幂等：同一状态重复检查产生同一结果；并发重复调用由调用方串行化即可。
    """
    diff: list[dict[str, Any]] = []
    error: str | None = None
    for file_name in entry.file_names:
        record = material_index.get(file_name)
        if record is None:
            diff.append({"file_name": file_name, "change": "missing", "detail": "文件已不在白名单 manifest 中。"})
            continue
        path = Path(record.get("path") or "")
        try:
            if not path.is_file():
                diff.append({"file_name": file_name, "change": "missing", "detail": "资料文件缺失。"})
                continue
            digest = hashlib_file_sha256(path)
            if digest is None:
                diff.append({"file_name": file_name, "change": "unreadable", "detail": "资料文件不可读。"})
                continue
        except OSError:
            diff.append({"file_name": file_name, "change": "unreadable", "detail": "资料文件不可读。"})
            continue
        if digest != record.get("sha256"):
            diff.append({"file_name": file_name, "change": "manifest_drift", "detail": "资料当前字节与白名单 SHA 不一致。"})
            continue
        baseline = entry.baseline.get(file_name)
        if baseline is None:
            diff.append({"file_name": file_name, "change": "new", "detail": "baseline 未记录该文件。"})
        elif digest != baseline:
            diff.append({"file_name": file_name, "change": "modified", "detail": "SHA 与 baseline 不一致。"})
    for report_id, baseline in entry.report_baseline.items():
        current = (report_bindings or {}).get(report_id)
        if current is None:
            diff.append({"report_id": report_id, "change": "report_unavailable", "detail": "已发布报告不存在或已失效。"})
        elif current != baseline:
            diff.append({"report_id": report_id, "change": "report_modified", "detail": "已发布报告版本发生变化，待人工复核。"})
    if any(item["change"] in {"missing", "unreadable", "report_unavailable"} for item in diff):
        status = WATCH_SOURCE_UNAVAILABLE
        error = "一个或多个资料缺失或不可读，无法判定版本变化。"
    elif any(item["change"] in {"modified", "new", "manifest_drift", "report_modified"} for item in diff):
        status = WATCH_NEEDS_REVIEW
        error = None
    else:
        status = WATCH_UNCHANGED
    result = {
        "checked_at": _now_iso(),
        "watch_id": entry.watch_id,
        "status": status,
        "diff": diff,
        "error": error,
    }
    with _lock:
        rows = _load_json(WATCHLIST_PATH)
        for index, item in enumerate(rows):
            candidate = WatchEntry.from_dict(item)
            if candidate.watch_id == entry.watch_id and candidate.tenant_id == entry.tenant_id:
                previous = candidate.last_check
                if previous and previous.get("status") == status and previous.get("diff") == diff:
                    return previous
                candidate.last_check = result
                rows[index] = candidate.to_dict()
                _write_json(WATCHLIST_PATH, rows)
                break
    return result


def reset_watch_baseline(watch_id: str, *, tenant_id: str, owner_id: str,
                         material_index: dict[str, dict[str, str]],
                         report_bindings: dict[str, dict[str, Any]] | None = None) -> WatchEntry | None:
    """人工确认当前资料版本后，把 baseline 重置为 manifest 当前 SHA。"""
    with _lock:
        rows = _load_json(WATCHLIST_PATH)
        for index, item in enumerate(rows):
            entry = WatchEntry.from_dict(item)
            if entry.watch_id == watch_id and entry.tenant_id == tenant_id and entry.owner_id == owner_id:
                fresh = {}
                for name in entry.file_names:
                    if name not in material_index:
                        raise ValueError("资料缺失，不能重置基线。")
                    digest = hashlib_file_sha256(Path(material_index[name]["path"]))
                    if digest is None or digest != material_index[name]["sha256"]:
                        raise ValueError("资料实际字节与 manifest 不一致，不能重置基线。")
                    fresh[name] = digest
                entry.baseline = fresh
                if report_bindings is not None:
                    entry.report_baseline = dict(report_bindings)
                entry.last_check = None
                rows[index] = entry.to_dict()
                _write_json(WATCHLIST_PATH, rows)
                return entry
    return None


def hashlib_file_sha256(path: Path) -> str | None:
    try:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# --- 研究记忆 ---------------------------------------------------------------------


@dataclass
class MemoryEntry:
    memory_id: str
    tenant_id: str
    owner_id: str
    report_id: str
    ticker: str
    content: str
    created_at: str
    expires_at: str
    binding: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MemoryEntry:
        return cls(
            memory_id=str(raw.get("memory_id") or ""),
            tenant_id=str(raw.get("tenant_id") or ""),
            owner_id=str(raw.get("owner_id") or ""),
            report_id=str(raw.get("report_id") or ""),
            ticker=str(raw.get("ticker") or ""),
            content=str(raw.get("content") or ""),
            created_at=str(raw.get("created_at") or ""),
            expires_at=str(raw.get("expires_at") or ""),
            binding=dict(raw.get("binding") or {}),
        )


def create_memory_entry(
    *,
    tenant_id: str,
    owner_id: str,
    report_id: str,
    ticker: str,
    content: str,
    expires_at: str,
    binding: dict[str, Any],
) -> MemoryEntry:
    if not str(content or "").strip():
        raise ValueError("研究记忆内容不能为空。")
    if not _valid_expiry(expires_at):
        raise ValueError("研究记忆必须提供有效的未来到期时间（有限期）。")
    entry = MemoryEntry(
        memory_id=_new_id("mem"),
        tenant_id=tenant_id,
        owner_id=owner_id,
        report_id=report_id,
        ticker=ticker,
        content=str(content).strip(),
        created_at=_now_iso(),
        expires_at=str(expires_at),
        binding=dict(binding),
    )
    with _lock:
        rows = _load_json(MEMORY_PATH)
        rows.append(entry.to_dict())
        _write_json(MEMORY_PATH, rows)
    return entry


def list_memory_entries(*, tenant_id: str, owner_id: str | None = None, report_id: str | None = None) -> list[MemoryEntry]:
    rows = [MemoryEntry.from_dict(item) for item in _load_json(MEMORY_PATH)]
    return [
        item
        for item in rows
        if item.tenant_id == tenant_id
        and (owner_id is None or item.owner_id == owner_id)
        and (report_id is None or item.report_id == report_id)
    ]


def get_memory_entry(memory_id: str, *, tenant_id: str) -> MemoryEntry | None:
    if not _ID.match(memory_id or ""):
        return None
    for item in list_memory_entries(tenant_id=tenant_id):
        if item.memory_id == memory_id:
            return item
    return None


def delete_memory_entry(memory_id: str, *, tenant_id: str, owner_id: str) -> bool:
    """显式删除（可撤销）；owner 或同租户 admin 可删，其余拒绝。"""
    with _lock:
        rows = _load_json(MEMORY_PATH)
        kept = []
        deleted = False
        for item in rows:
            entry = MemoryEntry.from_dict(item)
            if entry.memory_id == memory_id and entry.tenant_id == tenant_id and entry.owner_id == owner_id:
                deleted = True
                continue
            kept.append(item)
        if deleted:
            _write_json(MEMORY_PATH, kept)
        return deleted


def update_memory_entry(memory_id: str, *, tenant_id: str, owner_id: str,
                        content: str, expires_at: str, binding: dict[str, Any]) -> MemoryEntry | None:
    if not content.strip() or not _valid_expiry(expires_at):
        raise ValueError("记忆内容或期限无效。")
    with _lock:
        rows = _load_json(MEMORY_PATH)
        for index, raw in enumerate(rows):
            entry = MemoryEntry.from_dict(raw)
            if entry.memory_id == memory_id and entry.tenant_id == tenant_id and entry.owner_id == owner_id:
                entry.content = content.strip()
                entry.expires_at = expires_at
                entry.binding = dict(binding)
                rows[index] = entry.to_dict()
                _write_json(MEMORY_PATH, rows)
                return entry
    return None


def memory_injectable(
    entry: MemoryEntry,
    *,
    now: datetime | None = None,
    publish_effective: bool = False,
    current_binding: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """判断一条记忆能否注入问答：未过期 + 报告仍发布 + 报告版本未漂移。"""
    expiry = entry.expires_at
    try:
        expiry_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError:
        return False, MEMORY_EXPIRED
    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    if expiry_dt <= moment:
        return False, MEMORY_EXPIRED
    if not publish_effective:
        return False, MEMORY_WITHDRAWN
    if current_binding is None or not entry.binding or entry.binding != current_binding:
        return False, MEMORY_VERSION_DRIFT
    return True, MEMORY_ACTIVE


# --- 同标的已发布版本对比 -----------------------------------------------------------


_COMPARABLE_FIELDS = (
    ("revenue", "revenue_period_end"),
    ("net_income", "net_income_period_end"),
    ("free_cash_flow", "free_cash_flow_period_end"),
)


def compare_published_reports(
    left_audit: dict[str, Any],
    right_audit: dict[str, Any],
) -> dict[str, Any]:
    """同 ticker、两个已发布版本的**事实差异**对比；期间/单位不一致时拒绝肯定比较。"""
    left_ticker = str(left_audit.get("ticker") or "").upper().strip()
    right_ticker = str(right_audit.get("ticker") or "").upper().strip()
    if not left_ticker or left_ticker != right_ticker:
        return {"status": "cannot_compare", "reasons": ["两个报告标的不同，拒绝比较。"], "fields": []}
    fields: list[dict[str, Any]] = []
    reasons: list[str] = []
    for key, period_key in _COMPARABLE_FIELDS:
        left_group = left_audit.get("financial_snapshot") or {}
        right_group = right_audit.get("financial_snapshot") or {}
        left_value = left_group.get(key)
        right_value = right_group.get(key)
        row: dict[str, Any] = {"field": key, "left": left_value, "right": right_value,
                               "left_source": left_group.get("source") or "来源未登记",
                               "right_source": right_group.get("source") or "来源未登记"}
        if left_group.get("data_available") is not True or right_group.get("data_available") is not True:
            row["status"] = "cannot_compare"
            row["reason"] = "一侧数据源不可用，不能用残留快照生成事实变化。"
            row["left"] = None
            row["right"] = None
            reasons.append(f"{key}: 数据源不可用")
        elif left_value is None or right_value is None:
            row["status"] = "cannot_compare"
            row["reason"] = "一侧报告缺少该字段，无法比较。"
            reasons.append(f"{key}: 缺字段")
        else:
            left_period = left_group.get(period_key)
            right_period = right_group.get(period_key)
            left_unit = str(left_group.get("currency") or "").strip()
            right_unit = str(right_group.get("currency") or "").strip()
            row["left_period"] = left_period
            row["right_period"] = right_period
            row["left_unit"] = left_unit
            row["right_unit"] = right_unit
            if not left_unit or not right_unit or not left_period or not right_period:
                row["status"] = "cannot_compare"
                row["reason"] = "期间或单位未登记，不能肯定比较。"
                reasons.append(f"{key}: 期间或单位缺失")
            elif left_unit != right_unit:
                row["status"] = "cannot_compare"
                row["reason"] = f"单位不一致（{left_unit or '未登记'} vs {right_unit or '未登记'}）。"
                reasons.append(f"{key}: 单位不一致")
            elif left_period != right_period:
                row["status"] = "cannot_compare"
                row["reason"] = f"期间不一致（{left_period or '未登记'} vs {right_period or '未登记'}），不是同一期间的事实。"
                reasons.append(f"{key}: 期间不一致")
            else:
                try:
                    if isinstance(left_value, bool) or isinstance(right_value, bool):
                        raise ValueError("布尔值不是财务数值。")
                    delta = float(right_value) - float(left_value)
                    if not math.isfinite(delta):
                        raise ValueError("财务数值必须有限。")
                    row["status"] = "compared"
                    row["delta"] = delta
                    row["direction"] = "increase" if delta > 0 else ("decrease" if delta < 0 else "unchanged")
                except (TypeError, ValueError):
                    row["status"] = "cannot_compare"
                    row["reason"] = "数值不可解析或不是有限数字。"
                    for side in ("left", "right"):
                        if isinstance(row.get(side), float) and not math.isfinite(row[side]):
                            row[side] = None
                    reasons.append(f"{key}: 数值不可解析")
        fields.append(row)
    status = "compared" if fields and all(item["status"] == "compared" for item in fields) else "cannot_compare"
    return {
        "status": status,
        "ticker": left_ticker,
        "fields": fields,
        "reasons": reasons,
        "note": "仅同 ticker 且两个仍发布版本之间的事实差异；期间或单位不一致时只给出原因，不做任何投资建议。",
    }
