#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archive and remove duplicate NGA rows using stable logical fingerprints."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import message_hub


STATUS_RANK = {"sent": 5, "sending": 4, "pending": 3, "failed": 2, "baseline": 1}


def parse_args():
    parser = argparse.ArgumentParser(description="Deduplicate the unified NGA message store")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def event_key(row):
    return message_hub.stable_event_dedupe_key(
        row["source"], row["event_type"], row["event_time"], row["author"], row["content"]
    )


def post_time(row):
    value = f"{row['post_date']}T{row['post_time']}"
    if len(row["post_time"].split(":")) == 2:
        value += ":00"
    return value + "+08:00"


def post_key(row):
    return message_hub.stable_event_dedupe_key(
        "nga", "forum_post", post_time(row), row["author"], row["text"]
    )


def choose_event(rows):
    return max(
        rows,
        key=lambda row: (
            STATUS_RANK.get(row["delivery_status"], 0),
            bool(row["source_url"]),
            bool(row["metadata_json"] and row["metadata_json"] != "{}"),
            -len(row["event_id"]),
            row["ingested_at"],
        ),
    )


def choose_post(rows):
    return max(
        rows,
        key=lambda row: (
            int(row["notified"]),
            bool(row["pid"]),
            bool(row["tid"]),
            row["first_seen"],
        ),
    )


