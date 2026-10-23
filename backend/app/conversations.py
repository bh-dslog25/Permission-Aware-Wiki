"""Persistent conversation history scoped to a demo persona."""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.db import get_conn


def list_conversations(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT conversation_id, title, context_id, created_at, updated_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "context_id": row[2],
            "created_at": row[3],
            "updated_at": row[4],
        }
        for row in rows
    ]


def get_conversation(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT conversation_id, title, context_id, messages, created_at, updated_at
            FROM conversations
            WHERE user_id = %s AND conversation_id = %s
            """,
            (user_id, conversation_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "context_id": row[2],
        "messages": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }


def append_exchange(
    *,
    user_id: str,
    conversation_id: str,
    context_id: str,
    question: str,
    answer: dict[str, Any],
) -> None:
    entry = {"id": answer["request_id"], "question": question, "answer": answer}
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO conversations (conversation_id, user_id, title, context_id, messages)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (conversation_id) DO UPDATE
            SET messages = conversations.messages || EXCLUDED.messages,
                updated_at = now()
            WHERE conversations.user_id = EXCLUDED.user_id
            """,
            (
                conversation_id,
                user_id,
                question[:120],
                context_id,
                Jsonb([entry]),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Không thể lưu hội thoại cho tài khoản hiện tại")
