#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import message_hub

from bili_digest import database
from bili_digest.analysis import AnalysisError, DeepSeekClient
from bili_digest.bilibili import AuthenticationRequired, BilibiliError, NoTranscript
from bili_digest.config import ensure_directories, load_config
from bili_digest.pipeline import DigestPipeline, FileLock
from bili_digest.reporting import digest_markdown, video_markdown, write_versioned


CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def build_logger(config: dict[str, Any]) -> logging.Logger:
    logger = logging.getLogger("bilibili_nas_service")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    log_path = Path(os.environ.get("BILIBILI_NAS_LOG_FILE", config["root"] / "logs" / "nas_service.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


class NasBilibiliService:
    def __init__(self, config: dict[str, Any], logger: logging.Logger):
        self.config = config
        self.log = logger
        self.pipeline = DigestPipeline(config, logger)
        self.state_path = Path(
            os.environ.get("BILIBILI_NAS_STATE_FILE", config["root"] / "state" / "nas_service_state.json")
        )
        self.hub_db_path = Path(os.environ.get("MESSAGE_HUB_DB_FILE", "/bot-state/feishu_bot.db"))
        self.ai_config_path = Path(os.environ.get("BILIBILI_AI_CONFIG_FILE", "/bot-state/ai_config.json"))
        self.collect_interval = timedelta(hours=max(1, int(os.environ.get("BILIBILI_COLLECT_INTERVAL_HOURS", "2"))))
        self.report_slots = [
            item.strip() for item in os.environ.get("BILIBILI_REPORT_SLOTS", "08:00,21:00").split(",")
            if item.strip()
        ]
        self.poll_seconds = max(15, int(os.environ.get("BILIBILI_SERVICE_POLL_SECONDS", "30")))
        self._configure_ai()

    def _configure_ai(self) -> None:
        if not self.ai_config_path.exists():
            self.log.warning("未找到现有飞书AI配置：%s", self.ai_config_path)
            return
        payload = json.loads(self.ai_config_path.read_text(encoding="utf-8"))
        self.config["deepseek_api_key"] = str(payload.get("api_key") or "").strip()
        self.config["deepseek_base_url"] = str(payload.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.config["deepseek_model"] = str(payload.get("model") or "deepseek-chat").strip()
        self.pipeline.ai = DeepSeekClient(self.config)
        self.log.info("复用飞书AI配置：provider=%s model=%s", payload.get("provider", ""), self.pipeline.ai.model)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            baseline = now_iso()
            state = {
                "created_at": baseline,
                "last_collect": "",
                "last_report_cutoff": baseline,
                "completed_slots": {},
            }
            self._save_state(state)
            self.log.info("建立B站推送基线：%s；历史内容只入库、不补推", baseline)
            return state
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            history = self.state_path.parent / "history"
            history.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.state_path, history / f"nas_service_state.{stamp}.json")
        temp = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    @staticmethod
    def _parse_iso(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def collect(self) -> dict[str, int]:
        self.pipeline.bili.require_login_cookie()
        self.pipeline.bili.current_user()
        retry_days = max(1, int(os.environ.get("BILIBILI_SUBTITLE_RETRY_DAYS", "7")))
        cutoff = datetime.now(CST) - timedelta(days=retry_days)
        page_size = max(10, int(self.config.get("fetch", {}).get("page_size", 10)))
        stats = {"listed": 0, "new_parts": 0, "checked_transcripts": 0, "saved_transcripts": 0, "waiting": 0}

        with FileLock(self.config["lock_path"]):
            db = database.connect(self.config["db_path"])
            try:
                for uid in self.config["accounts"]:
                    database.ensure_creator(db, uid)
                    listing = self.pipeline.bili.list_creator_videos(uid, limit=page_size)
                    stats["listed"] += len(listing)
                    up_name = next((str(row.get("up_name") or "") for row in listing if row.get("up_name")), "")
                    database.ensure_creator(db, uid, up_name)
                    for entry in listing:
                        published = self._parse_iso(str(entry.get("published_at") or ""))
                        if database.bvid_exists(db, entry["bvid"]) and published and published < cutoff:
                            continue
                        try:
                            for item in self.pipeline.bili.video_parts(uid, entry["bvid"]):
                                stats["new_parts"] += int(database.upsert_video(db, item))
                        except BilibiliError as exc:
                            self.log.warning("视频详情暂时失败 %s/%s：%s", uid, entry["bvid"], exc)
                    checkpoint = max((str(row.get("published_at") or "") for row in listing), default="")
                    database.update_creator_status(db, uid, success=True, checkpoint=checkpoint)
                    db.commit()

                rows = db.execute(
                    """
                    SELECT v.* FROM videos v
                    WHERE v.published_at>=?
                      AND NOT EXISTS (
                        SELECT 1 FROM transcript_versions t
                        WHERE t.bvid=v.bvid AND t.cid=v.cid
                      )
                    ORDER BY v.published_at, v.bvid, v.cid
                    """,
                    (cutoff.isoformat(timespec="seconds"),),
                ).fetchall()
                for row in rows:
                    item = self.pipeline._item_from_row(row)
                    stats["checked_transcripts"] += 1
                    try:
                        transcript = self.pipeline.bili.extract_transcript(item)
                        if not transcript:
                            raise NoTranscript("B站尚未生成可用字幕")
                        _, created = self.pipeline._store_transcript(db, item, transcript)
                        stats["saved_transcripts"] += int(created)
                    except (AuthenticationRequired, BilibiliError, NoTranscript) as exc:
                        stats["waiting"] += 1
                        database.set_video_status(db, item["bvid"], item["cid"], "waiting_subtitle", str(exc))
                    db.commit()
                self.log.info("B站2小时采集完成：%s", json.dumps(stats, ensure_ascii=False))
                return stats
            finally:
                db.close()

    @staticmethod
    def _transcript_from_row(row) -> dict[str, Any]:
        return {
            "source": row["transcript_source"],
            "text": row["transcript"],
            "segments": json.loads(row["segments_json"] or "[]"),
            "detail": json.loads(row["source_detail_json"] or "{}"),
        }

    @staticmethod
    def _video_from_row(row) -> dict[str, Any]:
        keys = (
            "uid", "up_name", "bvid", "cid", "aid", "up_mid", "title", "part_title",
            "url", "published_at", "duration", "description", "page",
        )
        return {key: row[key] for key in keys}

    def _report_rows(self, db, cutoff: str):
        return db.execute(
            """
            SELECT
                t.id AS transcript_id,t.version,t.transcript_source,t.transcript,
                t.segments_json,t.source_detail_json,t.content_hash,t.created_at AS transcript_created_at,
                v.uid,v.up_name,v.bvid,v.cid,v.aid,v.up_mid,v.title,v.part_title,
                v.url,v.published_at,v.duration,v.description,v.page
            FROM transcript_versions t
            JOIN videos v ON v.bvid=t.bvid AND v.cid=t.cid
            WHERE t.created_at>?
              AND t.id=(
                SELECT MAX(t2.id) FROM transcript_versions t2
                WHERE t2.bvid=t.bvid AND t2.cid=t.cid
              )
            ORDER BY v.published_at,v.uid,v.bvid,v.cid
            """,
            (cutoff,),
        ).fetchall()

    def report(self, slot: str, *, since_hours: int | None = None) -> dict[str, Any]:
        if not self.pipeline.ai.configured:
            raise AnalysisError("现有飞书AI配置不可用，不能生成B站观点汇总")
        state = self._load_state()
        started = now_iso()
        cutoff = str(state.get("last_report_cutoff") or started)
        if since_hours is not None:
            cutoff = (datetime.now(CST) - timedelta(hours=max(1, since_hours))).isoformat(timespec="seconds")

        with FileLock(self.config["lock_path"]):
            db = database.connect(self.config["db_path"])
            try:
                rows = self._report_rows(db, cutoff)
                items: list[dict[str, Any]] = []
                hub = message_hub.connect_db(self.hub_db_path)
                try:
                    for row in rows:
                        item = self._video_from_row(row)
                        transcript = self._transcript_from_row(row)
                        transcript_id = int(row["transcript_id"])
                        existing = database.summary_for_transcript(db, transcript_id)
                        if existing:
                            summary = json.loads(existing["summary_json"])
                            markdown = str(existing["summary_markdown"])
                        else:
                            history_limit = int(self.config.get("ai", {}).get("recent_view_context", 5))
                            history = database.recent_view_context(
                                db, item["uid"], item["published_at"], history_limit
                            )
                            summary = self.pipeline.ai.summarize_video(item, transcript, history)
                            markdown = video_markdown(item, transcript, summary)
                            date_text = item["published_at"][:10] or datetime.now(CST).date().isoformat()
                            report_path = (
                                self.config["root"] / "summaries" / date_text /
                                f"{item['bvid']}_{item['cid']}_{row['content_hash'][:8]}.md"
                            )
                            write_versioned(report_path, markdown)
                            database.add_summary(
                                db, transcript_id, item, summary, markdown, self.pipeline.ai.model
                            )
                            db.commit()

                        message_hub.insert_event(
                            hub,
                            event_id=f"bilibili:video_summary:{item['bvid']}:{item['cid']}:v{row['version']}",
                            source="bilibili",
                            event_type="video_summary",
                            event_time=item["published_at"],
                            author=item["up_name"],
                            title=item["title"],
                            content=markdown,
                            source_url=item["url"],
                            tags=["B站", "UP主观点", "视频总结"],
                            metadata={
                                "uid": item["uid"], "bvid": item["bvid"], "cid": item["cid"],
                                "transcript_source": transcript["source"], "transcript_version": row["version"],
                            },
                            dedupe_key=f"bilibili:video_summary:{item['bvid']}:{item['cid']}:v{row['version']}",
                            notify=False,
                        )
                        items.append({"video": item, "summary": summary, "transcript_source": transcript["source"]})

                    if not items:
                        state["last_report_cutoff"] = started
                        state.setdefault("completed_slots", {})[slot] = datetime.now(CST).date().isoformat()
                        self._save_state(state)
                        self.log.info("B站%s汇总：无新增字幕，不调用AI、不推送", slot)
                        return {"slot": slot, "videos": 0, "pushed": False}

                    digest_data = self.pipeline.ai.summarize_digest(slot, items)
                    content = digest_markdown(slot, items, digest_data)
                    date_text = datetime.now(CST).date().isoformat()
                    digest_path = write_versioned(
                        self.config["root"] / "digests" / date_text / f"{slot.replace(':', '')}.md",
                        content,
                    )
                    newest_id = max(int(row["transcript_id"]) for row in rows)
                    event_id = f"bilibili:creator_digest:{date_text}:{slot.replace(':', '')}:{newest_id}"
                    created = message_hub.insert_event(
                        hub,
                        event_id=event_id,
                        source="bilibili",
                        event_type="creator_digest",
                        event_time=started,
                        author="bilibili_digest",
                        title=f"B站UP主观点汇总 {date_text} {slot}",
                        content=content,
                        tags=["B站", "UP主观点", "时段汇总"],
                        metadata={"slot": slot, "video_count": len(items), "digest_path": str(digest_path)},
                        dedupe_key=event_id,
                        notify=True,
                    )
                    hub.commit()
                finally:
                    hub.close()

                state["last_report_cutoff"] = started
                state.setdefault("completed_slots", {})[slot] = date_text
                self._save_state(state)
                self.log.info("B站%s汇总完成：视频=%s，已进入统一飞书队列=%s", slot, len(items), created)
                return {"slot": slot, "videos": len(items), "pushed": bool(created)}
            finally:
                db.close()

    def loop(self) -> None:
        state = self._load_state()
        self.log.info(
            "B站NAS服务启动：采集间隔=%s小时，汇总时点=%s",
            int(self.collect_interval.total_seconds() // 3600), ",".join(self.report_slots),
        )
        while True:
            now = datetime.now(CST)
            state = self._load_state()
            last_collect = self._parse_iso(str(state.get("last_collect") or ""))
            if last_collect is None or now - last_collect >= self.collect_interval:
                try:
                    self.collect()
                    state = self._load_state()
                    state["last_collect"] = now_iso()
                    self._save_state(state)
                except Exception:
                    self.log.exception("B站2小时采集失败，下一轮继续")

            for slot in self.report_slots:
                try:
                    hour, minute = (int(value) for value in slot.split(":", 1))
                except (TypeError, ValueError):
                    continue
                today = now.date().isoformat()
                completed = str((state.get("completed_slots") or {}).get(slot) or "")
                if now.hour == hour and now.minute >= minute and completed != today:
                    try:
                        self.collect()
                        self.report(slot)
                    except Exception:
                        self.log.exception("B站%s定时汇总失败，保持未完成状态以便重试", slot)
            time.sleep(self.poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站NAS定时采集、观点汇总与统一飞书入库")
    parser.add_argument("--loop", action="store_true", help="持续运行定时服务")
    parser.add_argument("--once", choices=["collect", "report"], help="只执行一次")
    parser.add_argument("--slot", default="manual", help="手动汇总的时段标签")
    parser.add_argument("--since-hours", type=int, default=None, help="手动汇总最近多少小时")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    ensure_directories(config)
    logger = build_logger(config)
    service = NasBilibiliService(config, logger)
    if args.loop:
        service.loop()
        return 0
    if args.once == "collect":
        print(json.dumps(service.collect(), ensure_ascii=False, indent=2))
        return 0
    if args.once == "report":
        print(json.dumps(service.report(args.slot, since_hours=args.since_hours), ensure_ascii=False, indent=2))
        return 0
    raise SystemExit("必须指定 --loop 或 --once")


if __name__ == "__main__":
    raise SystemExit(main())
