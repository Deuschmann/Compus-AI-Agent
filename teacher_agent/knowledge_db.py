from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from pypdf import PdfReader


DEFAULT_DB_PATH = Path("data/teacher_knowledge.db")
PDF_STORAGE_DIR = Path("data/pdfs")


@dataclass
class KnowledgeItem:
    title: str
    source_type: str = "textbook"
    language: str = "zh"
    topics: list[str] = field(default_factory=list)
    url: str = ""
    authors: str = ""
    year: str = ""
    summary: str = ""
    content: str = ""
    local_path: str = ""
    tags: list[str] = field(default_factory=list)
    item_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                language TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                url TEXT,
                authors TEXT,
                year TEXT,
                summary TEXT,
                content TEXT,
                local_path TEXT,
                tags_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "knowledge_items", "local_path", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY(item_id) REFERENCES knowledge_items(id) ON DELETE CASCADE,
                UNIQUE(item_id, page_number)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pages_item ON knowledge_pages(item_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pages_number ON knowledge_pages(page_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge_items(source_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_language ON knowledge_items(language)"
        )


def add_item(item: KnowledgeItem, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    init_db(db_path)
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (
                id, title, source_type, language, topics_json, url, authors, year,
                summary, content, local_path, tags_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.title,
                item.source_type,
                item.language,
                json.dumps(item.topics, ensure_ascii=False),
                item.url,
                item.authors,
                item.year,
                item.summary,
                item.content,
                item.local_path,
                json.dumps(item.tags, ensure_ascii=False),
                now,
                now,
            ),
        )
    return item.item_id


def list_items(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    limit: int = 20,
    source_type: str | None = None,
    language: str | None = None,
) -> list[sqlite3.Row]:
    init_db(db_path)
    clauses = []
    params: list[str | int] = []
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)
    if language:
        clauses.append("language = ?")
        params.append(language)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT id, title, source_type, language, topics_json, url, local_path, updated_at
            FROM knowledge_items
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def search_items(
    query: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    limit: int = 20,
) -> list[sqlite3.Row]:
    init_db(db_path)
    like = f"%{query}%"
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT DISTINCT
                knowledge_items.id,
                knowledge_items.title,
                knowledge_items.source_type,
                knowledge_items.language,
                knowledge_items.topics_json,
                knowledge_items.url,
                knowledge_items.local_path,
                knowledge_items.summary,
                knowledge_items.updated_at
            FROM knowledge_items
            LEFT JOIN knowledge_pages ON knowledge_pages.item_id = knowledge_items.id
            WHERE knowledge_items.title LIKE ?
               OR knowledge_items.topics_json LIKE ?
               OR knowledge_items.tags_json LIKE ?
               OR knowledge_items.summary LIKE ?
               OR knowledge_items.content LIKE ?
               OR knowledge_pages.text LIKE ?
            ORDER BY knowledge_items.updated_at DESC
            LIMIT ?
            """,
            (like, like, like, like, like, like, limit),
        ).fetchall()


def get_item(item_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Row | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (item_id,),
        ).fetchone()


def delete_item(item_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM knowledge_pages WHERE item_id = ?", (item_id,))
        cursor = conn.execute("DELETE FROM knowledge_items WHERE id = ?", (item_id,))
    return cursor.rowcount > 0


def import_pdf(
    *,
    title: str,
    file_path: str | Path | None = None,
    url: str = "",
    source_type: str = "textbook",
    language: str = "zh",
    topics: list[str] | None = None,
    authors: str = "",
    year: str = "",
    summary: str = "",
    tags: list[str] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    init_db(db_path)
    local_pdf = _prepare_pdf_file(title, file_path=file_path, url=url)
    pages = _extract_pdf_pages(local_pdf)
    content = "\n\n".join(f"[page {number}]\n{text}" for number, text in pages)

    item = KnowledgeItem(
        title=title,
        source_type=source_type,
        language=language,
        topics=topics or [],
        url=url,
        authors=authors,
        year=year,
        summary=summary,
        content=content,
        local_path=str(local_pdf),
        tags=tags or ["pdf"],
    )
    add_item(item, db_path)

    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO knowledge_pages (item_id, page_number, text)
            VALUES (?, ?, ?)
            """,
            [(item.item_id, page_number, text) for page_number, text in pages],
        )

    return item.item_id


def read_pages(
    item_id: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    start_page: int = 1,
    page_count: int = 1,
) -> list[sqlite3.Row]:
    init_db(db_path)
    end_page = start_page + max(1, page_count) - 1
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT item_id, page_number, text
            FROM knowledge_pages
            WHERE item_id = ? AND page_number BETWEEN ? AND ?
            ORDER BY page_number
            """,
            (item_id, start_page, end_page),
        ).fetchall()


def search_pages(
    query: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    init_db(db_path)
    like = f"%{query}%"
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT
                knowledge_pages.item_id,
                knowledge_items.title,
                knowledge_items.source_type,
                knowledge_pages.page_number,
                knowledge_pages.text
            FROM knowledge_pages
            JOIN knowledge_items ON knowledge_items.id = knowledge_pages.item_id
            WHERE knowledge_pages.text LIKE ?
            ORDER BY knowledge_items.updated_at DESC, knowledge_pages.page_number
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()


def seed_items(items: Iterable[KnowledgeItem], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    count = 0
    for item in items:
        add_item(item, db_path)
        count += 1
    return count


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("topics_json", "tags_json"):
        if key in data:
            target_key = key.replace("_json", "")
            data[target_key] = json.loads(data.pop(key) or "[]")
    return data


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


def _prepare_pdf_file(
    title: str,
    *,
    file_path: str | Path | None = None,
    url: str = "",
) -> Path:
    PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if file_path:
        source = Path(file_path)
        if not source.exists():
            raise FileNotFoundError(source)
        target = PDF_STORAGE_DIR / f"{_safe_filename(title)}.pdf"
        target.write_bytes(source.read_bytes())
        return target

    if not url:
        raise ValueError("import_pdf requires either file_path or url")

    response = requests.get(url, timeout=120)
    response.raise_for_status()
    target = PDF_STORAGE_DIR / f"{_safe_filename(title)}.pdf"
    target.write_bytes(response.content)
    return target


def _extract_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    reader = PdfReader(str(pdf_path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append((index, text.strip()))
    return pages


def _safe_filename(text: str) -> str:
    safe = []
    for char in text:
        if char.isalnum():
            safe.append(char)
        elif char in {" ", "-", "_"}:
            safe.append("_")
    return "".join(safe).strip("_") or str(uuid.uuid4())
