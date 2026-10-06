"""会话、消息、短期状态、摘要与长期画像的事务存储（Python 标准库 SQLite）。

边界说明
--------
* 单一 SQLite 文件，避免多JSON 之间半写入、并发覆盖与删除不一致；不引入外部数据库服务。
* **归属只能来自服务端 Principal**：所有读写函数都要求 ``tenant_id`` + ``owner_id``，
  调用方无法只凭 ``conversation_id`` 越权读取。归属不匹配一律返回 ``None``/``False``，
  由API 层转成404，不泄露资源存在性。
* 删除会话在**一个事务**内删除消息、状态、摘要与本会话待确认项；长期画像不随之删除。
* 画像删除是**软删除 + 撤销标记**：历史消息仍可展示，但后续摘要/画像提取必须跳过已撤销字段，
  避免旧摘要把已撤销偏好"复活"。
* ``request_id`` 唯一约束用于防重复提交；消息按 ``(conversation_id, seq)`` 唯一，
  迟到回答只能写回它自己的 conversation_id。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR

#: 默认存储位置；测试可通过``ConversationStore(tmp_path / "x.sqlite3")`` 隔离。
DEFAULT_DB_PATH = DATA_DIR / "conversation_memory.sqlite3"

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    title_source    TEXT NOT NULL DEFAULT 'auto',
    answer_mode     TEXT NOT NULL DEFAULT 'official',
    state_version   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations(tenant_id, owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    answer_json     TEXT,
    status          TEXT NOT NULL DEFAULT 'completed',
    request_id      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, seq);

-- 防重复提交：同一身份下同一 request_id 只能落一条用户消息。
CREATE TABLE IF NOT EXISTS request_ledger (
    request_id      TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, request_id)
);

CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    state_json      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    covered_through_seq INTEGER NOT NULL,
    state_version     INTEGER NOT NULL,
    summary_json      TEXT NOT NULL,
    summary_type      TEXT NOT NULL,
    degraded_reason   TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_entries (
    entry_id           TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    owner_id           TEXT NOT NULL,
    field              TEXT NOT NULL,
    value              TEXT NOT NULL,
    confirmed          INTEGER NOT NULL DEFAULT 0,
    source_message_id  TEXT,
    source_conversation_id TEXT,
    version            INTEGER NOT NULL DEFAULT 1,
    revoked            INTEGER NOT NULL DEFAULT 0,
    revoked_at         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(tenant_id, owner_id, field)
);
CREATE INDEX IF NOT EXISTS idx_profile_owner
    ON profile_entries(tenant_id, owner_id, revoked);

-- 待确认候选：确认动作必须绑定 candidate_id + version，过期/已替换候选不可重放。
CREATE TABLE IF NOT EXISTS profile_candidates (
    candidate_id         TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    owner_id             TEXT NOT NULL,
    conversation_id      TEXT,
    source_message_id    TEXT,
    field                TEXT NOT NULL,
    proposed_value       TEXT NOT NULL,
    previous_value       TEXT,
    op                   TEXT NOT NULL DEFAULT 'create',
    status               TEXT NOT NULL DEFAULT 'pending',
    version              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_owner
    ON profile_candidates(tenant_id, owner_id, status);
"""


class ConversationStoreError(Exception):
    """存储不可用或数据损坏；由API 层转成明确错误码，不暴露本地路径。"""


