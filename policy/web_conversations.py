"""匿名浏览器的制度问答会话存储。"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Any

from psycopg2.extras import RealDictCursor

from storage_adapter import storage

_WEB_TABLES_READY = False


def new_browser_token() -> str:
    """创建只供当前浏览器持有的不可预测访问令牌。"""
    return secrets.token_urlsafe(32)


def _owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_web_tables() -> None:
    """按需创建会话、消息与反馈表。"""
    global _WEB_TABLES_READY
    if _WEB_TABLES_READY:
        return
    sql = (
        '''CREATE TABLE IF NOT EXISTS policy_web_conversations (
            conversation_id VARCHAR(64) PRIMARY KEY,
            owner_hash CHAR(64) NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )''',
        '''CREATE INDEX IF NOT EXISTS policy_web_conversations_owner_idx
            ON policy_web_conversations(owner_hash, updated_at DESC)''',
        '''CREATE TABLE IF NOT EXISTS policy_web_messages (
            message_id VARCHAR(64) PRIMARY KEY,
            conversation_id VARCHAR(64) NOT NULL REFERENCES policy_web_conversations(conversation_id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            answer JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )''',
        '''CREATE INDEX IF NOT EXISTS policy_web_messages_conversation_idx
            ON policy_web_messages(conversation_id, created_at, message_id)''',
        '''CREATE TABLE IF NOT EXISTS policy_web_feedback (
            message_id VARCHAR(64) PRIMARY KEY REFERENCES policy_web_messages(message_id) ON DELETE CASCADE,
            rating VARCHAR(16) NOT NULL CHECK (rating IN ('helpful', 'unhelpful')),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )''',
    )
    with storage.relational.conn.cursor() as cursor:
        for statement in sql:
            cursor.execute(statement)
    _WEB_TABLES_READY = True


def create_conversation(token: str) -> dict[str, str]:
    ensure_web_tables()
    conversation_id = f"conv_{uuid.uuid4().hex}"
    with storage.relational.conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO policy_web_conversations(conversation_id, owner_hash) VALUES (%s, %s)",
            (conversation_id, _owner_hash(token)),
        )
    return {"conversation_id": conversation_id, "title": "新对话"}


def list_conversations(token: str) -> list[dict[str, Any]]:
    if not token:
        return []
    ensure_web_tables()
    with storage.relational.conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT conversation_id, title, updated_at FROM policy_web_conversations "
            "WHERE owner_hash = %s ORDER BY updated_at DESC LIMIT 100",
            (_owner_hash(token),),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_conversation(token: str, conversation_id: str) -> dict[str, Any] | None:
    if not token:
        return None
    ensure_web_tables()
    with storage.relational.conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT conversation_id, title, updated_at FROM policy_web_conversations "
            "WHERE conversation_id = %s AND owner_hash = %s",
            (conversation_id, _owner_hash(token)),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "SELECT message_id, question, answer, created_at FROM policy_web_messages "
            "WHERE conversation_id = %s ORDER BY created_at, message_id LIMIT 200",
            (conversation_id,),
        )
        return {**dict(row), "messages": [dict(item) for item in cursor.fetchall()]}


def save_message(token: str, conversation_id: str, question: str, answer: dict[str, Any]) -> dict[str, Any] | None:
    """仅允许会话所属浏览器写入问答。"""
    if not token:
        return None
    ensure_web_tables()
    message_id = f"msg_{uuid.uuid4().hex}"
    with storage.relational.conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO policy_web_messages(message_id, conversation_id, question, answer) "
            "SELECT %s, conversation_id, %s, %s::jsonb FROM policy_web_conversations "
            "WHERE conversation_id = %s AND owner_hash = %s RETURNING message_id",
            (message_id, question, json.dumps(answer, ensure_ascii=False), conversation_id, _owner_hash(token)),
        )
        if cursor.fetchone() is None:
            return None
        cursor.execute(
            "UPDATE policy_web_conversations SET title = CASE WHEN title = '' THEN %s ELSE title END, "
            "updated_at = NOW() WHERE conversation_id = %s",
            (question[:60], conversation_id),
        )
    return {"message_id": message_id, "question": question, "answer": answer}


def rename_conversation(token: str, conversation_id: str, title: str) -> bool:
    ensure_web_tables()
    with storage.relational.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE policy_web_conversations SET title = %s, updated_at = NOW() "
            "WHERE conversation_id = %s AND owner_hash = %s",
            (title, conversation_id, _owner_hash(token)),
        )
        return cursor.rowcount > 0


def delete_conversation(token: str, conversation_id: str) -> bool:
    ensure_web_tables()
    with storage.relational.conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM policy_web_conversations WHERE conversation_id = %s AND owner_hash = %s",
            (conversation_id, _owner_hash(token)),
        )
        return cursor.rowcount > 0


def save_feedback(token: str, message_id: str, rating: str) -> bool:
    ensure_web_tables()
    with storage.relational.conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO policy_web_feedback(message_id, rating) "
            "SELECT m.message_id, %s FROM policy_web_messages m "
            "JOIN policy_web_conversations c ON c.conversation_id = m.conversation_id "
            "WHERE m.message_id = %s AND c.owner_hash = %s "
            "ON CONFLICT (message_id) DO UPDATE SET rating = EXCLUDED.rating, updated_at = NOW() "
            "RETURNING message_id",
            (rating, message_id, _owner_hash(token)),
        )
        return cursor.fetchone() is not None
