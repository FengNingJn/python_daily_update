from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import os
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import database
from .analysis import AnalysisError, DeepSeekClient, whisper_transcribe
from .bilibili import AuthenticationRequired, BilibiliClient, BilibiliError, NoTranscript
from .reporting import digest_markdown, save_transcript, video_markdown, write_versioned


CST = timezone(timedelta(hours=8))


class FileLock(AbstractContextManager):
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write("0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("已有B站汇总任务正在运行") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
        return False


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"slots": {}, "last_success": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"slots": {}, "last_success": ""}
    except (OSError, json.JSONDecodeError):
        return {"slots": {}, "last_success": ""}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _row_transcript(row) -> dict[str, Any]:
    return {
        "source": row["transcript_source"],
        "text": row["transcript"],
        "segments": json.loads(row["segments_json"] or "[]"),
        "detail": json.loads(row["source_detail_json"] or "{}"),
    }


class DigestPipeline:
    def __init__(self, config: dict[str, Any], logger: logging.Logger):
        self.config = config
        self.root: Path = config["root"]
        self.log = logger
        self.bili = BilibiliClient(config)
        self.ai = DeepSeekClient(config)

    @staticmethod
    def _content_hash(transcript: dict[str, Any]) -> str:
        normalized = "\n".join(
            line.strip() for line in str(transcript.get("text") or "").splitlines() if line.strip()
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _item_from_row(row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _store_transcript(self, db, item: dict[str, Any], transcript: dict[str, Any]) -> tuple[int, bool]:
        digest = self._content_hash(transcript)
        _, version, created = database.add_transcript(db, item, transcript, digest)
        if created:
            save_transcript(self.root, item, transcript, digest)
        database.set_video_status(db, item["bvid"], item["cid"], "transcribed")
        return version, created

    def _backfill_inventory(self, db) -> dict[str, int]:
        fetch = self.config.get("fetch", {})
        page_size = min(50, max(1, int(fetch.get("backfill_page_size", 50))))
        delay = max(0.0, float(fetch.get("backfill_delay_seconds", 0.25)))
        totals = {"listed": 0, "inserted_parts": 0, "existing_bvids": 0, "failed_bvids": 0}

        for uid in self.config["accounts"]:
            database.ensure_creator(db, uid)
            page = 1
            total = None
            seen_for_creator = 0
            while total is None or (page - 1) * page_size < total:
                listing, total = self.bili.list_creator_videos_page(uid, page=page, page_size=page_size)
                if not listing:
                    break
                up_name = next((str(row.get("up_name") or "") for row in listing if row.get("up_name")), "")
                database.ensure_creator(db, uid, up_name)
                totals["listed"] += len(listing)
                seen_for_creator += len(listing)

                for summary in listing:
                    bvid = summary["bvid"]
                    if database.bvid_exists(db, bvid):
                        totals["existing_bvids"] += 1
                        continue
                    try:
                        parts = self.bili.video_parts(uid, bvid)
                        for item in parts:
                            totals["inserted_parts"] += int(database.upsert_video(db, item))
                        db.commit()
                    except Exception as exc:
                        totals["failed_bvids"] += 1
                        self.log.warning("全量清单：%s/%s 详情失败：%s", uid, bvid, exc)
                    if delay:
                        time.sleep(delay)

                pages = max(1, math.ceil(total / page_size))
                self.log.info(
                    "全量清单：UID %s 第 %s/%s 页，已扫描 %s/%s",
                    uid, page, pages, seen_for_creator, total,
                )
                db.commit()
                page += 1
                if delay:
                    time.sleep(delay)

            database.update_creator_status(db, uid, success=True)
            db.commit()
        return totals

    def _backfill_transcripts(self, db, *, use_whisper: bool, limit: int | None) -> dict[str, int]:
        if use_whisper and importlib.util.find_spec("faster_whisper") is None:
            raise RuntimeError("Whisper阶段尚未安装 faster-whisper，请先安装 requirements-whisper.txt")

        rows = database.videos_without_transcript(
            db, limit=limit, shortest_first=use_whisper, skip_whisper_failed=use_whisper
        )
        delay = max(0.0, float(self.config.get("fetch", {}).get("transcript_delay_seconds", 0.20)))
        totals = {
            "queued": len(rows), "saved": 0, "already_saved": 0,
            "waiting_whisper": 0, "failed": 0,
        }
        for index, row in enumerate(rows, 1):
            item = self._item_from_row(row)
            transcript = None
            direct_error = ""
            try:
                transcript = self.bili.extract_transcript(item)
            except (AuthenticationRequired, BilibiliError, NoTranscript) as exc:
                direct_error = f"{type(exc).__name__}: {exc}"

            if transcript is None and use_whisper:
                try:
                    transcript = self._whisper_fallback(item)
                except Exception as exc:
                    totals["failed"] += 1
                    error = f"{type(exc).__name__}: {exc}"
                    if direct_error:
                        error = f"{direct_error}；Whisper: {error}"
                    database.set_video_status(db, item["bvid"], item["cid"], "whisper_failed", error)
                    db.commit()
                    self.log.warning("本地转写失败 %s/%s：%s", item["bvid"], item["cid"], error)
                    if delay:
                        time.sleep(delay)
                    continue

            if transcript is None:
                totals["waiting_whisper"] += 1
                database.set_video_status(
                    db, item["bvid"], item["cid"], "waiting_whisper",
                    direct_error or "B站没有可直接取得的字幕",
                )
            else:
                _, created = self._store_transcript(db, item, transcript)
                totals["saved" if created else "already_saved"] += 1
            db.commit()
            if delay:
                time.sleep(delay)

            if index % 20 == 0 or index == len(rows):
                self.log.info(
                    "%s字幕进度 %s/%s，新增=%s，待Whisper=%s，失败=%s",
                    "Whisper" if use_whisper else "B站", index, len(rows),
                    totals["saved"], totals["waiting_whisper"], totals["failed"],
                )
        return totals

    def backfill(self, phase: str, *, limit: int | None = None) -> dict[str, Any]:
        if phase not in {"inventory", "subtitles", "whisper", "all"}:
            raise ValueError("phase 必须是 inventory、subtitles、whisper 或 all")
        self.bili.require_login_cookie()
        account = self.bili.current_user()
        self.log.info("开始本地全量回填：phase=%s，登录账号=%s", phase, account.get("uname") or account.get("mid"))

        result: dict[str, Any] = {"phase": phase}
        with FileLock(self.config["lock_path"]):
            db = database.connect(self.config["db_path"])
            try:
                if phase in {"inventory", "all"}:
                    result["inventory"] = self._backfill_inventory(db)
                if phase in {"subtitles", "all"}:
                    result["subtitles"] = self._backfill_transcripts(db, use_whisper=False, limit=limit)
                if phase in {"whisper", "all"}:
                    result["whisper"] = self._backfill_transcripts(db, use_whisper=True, limit=limit)
                return result
            finally:
                db.close()

    def _whisper_fallback(self, item: dict[str, Any]) -> dict[str, Any]:
        if not self.config.get("whisper", {}).get("enabled", True):
            raise NoTranscript("没有B站字幕，且Whisper回退已关闭")
        temp_root = self.root / "data" / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{item['bvid']}-", dir=temp_root) as temp_dir:
            audio = Path(temp_dir) / f"{item['bvid']}_{item['cid']}.m4a"
            self.log.info("%s/%s 未发现B站字幕，下载音频并运行Whisper", item["bvid"], item["cid"])
            self.bili.download_audio(item, audio)
            result = whisper_transcribe(audio, self.config)
            if self.config.get("output", {}).get("keep_audio"):
                kept = self.root / "data" / "audio" / audio.name
                kept.parent.mkdir(parents=True, exist_ok=True)
                kept.write_bytes(audio.read_bytes())
            return result

    def _process_item(self, db, item: dict[str, Any]) -> dict[str, Any] | None:
        transcript = self.bili.extract_transcript(item)
        if not transcript:
            transcript = self._whisper_fallback(item)
        digest = self._content_hash(transcript)
        transcript_id, version, created = database.add_transcript(db, item, transcript, digest)
        save_transcript(self.root, item, transcript, digest)

        existing_summary = database.summary_for_transcript(db, transcript_id)
        if existing_summary:
            database.set_video_status(db, item["bvid"], item["cid"], "processed")
            return None

        history_limit = int(self.config.get("ai", {}).get("recent_view_context", 5))
        history = database.recent_view_context(db, item["uid"], item["published_at"], history_limit)
        summary = self.ai.summarize_video(item, transcript, history)
        markdown = video_markdown(item, transcript, summary)
        date_text = item["published_at"][:10] or datetime.now(CST).strftime("%Y-%m-%d")
        part_suffix = f"_{item['cid']}" if int(item.get("page", 1)) > 1 else ""
        report_path = self.root / "summaries" / date_text / f"{item['bvid']}{part_suffix}_{digest[:8]}.md"
        write_versioned(report_path, markdown)
        database.add_summary(db, transcript_id, item, summary, markdown, self.ai.model)
        database.set_video_status(db, item["bvid"], item["cid"], "processed")
        self.log.info(
            "完成 %s/%s：字幕=%s 版本=%s 字符=%s",
            item["bvid"], item["cid"], transcript["source"], version, len(transcript["text"]),
        )
        return {"video": item, "summary": summary, "transcript_source": transcript["source"]}

    def _select_bvids(self, db, uid: str, listing: list[dict[str, Any]]) -> list[str]:
        fetch = self.config.get("fetch", {})
        checkpoint = database.creator_checkpoint(db, uid)
        pending = set(database.pending_bvids(db, uid))
        selected: list[str] = []

        if not checkpoint:
            baseline = self.config.get("baseline", {})
            days = int(baseline.get("days", 3))
            maximum = int(baseline.get("max_videos_per_creator", 3))
            cutoff = datetime.now(CST) - timedelta(days=days)
            recent = [row for row in listing if row.get("published_at") and datetime.fromisoformat(row["published_at"]) >= cutoff]
            source = recent if recent else listing
            selected.extend(row["bvid"] for row in source[:maximum])
        else:
            selected.extend(
                row["bvid"] for row in listing
                if not row.get("published_at") or row["published_at"] >= checkpoint
            )

        revision_days = int(fetch.get("revision_check_days", 7))
        revision_limit = int(fetch.get("revision_check_limit", 3))
        revision_cutoff = datetime.now(CST) - timedelta(days=revision_days)
        revisions = [
            row["bvid"] for row in listing
            if row.get("published_at") and datetime.fromisoformat(row["published_at"]) >= revision_cutoff
        ][:revision_limit]
        selected.extend(revisions)
        selected.extend(sorted(pending))
        return list(dict.fromkeys(selected))

    def _discover_creator(self, db, uid: str) -> tuple[list[dict[str, Any]], int]:
        database.ensure_creator(db, uid)
        page_size = int(self.config.get("fetch", {}).get("page_size", 10))
        listing = self.bili.list_creator_videos(uid, limit=page_size)
        up_name = next((row.get("up_name", "") for row in listing if row.get("up_name")), "")
        if not up_name:
            up_name = self.bili.creator_name(uid)
        database.ensure_creator(db, uid, up_name)
        selected = self._select_bvids(db, uid, listing)
        items: list[dict[str, Any]] = []
        inserted = 0
        for bvid in selected:
            for item in self.bili.video_parts(uid, bvid):
                if not item.get("up_name"):
                    item["up_name"] = up_name
                inserted += int(database.upsert_video(db, item))
                items.append(item)
        checkpoint = max((row.get("published_at", "") for row in listing), default="")
        database.update_creator_status(db, uid, success=True, checkpoint=checkpoint)
        db.commit()
        return items, inserted

    def run(self, slot: str) -> dict[str, Any]:
        if slot not in {"08:00", "21:00"}:
            raise ValueError("slot 必须是 08:00 或 21:00")
        self.bili.require_login_cookie()
        account = self.bili.current_user()
        if not self.ai.configured:
            raise AnalysisError("需要配置本地 DEEPSEEK_API_KEY 后才能生成结构化观点")
        self.log.info("开始B站汇总：slot=%s 登录账号=%s", slot, account.get("uname") or account.get("mid"))

        with FileLock(self.config["lock_path"]):
            db = database.connect(self.config["db_path"])
            run_id = database.start_run(db, slot)
            discovered = processed = failed = 0
            digest_items: list[dict[str, Any]] = []
            try:
                for uid in self.config["accounts"]:
                    try:
                        items, new_count = self._discover_creator(db, uid)
                        discovered += new_count
                    except AuthenticationRequired:
                        raise
                    except Exception as exc:
                        database.ensure_creator(db, uid)
                        database.update_creator_status(db, uid, success=False, error=f"{type(exc).__name__}: {exc}")
                        db.commit()
                        self.log.exception("UID %s 投稿检查失败，继续其他账号", uid)
                        continue

                    for item in items:
                        database.add_run_item(db, run_id, item["bvid"], item["cid"], "running")
                        db.commit()
                        try:
                            result = self._process_item(db, item)
                            database.add_run_item(db, run_id, item["bvid"], item["cid"], "processed")
                            if result:
                                digest_items.append(result)
                                processed += 1
                        except AuthenticationRequired:
                            raise
                        except Exception as exc:
                            failed += 1
                            error = f"{type(exc).__name__}: {exc}"
                            database.set_video_status(db, item["bvid"], item["cid"], "failed", error)
                            database.add_run_item(db, run_id, item["bvid"], item["cid"], "failed", error)
                            self.log.exception("处理 %s/%s 失败", item["bvid"], item["cid"])
                        db.commit()

                if digest_items:
                    digest_data = self.ai.summarize_digest(slot, digest_items)
                else:
                    digest_data = {
                        "creator_views": [], "consensus": [], "disagreements": [],
                        "new_topics": [], "changed_creators": [], "risks": [],
                    }
                digest_text = digest_markdown(slot, digest_items, digest_data)
                date_text = datetime.now(CST).strftime("%Y-%m-%d")
                digest_path = write_versioned(
                    self.root / "digests" / date_text / f"{slot.replace(':', '')}.md",
                    digest_text,
                )
                final_status = "success" if failed == 0 else "partial"
                database.finish_run(
                    db, run_id, status=final_status, discovered=discovered,
                    processed=processed, failed=failed, digest_path=str(digest_path),
                )
                state = _load_state(self.config["state_path"])
                completed_at = datetime.now(CST).isoformat(timespec="seconds")
                state.setdefault("slots", {})[slot] = {
                    "last_success": completed_at,
                    "run_id": run_id,
                    "digest_path": str(digest_path),
                    "new_summaries": processed,
                    "failed": failed,
                }
                state["last_success"] = completed_at
                _save_state(self.config["state_path"], state)
                return {
                    "run_id": run_id, "status": final_status, "discovered": discovered,
                    "processed": processed, "failed": failed, "digest_path": str(digest_path),
                }
            except Exception as exc:
                database.finish_run(
                    db, run_id, status="failed", discovered=discovered,
                    processed=processed, failed=failed, error=f"{type(exc).__name__}: {exc}",
                )
                raise
            finally:
                db.close()
