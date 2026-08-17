"""Small SQLite store for validator-only multi-turn conversations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


class ConversationStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    knowledge_base_names TEXT NOT NULL,
                    memory_summary TEXT NOT NULL DEFAULT '',
                    summary_through_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('streaming', 'completed', 'cancelled', 'failed')),
                    reply_to_message_id TEXT,
                    retrieval_query TEXT NOT NULL DEFAULT '',
                    source_pages TEXT NOT NULL DEFAULT '[]',
                    direct_evidence_pages TEXT NOT NULL DEFAULT '[]',
                    warnings TEXT NOT NULL DEFAULT '[]',
                    knowledge_status TEXT NOT NULL DEFAULT 'skipped',
                    knowledge_message TEXT NOT NULL DEFAULT '',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(conversation_id, sequence)
                );
                """
            )

    def create(self, knowledge_base_names: list[str]) -> dict[str, Any]:
        now, conversation_id = _now(), uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, knowledge_base_names, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, "新对话", _dump(knowledge_base_names), now, now),
            )
        return self.get(conversation_id, required=True)

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT conversations.*, COUNT(messages.id) AS message_count
                FROM conversations LEFT JOIN messages ON messages.conversation_id = conversations.id
                GROUP BY conversations.id ORDER BY conversations.updated_at DESC"""
            ).fetchall()
        return [self._conversation_payload(row, include_messages=False) for row in rows]

    def get(self, conversation_id: str, *, required: bool = False) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            messages = connection.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY sequence", (conversation_id,)).fetchall() if row else []
        if row is None:
            if required:
                raise ValueError("对话不存在。")
            return None
        payload = self._conversation_payload(row, include_messages=True)
        payload["messages"] = [self._message_payload(item) for item in messages]
        payload["message_count"] = len(messages)
        return payload

    def update(self, conversation_id: str, *, title: str | None = None, knowledge_base_names: list[str] | None = None) -> dict[str, Any]:
        current = self.get(conversation_id, required=True)
        with self._connection() as connection:
            connection.execute(
                "UPDATE conversations SET title = ?, knowledge_base_names = ?, updated_at = ? WHERE id = ?",
                (title.strip() if title else current["title"], _dump(knowledge_base_names if knowledge_base_names is not None else current["knowledge_base_names"]), _now(), conversation_id),
            )
        return self.get(conversation_id, required=True)

    def delete(self, conversation_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount > 0

    def begin_turn(self, conversation_id: str, question: str, knowledge_base_names: list[str]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Persist a user message and one reusable assistant placeholder."""

        current = self.get(conversation_id, required=True)
        history = [item for item in current["messages"] if item["state"] == "completed"]
        now, user_id, assistant_id = _now(), uuid.uuid4().hex, uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence = int(connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO messages (id, conversation_id, sequence, role, content, state, created_at, updated_at) VALUES (?, ?, ?, 'user', ?, 'completed', ?, ?)",
                (user_id, conversation_id, sequence, question, now, now),
            )
            connection.execute(
                "INSERT INTO messages (id, conversation_id, sequence, role, content, state, reply_to_message_id, created_at, updated_at) VALUES (?, ?, ?, 'assistant', '', 'streaming', ?, ?, ?)",
                (assistant_id, conversation_id, sequence + 1, user_id, now, now),
            )
            title = _title(question) if not history else current["title"]
            connection.execute(
                "UPDATE conversations SET title = ?, knowledge_base_names = ?, updated_at = ? WHERE id = ?",
                (title, _dump(knowledge_base_names), now, conversation_id),
            )
        return self.get(conversation_id, required=True), self.message(conversation_id, assistant_id, required=True), history

    def reset_for_regeneration(self, conversation_id: str, assistant_message_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
        current = self.get(conversation_id, required=True)
        assistant = self.message(conversation_id, assistant_message_id, required=True)
        if assistant["role"] != "assistant" or not assistant.get("reply_to_message_id"):
            raise ValueError("只能重新生成已有助手回答。")
        user = self.message(conversation_id, str(assistant["reply_to_message_id"]), required=True)
        history = [item for item in current["messages"] if item["sequence"] < user["sequence"] and item["state"] == "completed"]
        with self._connection() as connection:
            connection.execute(
                """UPDATE messages SET content = '', state = 'streaming', retrieval_query = '', source_pages = '[]',
                direct_evidence_pages = '[]', warnings = '[]', knowledge_status = 'skipped', knowledge_message = '',
                error_message = NULL, updated_at = ? WHERE id = ?""",
                (_now(), assistant_message_id),
            )
        return self.get(conversation_id, required=True), self.message(conversation_id, assistant_message_id, required=True), history, str(user["content"])

    def complete(
        self,
        conversation_id: str,
        assistant_message_id: str,
        *,
        answer: str,
        retrieval_query: str,
        source_pages: list[dict[str, Any]],
        direct_evidence_pages: list[dict[str, Any]],
        warnings: list[str],
        knowledge_status: str,
        knowledge_message: str,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._connection() as connection:
            updated = connection.execute(
                """UPDATE messages SET content = ?, state = 'completed', retrieval_query = ?, source_pages = ?,
                direct_evidence_pages = ?, warnings = ?, knowledge_status = ?, knowledge_message = ?, error_message = NULL,
                updated_at = ? WHERE id = ? AND state = 'streaming'""",
                (answer, retrieval_query, _dump(source_pages), _dump(direct_evidence_pages), _dump(warnings), knowledge_status, knowledge_message, now, assistant_message_id),
            )
            if updated.rowcount:
                connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return self.message(conversation_id, assistant_message_id, required=True) if updated.rowcount else None

    def terminal(self, conversation_id: str, assistant_message_id: str, *, state: str, content: str, error: str) -> None:
        with self._connection() as connection:
            updated = connection.execute("UPDATE messages SET content = ?, state = ?, error_message = ?, updated_at = ? WHERE id = ? AND state = 'streaming'", (content, state, error[:1000], _now(), assistant_message_id))
            if updated.rowcount:
                connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conversation_id))

    def update_memory(self, conversation_id: str, summary: str, through_sequence: int) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE conversations SET memory_summary = ?, summary_through_sequence = ?, updated_at = ? WHERE id = ?", (summary, through_sequence, _now(), conversation_id))

    def message(self, conversation_id: str, message_id: str, *, required: bool = False) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM messages WHERE id = ? AND conversation_id = ?", (message_id, conversation_id)).fetchone()
        if row is None:
            if required:
                raise ValueError("消息不存在。")
            return None
        return self._message_payload(row)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _conversation_payload(row: sqlite3.Row, *, include_messages: bool) -> dict[str, Any]:
        del include_messages
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "knowledge_base_names": _load(row["knowledge_base_names"]),
            "memory_summary": str(row["memory_summary"]),
            "summary_through_sequence": int(row["summary_through_sequence"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "message_count": int(row["message_count"]) if "message_count" in row.keys() else 0,
        }

    @staticmethod
    def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]), "sequence": int(row["sequence"]), "role": str(row["role"]),
            "content": str(row["content"]), "state": str(row["state"]),
            "reply_to_message_id": str(row["reply_to_message_id"]) if row["reply_to_message_id"] else None,
            "retrieval_query": str(row["retrieval_query"]), "source_pages": _load(row["source_pages"]),
            "direct_evidence_pages": _load(row["direct_evidence_pages"]), "warnings": _load(row["warnings"]),
            "knowledge_status": str(row["knowledge_status"]), "knowledge_message": str(row["knowledge_message"]),
            "error_message": str(row["error_message"]) if row["error_message"] else None,
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
        }


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value: object) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _title(question: str) -> str:
    return question.strip().replace("\n", " ")[:36] or "新对话"
