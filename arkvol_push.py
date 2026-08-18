#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scheduled combined A-share daily report and Arkvol greed push."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import message_hub


CST = timezone(timedelta(hours=8))
SKILL_DIR = Path(os.environ.get("ARKVOL_SKILL_DIR", "/skill"))
QUERY_SCRIPT = SKILL_DIR / "scripts" / "query.py"
NGA_CONFIG_FILE = Path(os.environ.get("NGA_CONFIG_FILE", "/data/config.json"))
STATE_FILE = Path(os.environ.get("ARKVOL_PUSH_STATE", "/state/arkvol_push_state.json"))
MESSAGE_HUB_DB_FILE = os.environ.get("MESSAGE_HUB_DB_FILE", "").strip()
PUSH_HOURS = (8, 17)
MAX_MESSAGE_BYTES = 3200
SLOT_RETRY_DELAY_SECONDS = max(30, int(os.environ.get("ARKVOL_RETRY_DELAY_SECONDS", "300")))
SLOT_MAX_ATTEMPTS = max(1, int(os.environ.get("ARKVOL_MAX_ATTEMPTS", "5")))

A_SHARE_BROAD_CODES = {
    "510050", "510300", "510500", "159845", "562660", "588000", "159915"
}
OVERSEAS_PATTERN = re.compile(
    r"香港|港股|恒生|纳斯达克|标普|道琼斯|日经|韩国|中韩|德国|法国|印度|越南|美国|海外|QDII",
    re.I,
)
SECTOR_RULES = [
    ("银行", r"银行"),
    ("证券", r"证券|券商"),
    ("保险", r"保险"),
    ("红利", r"红利|高股息"),
    ("煤炭", r"煤炭"),
    ("有色金属", r"有色|稀土|铜|铝|金属"),
    ("黄金珠宝", r"黄金|珠宝"),
    ("钢铁", r"钢铁"),
    ("石油化工", r"石油|油气|化工"),
    ("房地产", r"房地产|地产"),
    ("基建建材", r"基建|建筑|建材"),
    ("电力公用", r"电力|公用事业"),
    ("家电", r"家电|家居"),
    ("食品饮料", r"食品饮料|食品"),
    ("白酒", r"白酒|酒"),
    ("消费", r"消费|零售|旅游|酒店"),
    ("农业养殖", r"农业|养殖|畜牧|粮食|种业"),
    ("医药", r"医药|医疗|中药"),
    ("创新药", r"创新药"),
    ("半导体芯片", r"半导体|芯片|集成电路"),
    ("人工智能", r"人工智能|AI|算力"),
    ("通信", r"通信|5G|CPO|光通信"),
    ("计算机软件", r"计算机|软件|信息技术|互联网"),
    ("机器人", r"机器人|智能制造"),
    ("军工", r"军工|国防|航天|航空"),
    ("新能源车", r"新能源车|汽车|智能车"),
    ("光伏", r"光伏|太阳能"),
    ("风电", r"风电|风能"),
    ("储能电池", r"储能|电池|锂电"),
    ("环保", r"环保|碳中和"),
]


def log(message):
    print(f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_FILE)


def run_skill(args):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(QUERY_SCRIPT), *args],
        cwd=str(SKILL_DIR),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Arkvol 命令失败({result.returncode})：{result.stdout[-1000:]}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"Arkvol 返回非 JSON：{result.stdout[-1000:]}") from exc


def check_update():
    payload = run_skill(["--check-update", "--json"])
    if payload.get("update_required") or payload.get("update_available"):
        raise RuntimeError(
            f"Arkvol Skill 需要升级：{payload.get('current_version')} -> {payload.get('latest_version')}"
        )
    return payload


def query_page(page):
    # query.py 内部也会在每个数据请求前再次执行版本检查。
    payload = run_skill(["--page", page, "--view", "summary", "--json"])
    if payload.get("code") != 0:
        raise RuntimeError(f"Arkvol {page} 查询失败：{payload.get('msg')}")
    return payload["data"]


def score_value(value):
    if value is None:
        return None
    value = float(value)
    return value * 100 if -1 <= value <= 1 else value


def sentiment_label(score):
    if score < 20:
        return "极度恐慌"
    if score < 40:
        return "恐慌"
    if score < 60:
        return "中性"
    if score < 80:
        return "贪婪"
    return "极度贪婪"


def format_score(score):
    return f"{score:.1f}（{sentiment_label(score)}）"


def broad_lines(data):
    rows = []
    for item in data.get("items", []):
        if str(item.get("fund_code", "")) not in A_SHARE_BROAD_CODES:
            continue
        score = score_value(item.get("greed"))
        if score is None:
            continue
        rows.append((item.get("index_name") or item.get("etf_name"), item.get("fund_code"), score))
    return [f"• {name} {code}：{format_score(score)}" for name, code, score in rows]


def tech_lines(data):
    rows = []
    for item in data.get("items", []):
        score = score_value(item.get("greed"))
        if score is None:
            continue
        rows.append((item.get("index_name") or item.get("fund_name"), item.get("fund_code"), score))
    return [f"• {name} {code}：{format_score(score)}" for name, code, score in rows]


