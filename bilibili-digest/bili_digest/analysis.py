from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests


class AnalysisError(RuntimeError):
    pass


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def transcript_with_timestamps(transcript: dict[str, Any]) -> str:
    segments = transcript.get("segments") or []
    if not segments:
        return str(transcript.get("text") or "")
    return "\n".join(
        f"[{_format_seconds(item.get('start'))}-{_format_seconds(item.get('end'))}] {item.get('text', '')}"
        for item in segments
        if str(item.get("text") or "").strip()
    )


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", value, re.S | re.I)
    if fenced:
        value = fenced.group(1)
    else:
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            value = value[start:end + 1]
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"AI返回内容不是有效JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError("AI返回JSON不是对象")
    return data


def _normalize_summary(data: dict[str, Any], transcript_source: str) -> dict[str, Any]:
    list_fields = ["main_points", "evidence", "industries", "companies", "stocks", "hotspots", "risks", "counterarguments", "quotes"]
    normalized = dict(data)
    for field in list_fields:
        value = normalized.get(field, [])
        normalized[field] = value if isinstance(value, list) else [str(value)] if value else []
    stance = normalized.get("stance")
    if not isinstance(stance, dict):
        stance = {"label": str(stance or "中性"), "reason": "", "conditions": []}
    stance.setdefault("label", "中性")
    stance.setdefault("reason", "")
    stance.setdefault("conditions", [])
    normalized["stance"] = stance
    change = normalized.get("view_change")
    if not isinstance(change, dict):
        change = {"changed": False, "description": str(change or "未发现足够历史证据")}
    change.setdefault("changed", False)
    change.setdefault("description", "")
    normalized["view_change"] = change
    normalized.setdefault("one_sentence", "")
    normalized.setdefault("reasoning", "")
    normalized.setdefault("confidence", 0.5)
    normalized.setdefault("accuracy_note", "")
    normalized["transcript_source"] = transcript_source
    return normalized


class DeepSeekClient:
    def __init__(self, config: dict[str, Any]):
        self.api_key = str(config.get("deepseek_api_key") or "")
        self.base_url = str(config.get("deepseek_base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.model = str(config.get("deepseek_model") or "deepseek-chat")
        ai = config.get("ai", {})
        self.timeout = int(ai.get("timeout_seconds", 120))
        self.max_chunk_chars = int(ai.get("max_chunk_chars", 18000))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _chat(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> str:
        if not self.configured:
            raise AnalysisError("尚未配置 DEEPSEEK_API_KEY")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": self.model, "messages": messages, "temperature": temperature},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if not content:
                    raise AnalysisError("DeepSeek返回空内容")
                return content
            except (requests.RequestException, ValueError, AnalysisError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise AnalysisError(f"DeepSeek请求失败：{last_error}") from last_error

    def _chunk_notes(self, text: str) -> str:
        if len(text) <= self.max_chunk_chars:
            return text
        chunks = [text[index:index + self.max_chunk_chars] for index in range(0, len(text), self.max_chunk_chars)]
        notes = []
        for index, chunk in enumerate(chunks, 1):
            notes.append(self._chat([
                {
                    "role": "system",
                    "content": "你是严谨的视频转录整理员。只依据给定文本，保留观点、依据、实体、条件、风险和关键时间戳，不作投资建议。",
                },
                {
                    "role": "user",
                    "content": f"这是视频转录第{index}/{len(chunks)}段。请压缩成详细事实笔记：\n\n{chunk}",
                },
            ]))
        return "\n\n".join(f"## 分段笔记 {i}\n{note}" for i, note in enumerate(notes, 1))

    def summarize_video(
        self,
        item: dict[str, Any],
        transcript: dict[str, Any],
        recent_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_text = self._chunk_notes(transcript_with_timestamps(transcript))
        source = str(transcript.get("source") or "unknown")
        history = json.dumps(recent_context, ensure_ascii=False) if recent_context else "无可用历史观点"
        schema = {
            "one_sentence": "一句话结论",
            "main_points": ["主要观点"],
            "evidence": ["观点依据或例证"],
            "reasoning": "完整推理过程",
            "industries": ["行业"],
            "companies": ["公司"],
            "stocks": ["股票或代码"],
            "hotspots": ["热点"],
            "stance": {"label": "看多/看空/中性/条件判断", "reason": "理由", "conditions": ["成立条件"]},
            "view_change": {"changed": False, "description": "与过去观点相比的变化"},
            "risks": ["风险或前提"],
            "counterarguments": ["反方观点"],
            "quotes": [{"text": "关键原话", "start_seconds": 0, "end_seconds": 0}],
            "confidence": 0.0,
            "accuracy_note": "字幕来源及准确性说明",
        }
        response = self._chat([
            {
                "role": "system",
                "content": (
                    "你是金融视频观点档案员。只能根据字幕和明确给出的历史记录总结，不得编造原话、时间戳或证券代码。"
                    "区分UP主事实陈述、推测、条件判断和情绪表达。输出严格JSON，不要Markdown代码块。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"视频标题：{item['title']}\nUP主：{item.get('up_name','')}\n发布时间：{item['published_at']}\n"
                    f"字幕来源：{source}\n视频链接：{item['url']}\n\n"
                    f"该UP主过去观点：\n{history}\n\n本次字幕或分段事实笔记：\n{source_text}\n\n"
                    f"请输出以下结构，数组没有内容时留空：\n{json.dumps(schema, ensure_ascii=False)}"
                ),
            },
        ])
        return _normalize_summary(_extract_json(response), source)

    def summarize_digest(self, slot: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        compact = [
            {
                "uid": item["video"]["uid"],
                "up_name": item["video"].get("up_name", ""),
                "title": item["video"]["title"],
                "url": item["video"]["url"],
                "summary": item["summary"],
            }
            for item in items
        ]
        response = self._chat([
            {
                "role": "system",
                "content": "你负责合并多个UP主的视频观点。只做归纳，不生成买卖建议。输出严格JSON。",
            },
            {
                "role": "user",
                "content": (
                    f"时段：{slot}\n视频摘要：{json.dumps(compact, ensure_ascii=False)}\n\n"
                    "输出字段：creator_views（数组，含up_name、view）、consensus（数组）、"
                    "disagreements（数组）、new_topics（数组）、changed_creators（数组）、risks（数组）。"
                ),
            },
        ])
        return _extract_json(response)


_WHISPER_MODELS: dict[tuple[str, str, str], Any] = {}


def whisper_transcribe(audio_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AnalysisError("未安装 faster-whisper；请安装 requirements-whisper.txt") from exc
    settings = config.get("whisper", {})
    model_name = str(settings.get("model", "small"))
    device = str(settings.get("device", "cpu"))
    compute_type = str(settings.get("compute_type", "int8"))
    key = (model_name, device, compute_type)
    model = _WHISPER_MODELS.get(key)
    if model is None:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _WHISPER_MODELS[key] = model
    iterable, info = model.transcribe(
        str(audio_path),
        language=str(settings.get("language") or "zh"),
        vad_filter=True,
    )
    segments = [
        {"start": float(segment.start), "end": float(segment.end), "text": str(segment.text).strip()}
        for segment in iterable
        if str(segment.text).strip()
    ]
    text = "\n".join(segment["text"] for segment in segments).strip()
    if not text:
        raise AnalysisError("Whisper未识别出有效文本")
    return {
        "source": "whisper_local",
        "text": text,
        "segments": segments,
        "detail": {
            "model": model_name,
            "language": getattr(info, "language", ""),
            "language_probability": getattr(info, "language_probability", None),
        },
    }

