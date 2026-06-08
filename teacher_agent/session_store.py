from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SESSION_DB_PATH = Path("data/teacher_sessions.db")


@dataclass
class ChatSession:
    id: str
    title: str
    user_role: str
    user_id: str
    dify_conversation_id: str
    created_at: str
    updated_at: str


def connect(db_path: str | Path = DEFAULT_SESSION_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_session_db(db_path: str | Path = DEFAULT_SESSION_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                user_role TEXT NOT NULL DEFAULT 'teacher',
                user_id TEXT NOT NULL DEFAULT 'teacher_demo',
                dify_conversation_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "sessions", "user_role", "TEXT NOT NULL DEFAULT 'teacher'")
        _ensure_column(conn, "sessions", "user_id", "TEXT NOT NULL DEFAULT 'teacher_demo'")
        _ensure_column(conn, "sessions", "dify_conversation_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )


def create_session(
    title: str = "新的老师会话",
    user_role: str = "teacher",
    user_id: str = "teacher_demo",
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> ChatSession:
    init_session_db(db_path)
    now = _now()
    session = ChatSession(
        id=str(uuid.uuid4()),
        title=title,
        user_role=user_role,
        user_id=user_id,
        dify_conversation_id="",
        created_at=now,
        updated_at=now,
    )
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, user_role, user_id, dify_conversation_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.title,
                session.user_role,
                session.user_id,
                session.dify_conversation_id,
                session.created_at,
                session.updated_at,
            ),
        )
    return session


def list_sessions(db_path: str | Path = DEFAULT_SESSION_DB_PATH) -> list[ChatSession]:
    init_session_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_session(row) for row in rows]


def get_session(
    session_id: str,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> ChatSession | None:
    init_session_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_session(row) if row else None


def delete_session(
    session_id: str,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> bool:
    init_session_db(db_path)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


def update_session(
    session_id: str,
    *,
    title: str | None = None,
    user_role: str | None = None,
    user_id: str | None = None,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> ChatSession | None:
    init_session_db(db_path)
    current = get_session(session_id, db_path)
    if not current:
        return None
    new_title = title if title is not None else current.title
    new_role = user_role if user_role is not None else current.user_role
    new_user_id = user_id if user_id is not None else current.user_id
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE sessions
            SET title = ?, user_role = ?, user_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_title, new_role, new_user_id, now, session_id),
        )
    return get_session(session_id, db_path)


def add_message(
    session_id: str,
    role: str,
    content: str,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> None:
    init_session_db(db_path)
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )


def update_dify_conversation_id(
    session_id: str,
    dify_conversation_id: str,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
) -> None:
    init_session_db(db_path)
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE sessions
            SET dify_conversation_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (dify_conversation_id, now, session_id),
        )


def get_messages(
    session_id: str,
    db_path: str | Path = DEFAULT_SESSION_DB_PATH,
    *,
    limit: int = 30,
) -> list[sqlite3.Row]:
    init_session_db(db_path)
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()[::-1]


def _row_to_session(row: sqlite3.Row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        title=row["title"],
        user_role=row["user_role"],
        user_id=row["user_id"],
        dify_conversation_id=row["dify_conversation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
) -> None:
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
