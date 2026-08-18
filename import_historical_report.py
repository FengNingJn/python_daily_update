#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import an archived NGA Markdown report into the unified message hub.

Historical rows are always inserted as baseline deliveries, so importing an
archive never pushes old posts to Feishu. Existing event IDs are left intact.
"""

import argparse
from collections import Counter
from pathlib import Path

import message_hub
import nas_runner


def parse_args():
    parser = argparse.ArgumentParser(description="Import an archived NGA report without notifications")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    report = args.report.resolve()
    if not report.is_file():
        raise SystemExit(f"Report not found: {report}")

    nas_runner.REPORT_FILE = report
    posts = nas_runner.parse_report()
    if not posts:
        raise SystemExit("No historical posts parsed; database was not changed")

    dates = [post["date"] for post in posts]
    author_counts = Counter(post["author"] for post in posts)
    print(
        f"parsed={len(posts)} date_min={min(dates)} date_max={max(dates)} "
        f"authors={len(author_counts)} with_pid={sum(bool(post.get('pid')) for post in posts)}"
    )
    if args.dry_run:
        print("dry_run=true database_unchanged=true")
        return

    inserted = 0
    existing = 0
    with message_hub.connect_db(args.db) as db:
        for post in posts:
            event_time = f"{post['date']}T{post['time']}"
            if len(post["time"].split(":")) == 2:
                event_time += ":00"
            event_time += "+08:00"
            tid = str(post.get("tid", "")).strip()
            pid = str(post.get("pid", "")).strip()
            source_url = f"https://ngabbs.com/read.php?tid={tid}" if tid else ""
            if source_url and pid:
                source_url += f"&pid={pid}"
            event_id = f"nga:{post['key']}"
            created = message_hub.insert_event(
                db,
                event_id=event_id,
                source="nga",
                event_type="forum_post",
                event_time=event_time,
                author=post["author"],
                title=f"{post['author']} 的 NGA 历史发言",
                content=post["text"],
                source_url=source_url,
                tags=[post["author"], "historical_import"],
                metadata={
                    "tid": tid,
                    "pid": pid,
                    "historical_import": True,
                    "archive_file": report.name,
                },
                dedupe_key=event_id,
                notify=False,
            )
            inserted += int(created)
            existing += int(not created)
        db.commit()
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        baseline = db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE status='baseline'"
        ).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    print(
        f"inserted={inserted} existing={existing} total_events={total} "
        f"baseline={baseline} quick_check={check} notifications_queued=0"
    )


if __name__ == "__main__":
    main()