class DuplicateRequest(Exception):
    """同一身份下 request_id 重复提交。"""

    def __init__(self, message_id: str) -> None:
        super().__init__("duplicate_request")
        self.message_id = message_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConversationStoreError("会话存储内容损坏。") from exc


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    tenant_id: str
    owner_id: str
    title: str
    title_source: str
    answer_mode: str
    state_version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "title_source": self.title_source,
            "answer_mode": self.answer_mode,
            "state_version": self.state_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class StoredMessage:
    message_id: str
    conversation_id: str
    seq: int
    role: str
    text: str
    answer: dict[str, Any] | None
    status: str
    request_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "seq": self.seq,
            "role": self.role,
            "text": self.text,
            "answer": self.answer,
            "status": self.status,
            "request_id": self.request_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ProfileEntry:
    entry_id: str
    field: str
    value: str
    confirmed: bool
    source_message_id: str | None
    source_conversation_id: str | None
    version: int
    revoked: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "field": self.field,
            "value": self.value,
            "confirmed": self.confirmed,
            "source_message_id": self.source_message_id,
            "source_conversation_id": self.source_conversation_id,
            "version": self.version,
            "revoked": self.revoked,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ProfileCandidate:
    candidate_id: str
    conversation_id: str | None
    source_message_id: str | None
    field: str
    proposed_value: str
    previous_value: str | None
    op: str
    status: str
    version: int
    created_at: str
    resolved_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "conversation_id": self.conversation_id,
            "source_message_id": self.source_message_id,
            "field": self.field,
            "proposed_value": self.proposed_value,
            "previous_value": self.previous_value,
            "op": self.op,
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class ConversationStore:
    """单文件 SQLite 存储；线程安全由一把可重入锁 + 每连接独立事务保证。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_DB_PATH
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False``：Streamlit 与 FastAPI 在不同线程访问同一存储。
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_schema(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._connection.executescript(_SCHEMA)
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
        except sqlite3.Error as exc:
            raise ConversationStoreError("会话存储不可用。") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务边界：要么整组写入生效，要么整体回滚。"""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # --- 会话 -----------------------------------------------------------------

    def create_conversation(self, *, tenant_id: str, owner_id: str, title: str = "",
                            answer_mode: str = "official") -> Conversation:
        if not tenant_id or not owner_id:
            raise ConversationStoreError("会话归属不能为空。")
        moment = _now_iso()
        conversation = Conversation(
            conversation_id=_new_id("conv"),
            tenant_id=str(tenant_id),
            owner_id=str(owner_id),
            title=str(title or "").strip(),
            title_source="manual" if str(title or "").strip() else "auto",
            answer_mode=str(answer_mode or "official"),
            state_version=0,
            created_at=moment,
            updated_at=moment,
        )
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO conversations(conversation_id, tenant_id, owner_id, title, title_source,"
                " answer_mode, state_version, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (conversation.conversation_id, conversation.tenant_id, conversation.owner_id,
                 conversation.title, conversation.title_source, conversation.answer_mode,
                 conversation.state_version, conversation.created_at, conversation.updated_at),
            )
        return conversation

    def get_conversation(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> Conversation | None:
        """归属不匹配返回 ``None``；调用方不得据此区分"不存在"和"不属于你"。"""
        if not conversation_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
        return self._conversation_from_row(row)

    def list_conversations(self, *, tenant_id: str, owner_id: str, limit: int = 20,
                           offset: int = 0) -> list[Conversation]:
        """按更新时间倒序返回本人会话；分页由调用方控制。"""
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM conversations WHERE tenant_id=? AND owner_id=?"
                " ORDER BY updated_at DESC, conversation_id DESC LIMIT ? OFFSET ?",
                (str(tenant_id), str(owner_id), bounded_limit, bounded_offset),
            ).fetchall()
        return [self._conversation_from_row(row) for row in rows]

    def count_conversations(self, *, tenant_id: str, owner_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM conversations WHERE tenant_id=? AND owner_id=?",
                (str(tenant_id), str(owner_id)),
            ).fetchone()
        return int(row["total"]) if row else 0

    def rename_conversation(self, conversation_id: str, title: str, *, tenant_id: str,
                            owner_id: str) -> Conversation | None:
        normalized = str(title or "").strip()
        if not normalized:
            return None
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title=?, title_source='manual', updated_at=?"
                " WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (normalized[:120], _now_iso(), str(conversation_id), str(tenant_id), str(owner_id)),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id)

    def set_answer_mode(self, conversation_id: str, answer_mode: str, *, tenant_id: str,
                        owner_id: str) -> Conversation | None:
        """切换数据模式**不删消息**：只更新模式标记与状态版本。"""
        if answer_mode not in {"official", "mcp"}:
            return None
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET answer_mode=?, updated_at=?"
                " WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (answer_mode, _now_iso(), str(conversation_id), str(tenant_id), str(owner_id)),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id)

    def touch_conversation(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (_now_iso(), str(conversation_id), str(tenant_id), str(owner_id)),
            )

    def apply_auto_title(self, conversation_id: str, title: str, *, tenant_id: str,
                         owner_id: str) -> Conversation | None:
        """自动标题只填空标题，不覆盖用户手动重命名。"""
        normalized = str(title or "").strip()
        if not normalized:
            return None
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title=?, title_source='auto'"
                " WHERE conversation_id=? AND tenant_id=? AND owner_id=? AND title=''",
                (normalized[:120], str(conversation_id), str(tenant_id), str(owner_id)),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id)

    def delete_conversation(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> bool:
        """单事务删除消息、状态、摘要、待确认项与 request_ledger；**不动长期画像**。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM messages WHERE conversation_id=?", (str(conversation_id),))
            connection.execute("DELETE FROM request_ledger WHERE conversation_id=?", (str(conversation_id),))
            connection.execute("DELETE FROM conversation_state WHERE conversation_id=?", (str(conversation_id),))
            connection.execute("DELETE FROM conversation_summaries WHERE conversation_id=?", (str(conversation_id),))
            # 只清本会话未决候选；已确认/已拒绝的候选保留为审计痕迹。
            connection.execute(
                "DELETE FROM profile_candidates WHERE conversation_id=? AND status='pending'",
                (str(conversation_id),),
            )
            connection.execute("DELETE FROM conversations WHERE conversation_id=?", (str(conversation_id),))
        return True

    # --- 消息 -----------------------------------------------------------------

    def append_message(self, *, tenant_id: str, owner_id: str, conversation_id: str, role: str,
                       text: str, answer: dict[str, Any] | None = None,
                       request_id: str | None = None, status: str = "completed") -> StoredMessage:
        """在同一事务内分配 seq 并写入；调用方不需要自己算序号。"""
        moment = _now_iso()
        message_id = _new_id("msg")
        with self._transaction() as connection:
            conversation = connection.execute(
                "SELECT state_version FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if conversation is None:
                raise ConversationStoreError("会话不存在或不可访问。")
            if request_id:
                existing = connection.execute(
                    "SELECT message_id FROM request_ledger WHERE tenant_id=? AND owner_id=? AND request_id=?",
                    (str(tenant_id), str(owner_id), str(request_id)),
                ).fetchone()
                if existing is not None:
                    raise DuplicateRequest(str(existing["message_id"]))
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM messages WHERE conversation_id=?",
                (str(conversation_id),),
            ).fetchone()
            seq = int(row["last_seq"]) + 1
            connection.execute(
                "INSERT INTO messages(message_id, conversation_id, tenant_id, owner_id, seq, role,"
                " text, answer_json, status, request_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, str(conversation_id), str(tenant_id), str(owner_id), seq, str(role),
                 str(text), _json_dump(answer) if answer is not None else None,
                 str(status), str(request_id) if request_id else None, moment),
            )
            if request_id:
                connection.execute(
                    "INSERT INTO request_ledger(request_id, tenant_id, owner_id, conversation_id,"
                    " message_id, created_at) VALUES(?,?,?,?,?,?)",
                    (str(request_id), str(tenant_id), str(owner_id), str(conversation_id), message_id, moment),
                )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (moment, str(conversation_id)),
            )
        return StoredMessage(
            message_id=message_id, conversation_id=str(conversation_id), seq=seq, role=str(role),
            text=str(text), answer=answer, status=str(status),
            request_id=str(request_id) if request_id else None, created_at=moment,
        )

    def list_messages(self, conversation_id: str, *, tenant_id: str, owner_id: str,
                      limit: int | None = None, since_seq: int = 0) -> list[StoredMessage]:
        if self.get_conversation(conversation_id, tenant_id=tenant_id, owner_id=owner_id) is None:
            return []
        sql = ("SELECT * FROM messages WHERE conversation_id=? AND tenant_id=? AND owner_id=? AND seq>?"
               " ORDER BY seq ASC")
        params: list[Any] = [str(conversation_id), str(tenant_id), str(owner_id), int(since_seq)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._message_from_row(row) for row in rows]

    def latest_seq(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM messages"
                " WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
        return int(row["last_seq"]) if row else 0

    def append_turn(self, *, tenant_id: str, owner_id: str, conversation_id: str, user_text: str,
                    request_id: str, answer_text: str | None = None,
                    answer: dict[str, Any] | None = None) -> tuple[StoredMessage, StoredMessage | None]:
        """一问一答**同事务**写入：要么两条都在，要么都不在。

        ``answer_text is None`` 表示本轮失败/中断，只保留用户消息并标记 ``status``，
        不伪造助手回复。
        """
        moment = _now_iso()
        user_message_id = _new_id("msg")
        assistant_message: StoredMessage | None = None
        with self._transaction() as connection:
            conversation = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if conversation is None:
                raise ConversationStoreError("会话不存在或不可访问。")
            existing = connection.execute(
                "SELECT message_id FROM request_ledger WHERE tenant_id=? AND owner_id=? AND request_id=?",
                (str(tenant_id), str(owner_id), str(request_id)),
            ).fetchone()
            if existing is not None:
                raise DuplicateRequest(str(existing["message_id"]))
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM messages WHERE conversation_id=?",
                (str(conversation_id),),
            ).fetchone()
            seq = int(row["last_seq"]) + 1
            connection.execute(
                "INSERT INTO messages(message_id, conversation_id, tenant_id, owner_id, seq, role,"
                " text, answer_json, status, request_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (user_message_id, str(conversation_id), str(tenant_id), str(owner_id), seq, "user",
                 str(user_text), None, "completed", str(request_id), moment),
            )
            connection.execute(
                "INSERT INTO request_ledger(request_id, tenant_id, owner_id, conversation_id,"
                " message_id, created_at) VALUES(?,?,?,?,?,?)",
                (str(request_id), str(tenant_id), str(owner_id), str(conversation_id), user_message_id, moment),
            )
            if answer_text is not None:
                assistant_message = StoredMessage(
                    message_id=_new_id("msg"), conversation_id=str(conversation_id), seq=seq + 1,
                    role="assistant", text=str(answer_text), answer=answer, status="completed",
                    request_id=str(request_id), created_at=moment,
                )
                connection.execute(
                    "INSERT INTO messages(message_id, conversation_id, tenant_id, owner_id, seq, role,"
                    " text, answer_json, status, request_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (assistant_message.message_id, str(conversation_id), str(tenant_id), str(owner_id),
                     seq + 1, "assistant", str(answer_text),
                     _json_dump(answer) if answer is not None else None, "completed", str(request_id), moment),
                )
            connection.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                               (moment, str(conversation_id)))
        user_message = StoredMessage(
            message_id=user_message_id, conversation_id=str(conversation_id), seq=seq, role="user",
            text=str(user_text), answer=None, status="completed", request_id=str(request_id),
            created_at=moment,
        )
        return user_message, assistant_message

    def find_by_request_id(self, request_id: str, *, tenant_id: str, owner_id: str) -> StoredMessage | None:
        """重复提交时定位已落库的用户消息；跨身份查不到。"""
        if not request_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT m.* FROM messages m JOIN request_ledger r ON r.message_id = m.message_id"
                " WHERE r.tenant_id=? AND r.owner_id=? AND r.request_id=?",
                (str(tenant_id), str(owner_id), str(request_id)),
            ).fetchone()
        return self._message_from_row(row)

    # --- 会话短期状态 ---------------------------------------------------------

    def save_state(self, conversation_id: str, state: dict[str, Any], *, tenant_id: str,
                   owner_id: str) -> int | None:
        """整体覆盖式保存状态；返回新版本号。会话不存在返回 ``None``。"""
        moment = _now_iso()
        with self._transaction() as connection:
            conversation = connection.execute(
                "SELECT state_version FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if conversation is None:
                return None
            version = int(conversation["state_version"]) + 1
            connection.execute(
                "INSERT INTO conversation_state(conversation_id, tenant_id, owner_id, state_json,"
                " version, updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json,"
                " version=excluded.version, updated_at=excluded.updated_at",
                (str(conversation_id), str(tenant_id), str(owner_id), _json_dump(state), version, moment),
            )
            connection.execute(
                "UPDATE conversations SET state_version=?, updated_at=? WHERE conversation_id=?",
                (version, moment, str(conversation_id)),
            )
        return version

    def load_state(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json, version FROM conversation_state"
                " WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
        if row is None:
            return None
        return {"state": _json_load(row["state_json"], {}), "version": int(row["version"])}

    def bump_state_version(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> int | None:
        """状态语义变化（换公司/换模式）但结构不变时也要推进版本，避免摘要与状态错位。"""
        with self._transaction() as connection:
            conversation = connection.execute(
                "SELECT state_version FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if conversation is None:
                return None
            version = int(conversation["state_version"]) + 1
            connection.execute(
                "UPDATE conversations SET state_version=?, updated_at=? WHERE conversation_id=?",
                (version, _now_iso(), str(conversation_id)),
            )
        return version

    # --- 摘要 -----------------------------------------------------------------

    def save_summary(self, conversation_id: str, *, tenant_id: str, owner_id: str,
                     covered_through_seq: int, state_version: int, summary: dict[str, Any],
                     summary_type: str, degraded_reason: str | None = None) -> bool:
        """摘要失败时**不覆盖**上一份有效摘要：调用方只在成功后调用本函数。

        ``summary_type`` 必须是 ``"structured_extractive"``（MVP 不做生成式摘要），
        ``degraded_reason`` 非空表示这是降级产物，UI 与测试据此判断。
        """
        if summary_type not in {"structured_extractive"}:
            raise ConversationStoreError("摘要类型必须是受控提取式，不得写成生成式摘要。")
        if int(covered_through_seq) < 0 or int(state_version) < 0:
            raise ConversationStoreError("摘要覆盖位置无效。")
        with self._transaction() as connection:
            conversation = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if conversation is None:
                return False
            connection.execute(
                "INSERT INTO conversation_summaries(conversation_id, tenant_id, owner_id,"
                " covered_through_seq, state_version, summary_json, summary_type, degraded_reason, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET covered_through_seq=excluded.covered_through_seq,"
                " state_version=excluded.state_version, summary_json=excluded.summary_json,"
                " summary_type=excluded.summary_type, degraded_reason=excluded.degraded_reason,"
                " created_at=excluded.created_at",
                (str(conversation_id), str(tenant_id), str(owner_id), int(covered_through_seq),
                 int(state_version), _json_dump(summary), summary_type,
                 str(degraded_reason) if degraded_reason else None, _now_iso()),
            )
        return True

    def load_summary(self, conversation_id: str, *, tenant_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (str(conversation_id), str(tenant_id), str(owner_id)),
            ).fetchone()
        if row is None:
            return None
        return {
            "conversation_id": row["conversation_id"],
            "covered_through_seq": int(row["covered_through_seq"]),
            "state_version": int(row["state_version"]),
            "summary": _json_load(row["summary_json"], {}),
            "summary_type": row["summary_type"],
            "degraded_reason": row["degraded_reason"],
            "created_at": row["created_at"],
        }

    # --- 长期画像 -------------------------------------------------------------

    def upsert_profile_entry(self, *, tenant_id: str, owner_id: str, field: str, value: str,
                             source_message_id: str | None = None,
                             source_conversation_id: str | None = None,
                             confirmed: bool = True) -> ProfileEntry:
        """同一 field 唯一；重新确认即替换旧值并推进版本（旧值可在删除前展示）。"""
        moment = _now_iso()
        entry_id = _new_id("prof")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT entry_id, version FROM profile_entries WHERE tenant_id=? AND owner_id=? AND field=?",
                (str(tenant_id), str(owner_id), str(field)),
            ).fetchone()
            if row is None:
                version = 1
                connection.execute(
                    "INSERT INTO profile_entries(entry_id, tenant_id, owner_id, field, value, confirmed,"
                    " source_message_id, source_conversation_id, version, revoked, revoked_at,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,NULL,?,?)",
                    (entry_id, str(tenant_id), str(owner_id), str(field), str(value),
                     1 if confirmed else 0, source_message_id, source_conversation_id, version,
                     moment, moment),
                )
            else:
                entry_id = str(row["entry_id"])
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE profile_entries SET value=?, confirmed=?, source_message_id=?,"
                    " source_conversation_id=?, version=?, revoked=0, revoked_at=NULL, updated_at=?"
                    " WHERE entry_id=?",
                    (str(value), 1 if confirmed else 0, source_message_id, source_conversation_id,
                     version, moment, entry_id),
                )
        return self._profile_entry(entry_id, tenant_id=tenant_id, owner_id=owner_id)

    def list_profile_entries(self, *, tenant_id: str, owner_id: str,
                             include_revoked: bool = False) -> list[ProfileEntry]:
        sql = "SELECT entry_id FROM profile_entries WHERE tenant_id=? AND owner_id=?"
        params: list[Any] = [str(tenant_id), str(owner_id)]
        if not include_revoked:
            sql += " AND revoked=0"
        sql += " ORDER BY field ASC"
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._profile_entry(row["entry_id"], tenant_id=tenant_id, owner_id=owner_id) for row in rows]

    def get_profile_entry(self, field: str, *, tenant_id: str, owner_id: str) -> ProfileEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT entry_id FROM profile_entries WHERE tenant_id=? AND owner_id=? AND field=?",
                (str(tenant_id), str(owner_id), str(field)),
            ).fetchone()
        if row is None:
            return None
        return self._profile_entry(row["entry_id"], tenant_id=tenant_id, owner_id=owner_id)

    def revoke_profile_field(self, field: str, *, tenant_id: str, owner_id: str) -> bool:
        """单项撤销：软删除 + 打标记，后续摘要/提取必须跳过该字段。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE profile_entries SET revoked=1, revoked_at=?, version=version+1, updated_at=?"
                " WHERE tenant_id=? AND owner_id=? AND field=? AND revoked=0",
                (_now_iso(), _now_iso(), str(tenant_id), str(owner_id), str(field)),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._purge_profile_derived(connection, tenant_id, owner_id, {str(field)})
            return changed

    def revoke_all_profile_fields(self, *, tenant_id: str, owner_id: str) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE profile_entries SET revoked=1, revoked_at=?, version=version+1, updated_at=?"
                " WHERE tenant_id=? AND owner_id=? AND revoked=0",
                (_now_iso(), _now_iso(), str(tenant_id), str(owner_id)),
            )
            fields = {str(row["field"]) for row in connection.execute(
                "SELECT field FROM profile_entries WHERE tenant_id=? AND owner_id=? AND revoked=1",
                (str(tenant_id), str(owner_id))).fetchall()}
            self._purge_profile_derived(connection, tenant_id, owner_id, fields)
            return int(cursor.rowcount)

    def _purge_profile_derived(self, connection: sqlite3.Connection, tenant_id: str,
                               owner_id: str, fields: set[str]) -> None:
        """撤销偏好后同步清理所有会话中的派生偏好状态与摘要。"""
        if not fields:
            return
        for field in fields:
            connection.execute(
                "UPDATE profile_candidates SET status='revoked', version=version+1, resolved_at=? "
                "WHERE tenant_id=? AND owner_id=? AND field=? AND status='pending'",
                (_now_iso(), str(tenant_id), str(owner_id), field),
            )
        rows = connection.execute(
            "SELECT conversation_id, state_json FROM conversation_state WHERE tenant_id=? AND owner_id=?",
            (str(tenant_id), str(owner_id))).fetchall()
        for row in rows:
            state = _json_load(row["state_json"], {})
            for key in ("session_preferences", "hard_constraints"):
                state[key] = {k: v for k, v in (state.get(key) or {}).items() if k not in fields}
            connection.execute("UPDATE conversation_state SET state_json=? WHERE conversation_id=?",
                               (_json_dump(state), row["conversation_id"]))
        connection.execute("DELETE FROM conversation_summaries WHERE tenant_id=? AND owner_id=?",
                           (str(tenant_id), str(owner_id)))

    def revoked_profile_fields(self, *, tenant_id: str, owner_id: str) -> set[str]:
        """已撤销字段集合；候选提取与摘要重建都必须读它，避免旧偏好复活。"""
        with self._lock:
            rows = self._connection.execute(
                "SELECT field FROM profile_entries WHERE tenant_id=? AND owner_id=? AND revoked=1",
                (str(tenant_id), str(owner_id)),
            ).fetchall()
        return {str(row["field"]) for row in rows}

    # --- 画像候选 -------------------------------------------------------------

    def create_profile_candidate(self, *, tenant_id: str, owner_id: str, field: str, proposed_value: str,
                                 previous_value: str | None = None, op: str = "create",
                                 conversation_id: str | None = None,
                                 source_message_id: str | None = None) -> ProfileCandidate:
        """候选只是"拟保存内容"；未经确认绝不进入 ``profile_entries``。"""
        moment = _now_iso()
        candidate = ProfileCandidate(
            candidate_id=_new_id("cand"), conversation_id=conversation_id,
            source_message_id=source_message_id, field=str(field),
            proposed_value=str(proposed_value), previous_value=previous_value, op=str(op),
            status="pending", version=1, created_at=moment, resolved_at=None,
        )
        with self._transaction() as connection:
            if conversation_id and connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                (conversation_id, tenant_id, owner_id),
            ).fetchone() is None:
                raise ConversationStoreError("会话不存在或不可访问。")
            connection.execute(
                "UPDATE profile_candidates SET status='superseded', version=version+1, resolved_at=?"
                " WHERE tenant_id=? AND owner_id=? AND field=? AND status='pending'",
                (moment, tenant_id, owner_id, field),
            )
            connection.execute(
                "INSERT INTO profile_candidates(candidate_id, tenant_id, owner_id, conversation_id,"
                " source_message_id, field, proposed_value, previous_value, op, status, version,"
                " created_at, resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate.candidate_id, str(tenant_id), str(owner_id), conversation_id,
                 source_message_id, candidate.field, candidate.proposed_value, previous_value,
                 candidate.op, candidate.status, candidate.version, moment, None),
            )
        return candidate

    def get_profile_candidate(self, candidate_id: str, *, tenant_id: str,
                              owner_id: str) -> ProfileCandidate | None:
        if not candidate_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM profile_candidates WHERE candidate_id=? AND tenant_id=? AND owner_id=?",
                (str(candidate_id), str(tenant_id), str(owner_id)),
            ).fetchone()
        return self._candidate_from_row(row)

    def list_profile_candidates(self, *, tenant_id: str, owner_id: str,
                                status: str = "pending") -> list[ProfileCandidate]:
        sql = "SELECT * FROM profile_candidates WHERE tenant_id=? AND owner_id=?"
        params: list[Any] = [str(tenant_id), str(owner_id)]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def resolve_profile_candidate(self, candidate_id: str, *, tenant_id: str, owner_id: str,
                                  status: str = "confirmed",
                                  expected_version: int | None = None) -> ProfileCandidate | None:
        """标记候选已处理；``expected_version`` 不匹配或已处理过都返回 ``None``（不可重放）。"""
        if status not in {"confirmed", "rejected", "superseded"}:
            raise ConversationStoreError("候选状态无效。")
        moment = _now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM profile_candidates WHERE candidate_id=? AND tenant_id=? AND owner_id=?",
                (str(candidate_id), str(tenant_id), str(owner_id)),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            if expected_version is not None and int(row["version"]) != int(expected_version):
                return None
            connection.execute(
                "UPDATE profile_candidates SET status=?, version=version+1, resolved_at=? WHERE candidate_id=?",
                (status, moment, str(candidate_id)),
            )
        return self.get_profile_candidate(candidate_id, tenant_id=tenant_id, owner_id=owner_id)

    def apply_profile_candidate(self, candidate_id: str, *, tenant_id: str, owner_id: str,
                                expected_version: int | None = None) -> dict[str, Any]:
        """确认与改值/删除同事务，绑定候选版本、有效期和画像旧值。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM profile_candidates WHERE candidate_id=? AND tenant_id=? AND owner_id=?",
                (candidate_id, tenant_id, owner_id),
            ).fetchone()
            if row is None:
                return {"status": "not_found"}
            if row["status"] != "pending":
                return {"status": "already_resolved"}
            if expected_version is not None and row["version"] != expected_version:
                return {"status": "version_mismatch"}
            if datetime.fromisoformat(row["created_at"]) < datetime.now(UTC) - timedelta(hours=24):
                connection.execute("UPDATE profile_candidates SET status='expired', version=version+1 WHERE candidate_id=?", (candidate_id,))
                return {"status": "expired"}
            current = connection.execute(
                "SELECT value, revoked FROM profile_entries WHERE tenant_id=? AND owner_id=? AND field=?",
                (tenant_id, owner_id, row["field"]),
            ).fetchone()
            current_value = current["value"] if current and not current["revoked"] else None
            if current_value != row["previous_value"]:
                connection.execute("UPDATE profile_candidates SET status='superseded', version=version+1 WHERE candidate_id=?", (candidate_id,))
                return {"status": "version_mismatch"}
            # 复用既有写入方法，但不得嵌套 BEGIN；当前事务中直接操作。
            moment = _now_iso()
            if row["op"] == "delete":
                connection.execute(
                    "UPDATE profile_entries SET revoked=1, revoked_at=?, version=version+1, updated_at=?"
                    " WHERE tenant_id=? AND owner_id=? AND field=?",
                    (moment, moment, tenant_id, owner_id, row["field"]),
                )
                self._purge_profile_derived(connection, tenant_id, owner_id, {row["field"]})
                status = "deleted"
            else:
                connection.execute(
                    "INSERT INTO profile_entries(entry_id, tenant_id, owner_id, field, value, confirmed,"
                    " source_message_id, source_conversation_id, created_at, updated_at) VALUES(?,?,?,?,?,1,?,?,?,?)"
                    " ON CONFLICT(tenant_id,owner_id,field) DO UPDATE SET value=excluded.value, confirmed=1,"
                    " source_message_id=excluded.source_message_id, source_conversation_id=excluded.source_conversation_id,"
                    " revoked=0, revoked_at=NULL, version=profile_entries.version+1, updated_at=excluded.updated_at",
                    (_new_id("profile"), tenant_id, owner_id, row["field"], row["proposed_value"],
                     row["source_message_id"], row["conversation_id"], moment, moment),
                )
                status = "confirmed"
            connection.execute(
                "UPDATE profile_candidates SET status=?,version=version+1,resolved_at=? WHERE candidate_id=?",
                ("confirmed", moment, candidate_id),
            )
        entry = self.get_profile_entry(row["field"], tenant_id=tenant_id, owner_id=owner_id)
        return {"status": status, "entry": entry.to_dict() if entry else None, "field": row["field"]}

    def _purge_profile_derived(self, connection: sqlite3.Connection, tenant_id: str,
                               owner_id: str, fields: set[str]) -> None:
        """撤销偏好使待确认项和派生状态失效；历史消息仍可作为历史查看。"""
        for field in fields:
            connection.execute(
                "UPDATE profile_candidates SET status='revoked',version=version+1,resolved_at=?"
                " WHERE tenant_id=? AND owner_id=? AND field=? AND status='pending'",
                (_now_iso(), tenant_id, owner_id, field),
            )
        for row in connection.execute(
            "SELECT conversation_id,state_json FROM conversation_state WHERE tenant_id=? AND owner_id=?",
            (tenant_id, owner_id),
        ).fetchall():
            state = _json_load(row["state_json"], {})
            for key in ("session_preferences", "hard_constraints"):
                state[key] = {k: v for k, v in (state.get(key) or {}).items() if k not in fields}
            connection.execute("UPDATE conversation_state SET state_json=? WHERE conversation_id=?",
                               (_json_dump(state), row["conversation_id"]))
        connection.execute("DELETE FROM conversation_summaries WHERE tenant_id=? AND owner_id=?", (tenant_id, owner_id))

    def claim_turn(self, conversation_id: str, text: str, request_id: str, *,
                   tenant_id: str, owner_id: str) -> dict[str, Any]:
        """在工具调用前落用户消息并抢占会话，跨连接阻止重复执行与乱序状态覆盖。"""
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                                  (conversation_id, tenant_id, owner_id)).fetchone() is None:
                return {"status": "conversation_not_found"}
            row = connection.execute(
                "SELECT m.* FROM messages m JOIN request_ledger r ON r.message_id=m.message_id"
                " WHERE r.tenant_id=? AND r.owner_id=? AND r.request_id=?",
                (tenant_id, owner_id, request_id),
            ).fetchone()
            if row:
                if row["conversation_id"] != conversation_id or row["text"] != text:
                    return {"status": "request_conflict", "message": "请求编号已用于其他问题，请勿重复使用。"}
                reply = connection.execute("SELECT * FROM messages WHERE conversation_id=? AND request_id=? AND role='assistant'",
                                           (conversation_id, request_id)).fetchone()
                return {"status": "request_in_progress" if row["status"] == "processing" else "duplicate_request",
                        "message_id": row["message_id"], "conversation_id": conversation_id,
                        "message": reply["text"] if reply else "该请求正在处理或尚未完成，请稍后查看会话。"}
            active = connection.execute("SELECT created_at FROM messages WHERE conversation_id=? AND status='processing' AND role='user'",
                                        (conversation_id,)).fetchone()
            if active:
                if datetime.fromisoformat(active["created_at"]) >= datetime.now(UTC) - timedelta(seconds=180):
                    return {"status": "conversation_busy", "message": "本会话上一条问题仍在处理，请稍后再试。"}
                connection.execute("UPDATE messages SET status='interrupted' WHERE conversation_id=? AND status='processing'", (conversation_id,))
            seq = connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            message_id, moment = _new_id("msg"), _now_iso()
            connection.execute("INSERT INTO messages(message_id,conversation_id,tenant_id,owner_id,seq,role,text,status,request_id,created_at) VALUES(?,?,?,?,?,'user',?,'processing',?,?)",
                               (message_id, conversation_id, tenant_id, owner_id, seq, text, request_id, moment))
            connection.execute("INSERT INTO request_ledger(request_id,tenant_id,owner_id,conversation_id,message_id,created_at) VALUES(?,?,?,?,?,?)",
                               (request_id, tenant_id, owner_id, conversation_id, message_id, moment))
        return {"status": "claimed", "message_id": message_id, "seq": seq}

    def complete_turn(self, conversation_id: str, request_id: str, *, tenant_id: str, owner_id: str,
                      answer_text: str, answer: dict[str, Any] | None, state: dict[str, Any]) -> tuple[StoredMessage, StoredMessage] | None:
        """回复和结构化状态同事务，删除/中断后的迟到结果不重新建立会话。"""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM messages WHERE conversation_id=? AND tenant_id=? AND owner_id=? AND request_id=? AND role='user' AND status='processing'",
                                     (conversation_id, tenant_id, owner_id, request_id)).fetchone()
            conv = connection.execute("SELECT * FROM conversations WHERE conversation_id=? AND tenant_id=? AND owner_id=?",
                                      (conversation_id, tenant_id, owner_id)).fetchone()
            if not row or not conv:
                return None
            seq = connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            moment, message_id = _now_iso(), _new_id("msg")
            connection.execute("INSERT INTO messages(message_id,conversation_id,tenant_id,owner_id,seq,role,text,answer_json,status,request_id,created_at) VALUES(?,?,?,?,?,'assistant',?,?,'completed',?,?)",
                               (message_id, conversation_id, tenant_id, owner_id, seq, answer_text, _json_dump(answer) if answer else None, request_id, moment))
            connection.execute("UPDATE messages SET status='completed' WHERE message_id=?", (row["message_id"],))
            revoked = {r["field"] for r in connection.execute("SELECT field FROM profile_entries WHERE tenant_id=? AND owner_id=? AND revoked=1", (tenant_id, owner_id))}
            state = {k: v for k, v in state.items() if k not in {"messages", "failure", "conversation_memory"}}
            for key in ("session_preferences", "hard_constraints"):
                state[key] = {k: v for k, v in (state.get(key) or {}).items() if k not in revoked}
            state["answer_mode"] = conv["answer_mode"]
            version = int(conv["state_version"]) + 1
            connection.execute("INSERT INTO conversation_state(conversation_id,tenant_id,owner_id,state_json,version,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json,version=excluded.version,updated_at=excluded.updated_at",
                               (conversation_id, tenant_id, owner_id, _json_dump(state), version, moment))
            connection.execute("UPDATE conversations SET state_version=?,updated_at=? WHERE conversation_id=?", (version, moment, conversation_id))
        user = self.find_by_request_id(request_id, tenant_id=tenant_id, owner_id=owner_id)
        return user, StoredMessage(message_id, conversation_id, seq, "assistant", answer_text, answer, "completed", request_id, moment)

    # --- 行→对象 --------------------------------------------------------------

    def _conversation_from_row(self, row: sqlite3.Row | None) -> Conversation | None:
        if row is None:
            return None
        return Conversation(
            conversation_id=str(row["conversation_id"]), tenant_id=str(row["tenant_id"]),
            owner_id=str(row["owner_id"]), title=str(row["title"]),
            title_source=str(row["title_source"]), answer_mode=str(row["answer_mode"]),
            state_version=int(row["state_version"]), created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _message_from_row(self, row: sqlite3.Row | None) -> StoredMessage | None:
        if row is None:
            return None
        raw_answer = row["answer_json"]
        return StoredMessage(
            message_id=str(row["message_id"]), conversation_id=str(row["conversation_id"]),
            seq=int(row["seq"]), role=str(row["role"]), text=str(row["text"]),
            answer=_json_load(raw_answer, None) if raw_answer else None,
            status=str(row["status"]), request_id=row["request_id"], created_at=str(row["created_at"]),
        )

    def _profile_entry(self, entry_id: str, *, tenant_id: str, owner_id: str) -> ProfileEntry:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM profile_entries WHERE entry_id=? AND tenant_id=? AND owner_id=?",
                (entry_id, str(tenant_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise ConversationStoreError("画像条目不存在。")
        return ProfileEntry(
            entry_id=str(row["entry_id"]), field=str(row["field"]), value=str(row["value"]),
            confirmed=bool(row["confirmed"]), source_message_id=row["source_message_id"],
            source_conversation_id=row["source_conversation_id"], version=int(row["version"]),
            revoked=bool(row["revoked"]), created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _candidate_from_row(self, row: sqlite3.Row | None) -> ProfileCandidate | None:
        if row is None:
            return None
        return ProfileCandidate(
            candidate_id=str(row["candidate_id"]), conversation_id=row["conversation_id"],
            source_message_id=row["source_message_id"], field=str(row["field"]),
            proposed_value=str(row["proposed_value"]), previous_value=row["previous_value"],
            op=str(row["op"]), status=str(row["status"]), version=int(row["version"]),
            created_at=str(row["created_at"]), resolved_at=row["resolved_at"],
        )


#: 进程级默认存储。仅在**首次访问**时连接，避免 import 阶段就创建文件。
_default_store: ConversationStore | None = None
_default_lock = threading.Lock()


def get_store(path: Path | str | None = None) -> ConversationStore:
    """返回默认存储；``path`` 非空时返回独立存储（测试隔离用）。"""
    global _default_store
    if path is not None:
        return ConversationStore(path)
    with _default_lock:
        if _default_store is None:
            _default_store = ConversationStore()
        return _default_store


def reset_default_store() -> None:
    """仅供测试：丢弃进程级缓存连接。"""
    global _default_store
    with _default_lock:
        if _default_store is not None:
            _default_store.close()
        _default_store = None