#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared, SQLite-backed event store for push delivery and AI retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


CST = timezone(timedelta(hours=8))
VALID_SOURCES = {"nga", "arkvol", "daily_market", "daily_report", "bilibili", "portfolio", "system"}
VALID_DELIVERY_STATUSES = {"pending", "sending", "sent", "baseline", "failed"}


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(source: str, event_type: str, author: str, title: str, content: str) -> str:
    raw = "\n".join((source, event_type, author, title, content)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def stable_event_dedupe_key(
    source: str,
    event_type: str,
    event_time: str,
    author: str,
    content: str,
) -> str:
    """Return a source-ID-independent identity for the same logical message."""
    normalized_content = re.sub(r"\s+", " ", str(content)).strip()
    identity = canonical_json(
        {
            "source": str(source).strip().lower(),
            "event_type": str(event_type).strip().lower(),
            "event_time": str(event_time).strip(),
            "author": str(author).strip(),
            "content": normalized_content,
        }
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{str(source).strip().lower()}:{str(event_type).strip().lower()}:fingerprint:{digest}"


def connect_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    init_schema(db)
    return db


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            event_date TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            ingested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_time
            ON events(event_time DESC, event_id DESC);
        CREATE INDEX IF NOT EXISTS idx_events_date_source
            ON events(event_date DESC, source, event_type);
        CREATE INDEX IF NOT EXISTS idx_events_author_date
            ON events(author, event_date DESC, event_time DESC);

        CREATE TABLE IF NOT EXISTS deliveries (
            event_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'feishu',
            platform_message_ids TEXT NOT NULL DEFAULT '[]',
            last_error TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_status
            ON deliveries(status, last_attempt_at, event_id);
        """
    )


def _event_date(event_time: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(event_time))
    return match.group(1) if match else datetime.now(CST).date().isoformat()


def insert_event(
    db: sqlite3.Connection,
    *,
    event_id: str,
    source: str,
    event_type: str,
    event_time: str,
    content: str,
    author: str = "",
    title: str = "",
    source_url: str = "",
    tags: Iterable[str] | None = None,
    metadata: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    notify: bool = True,
) -> bool:
    event_id = str(event_id).strip()
    source = str(source).strip().lower()
    event_type = str(event_type).strip().lower()
    event_time = str(event_time).strip() or now_iso()
    content = str(content).strip()
    if not event_id or not source or not event_type or not content:
        raise ValueError("event_id、source、event_type 和 content 不能为空")
    if source not in VALID_SOURCES:
        raise ValueError(f"不支持的消息来源：{source}")

    tags_list = sorted({str(item).strip() for item in (tags or []) if str(item).strip()})
    metadata_value = metadata or {}
    digest = content_hash(source, event_type, author, title, content)
    # NGA can expose different pid values for the same post when a thread is
    # viewed normally versus through an author filter. Use the logical post
    # identity instead of the unstable pid-derived caller key.
    if source == "nga":
        unique_key = stable_event_dedupe_key(source, event_type, event_time, author, content)
    else:
        unique_key = str(dedupe_key or event_id).strip()
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO events(
            event_id,source,event_type,event_time,event_date,author,title,content,
            source_url,tags_json,metadata_json,content_hash,dedupe_key,ingested_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            source,
            event_type,
            event_time,
            _event_date(event_time),
            str(author).strip(),
            str(title).strip(),
            content,
            str(source_url).strip(),
            canonical_json(tags_list),
            canonical_json(metadata_value),
            digest,
            unique_key,
            now_iso(),
        ),
    )
    if cursor.rowcount:
        db.execute(
            """
            INSERT INTO deliveries(event_id,status,platform)
            VALUES(?,?, 'feishu')
            """,
            (event_id, "pending" if notify else "baseline"),
        )
        return True
    return False


def get_pending_events(db: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    safe_limit = max(1, min(int(limit), 200))
    return db.execute(
        """
        SELECT e.*, d.status, d.attempts
        FROM events e
        JOIN deliveries d ON d.event_id=e.event_id
        WHERE d.status='pending'
        ORDER BY e.event_time, e.ingested_at, e.event_id
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()


def mark_sending(db: sqlite3.Connection, event_ids: Iterable[str]) -> None:
    ids = [str(item) for item in event_ids]
    if not ids:
        return
    timestamp = now_iso()
    db.executemany(
        """
        UPDATE deliveries
        SET status='sending', attempts=attempts+1, last_attempt_at=?, last_error=''
        WHERE event_id=? AND status='pending'
        """,
        [(timestamp, event_id) for event_id in ids],
    )


def mark_sent(
    db: sqlite3.Connection,
    event_ids: Iterable[str],
    platform_message_ids: Iterable[str] | None = None,
) -> None:
    ids = [str(item) for item in event_ids]
    payload = canonical_json([str(item) for item in (platform_message_ids or []) if str(item)])
    timestamp = now_iso()
    db.executemany(
        """
        UPDATE deliveries
        SET status='sent', sent_at=?, platform_message_ids=?, last_error=''
        WHERE event_id=? AND status='sending'
        """,
        [(timestamp, payload, event_id) for event_id in ids],
    )


def mark_failed(db: sqlite3.Connection, event_ids: Iterable[str], error: str) -> None:
    ids = [str(item) for item in event_ids]
    db.executemany(
        """
        UPDATE deliveries
        SET status='failed', last_error=?
        WHERE event_id=? AND status='sending'
        """,
        [(str(error)[:2000], event_id) for event_id in ids],
    )


def requeue_failed(db: sqlite3.Connection, event_ids: Iterable[str] | None = None) -> int:
    """Explicit retry only; failed deliveries are never retried automatically."""
    ids = [str(item) for item in (event_ids or [])]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        cursor = db.execute(
            f"UPDATE deliveries SET status='pending' WHERE status='failed' AND event_id IN ({placeholders})",
            ids,
        )
    else:
        cursor = db.execute("UPDATE deliveries SET status='pending' WHERE status='failed'")
    return cursor.rowcount


def _safe_date(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"日期必须是 YYYY-MM-DD：{text}")
    return text


def search_events(
    db: sqlite3.Connection,
    *,
    query: str = "",
    source: str = "",
    author: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    event_type: str = "",
    limit: int = 50,
    ascending: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    start = _safe_date(start_date)
    end = _safe_date(end_date)
    if start:
        clauses.append("e.event_date>=?")
        params.append(start)
    if end:
        clauses.append("e.event_date<=?")
        params.append(end)
    if source:
        clauses.append("e.source=?")
        params.append(str(source).strip().lower())
    if event_type:
        clauses.append("e.event_type=?")
        params.append(str(event_type).strip().lower())
    if author:
        clauses.append("e.author LIKE ?")
        params.append(f"%{str(author).strip()}%")

    tokens = [item for item in re.split(r"\s+", str(query).strip()) if item]
    for token in tokens[:8]:
        clauses.append(
            "(e.author LIKE ? OR e.title LIKE ? OR e.content LIKE ? OR e.tags_json LIKE ?)"
        )
        needle = f"%{token}%"
        params.extend((needle, needle, needle, needle))

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    direction = "ASC" if ascending else "DESC"
    safe_limit = max(1, min(int(limit), 200))
    params.append(safe_limit)
    return db.execute(
        f"""
        SELECT e.*, d.status AS delivery_status, d.sent_at
        FROM events e
        JOIN deliveries d ON d.event_id=e.event_id
        {where}
        ORDER BY e.event_time {direction}, e.ingested_at {direction}, e.event_id {direction}
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_event(db: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT e.*, d.status AS delivery_status, d.sent_at, d.last_error
        FROM events e JOIN deliveries d ON d.event_id=e.event_id
        WHERE e.event_id=?
        """,
        (str(event_id),),
    ).fetchone()


def event_as_dict(row: sqlite3.Row, include_content: bool = True) -> dict[str, Any]:
    result = {
        "event_id": row["event_id"],
        "source": row["source"],
        "event_type": row["event_type"],
        "event_time": row["event_time"],
        "author": row["author"],
        "title": row["title"],
        "source_url": row["source_url"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }
    if include_content:
        result["content"] = row["content"]
    if "delivery_status" in row.keys():
        result["delivery_status"] = row["delivery_status"]
    return result


def database_stats(db: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        row["source"]: row["count"]
        for row in db.execute("SELECT source, COUNT(*) AS count FROM events GROUP BY source")
    }
    deliveries = {
        row["status"]: row["count"]
        for row in db.execute("SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status")
    }
    latest = db.execute("SELECT MAX(event_time) AS value FROM events").fetchone()["value"]
    return {"events_by_source": counts, "deliveries": deliveries, "latest_event_time": latest or ""}


def backup_database(source_path: str | Path, backup_dir: str | Path) -> Path:
    """Create a consistent SQLite online backup without deleting older backups."""
    source_path = Path(source_path)
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"feishu_bot-{timestamp}.db"
    source = connect_db(source_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite 备份校验失败：{check}")
    finally:
        destination.close()
        source.close()
    return target
