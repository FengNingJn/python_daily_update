from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests


CST = timezone(timedelta(hours=8))
API_ROOT = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


class BilibiliError(RuntimeError):
    pass


class AuthenticationRequired(BilibiliError):
    pass


class NoTranscript(BilibiliError):
    pass


def _timestamp_iso(value: int | float | str | None) -> str:
    try:
        return datetime.fromtimestamp(float(value), CST).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _parse_raw_cookie(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in value.replace("\r", "").replace("\n", "").split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = raw.strip()
    return cookies


class BilibiliClient:
    def __init__(self, config: dict[str, Any]):
        fetch = config.get("fetch", {})
        self.timeout = int(fetch.get("request_timeout_seconds", 25))
        self.retries = max(1, int(fetch.get("retries", 3)))
        self.backoff = max(0.2, float(fetch.get("retry_backoff_seconds", 2)))
        self.preferred_languages = list(
            config.get("subtitles", {}).get(
                "preferred_languages", ["zh-CN", "zh-Hans", "ai-zh", "zh", "ai-en", "en"]
            )
        )
        self.cookie_path = Path(config["cookie_path"])
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_at = 0.0
        self._load_cookie_file()

    def _load_cookie_file(self) -> None:
        if not self.cookie_path.exists():
            return
        content = self.cookie_path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return
        if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
            jar = http.cookiejar.MozillaCookieJar(str(self.cookie_path))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError) as exc:
                raise BilibiliError(f"B站 Cookie 文件格式无效：{exc}") from exc
            self.session.cookies.update(jar)
        else:
            self.session.cookies.update(_parse_raw_cookie(content))

    @property
    def has_login_cookie(self) -> bool:
        return bool(self.session.cookies.get("SESSDATA"))

    def auth_status(self) -> dict[str, Any]:
        return {
            "cookie_file_exists": self.cookie_path.exists(),
            "has_sessdata": self.has_login_cookie,
            "cookie_path": str(self.cookie_path),
        }

    def require_login_cookie(self) -> None:
        if not self.has_login_cookie:
            raise AuthenticationRequired(
                "需要B站登录Cookie（至少包含 SESSDATA）；已按用户要求停止，不自动登录。"
            )

    def _request_json(self, url: str, *, params: dict[str, Any] | None = None, referer: str | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        headers = {"Referer": referer} if referer else None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise BilibiliError(f"B站接口返回的不是对象：{url}")
                return data
            except (requests.RequestException, ValueError, BilibiliError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
        raise BilibiliError(f"B站接口请求失败：{url}；{last_error}") from last_error

    def _get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys and time.time() - self._wbi_keys_at < 300:
            return self._wbi_keys
        payload = self._request_json(f"{API_ROOT}/x/web-interface/nav")
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        if not img_key or not sub_key:
            raise BilibiliError("无法取得B站WBI签名密钥")
        self._wbi_keys = (img_key, sub_key)
        self._wbi_keys_at = time.time()
        return self._wbi_keys

    def _sign_wbi(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = self._get_wbi_keys()
        mixin_raw = img_key + sub_key
        mixin_key = "".join(mixin_raw[index] for index in MIXIN_KEY_ENC_TAB if index < len(mixin_raw))[:32]
        signed = {str(k): str(v) for k, v in params.items() if v is not None}
        signed["wts"] = str(int(time.time()))
        cleaned = {
            key: re.sub(r"[!'()*]", "", value)
            for key, value in sorted(signed.items())
        }
        query = urllib.parse.urlencode(cleaned)
        cleaned["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return cleaned

    @staticmethod
    def _require_ok(payload: dict[str, Any], label: str) -> dict[str, Any]:
        code = payload.get("code")
        if code == 0:
            return payload.get("data") or {}
        message = str(payload.get("message") or "unknown")
        if code in (-101, -403) or re.search(r"登录|权限|login|permission|forbidden", message, re.I):
            raise AuthenticationRequired(f"B站{label}接口需要登录：{message} ({code})")
        raise BilibiliError(f"B站{label}接口失败：{message} ({code})")

    def current_user(self) -> dict[str, Any]:
        self.require_login_cookie()
        data = self._require_ok(self._request_json(f"{API_ROOT}/x/web-interface/nav"), "账号")
        if not data.get("isLogin"):
            raise AuthenticationRequired("B站Cookie已存在但登录状态无效或已过期")
        return {"mid": str(data.get("mid") or ""), "uname": str(data.get("uname") or "")}

    def creator_name(self, uid: str) -> str:
        payload = self._request_json(
            f"{API_ROOT}/x/space/wbi/acc/info",
            params=self._sign_wbi({"mid": uid}),
            referer=f"https://space.bilibili.com/{uid}",
        )
        return str(self._require_ok(payload, "UP主资料").get("name") or "")

    def list_creator_videos_page(
        self, uid: str, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        self.require_login_cookie()
        params = {
            "mid": uid,
            "pn": max(1, int(page)),
            "ps": min(max(int(page_size), 1), 50),
            "order": "pubdate",
        }
        payload = self._request_json(
            f"{API_ROOT}/x/space/wbi/arc/search",
            params=self._sign_wbi(params),
            referer=f"https://space.bilibili.com/{uid}/video",
        )
        data = self._require_ok(payload, "UP主投稿")
        videos = []
        for item in ((data.get("list") or {}).get("vlist") or []):
            bvid = str(item.get("bvid") or "")
            if not bvid:
                continue
            videos.append({
                "uid": uid,
                "up_name": str(item.get("author") or ""),
                "bvid": bvid,
                "title": str(item.get("title") or ""),
                "published_at": _timestamp_iso(item.get("created")),
                "url": f"https://www.bilibili.com/video/{bvid}/",
            })
        total = int((data.get("page") or {}).get("count") or len(videos))
        return videos, total

    def list_creator_videos(self, uid: str, *, limit: int = 10) -> list[dict[str, Any]]:
        videos, _ = self.list_creator_videos_page(uid, page=1, page_size=limit)
        return videos[: max(0, int(limit))]

    def video_parts(self, uid: str, bvid: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{API_ROOT}/x/web-interface/view", params={"bvid": bvid},
            referer=f"https://www.bilibili.com/video/{bvid}/",
        )
        data = self._require_ok(payload, "视频详情")
        owner = data.get("owner") or {}
        pages = data.get("pages") or [{"cid": data.get("cid"), "part": data.get("title"), "duration": data.get("duration")}]
        published = _timestamp_iso(data.get("pubdate") or data.get("ctime"))
        results = []
        for index, page in enumerate(pages, 1):
            cid = str(page.get("cid") or "")
            if not cid:
                continue
            results.append({
                "uid": uid,
                "up_name": str(owner.get("name") or ""),
                "bvid": bvid,
                "cid": cid,
                "title": str(data.get("title") or ""),
                "part_title": str(page.get("part") or ""),
                "url": f"https://www.bilibili.com/video/{bvid}/" + (f"?p={index}" if len(pages) > 1 else ""),
                "published_at": published,
                "duration": int(page.get("duration") or data.get("duration") or 0),
                "description": str(data.get("desc") or ""),
                "aid": str(data.get("aid") or ""),
                "up_mid": str(owner.get("mid") or uid),
                "page": index,
            })
        return results

    @staticmethod
    def _segments_from_subtitle_body(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
        segments = []
        for item in body:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            segments.append({
                "start": float(item.get("from") or 0),
                "end": float(item.get("to") or item.get("from") or 0),
                "text": content,
            })
        return segments

    @staticmethod
    def _transcript_result(source: str, segments: list[dict[str, Any]], detail: dict[str, Any]) -> dict[str, Any] | None:
        text = "\n".join(segment["text"] for segment in segments if segment.get("text")).strip()
        if not text:
            return None
        return {"source": source, "text": text, "segments": segments, "detail": detail}

    def _pick_track(self, tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
        for preferred in self.preferred_languages:
            for track in tracks:
                if str(track.get("lan") or "") == preferred:
                    return track
        return tracks[0] if tracks else None

    def player_subtitle(self, item: dict[str, Any]) -> dict[str, Any] | None:
        payload = self._request_json(
            f"{API_ROOT}/x/player/wbi/v2",
            params=self._sign_wbi({"bvid": item["bvid"], "cid": item["cid"]}),
            referer=item["url"],
        )
        data = self._require_ok(payload, "播放器字幕")
        tracks = ((data.get("subtitle") or {}).get("subtitles") or [])
        if not tracks and data.get("need_login_subtitle"):
            raise AuthenticationRequired("该视频的字幕需要有效B站登录Cookie")
        track = self._pick_track(tracks)
        if not track:
            return None
        url = str(track.get("subtitle_url") or "").strip()
        if not url:
            raise AuthenticationRequired("B站返回了字幕条目，但字幕地址为空；Cookie可能无效或触发风控")
        if url.startswith("//"):
            url = "https:" + url
        subtitle = self._request_json(url, referer=item["url"])
        segments = self._segments_from_subtitle_body(subtitle.get("body") or [])
        language = str(track.get("lan") or "unknown")
        source = "bilibili_ai_subtitle" if language.startswith("ai-") else "bilibili_human_subtitle"
        return self._transcript_result(source, segments, {
            "language": language,
            "language_name": str(track.get("lan_doc") or ""),
            "track_id": str(track.get("id") or ""),
        })

    def ai_conclusion(self, item: dict[str, Any]) -> dict[str, Any] | None:
        payload = self._request_json(
            f"{API_ROOT}/x/web-interface/view/conclusion/get",
            params=self._sign_wbi({
                "bvid": item["bvid"], "cid": item["cid"], "up_mid": item.get("up_mid") or item["uid"],
            }),
            referer=item["url"],
        )
        data = self._require_ok(payload, "AI总结")
        if data.get("code") != 0:
            return None
        model = data.get("model_result") or {}
        if isinstance(model, str):
            try:
                model = json.loads(model)
            except json.JSONDecodeError:
                return None
        if not isinstance(model, dict):
            return None

        segments = []
        for block in model.get("subtitle") or []:
            for part in block.get("part_subtitle") or []:
                text = str(part.get("content") or "").strip()
                if text:
                    segments.append({
                        "start": float(part.get("start_timestamp") or 0),
                        "end": float(part.get("end_timestamp") or part.get("start_timestamp") or 0),
                        "text": text,
                    })
        detail = {
            "stid": str(data.get("stid") or ""),
            "result_type": model.get("result_type"),
            "official_summary": str(model.get("summary") or ""),
            "outline": model.get("outline") or [],
        }
        result = self._transcript_result("bilibili_ai_conclusion_subtitle", segments, detail)
        if result:
            return result

        summary_parts = [str(model.get("summary") or "").strip()]
        for section in model.get("outline") or []:
            title = str(section.get("title") or "").strip()
            if title:
                summary_parts.append(title)
            for point in section.get("part_outline") or []:
                content = str(point.get("content") or "").strip()
                if content:
                    summary_parts.append(content)
        text = "\n".join(part for part in summary_parts if part).strip()
        if text:
            return {
                "source": "bilibili_ai_conclusion",
                "text": text,
                "segments": [],
                "detail": detail,
            }
        return None

    def extract_transcript(self, item: dict[str, Any]) -> dict[str, Any] | None:
        self.require_login_cookie()
        errors: list[Exception] = []
        try:
            result = self.player_subtitle(item)
            if result:
                return result
        except BilibiliError as exc:
            errors.append(exc)

        try:
            result = self.ai_conclusion(item)
            if result:
                if errors:
                    result.setdefault("detail", {})["player_subtitle_error"] = str(errors[0])
                return result
        except BilibiliError as exc:
            errors.append(exc)
        if errors:
            raise NoTranscript("；".join(str(exc) for exc in errors))
        return None

    def audio_url(self, item: dict[str, Any]) -> str:
        params = {
            "bvid": item["bvid"], "cid": item["cid"], "fnval": 4048, "qn": 64, "fourk": 1,
        }
        payload = self._request_json(
            f"{API_ROOT}/x/player/wbi/playurl",
            params=self._sign_wbi(params),
            referer=item["url"],
        )
        data = self._require_ok(payload, "音频地址")
        audios = ((data.get("dash") or {}).get("audio") or [])
        if audios:
            best = max(audios, key=lambda row: int(row.get("bandwidth") or 0))
            url = str(best.get("baseUrl") or best.get("base_url") or best.get("url") or "")
        else:
            progressive = data.get("durl") or []
            if not progressive:
                legacy = self._request_json(
                    f"{API_ROOT}/x/player/wbi/playurl",
                    params=self._sign_wbi({"bvid": item["bvid"], "cid": item["cid"], "qn": 64, "fnval": 0}),
                    referer=item["url"],
                )
                legacy_data = self._require_ok(legacy, "旧格式音视频地址")
                progressive = legacy_data.get("durl") or []
            best = max(progressive, key=lambda row: int(row.get("size") or 0), default={})
            url = str(best.get("url") or "")
        if not url:
            raise NoTranscript("视频没有可用的DASH音轨或旧格式音视频流")
        return url

    def download_audio(self, item: dict[str, Any], target: Path) -> Path:
        self.require_login_cookie()
        url = self.audio_url(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, headers={"Referer": item["url"]}, timeout=max(self.timeout, 60), stream=True) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        return target
