#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the NGA incremental updater repeatedly and push newly archived posts."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    import fcntl
except ImportError:  # Windows local verification
    fcntl = None
    import msvcrt


CST = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NGA_OUTPUT_DIR", BASE_DIR))
CONFIG_FILE = Path(os.environ.get("NGA_CONFIG_FILE", DATA_DIR / "config.json"))
STATE_FILE = Path(os.environ.get("NGA_STATE_FILE", DATA_DIR / "state" / "push_state.json"))
LOG_FILE = Path(os.environ.get("NGA_LOG_FILE", DATA_DIR / "logs" / "nas_runner.log"))
LOCK_FILE = Path(os.environ.get("NGA_LOCK_FILE", DATA_DIR / "state" / "update.lock"))
REPORT_FILE = DATA_DIR / "nga_daily_report.md"
UPDATE_SCRIPT = BASE_DIR / "update_nga.py"
LOCK_HANDLE = None

DEFAULT_CONFIG = {
    "interval_seconds": 300,
    "update_days": 1,
    "update_timeout_seconds": 270,
    "watch_authors": [
        "狼大",
        "海指导",
        "灰兔尾",
        "zippo578",
        "Plezl",
        "村上吹树",
        "fuelish",
        "铁锤狂砸盘",
        "丨阿疯",
    ],
    "max_items_per_push": 30,
    "max_chars_per_push": 5000,
    # ntfy 的普通消息上限约为 4 KiB；超过后可能被转成临时附件。
    "max_bytes_per_push": 3500,
    "push": {
        "provider": "none",
        "serverchan_sendkey": "",
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
    },
}


def log(message):
    timestamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    if not CONFIG_FILE.exists():
        log(f"配置文件不存在：{CONFIG_FILE}，使用安全默认值（不推送）")
        return DEFAULT_CONFIG
    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        return deep_merge(DEFAULT_CONFIG, json.load(handle))


def load_state():
    if not STATE_FILE.exists():
        return {"sent_keys": [], "failed_pushes": [], "last_success": None}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        # 兼容旧版 push_state.json，升级过程不会重新推送历史消息。
        if not isinstance(state.get("sent_keys"), list):
            state["sent_keys"] = list(state.get("seen", [])) if isinstance(state.get("seen"), list) else []
        if not isinstance(state.get("failed_pushes"), list):
            state["failed_pushes"] = []
        return state
    except (OSError, json.JSONDecodeError):
        return {"sent_keys": [], "failed_pushes": [], "last_success": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temp_file, STATE_FILE)


