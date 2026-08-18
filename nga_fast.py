#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Low-latency NGA watcher that writes new tracked posts directly to the message hub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import message_hub
import update_nga


CST = update_nga.CST
DATA_DIR = Path(os.environ.get("NGA_OUTPUT_DIR", "/data"))
DB_FILE = Path(os.environ.get("MESSAGE_HUB_DB_FILE", "/bot-state/feishu_bot.db"))
LOG_FILE = Path(os.environ.get("NGA_FAST_LOG_FILE", "/logs/fast_runner.log"))
INTERVAL_SECONDS = max(30, int(os.environ.get("NGA_FAST_INTERVAL_SECONDS", "60")))
REQUEST_TIMEOUT = max(2, int(os.environ.get("NGA_FAST_REQUEST_TIMEOUT", "5")))
MAX_WORKERS = max(2, int(os.environ.get("NGA_FAST_MAX_WORKERS", "12")))
MAX_THREADS_PER_USER = max(1, int(os.environ.get("NGA_FAST_MAX_THREADS_PER_USER", "5")))
FIRST_RUN_NOTIFY_MINUTES = max(1, int(os.environ.get("NGA_FAST_FIRST_RUN_NOTIFY_MINUTES", "5")))


def log(message: str) -> None:
    line = f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _load_thread_cache() -> dict:
    path = DATA_DIR / "thread_cache.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_targets() -> list[tuple[str, str]]:
    """Return (tid, author_uid) targets; blank uid means scan the thread's last page."""
    targets: set[tuple[str, str]] = {(str(tid), "") for tid, _ in update_nga.TRACKED_THREADS}
    cache = _load_thread_cache()
    for uid in update_nga.TRACKED_UIDS:
        tids: list[str] = []
        tids.extend(str(tid) for tid, _ in update_nga.KNOWN_USER_THREADS.get(uid, []))
        entry = cache.get(uid, {})
        for item in entry.get("threads", [])[:MAX_THREADS_PER_USER]:
            tid = str(item.get("tid", "")).strip()
            if tid:
                tids.append(tid)
        for tid in dict.fromkeys(tids):
            targets.add((tid, uid))
    return sorted(targets)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(update_nga.HEADERS)
    for key, value in update_nga.load_cookies().items():
        session.cookies.set(key, value)
    return session


def _fetch_target(target: tuple[str, str]) -> tuple[list[dict], str]:
    tid, author_uid = target
    url = f"https://ngabbs.com/read.php?tid={tid}"
    if author_uid:
        url += f"&authorid={author_uid}"
    url += "&page=-1"
    try:
        with _session() as session:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"
        html = response.content.decode("gbk", errors="replace")
        if "ERROR:15" in html[:1000]:
            return [], "NGA ERROR:15"
        posts = []
        for table in BeautifulSoup(html, "lxml").find_all("table", class_=re.compile(r"forumbox")):
            post = update_nga.extract_post(table, tid, download_media=False)
            if not post or not post.get("uid") or not post.get("time") or not post.get("content"):
                continue
            if author_uid and post["uid"] != author_uid:
                continue
            if post["uid"] not in update_nga.TRACKED_UIDS:
                continue
            posts.append(post)
        return posts, ""
    except requests.RequestException as exc:
        return [], type(exc).__name__
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def collect_latest() -> tuple[list[dict], int, int]:
    targets = build_targets()
    posts: dict[str, dict] = {}
    failures = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_target, target): target for target in targets}
        for future in as_completed(futures):
            result, error = future.result()
            if error:
                failures += 1
                continue
            for post in result:
                normalized = re.sub(r"\s+", " ", post["content"]).strip()
                identity = "|".join((post["uid"], post["time"], normalized))
                posts.setdefault(identity, post)
    ordered = sorted(posts.values(), key=lambda item: (item["time"], item["uid"], item.get("pid", "")))
    return ordered, len(targets), failures


def _event_content(post: dict) -> str:
    content = str(post.get("content", "")).strip()
    quoted = str(post.get("quoted", "")).strip()
    if quoted:
        # Keep the same quote length as the Markdown archive path so the
        # shared logical fingerprint suppresses fast/full duplicates.
        content += f"  [引用: {quoted[:80]}]"
    return content


def _event_time(post: dict) -> str:
    return datetime.strptime(post["time"], "%Y-%m-%d %H:%M").replace(tzinfo=CST).isoformat()


def _insert_batch(posts: list[dict]) -> tuple[int, int]:
    for attempt in range(1, 6):
        try:
            with closing(message_hub.connect_db(DB_FILE)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                row = db.execute(
                    "SELECT value FROM settings WHERE key='nga_fast_baseline_done'"
                ).fetchone()
                baseline_done = bool(row and row[0] == "1")
                recent_cutoff = datetime.now(CST) - timedelta(minutes=FIRST_RUN_NOTIFY_MINUTES)
                inserted = queued = 0
                for post in posts:
                    content = _event_content(post)
                    event_time = _event_time(post)
                    post_dt = datetime.fromisoformat(event_time)
                    notify = baseline_done or post_dt >= recent_cutoff
                    identity = "|".join(
                        (post.get("tid", ""), post.get("uid", ""), event_time, content)
                    )
                    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    tid = str(post.get("tid", "")).strip()
                    pid = str(post.get("pid", "")).strip()
                    author = update_nga.TRACKED_UIDS.get(post["uid"], f"UID{post['uid']}")
                    source_url = f"https://ngabbs.com/read.php?tid={tid}"
                    if pid:
                        source_url += f"&pid={pid}"
                    created = message_hub.insert_event(
                        db,
                        event_id=f"nga:fast:{digest}",
                        source="nga",
                        event_type="forum_post",
                        event_time=event_time,
                        author=author,
                        title=f"{author} 的NGA发言",
                        content=content,
                        source_url=source_url,
                        tags=[author],
                        metadata={"tid": tid, "pid": pid, "ingest_path": "fast"},
                        dedupe_key=f"nga:fast:{digest}",
                        notify=notify,
                    )
                    inserted += int(created)
                    queued += int(created and notify)
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('nga_fast_baseline_done','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'"
                )
                db.commit()
                return inserted, queued
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(attempt * 2)
    return 0, 0


def run_once() -> tuple[int, int]:
    started = time.monotonic()
    posts, targets, failures = collect_latest()
    inserted, queued = _insert_batch(posts)
    elapsed = time.monotonic() - started
    log(
        f"快速扫描完成: 目标={targets} 失败={failures} 候选={len(posts)} "
        f"新增={inserted} 待推送={queued} 耗时={elapsed:.1f}s"
    )
    return inserted, queued


def main() -> None:
    parser = argparse.ArgumentParser(description="Low-latency NGA watcher")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS)
    args = parser.parse_args()
    if args.once:
        run_once()
        return
    interval = max(30, args.interval)
    log(f"快速抓取服务启动，间隔={interval}s")
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            log(f"快速扫描失败: {type(exc).__name__}: {exc}")
        elapsed = time.monotonic() - started
        time.sleep(max(5, interval - elapsed))


if __name__ == "__main__":
    main()