def sector_lines(data):
    grouped = defaultdict(list)
    for item in data.get("items", []):
        name = str(item.get("fund_name", ""))
        if not name or OVERSEAS_PATTERN.search(name):
            continue
        score = score_value(item.get("greed_index"))
        if score is None:
            continue
        for sector, pattern in SECTOR_RULES:
            if re.search(pattern, name, flags=re.I):
                grouped[sector].append(score)
                break
    rows = []
    for sector, _pattern in SECTOR_RULES:
        values = grouped.get(sector)
        if not values:
            continue
        average = sum(values) / len(values)
        rows.append((sector, average, len(values)))
    rows.sort(key=lambda row: row[1], reverse=True)
    return [
        f"• {sector}：{format_score(average)}｜样本{count}"
        for sector, average, count in rows
    ]


def build_sections(alla, tech, funds):
    data_dates = sorted({str(x.get("as_of", "")) for x in (alla, tech, funds) if x.get("as_of")})
    header = [
        f"数据日期：{' / '.join(data_dates)}",
        "Arkvol 原始综合分数：",
        f"• 宽基：{alla.get('sentiment_score', 0):.2f}（{alla.get('sentiment_label', '-')}）",
        f"• 科技：{tech.get('sentiment_score', 0):.2f}（{tech.get('sentiment_label', '-')}）",
        f"• 基金：{funds.get('sentiment_score', 0):.2f}（{funds.get('sentiment_label', '-')}）",
        "",
        "【A股宽基 ETF】",
        *broad_lines(alla),
        "",
        "【科技板块 ETF】",
        *tech_lines(tech),
    ]
    sectors = [
        "【A股行业板块】",
        "以下为 Arkvol 基金库中同类基金贪婪指数的简单平均（聚合计算）：",
        *sector_lines(funds),
        "",
        "0-20极度恐慌｜20-40恐慌｜40-60中性｜60-80贪婪｜80-100极度贪婪",
        "仅供市场数据研究，不构成投资建议。",
    ]
    return ["\n".join(header), "\n".join(sectors)]


def split_utf8(text, max_bytes=MAX_MESSAGE_BYTES):
    chunks = []
    current = []
    current_bytes = 0
    for line in text.splitlines():
        line_bytes = len((line + "\n").encode("utf-8"))
        if current and current_bytes + line_bytes > max_bytes:
            chunks.append("\n".join(current).strip())
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        chunks.append("\n".join(current).strip())
    return chunks