def acquire_lock(_stale_seconds):
    global LOCK_HANDLE
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_HANDLE = LOCK_FILE.open("a+", encoding="ascii")
        if fcntl is not None:
            fcntl.flock(LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            LOCK_HANDLE.seek(0)
            if not LOCK_HANDLE.read(1):
                LOCK_HANDLE.write("0")
                LOCK_HANDLE.flush()
            LOCK_HANDLE.seek(0)
            msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        LOCK_HANDLE.seek(0)
        LOCK_HANDLE.truncate()
        LOCK_HANDLE.write(str(os.getpid()))
        LOCK_HANDLE.flush()
        return True
    except (BlockingIOError, OSError):
        if LOCK_HANDLE is not None:
            LOCK_HANDLE.close()
            LOCK_HANDLE = None
        return False


def release_lock():
    global LOCK_HANDLE
    if LOCK_HANDLE is not None:
        if fcntl is not None:
            fcntl.flock(LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        else:
            LOCK_HANDLE.seek(0)
            msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        LOCK_HANDLE.close()
        LOCK_HANDLE = None


def normalize_text(text):
    text = re.sub(r"<!--\s*nga:.*?-->", " ", text, flags=re.I | re.S)
    text = re.sub(r"\[img\].*?\[/img\]", "[图片]", text, flags=re.I | re.S)
    text = re.sub(
        r"\[(?:/?quote|/?b|/?pid(?:=[^\]]+)?|/?uid(?:=[^\]]+)?)[^\]]*\]",
        " ",
        text,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip()


def split_reply_context(text):
    """Split a tracked author's reply from the quoted post it answers.

    Historical reports store the quote at the end as ``[引用: ...]``.  Keep
    the stored text unchanged for deduplication, but expose the two semantic
    parts so every delivery channel can render them unambiguously.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    legacy = re.search(r"\s*\[引用:\s*(.*?)\]\s*$", value, flags=re.S)
    if legacy:
        reply = value[:legacy.start()].strip()
        context = legacy.group(1).strip()
        if reply and context:
            return context, reply

    context_marker = "【被回复内容 / 提问】"
    reply_marker = "【UP 回复】"
    context_at = value.find(context_marker)
    reply_at = value.find(reply_marker)
    if 0 <= context_at < reply_at:
        context = value[context_at + len(context_marker):reply_at].strip()
        reply = value[reply_at + len(reply_marker):].strip()
        if reply and context:
            return context, reply
    return "", value


def format_nga_post(text):
    """Render one NGA post with mobile-friendly context/reply separators."""
    context, reply = split_reply_context(text)
    if context:
        return (
            "────【被回复内容 / 提问】────\n"
            f"{context}\n\n"
            "────【UP 回复】────\n"
            f"{reply}"
        )
    return f"────【UP 发言】────\n{reply}"


def legacy_post_key(post):
    """兼容没有 pid 元数据的旧报告。"""
    raw = f"{post['date']}|{post['author']}|{post['time']}|{post['text']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def post_key(post):
    if post.get("pid"):
        return f"nga:{post.get('tid', '')}:{post['pid']}"
    return legacy_post_key(post)


def all_post_keys(post):
    """返回稳定 pid 键以及升级前的正文键，保证状态格式迁移不重推。"""
    return {post["key"], legacy_post_key(post)}


def parse_report():
    if not REPORT_FILE.exists():
        return []
    content = REPORT_FILE.read_text(encoding="utf-8", errors="replace")
    date_matches = list(re.finditer(r"(?m)^### (\d{4}-\d{2}-\d{2}) \(\d+条\)\s*$", content))
    posts = []
    for date_index, date_match in enumerate(date_matches):
        date_text = date_match.group(1)
        block_end = date_matches[date_index + 1].start() if date_index + 1 < len(date_matches) else len(content)
        date_block = content[date_match.end():block_end]
        report_tail = re.search(r"(?m)^## (?:每日市场数据|明日展望) - ", date_block)
        if report_tail:
            date_block = date_block[:report_tail.start()]
        # 日期区块末尾的 Markdown 分隔线不属于最后一条发言。若不剥离，
        # 原来的最后一条在下一轮新增帖子后会发生指纹变化并被重复推送。
        date_block = re.sub(r"\n---\s*$", "", date_block)
        author_matches = list(re.finditer(r"(?m)^\*\*(.+?)\*\* \(\d+条\)\s*$", date_block))
        for author_index, author_match in enumerate(author_matches):
            author = author_match.group(1).strip()
            author_end = author_matches[author_index + 1].start() if author_index + 1 < len(author_matches) else len(date_block)
            author_block = date_block[author_match.end():author_end]
            item_matches = list(re.finditer(r"(?m)^- \[(\d{2}:\d{2})\]\s*(.*)$", author_block))
            for item_index, item_match in enumerate(item_matches):
                item_end = item_matches[item_index + 1].start() if item_index + 1 < len(item_matches) else len(author_block)
                full_text = item_match.group(2) + author_block[item_match.end():item_end]
                source = re.search(r"<!--\s*nga:tid=(\d*)\s+pid=(\d*)\s*-->", full_text, flags=re.I)
                cleaned = normalize_text(full_text)
                if cleaned:
                    post = {
                        "date": date_text,
                        "author": author,
                        "time": item_match.group(1),
                        "text": cleaned,
                        "tid": source.group(1) if source else "",
                        "pid": source.group(2) if source else "",
                    }
                    post["key"] = post_key(post)
                    posts.append(post)
    # 报告合并或历史归档中即使出现重复段，也只保留同一帖子一次。
    unique = {}
    for post in posts:
        unique.setdefault(post["key"], post)
    return list(unique.values())


def run_update(config):
    command = [
        sys.executable,
        str(UPDATE_SCRIPT),
        "--inc",
        f"--days={int(config['update_days'])}",
    ]
    env = os.environ.copy()
    env["NGA_OUTPUT_DIR"] = str(DATA_DIR)
    log("开始增量更新")
    result = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=int(config["update_timeout_seconds"]),
        check=False,
    )
    output_tail = "\n".join(result.stdout.splitlines()[-20:])
    if output_tail:
        log("更新输出：\n" + output_tail)
    if result.returncode != 0:
        raise RuntimeError(f"更新脚本退出码 {result.returncode}")


def truncate_utf8(text, max_bytes, suffix="\n\n（内容已截断，请查看完整报告）"):
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    budget = max(0, max_bytes - len(suffix_bytes))
    prefix = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return prefix + suffix


def build_message(posts, config):
    selected = posts[: int(config["max_items_per_push"])]
    sections = []
    current_author = None
    for post in selected:
        if post["author"] != current_author:
            current_author = post["author"]
            sections.append(f"\n### {current_author}")
        sections.append(f"\n- {post['time']}\n{format_nga_post(post['text'])}")
    body = "\n".join(sections).strip()
    if len(posts) > len(selected):
        body += f"\n\n另有 {len(posts) - len(selected)} 条，请查看完整报告。"
    # 按 UTF-8 字节而不是 Python 字符数限制；中文通常占 3 字节。
    max_bytes = int(config.get("max_bytes_per_push", 3500))
    return truncate_utf8(body, max_bytes)


def push_message(title, body, config):
    push = config.get("push", {})
    provider = str(push.get("provider", "none")).lower()
    if provider == "none":
        log(f"检测到新内容，但推送未配置：{title}")
        return False
    if provider == "serverchan":
        sendkey = str(push.get("serverchan_sendkey", "")).strip()
        if not sendkey:
            raise RuntimeError("Server酱 SendKey 未配置")
        response = requests.post(
            f"https://sctapi.ftqq.com/{sendkey}.send",
            data={"title": title, "desp": body},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"Server酱返回异常：{payload}")
        return True
    if provider == "ntfy":
        server = str(push.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
        topic = str(push.get("ntfy_topic", "")).strip()
        if not topic:
            raise RuntimeError("ntfy topic 未配置")
        response = requests.post(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8").decode("latin1", errors="ignore")},
            timeout=20,
        )
        response.raise_for_status()
        return True
    raise RuntimeError(f"不支持的推送方式：{provider}")


def execute_once(config, dry_run=False, skip_update=False):
    stale_seconds = max(int(config["update_timeout_seconds"]) * 2, 600)
    if not acquire_lock(stale_seconds):
        log("已有更新任务运行，本轮跳过")
        return
    try:
        state = load_state()
        before_posts = parse_report()
        sent = set(state["sent_keys"])

        # 本轮抓取前已经存在于报告的内容全部作为历史基线，避免报告格式修正、
        # pid 元数据升级或容器重启造成历史消息重复推送。
        sent.update(key for post in before_posts for key in all_post_keys(post))
        state["sent_keys"] = list(sent)[-50000:]
        state.pop("seen", None)
        save_state(state)
        if not before_posts:
            log("当前报告为空，等待首次抓取建立基线")

        if not skip_update:
            run_update(config)

        after_posts = parse_report()
        watch_authors = set(config.get("watch_authors", []))
        new_posts = []
        new_keys = set()
        for post in after_posts:
            candidate_keys = all_post_keys(post)
            if candidate_keys & sent or candidate_keys & new_keys:
                continue
            if watch_authors and post["author"] not in watch_authors:
                continue
            new_posts.append(post)
            new_keys.update(candidate_keys)
        new_posts.sort(key=lambda item: (item["date"], item["time"], item["author"]))

        if new_posts:
            title = f"NGA更新 {datetime.now(CST).strftime('%m-%d %H:%M')}（{len(new_posts)}条）"
            body = build_message(new_posts, config)
            if dry_run:
                log("测试推送：\n" + title + "\n" + body)
            else:
                # 采用“至多一次”语义：先持久化已发送键，再调用推送接口。
                # 即使 ntfy 已接收但响应在网络中丢失，下轮也不会重复发送。
                sent.update(new_keys)
                state["sent_keys"] = list(sent)[-50000:]
                state["last_push_attempt"] = datetime.now(CST).isoformat()
                save_state(state)
                try:
                    push_message(title, body, config)
                except Exception as exc:
                    failures = state.get("failed_pushes", [])
                    failures.append({
                        "time": datetime.now(CST).isoformat(),
                        "keys": sorted(new_keys),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    state["failed_pushes"] = failures[-100:]
                    save_state(state)
                    raise
            log(f"新增重点发言：{len(new_posts)} 条")
        else:
            log("没有新增重点发言")

        sent.update(key for post in after_posts for key in all_post_keys(post))
        state["sent_keys"] = list(sent)[-50000:]
        state["last_success"] = datetime.now(CST).isoformat()
        save_state(state)
    finally:
        release_lock()


def main():
    parser = argparse.ArgumentParser(description="NGA NAS updater")
    parser.add_argument("--loop", action="store_true", help="每隔指定时间循环运行")
    parser.add_argument("--interval", type=int, help="循环间隔秒数")
    parser.add_argument("--dry-run", action="store_true", help="打印推送但不发送")
    parser.add_argument("--skip-update", action="store_true", help="只测试报告解析和推送")
    args = parser.parse_args()

    config = load_config()
    interval = args.interval or int(config["interval_seconds"])
    if not args.loop:
        execute_once(config, dry_run=args.dry_run, skip_update=args.skip_update)
        return

    log(f"循环服务启动，间隔 {interval} 秒")
    while True:
        started = time.monotonic()
        try:
            execute_once(config, dry_run=args.dry_run, skip_update=args.skip_update)
        except subprocess.TimeoutExpired:
            log("更新超时，等待下一轮")
        except Exception as exc:
            log(f"本轮失败：{type(exc).__name__}: {exc}")
        elapsed = time.monotonic() - started
        time.sleep(max(10, interval - elapsed))


if __name__ == "__main__":
    main()
