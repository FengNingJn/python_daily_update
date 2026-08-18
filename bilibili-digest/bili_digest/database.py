from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(Path(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    init_schema(db)
    return db


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS creators (
            uid TEXT PRIMARY KEY,
            up_name TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            checkpoint_published_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS videos (
            bvid TEXT NOT NULL,
            cid TEXT NOT NULL,
            uid TEXT NOT NULL,
            up_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            part_title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            duration INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            aid TEXT NOT NULL DEFAULT '',
            up_mid TEXT NOT NULL DEFAULT '',
            page INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'discovered',
            error TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            discovered_at TEXT NOT NULL,
            processed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (bvid, cid),
            FOREIGN KEY (uid) REFERENCES creators(uid) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_videos_uid_published
            ON videos(uid, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_videos_status
            ON videos(status, published_at);

        CREATE TABLE IF NOT EXISTS transcript_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bvid TEXT NOT NULL,
            cid TEXT NOT NULL,
            version INTEGER NOT NULL,
            transcript_source TEXT NOT NULL,
            transcript TEXT NOT NULL,
            segments_json TEXT NOT NULL DEFAULT '[]',
            source_detail_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (bvid, cid, version),
            UNIQUE (bvid, cid, content_hash),
            FOREIGN KEY (bvid, cid) REFERENCES videos(bvid, cid) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS video_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_version_id INTEGER NOT NULL UNIQUE,
            uid TEXT NOT NULL,
            bvid TEXT NOT NULL,
            cid TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            summary_markdown TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (transcript_version_id) REFERENCES transcript_versions(id) ON DELETE RESTRICT,
            FOREIGN KEY (uid) REFERENCES creators(uid) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_summaries_uid_created
            ON video_summaries(uid, created_at DESC);

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            discovered_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            digest_path TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS run_items (
            run_id INTEGER NOT NULL,
            bvid TEXT NOT NULL,
            cid TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (run_id, bvid, cid),
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE RESTRICT
        );
        """
    )
    db.commit()


def ensure_creator(db: sqlite3.Connection, uid: str, name: str = "") -> None:
    db.execute(
        """
        INSERT INTO creators(uid,up_name,first_seen_at) VALUES(?,?,?)
        ON CONFLICT(uid) DO UPDATE SET
            up_name=CASE WHEN excluded.up_name<>'' THEN excluded.up_name ELSE creators.up_name END
        """,
        (uid, name, now_iso()),
    )


def creator_checkpoint(db: sqlite3.Connection, uid: str) -> str:
    row = db.execute("SELECT checkpoint_published_at FROM creators WHERE uid=?", (uid,)).fetchone()
    return str(row[0] or "") if row else ""


def update_creator_status(db: sqlite3.Connection, uid: str, *, success: bool, checkpoint: str = "", error: str = "") -> None:
    timestamp = now_iso()
    db.execute(
        """
        UPDATE creators SET
            last_checked_at=?,
            last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
            checkpoint_published_at=CASE WHEN ?<>'' THEN ? ELSE checkpoint_published_at END,
            last_error=?
        WHERE uid=?
        """,
        (timestamp, int(success), timestamp, checkpoint, checkpoint, error[:2000], uid),
    )


def upsert_video(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO videos(
            bvid,cid,uid,up_name,title,part_title,url,published_at,duration,
            description,aid,up_mid,page,status,discovered_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item["bvid"], item["cid"], item["uid"], item.get("up_name", ""),
            item["title"], item.get("part_title", ""), item["url"], item["published_at"],
            int(item.get("duration", 0)), item.get("description", ""), item.get("aid", ""),
            item.get("up_mid", ""), int(item.get("page", 1)), "discovered", now_iso(),
        ),
    )
    if not cursor.rowcount:
        db.execute(
            """
            UPDATE videos SET up_name=?,title=?,part_title=?,url=?,duration=?,description=?,aid=?,up_mid=?,page=?
            WHERE bvid=? AND cid=?
            """,
            (
                item.get("up_name", ""), item["title"], item.get("part_title", ""), item["url"],
                int(item.get("duration", 0)), item.get("description", ""), item.get("aid", ""),
                item.get("up_mid", ""), int(item.get("page", 1)), item["bvid"], item["cid"],
            ),
        )
    return bool(cursor.rowcount)


def set_video_status(db: sqlite3.Connection, bvid: str, cid: str, status: str, error: str = "") -> None:
    db.execute(
        """
        UPDATE videos SET status=?,error=?,retry_count=retry_count+?,
            processed_at=CASE WHEN ?='processed' THEN ? ELSE processed_at END
        WHERE bvid=? AND cid=?
        """,
        (status, error[:4000], int(status == "failed"), status, now_iso(), bvid, cid),
    )


def add_transcript(db: sqlite3.Connection, item: dict[str, Any], transcript: dict[str, Any], content_hash: str) -> tuple[int, int, bool]:
    existing = db.execute(
        "SELECT id,version FROM transcript_versions WHERE bvid=? AND cid=? AND content_hash=?",
        (item["bvid"], item["cid"], content_hash),
    ).fetchone()
    if existing:
        return int(existing["id"]), int(existing["version"]), False
    next_version = db.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM transcript_versions WHERE bvid=? AND cid=?",
        (item["bvid"], item["cid"]),
    ).fetchone()[0]
    cursor = db.execute(
        """
        INSERT INTO transcript_versions(
            bvid,cid,version,transcript_source,transcript,segments_json,
            source_detail_json,content_hash,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            item["bvid"], item["cid"], next_version, transcript["source"], transcript["text"],
            json.dumps(transcript.get("segments", []), ensure_ascii=False),
            json.dumps(transcript.get("detail", {}), ensure_ascii=False), content_hash, now_iso(),
        ),
    )
    return int(cursor.lastrowid), int(next_version), True


def has_summary(db: sqlite3.Connection, transcript_version_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM video_summaries WHERE transcript_version_id=?", (transcript_version_id,)
    ).fetchone() is not None


def latest_transcript(db: sqlite3.Connection, bvid: str, cid: str) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT * FROM transcript_versions
        WHERE bvid=? AND cid=? ORDER BY version DESC LIMIT 1
        """,
        (bvid, cid),
    ).fetchone()


def summary_for_transcript(db: sqlite3.Connection, transcript_version_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM video_summaries WHERE transcript_version_id=?", (transcript_version_id,)
    ).fetchone()


def video_status(db: sqlite3.Connection, bvid: str, cid: str) -> str:
    row = db.execute("SELECT status FROM videos WHERE bvid=? AND cid=?", (bvid, cid)).fetchone()
    return str(row[0]) if row else ""


def pending_bvids(db: sqlite3.Connection, uid: str) -> list[str]:
    return [
        str(row[0])
        for row in db.execute(
            "SELECT DISTINCT bvid FROM videos WHERE uid=? AND status<>'processed' ORDER BY published_at",
            (uid,),
        ).fetchall()
    ]


def bvid_exists(db: sqlite3.Connection, bvid: str) -> bool:
    return db.execute("SELECT 1 FROM videos WHERE bvid=? LIMIT 1", (bvid,)).fetchone() is not None


def videos_without_transcript(
    db: sqlite3.Connection, *, limit: int | None = None, shortest_first: bool = False,
    skip_whisper_failed: bool = False,
) -> list[sqlite3.Row]:
    sql = """
        SELECT v.* FROM videos v
        WHERE NOT EXISTS (
            SELECT 1 FROM transcript_versions t
            WHERE t.bvid=v.bvid AND t.cid=v.cid
        )
    """
    if skip_whisper_failed:
        sql += " AND v.status<>'whisper_failed'"
    if shortest_first:
        sql += " ORDER BY CASE WHEN v.duration>0 THEN v.duration ELSE 2147483647 END, v.published_at DESC"
    else:
        sql += " ORDER BY v.published_at DESC, v.uid, v.bvid, v.cid"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (max(0, int(limit)),)
    return db.execute(sql, params).fetchall()


def add_summary(
    db: sqlite3.Connection,
    transcript_version_id: int,
    item: dict[str, Any],
    summary: dict[str, Any],
    markdown: str,
    model: str,
) -> int:
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO video_summaries(
            transcript_version_id,uid,bvid,cid,summary_json,summary_markdown,model,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            transcript_version_id, item["uid"], item["bvid"], item["cid"],
            json.dumps(summary, ensure_ascii=False), markdown, model, now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def recent_view_context(db: sqlite3.Connection, uid: str, before: str, limit: int) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT v.published_at,v.title,s.summary_json
        FROM video_summaries s
        JOIN videos v ON v.bvid=s.bvid AND v.cid=s.cid
        WHERE s.uid=? AND v.published_at<?
        ORDER BY v.published_at DESC LIMIT ?
        """,
        (uid, before, max(0, int(limit))),
    ).fetchall()
    return [
        {"published_at": row["published_at"], "title": row["title"], "summary": json.loads(row["summary_json"])}
        for row in rows
    ]


def start_run(db: sqlite3.Connection, slot: str) -> int:
    cursor = db.execute(
        "INSERT INTO runs(slot,started_at,status) VALUES(?,?,?)", (slot, now_iso(), "running")
    )
    db.commit()
    return int(cursor.lastrowid)


def finish_run(db: sqlite3.Connection, run_id: int, *, status: str, discovered: int, processed: int, failed: int, digest_path: str = "", error: str = "") -> None:
    db.execute(
        """
        UPDATE runs SET finished_at=?,status=?,discovered_count=?,processed_count=?,
            failed_count=?,digest_path=?,error=? WHERE id=?
        """,
        (now_iso(), status, discovered, processed, failed, digest_path, error[:4000], run_id),
    )
    db.commit()


def add_run_item(db: sqlite3.Connection, run_id: int, bvid: str, cid: str, status: str, error: str = "") -> None:
    db.execute(
        """
        INSERT INTO run_items(run_id,bvid,cid,status,error) VALUES(?,?,?,?,?)
        ON CONFLICT(run_id,bvid,cid) DO UPDATE SET status=excluded.status,error=excluded.error
        """,
        (run_id, bvid, cid, status, error[:2000]),
    )
