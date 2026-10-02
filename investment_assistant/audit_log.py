"""R6 审计留痕：受保护动作的持久化记录层。

* 记录 actor、tenant、action、target、result、timestamp、版本与错误码；
* 追加写 + 进程内锁保证并发安全；不写入 token 与未发布报告正文；
* JSONL 追加日志可追溯，但**不声称外部不可篡改**；
* 配置不全（目录不可创建）时 fail-closed 抛错，不静默丢审计。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

AUDIT_LOG_DIR = DATA_DIR / "audit_log"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_audit_event(
    *,
    actor_id: str | None,
    tenant_id: str | None,
    action: str,
    target: str,
    result: str,
    error_code: str | None = None,
    version: str | None = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """追加一条审计事件并返回它。``result`` 为 ``allow`` / ``deny``。

    调用方必须保证 ``target``/``detail`` 不含 token 与未发布正文；本函数再做一次
    长度截断防御，绝不写入凭证类字段。
    """
    directory = Path(log_dir) if log_dir is not None else AUDIT_LOG_DIR
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "actor_id": str(actor_id or "anonymous"),
        "tenant_id": str(tenant_id or "-"),
        "action": str(action)[:80],
        "target": str(target)[:200],
        "result": str(result)[:16],
        "error_code": str(error_code)[:60] if error_code else None,
        "version": str(version)[:120] if version else None,
    }
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    # 单行 O_APPEND 追加 + 进程内锁；多进程部署需换专用日志后端（诚实边界）。
    with _lock:
        with open(directory / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return entry


def read_audit_events(log_dir: Path | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """读取最近的审计事件（新在前）；文件缺失返回空列表。"""
    directory = Path(log_dir) if log_dir is not None else AUDIT_LOG_DIR
    target = directory / "audit.jsonl"
    if not target.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    bounded = max(1, min(int(limit), 1000))
    return list(reversed(events[-bounded:]))
