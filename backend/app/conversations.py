from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime


class ConversationNotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _owned_conversation(
    connection: sqlite3.Connection, conversation_id: str, user_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()


def recent_messages(
    connection: sqlite3.Connection,
    conversation_id: str | None,
    user_id: int,
    limit: int = 8,
) -> list[dict[str, str]]:
    if conversation_id is None:
        return []
    if _owned_conversation(connection, conversation_id, user_id) is None:
        raise ConversationNotFound
    rows = connection.execute(
        "SELECT role, content FROM (SELECT id, role, content FROM conversation_messages "
        "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
        (conversation_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def conversation_for_record(
    connection: sqlite3.Connection, record_id: int, user_id: int
) -> str | None:
    row = connection.execute(
        "SELECT c.id FROM conversations c JOIN conversation_messages m "
        "ON m.conversation_id = c.id WHERE m.record_id = ? AND c.user_id = ? LIMIT 1",
        (record_id, user_id),
    ).fetchone()
    return str(row["id"]) if row else None


def conversation_for_clarification(
    connection: sqlite3.Connection, clarification_id: str, user_id: int
) -> str | None:
    row = connection.execute(
        "SELECT c.id FROM clarification_sessions cs "
        "JOIN conversation_messages m ON m.record_id = cs.record_id "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE cs.id = ? AND c.user_id = ? LIMIT 1",
        (clarification_id, user_id),
    ).fetchone()
    return str(row["id"]) if row else None


def save_exchange(
    connection: sqlite3.Connection,
    user_id: int,
    user_message: str,
    assistant_message: str,
    record_id: int,
    conversation_id: str | None = None,
) -> str:
    now = _now()
    if conversation_id is None:
        conversation_id = f"cv_{secrets.token_hex(12)}"
        title = user_message.strip().replace("\n", " ")[:30] or "新会话"
        connection.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, user_id, title, now, now),
        )
    elif _owned_conversation(connection, conversation_id, user_id) is None:
        raise ConversationNotFound

    connection.executemany(
        "INSERT INTO conversation_messages "
        "(conversation_id, role, content, record_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            (conversation_id, "user", user_message, record_id, now),
            (conversation_id, "assistant", assistant_message, record_id, now),
        ),
    )
    connection.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
    )
    connection.commit()
    return conversation_id


def list_conversations(
    connection: sqlite3.Connection, user_id: int
) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count "
        "FROM conversations c LEFT JOIN conversation_messages m ON m.conversation_id = c.id "
        "WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(
    connection: sqlite3.Connection, conversation_id: str, user_id: int
) -> dict[str, object] | None:
    conversation = _owned_conversation(connection, conversation_id, user_id)
    if conversation is None:
        return None
    rows = connection.execute(
        "SELECT m.id, m.role, m.content, m.record_id, m.created_at, "
        "r.status, r.final_answer, r.result_json, r.trace_id "
        "FROM conversation_messages m LEFT JOIN processing_records r ON r.id = m.record_id "
        "WHERE m.conversation_id = ? ORDER BY m.id",
        (conversation_id,),
    ).fetchall()
    messages = []
    latest_result = None
    latest_trace_id = None
    for row in rows:
        item = {key: row[key] for key in ("id", "role", "content", "record_id", "created_at")}
        if (
            row["role"] == "assistant"
            and row["status"] == "completed"
            and row["content"] == row["final_answer"]
            and row["result_json"]
        ):
            item["result"] = json.loads(row["result_json"])
            latest_result = item["result"]
            latest_trace_id = row["trace_id"]
        messages.append(item)
    if rows and latest_result is None:
        last = rows[-1]
        if last["status"] == "waiting_clarification":
            clarification = connection.execute(
                "SELECT id, intent, slots_json, missing_slots_json FROM clarification_sessions "
                "WHERE record_id = ? AND status = 'active'",
                (last["record_id"],),
            ).fetchone()
            if clarification:
                latest_result = {
                    "status": "waiting_clarification",
                    "record_id": last["record_id"],
                    "session_id": clarification["id"],
                    "intent": clarification["intent"],
                    "known_params": {
                        key: value
                        for key, value in json.loads(clarification["slots_json"]).items()
                        if value is not None
                    },
                    "missing_fields": json.loads(clarification["missing_slots_json"]),
                    "clarification_question": last["content"],
                }
                latest_trace_id = last["trace_id"]
    if latest_result is not None:
        latest_result["conversation_id"] = conversation_id
    return {
        **dict(conversation),
        "messages": messages,
        "latest_result": latest_result,
        "latest_trace_id": latest_trace_id,
    }
