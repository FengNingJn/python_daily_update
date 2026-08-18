from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _bullet_lines(values: Any, empty: str = "未提及") -> list[str]:
    if not isinstance(values, list) or not values:
        return [f"- {empty}"]
    lines = []
    for value in values:
        if isinstance(value, dict):
            lines.append(f"- {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"- {value}")
    return lines


def video_markdown(item: dict[str, Any], transcript: dict[str, Any], summary: dict[str, Any]) -> str:
    stance = summary.get("stance") or {}
    change = summary.get("view_change") or {}
    quotes = []
    for quote in summary.get("quotes") or []:
        if not isinstance(quote, dict):
            continue
        start = int(float(quote.get("start_seconds") or 0))
        minutes, seconds = divmod(start, 60)
        quotes.append(f"- [{minutes:02d}:{seconds:02d}] {quote.get('text','')}")
    title = item["title"]
    if item.get("part_title") and item["part_title"] != title:
        title += f" / {item['part_title']}"
    lines = [
        f"# {title}",
        "",
        f"- UP主：{item.get('up_name','')}（UID {item['uid']}）",
        f"- 发布时间：{item['published_at']}",
        f"- 视频：[{item['bvid']}]({item['url']})",
        f"- CID：{item['cid']}",
        f"- 时长：{item.get('duration',0)} 秒",
        f"- 字幕来源：{transcript.get('source','unknown')}",
        f"- 总结置信度：{summary.get('confidence','')}",
        "",
        "## 一句话结论",
        "",
        str(summary.get("one_sentence") or "未生成"),
        "",
        "## 主要观点",
        "",
        *_bullet_lines(summary.get("main_points")),
        "",
        "## 观点依据",
        "",
        *_bullet_lines(summary.get("evidence")),
        "",
        "## 推理过程",
        "",
        str(summary.get("reasoning") or "未提及"),
        "",
        "## 涉及对象",
        "",
        f"- 行业：{'、'.join(map(str, summary.get('industries') or [])) or '未提及'}",
        f"- 公司：{'、'.join(map(str, summary.get('companies') or [])) or '未提及'}",
        f"- 股票：{'、'.join(map(str, summary.get('stocks') or [])) or '未提及'}",
        f"- 热点：{'、'.join(map(str, summary.get('hotspots') or [])) or '未提及'}",
        "",
        "## UP主态度",
        "",
        f"- 判断：{stance.get('label','中性')}",
        f"- 理由：{stance.get('reason','')}",
        f"- 条件：{'；'.join(map(str, stance.get('conditions') or [])) or '未提及'}",
        "",
        "## 与过去观点比较",
        "",
        f"- 是否变化：{'是' if change.get('changed') else '否或证据不足'}",
        f"- 说明：{change.get('description','')}",
        "",
        "## 风险与反方观点",
        "",
        *_bullet_lines(summary.get("risks")),
        *_bullet_lines(summary.get("counterarguments"), empty="未提供反方观点"),
        "",
        "## 关键原话",
        "",
        *(quotes or ["- 没有可核对的带时间戳原话"]),
        "",
        "## 准确性提示",
        "",
        str(summary.get("accuracy_note") or "请结合原视频核对。"),
        "",
    ]
    return "\n".join(lines)


def digest_markdown(slot: str, items: list[dict[str, Any]], digest: dict[str, Any]) -> str:
    date_text = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# B站UP主观点汇总 {date_text} {slot}", "", f"本时段新增并完成总结：{len(items)} 个视频。", ""]
    for item in items:
        video, summary = item["video"], item["summary"]
        lines.extend([
            f"## {video.get('up_name','')}｜[{video['title']}]({video['url']})",
            "",
            f"- 一句话：{summary.get('one_sentence','')}",
            f"- 态度：{(summary.get('stance') or {}).get('label','中性')}",
            f"- 变化：{(summary.get('view_change') or {}).get('description','')}",
            "",
        ])
    for heading, key in (
        ("各UP主本时段观点", "creator_views"),
        ("共识", "consensus"),
        ("分歧点", "disagreements"),
        ("新出现的主题", "new_topics"),
        ("观点发生变化的UP主", "changed_creators"),
        ("共同风险", "risks"),
    ):
        lines.extend([f"## {heading}", "", *_bullet_lines(digest.get(key)), ""])
    lines.extend(["## 原视频链接", ""])
    lines.extend(f"- [{item['video']['title']}]({item['video']['url']})" for item in items)
    lines.append("")
    return "\n".join(lines)


def write_versioned(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")
        return path
    if path.read_text(encoding="utf-8") == content:
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}_v{index}{path.suffix}")
        if not candidate.exists():
            candidate.write_text(content, encoding="utf-8")
            return candidate
        index += 1


def save_transcript(root: Path, item: dict[str, Any], transcript: dict[str, Any], content_hash: str) -> Path:
    date_text = item["published_at"][:10] or datetime.now().strftime("%Y-%m-%d")
    suffix = f"_{item['cid']}" if item.get("page", 1) > 1 else ""
    path = root / "transcripts" / date_text / f"{item['bvid']}{suffix}_{content_hash[:8]}.json"
    payload = {
        "video": item,
        "source": transcript.get("source"),
        "detail": transcript.get("detail", {}),
        "segments": transcript.get("segments", []),
        "text": transcript.get("text", ""),
        "content_hash": content_hash,
    }
    return write_versioned(path, json.dumps(payload, ensure_ascii=False, indent=2))