def ntfy_config():
    config = load_json(NGA_CONFIG_FILE, {})
    push = config.get("push", {})
    server = str(push.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
    topic = str(push.get("ntfy_topic", "")).strip()
    if not topic:
        raise RuntimeError("NGA config.json 中没有 ntfy_topic")
    return server, topic


def publish(title, message):
    server, topic = ntfy_config()
    response = requests.post(
        server,
        json={"topic": topic, "title": title, "message": message},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("attachment"):
        raise RuntimeError("ntfy 将消息转成了附件，已停止本次批次")
    return payload.get("id")


def generate_report():
    version = check_update()
    alla = query_page("alla")
    tech = query_page("alla-tech")
    funds = query_page("funds-greed")
    sections = build_sections(alla, tech, funds)
    chunks = []
    for section in sections:
        chunks.extend(split_utf8(section))
    return version, chunks


def generate_combined_report():
    """Build one database/push payload containing both daily market and Arkvol."""
    # The Feishu bot imports this module in offline tests but does not ship
    # crawler dependencies. Load the market collector only when generating.
    import update_nga

    # arkvol_push 是常驻进程，而 update_nga.TODAY 在模块首次导入时计算。
    # 若不在每次生成前刷新，容器连续运行数日后标题和期货合约日期会永久停在启动日。
    report_date = datetime.now(CST).strftime("%Y-%m-%d")
    update_nga.TODAY = report_date
    version, arkvol_chunks = generate_report()
    market_text, indices, futs, flow, margin, limits = update_nga.generate_market_report()
    outlook_text = update_nga.analyze_tomorrow({}, indices, futs, flow, margin, limits)
    expected_market_header = f"## 每日市场数据 - {report_date}"
    expected_outlook_header = f"## 明日展望 - {report_date} 夜盘"
    if expected_market_header not in market_text or expected_outlook_header not in outlook_text:
        raise RuntimeError(
            "日报日期校验失败："
            f"expected={report_date}, update_nga.TODAY={getattr(update_nga, 'TODAY', '')}"
        )
    content = "\n\n".join(
        part.strip()
        for part in (
            market_text,
            outlook_text,
            "## Arkvol A股贪婪指数\n\n" + "\n\n".join(arkvol_chunks),
        )
        if part and part.strip()
    )
    return version, content


def send_report(slot_label):
    version, content = generate_combined_report()
    now = datetime.now(CST)
    title = f"A股每日数据 + Arkvol 贪婪指数 {slot_label}"
    event_id = f"daily_report:{now.date().isoformat()}:{slot_label}"
    data_dates = sorted(set(re.findall(r"数据日期：(\d{4}-\d{2}-\d{2})", content)))
    market_dates = sorted(set(re.findall(r"## 每日市场数据 - (\d{4}-\d{2}-\d{2})", content)))
    if MESSAGE_HUB_DB_FILE:
        with message_hub.connect_db(MESSAGE_HUB_DB_FILE) as db:
            created = message_hub.insert_event(
                db,
                event_id=event_id,
                source="daily_report",
                event_type="combined_market_report",
                event_time=now.isoformat(timespec="seconds"),
                title=title,
                content=content,
                source_url="https://github.com/FengNingJn/python_daily_update",
                tags=["A股", "每日市场", "复盘", "明日展望", "贪婪指数", slot_label],
                metadata={
                    "slot": slot_label,
                    "data_dates": data_dates,
                    "market_dates": market_dates,
                    "skill_version": version.get("current_version", ""),
                    "contains": ["daily_market", "market_outlook", "arkvol"],
                },
                dedupe_key=event_id,
                notify=True,
            )
            db.commit()
        log(
            f"合并日报入库完成：slot={slot_label} version={version.get('current_version')} "
            f"id={event_id} created={int(created)}"
        )
        return [event_id if created else f"{event_id}:duplicate"]

    # 未配置共享数据库时保留 ntfy 兼容路径，便于独立诊断。
    message_id = publish(title, content)
    log(f"ntfy 合并日报推送完成：slot={slot_label} version={version.get('current_version')} id={message_id}")
    return [message_id]


def slot_key(day, hour):
    return f"{day.isoformat()}@{hour:02d}"


def prune_state(state):
    cutoff = datetime.now(CST).date() - timedelta(days=90)
    state["slots"] = {
        key: value for key, value in state.get("slots", {}).items()
        if key[:10] >= cutoff.isoformat()
    }


def run_slot(hour):
    now = datetime.now(CST)
    state = load_json(STATE_FILE, {"slots": {}})
    state.setdefault("slots", {})
    key = slot_key(now.date(), hour)
    previous = state["slots"].get(key) or {}
    status = str(previous.get("status", ""))
    try:
        attempts = max(0, int(previous.get("attempts", 0)))
    except (TypeError, ValueError):
        attempts = 0

    if status == "sent":
        return False
    # started 表示上一次可能已经完成对共享数据库的写入；为避免重复发送，
    # 不自动重试这种状态，只对明确记录为 failed 的尝试做有限重试。
    if status == "started":
        return False
    if status == "failed":
        attempts = max(1, attempts)
        if attempts >= SLOT_MAX_ATTEMPTS:
            if not previous.get("exhausted_logged"):
                previous["exhausted_logged"] = True
                state["slots"][key] = previous
                save_state(state)
                log(f"本时段重试次数已耗尽：{key} attempts={attempts}")
            return False
        retry_at_text = str(previous.get("next_retry_at", ""))
        try:
            retry_at = datetime.fromisoformat(retry_at_text) if retry_at_text else None
        except ValueError:
            retry_at = None
        if retry_at and now < retry_at:
            return False
        log(f"重试时段：{key} attempt={attempts + 1}/{SLOT_MAX_ATTEMPTS}")

    attempt = attempts + 1
    # 先记录 started；合并日报使用固定 event_id，数据库唯一键仍会兜底去重。
    state["slots"][key] = {
        "status": "started",
        "time": now.isoformat(),
        "attempts": attempt,
    }
    prune_state(state)
    save_state(state)
    try:
        send_report(f"{hour:02d}:00")
        state = load_json(STATE_FILE, {"slots": {}})
        state.setdefault("slots", {})[key] = {
            "status": "sent",
            "time": datetime.now(CST).isoformat(),
            "attempts": attempt,
        }
        save_state(state)
        return True
    except Exception as exc:
        failed_at = datetime.now(CST)
        state = load_json(STATE_FILE, {"slots": {}})
        state.setdefault("slots", {})[key] = {
            "status": "failed",
            "time": failed_at.isoformat(),
            "attempts": attempt,
            "next_retry_at": (failed_at + timedelta(seconds=SLOT_RETRY_DELAY_SECONDS)).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        save_state(state)
        raise


def loop():
    log("A股每日数据 + Arkvol 合并推送启动：每天 08:00、17:00")
    while True:
        now = datetime.now(CST)
        for hour in PUSH_HOURS:
            if now.hour >= hour:
                try:
                    run_slot(hour)
                except Exception as exc:
                    log(f"时段 {hour:02d}:00 失败：{type(exc).__name__}: {exc}")
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Combined daily market and Arkvol publisher")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--run-slot", type=int, choices=PUSH_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        version, content = generate_combined_report()
        print(json.dumps({
            "version": version,
            "bytes": len(content.encode("utf-8")),
            "text": content,
        }, ensure_ascii=False, indent=2))
        return
    if args.run_slot:
        run_slot(args.run_slot)
        return
    if args.loop:
        loop()
        return
    parser.error("请指定 --loop、--run-slot 或 --dry-run")


if __name__ == "__main__":
    main()