def init_archive_tables(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS duplicate_events_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
            archived_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            event_date TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_url TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL,
            delivery_attempts INTEGER NOT NULL,
            delivery_last_attempt_at TEXT NOT NULL,
            delivery_sent_at TEXT NOT NULL,
            delivery_platform TEXT NOT NULL,
            delivery_platform_message_ids TEXT NOT NULL,
            delivery_last_error TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS duplicate_posts_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
            archived_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            canonical_post_key TEXT NOT NULL,
            original_post_key TEXT NOT NULL UNIQUE,
            tid TEXT NOT NULL,
            pid TEXT NOT NULL,
            post_date TEXT NOT NULL,
            post_time TEXT NOT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            notified INTEGER NOT NULL
        );
        """
    )


def merge_delivery(db, canonical, rows):
    statuses = [row["delivery_status"] for row in rows]
    status = max(statuses, key=lambda value: STATUS_RANK.get(value, 0))
    attempts = sum(int(row["delivery_attempts"]) for row in rows)
    sent_times = sorted(row["delivery_sent_at"] for row in rows if row["delivery_sent_at"])
    message_ids = []
    for row in rows:
        try:
            values = json.loads(row["delivery_platform_message_ids"] or "[]")
        except ValueError:
            values = []
        for value in values:
            if value and value not in message_ids:
                message_ids.append(value)
    errors = [row["delivery_last_error"] for row in rows if row["delivery_last_error"]]
    db.execute(
        """
        UPDATE deliveries
        SET status=?, attempts=?, sent_at=?, platform_message_ids=?, last_error=?
        WHERE event_id=?
        """,
        (
            status,
            attempts,
            sent_times[0] if sent_times else "",
            json.dumps(message_ids, ensure_ascii=False),
            " | ".join(dict.fromkeys(errors))[:2000],
            canonical["event_id"],
        ),
    )


def main():
    args = parse_args()
    with message_hub.connect_db(args.db) as db:
        event_rows = db.execute(
            """
            SELECT e.*,
                   d.status AS delivery_status,
                   d.attempts AS delivery_attempts,
                   d.last_attempt_at AS delivery_last_attempt_at,
                   d.sent_at AS delivery_sent_at,
                   d.platform AS delivery_platform,
                   d.platform_message_ids AS delivery_platform_message_ids,
                   d.last_error AS delivery_last_error
            FROM events e JOIN deliveries d ON d.event_id=e.event_id
            WHERE e.source='nga'
            """
        ).fetchall()
        event_groups = defaultdict(list)
        for row in event_rows:
            event_groups[event_key(row)].append(row)
        duplicate_event_groups = [rows for rows in event_groups.values() if len(rows) > 1]

        post_rows = db.execute("SELECT * FROM posts").fetchall()
        post_groups = defaultdict(list)
        for row in post_rows:
            post_groups[post_key(row)].append(row)
        duplicate_post_groups = [rows for rows in post_groups.values() if len(rows) > 1]

        print(
            f"event_rows={len(event_rows)} event_unique={len(event_groups)} "
            f"event_duplicate_groups={len(duplicate_event_groups)} "
            f"event_rows_to_archive={sum(len(rows)-1 for rows in duplicate_event_groups)}"
        )
        print(
            f"post_rows={len(post_rows)} post_unique={len(post_groups)} "
            f"post_duplicate_groups={len(duplicate_post_groups)} "
            f"post_rows_to_archive={sum(len(rows)-1 for rows in duplicate_post_groups)}"
        )
        if args.dry_run:
            print("dry_run=true database_unchanged=true")
            return

        init_archive_tables(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        archived_at = message_hub.now_iso()

        for rows in duplicate_event_groups:
            canonical = choose_event(rows)
            merge_delivery(db, canonical, rows)
            for row in rows:
                if row["event_id"] == canonical["event_id"]:
                    continue
                db.execute(
                    """
                    INSERT OR IGNORE INTO duplicate_events_archive(
                        archived_at,reason,canonical_event_id,original_event_id,
                        source,event_type,event_time,event_date,author,title,content,
                        source_url,tags_json,metadata_json,content_hash,dedupe_key,ingested_at,
                        delivery_status,delivery_attempts,delivery_last_attempt_at,
                        delivery_sent_at,delivery_platform,delivery_platform_message_ids,
                        delivery_last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        archived_at, "stable_content_fingerprint", canonical["event_id"], row["event_id"],
                        row["source"], row["event_type"], row["event_time"], row["event_date"],
                        row["author"], row["title"], row["content"], row["source_url"],
                        row["tags_json"], row["metadata_json"], row["content_hash"],
                        row["dedupe_key"], row["ingested_at"], row["delivery_status"],
                        row["delivery_attempts"], row["delivery_last_attempt_at"],
                        row["delivery_sent_at"], row["delivery_platform"],
                        row["delivery_platform_message_ids"], row["delivery_last_error"],
                    ),
                )
                db.execute("DELETE FROM deliveries WHERE event_id=?", (row["event_id"],))
                db.execute("DELETE FROM events WHERE event_id=?", (row["event_id"],))

        for key, rows in event_groups.items():
            canonical = choose_event(rows)
            db.execute("UPDATE events SET dedupe_key=? WHERE event_id=?", (key, canonical["event_id"]))

        for rows in duplicate_post_groups:
            canonical = choose_post(rows)
            stable_key = post_key(canonical)
            for row in rows:
                if row["post_key"] == canonical["post_key"]:
                    continue
                db.execute(
                    """
                    INSERT OR IGNORE INTO duplicate_posts_archive(
                        archived_at,reason,canonical_post_key,original_post_key,tid,pid,
                        post_date,post_time,author,text,first_seen,notified
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        archived_at, "stable_content_fingerprint", stable_key, row["post_key"],
                        row["tid"], row["pid"], row["post_date"], row["post_time"],
                        row["author"], row["text"], row["first_seen"], row["notified"],
                    ),
                )
                db.execute("DELETE FROM posts WHERE post_key=?", (row["post_key"],))

        # Post keys are independent from events, so they can be migrated after
        # duplicate post rows are archived and removed.
        for rows in post_groups.values():
            canonical = choose_post(rows)
            db.execute(
                "UPDATE posts SET post_key=? WHERE post_key=?",
                (post_key(canonical), canonical["post_key"]),
            )

        db.commit()
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        active_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        active_posts = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        archived_events = db.execute("SELECT COUNT(*) FROM duplicate_events_archive").fetchone()[0]
        archived_posts = db.execute("SELECT COUNT(*) FROM duplicate_posts_archive").fetchone()[0]
        statuses = Counter(
            row[0] for row in db.execute("SELECT status FROM deliveries").fetchall()
        )

    print(
        f"active_events={active_events} active_posts={active_posts} "
        f"archived_events={archived_events} archived_posts={archived_posts} "
        f"quick_check={check} delivery_statuses={dict(statuses)}"
    )


if __name__ == "__main__":
    main()
