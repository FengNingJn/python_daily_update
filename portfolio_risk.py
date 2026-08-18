#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update Feishu holdings with market data and publish stop levels twice daily."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import message_hub


CST = timezone(timedelta(hours=8))
APP_TOKEN = os.environ.get("PORTFOLIO_APP_TOKEN", "S5lfbSNSzaJ65ns0LblcpkO4nYz").strip()
HOLDINGS_TABLE_ID = os.environ.get("PORTFOLIO_HOLDINGS_TABLE_ID", "tblprTmIHdFzi1GH").strip()
LOG_TABLE_ID = os.environ.get("PORTFOLIO_LOG_TABLE_ID", "tblRA7YYZxmUBK76").strip()
STATE_FILE = Path(os.environ.get("PORTFOLIO_STATE_FILE", "/state/portfolio_risk_state.json"))
LOG_FILE = Path(os.environ.get("PORTFOLIO_LOG_FILE", "/logs/portfolio_risk.log"))
MESSAGE_HUB_DB_FILE = os.environ.get("MESSAGE_HUB_DB_FILE", "/bot-state/feishu_bot.db").strip()
PUSH_HOURS = tuple(
    sorted({int(value.strip()) for value in os.environ.get("PORTFOLIO_PUSH_HOURS", "6,8,20").split(",") if value.strip()})
)
POLL_SECONDS = max(10, int(os.environ.get("PORTFOLIO_POLL_SECONDS", "30")))
SOURCE_URL = f"https://my.feishu.cn/base/{APP_TOKEN}?table={HOLDINGS_TABLE_ID}"

REQUIRED_FIELDS = {
    "持仓ID", "证券代码", "证券名称", "成本价", "最新价", "持仓后最高价", "最新ATR14",
    "MA5", "MA13", "MA34", "MA60", "前一日最低价", "波段低点", "行情更新时间",
    "海-止盈模式", "海-止损线", "海-当前信号", "狼-记忆点", "狼-趋势止盈线",
    "狼-当前保护线", "狼-当前信号", "我的最终止盈价", "我的最终止损价", "计划动作",
    "触发状态", "最后提醒时间",
}


def log(message: str) -> None:
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def as_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, dict):
        value = value.get("value", value.get("text", default))
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def price_round(value: float) -> float:
    digits = 3 if abs(value) < 10 else 2
    return round(float(value), digits)


def price_text(value: Any) -> str:
    number = as_number(value)
    if number <= 0:
        return "未设置"
    return f"{number:.3f}".rstrip("0").rstrip(".")


def percent_text(value: float | None) -> str:
    return "未知" if value is None else f"{value * 100:+.2f}%"


def parse_ms_date(value: Any) -> date | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, CST).date()
    except (TypeError, ValueError, OSError):
        return None


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"slots": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_FILE)


class FeishuBitable:
    def __init__(self) -> None:
        self.app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET 未配置")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "portfolio-risk/1.0"})
        self.token = ""

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                response = self.session.request(method, url, json=payload, headers=headers, timeout=30)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code >= 400:
                    api_code = body.get("code")
                    api_msg = body.get("msg") or response.reason
                    raise RuntimeError(
                        f"飞书接口失败 HTTP={response.status_code} code={api_code} msg={api_msg}"
                    )
                if body.get("code") not in (0, None):
                    raise RuntimeError(f"飞书接口失败 code={body.get('code')} msg={body.get('msg')}")
                return body
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"飞书请求失败：{last_error}")

    def authenticate(self) -> None:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = self._request("POST", url, {"app_id": self.app_id, "app_secret": self.app_secret})
        self.token = str(body.get("tenant_access_token") or "")
        if not self.token:
            raise RuntimeError("飞书未返回 tenant_access_token")

    def list_all(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            separator = "&" if "?" in path else "?"
            url = path + (separator + "page_token=" + page_token if page_token else "")
            body = self._request("GET", url)
            data = body.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return items

    def fields(self, table_id: str) -> list[dict[str, Any]]:
        return self.list_all(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields?page_size=100"
        )

    def records(self, table_id: str) -> list[dict[str, Any]]:
        return self.list_all(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records?page_size=100"
        )

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{record_id}"
        self._request("PUT", url, {"fields": fields})


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    name: str
    price: float
    quote_time: datetime
    bars: tuple[Bar, ...]


class TencentMarket:
    QUOTE_URL = "https://qt.gtimg.cn/q="
    KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    SEARCH_URL = "https://smartbox.gtimg.cn/s3/"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})

    @staticmethod
    def symbol(code: str, market: str = "") -> str:
        raw = re.sub(r"[^0-9A-Za-z]", "", str(code or "")).upper()
        market_text = str(market or "")
        if raw.startswith("HK") or "港" in market_text:
            digits = re.sub(r"\D", "", raw)
            if not digits:
                raise ValueError(f"无法识别港股代码：{code}")
            return "hk" + digits.zfill(5)
        digits = re.sub(r"\D", "", raw)
        if len(digits) != 6:
            raise ValueError(f"无法识别A股代码：{code}")
        if digits.startswith(("4", "8", "92")):
            return "bj" + digits
        if digits.startswith(("5", "6", "9")):
            return "sh" + digits
        return "sz" + digits

    def _get(self, url: str, *, encoding: str | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=20)
                response.raise_for_status()
                if encoding:
                    return response.content.decode(encoding, errors="replace")
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"腾讯行情请求失败：{last_error}")

    def quote(self, symbol: str) -> tuple[str, float, datetime | None]:
        text = self._get(self.QUOTE_URL + symbol, encoding="gbk")
        match = re.search(r'="(.*?)";', text)
        if not match:
            raise RuntimeError(f"腾讯快照无数据：{symbol}")
        parts = match.group(1).split("~")
        name = parts[1].strip() if len(parts) > 1 else symbol
        price = as_number(parts[3] if len(parts) > 3 else 0)
        timestamp = parts[30].strip() if len(parts) > 30 else ""
        try:
            quote_time = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=CST)
        except ValueError:
            quote_time = None
        return name, price, quote_time

    def resolve_name(self, name: str) -> tuple[str, str, str]:
        query = str(name or "").strip()
        if not query:
            raise ValueError("证券名称为空")
        text = self._get(
            self.SEARCH_URL + "?q=" + requests.utils.quote(query) + "&t=all",
            encoding="gbk",
        )
        match = re.search(r'v_hint="(.*)"', text)
        if not match:
            raise RuntimeError(f"未找到证券：{query}")
        payload = match.group(1)
        try:
            payload = json.loads('"' + payload.replace('"', '\\"') + '"')
        except json.JSONDecodeError:
            pass
        candidates: list[tuple[str, str, str]] = []
        for item in payload.split("^"):
            parts = item.split("~")
            if len(parts) < 3:
                continue
            prefix, code, canonical = parts[0].lower(), parts[1].strip(), parts[2].strip()
            if prefix in {"sh", "sz", "bj"} and re.fullmatch(r"\d{6}", code):
                candidates.append((code, canonical, "A股"))
            elif prefix == "hk" and re.fullmatch(r"\d{1,5}", code):
                candidates.append((code.zfill(5), canonical, "港股"))
        exact = [item for item in candidates if item[1] == query]
        if len(exact) == 1:
            return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise RuntimeError(f"未找到可用的A股或港股标的：{query}")
        names = "、".join(f"{item[1]}({item[0]})" for item in candidates[:5])
        raise RuntimeError(f"证券名称不唯一：{query}，候选：{names}")

    def bars(self, symbol: str, limit: int = 500) -> tuple[Bar, ...]:
        params = f"{symbol},day,,,{limit},qfq"
        payload = self._get(self.KLINE_URL + "?param=" + params)
        node = (payload.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        result: list[Bar] = []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                result.append(
                    Bar(
                        day=datetime.strptime(row[0], "%Y-%m-%d").date(),
                        open=float(row[1]), close=float(row[2]), high=float(row[3]),
                        low=float(row[4]), volume=float(row[5]),
                    )
                )
            except (TypeError, ValueError):
                continue
        if len(result) < 60:
            raise RuntimeError(f"{symbol} 日线不足60根，实际{len(result)}根")
        return tuple(result)

    def snapshot(self, code: str, market: str = "") -> MarketSnapshot:
        symbol = self.symbol(code, market)
        bars = self.bars(symbol)
        name, price, quote_time = self.quote(symbol)
        if quote_time is None:
            quote_time = datetime.combine(bars[-1].day, datetime.min.time(), CST).replace(hour=16)
        if price <= 0:
            price = bars[-1].close
            quote_time = datetime.combine(bars[-1].day, datetime.min.time(), CST).replace(hour=16)
        return MarketSnapshot(symbol=symbol, name=name, price=price, quote_time=quote_time, bars=bars)


def moving_average(bars: tuple[Bar, ...], period: int) -> float:
    if len(bars) < period:
        raise ValueError(f"日线不足{period}根")
    return sum(item.close for item in bars[-period:]) / period


def atr(bars: tuple[Bar, ...], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError(f"ATR{period}至少需要{period + 1}根日线")
    values: list[float] = []
    for index in range(1, len(bars)):
        current, previous = bars[index], bars[index - 1]
        values.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return sum(values[-period:]) / period


def valid_levels(*values: Any) -> list[float]:
    return [number for number in (as_number(value) for value in values) if number > 0]


def risk_calculation(fields: dict[str, Any], snapshot: MarketSnapshot, now: datetime) -> dict[str, Any]:
    bars = snapshot.bars
    latest = snapshot.price
    cost = as_number(fields.get("成本价"))
    buy_day = parse_ms_date(fields.get("买入日期")) or now.date()
    held_bars = [item for item in bars if not buy_day or item.day >= buy_day]
    observed_high = max((item.high for item in held_bars), default=latest)
    # When the downloaded history fully covers the holding period it is the
    # authoritative high. This also repairs stale demo values left in a row.
    if buy_day and bars[0].day <= buy_day:
        held_high = max(latest, observed_high)
    else:
        held_high = max(latest, observed_high, as_number(fields.get("持仓后最高价")))
    atr14 = atr(bars)
    ma = {period: moving_average(bars, period) for period in (5, 13, 34, 60)}
    previous_low = bars[-2].low
    wave_low = min(item.low for item in bars[-13:])

    sea_take = held_high - 2 * atr14
    # 海指导明确给出了 -2ATR 移动止盈，但没有统一的固定止损公式。
    # 自动模式使用“成本-2ATR”和近13日低点中较高者，并限制在成本下方3%，
    # 作为可审计的系统补充线，不把它冒充为海指导原话。
    sea_stop = min(cost * 0.97, max(cost - 2 * atr14, wave_low)) if cost > 0 else 0
    sea_mode = "跟踪止盈"
    if sea_stop > 0 and latest <= sea_stop:
        sea_signal = "止损"
    elif sea_mode == "跟踪止盈" and cost > 0 and sea_take > cost and latest <= sea_take:
        sea_signal = "止盈"
    else:
        sea_signal = "持有"

    memory_point = max(as_number(fields.get("狼-记忆点")), held_high)
    wolf_13 = wave_low * 0.97
    wolf_day = memory_point * 0.97 if memory_point > 0 else 0
    wolf_week = memory_point * 0.95 if memory_point > 0 else 0
    wolf_trend = ma[13]
    wolf_candidates = valid_levels(wolf_13, wolf_day, wolf_week, wolf_trend)
    wolf_protection = max(wolf_candidates, default=0)
    if wolf_protection > 0 and latest <= wolf_protection:
        wolf_signal = "止盈" if cost > 0 and latest > cost else "止损"
    else:
        wolf_signal = "持有"

    take_candidates = [level for level in (sea_take, wolf_protection) if cost > 0 and level > cost]
    stop_candidates = [level for level in (sea_stop, wolf_13) if cost > 0 and 0 < level < cost]
    final_take = max(take_candidates, default=0)
    final_stop = max(stop_candidates, default=0)
    if sea_signal == "止损" or wolf_signal == "止损":
        final_action = "止损"
    elif sea_signal == "止盈" or wolf_signal == "止盈":
        final_action = "止盈"
    else:
        final_action = "持有"
    triggered = final_action in {"止盈", "止损"}
    warnings: list[str] = []
    if cost > 0 and (cost / latest > 5 or latest / cost > 5):
        warnings.append("成本价与现价相差超过5倍，请核对成本")
    if sea_stop > 0 and sea_stop < latest * 0.1:
        warnings.append("海-止损线远低于现价，可能填的是价差而非价格")
    if sea_stop > latest * 1.2:
        warnings.append("海-止损线高于现价20%以上，当前会被判为已触发")
    if wolf_trend > latest * 1.2:
        warnings.append("狼-趋势止盈线高于现价20%以上，当前会被判为已触发")
    if snapshot.name and str(fields.get("证券名称") or "") and snapshot.name not in str(fields.get("证券名称")):
        warnings.append(f"行情名称为“{snapshot.name}”，请核对证券代码")

    updates = {
        "最新价": price_round(latest),
        "持仓后最高价": price_round(held_high),
        "最新ATR14": price_round(atr14),
        "MA5": price_round(ma[5]),
        "MA13": price_round(ma[13]),
        "MA34": price_round(ma[34]),
        "MA60": price_round(ma[60]),
        "前一日最低价": price_round(previous_low),
        "波段低点": price_round(wave_low),
        "买入日期": int(datetime.combine(buy_day, datetime.min.time(), CST).timestamp() * 1000),
        "行情更新时间": int(snapshot.quote_time.timestamp() * 1000),
        "海-止盈模式": sea_mode,
        "海-止损线": price_round(sea_stop),
        "海-止损依据": "ATR",
        "海-当前信号": sea_signal,
        "狼-策略类型": "趋势" if latest >= ma[34] and ma[34] >= ma[60] else "震荡",
        "狼-记忆点": price_round(memory_point),
        "狼-趋势止盈线": price_round(wolf_trend),
        "狼-当前保护线": price_round(wolf_protection),
        "狼-当前信号": wolf_signal,
        "我的最终止盈价": price_round(final_take) if final_take > 0 else None,
        "我的最终止损价": price_round(final_stop) if final_stop > 0 else None,
        "计划动作": final_action,
        "触发状态": "已触发" if triggered else "未触发",
        "最后提醒时间": int(now.timestamp() * 1000),
    }
    return {
        "latest": latest, "cost": cost, "profit_rate": (latest / cost - 1) if cost > 0 else None,
        "sea_take": sea_take, "sea_stop": sea_stop, "sea_signal": sea_signal,
        "wolf_13": wolf_13, "wolf_day": wolf_day, "wolf_week": wolf_week,
        "wolf_trend": wolf_trend, "wolf_protection": wolf_protection, "wolf_signal": wolf_signal,
        "final_take": final_take, "final_stop": final_stop, "final_action": final_action,
        "triggered": triggered, "warnings": warnings, "updates": updates,
        "quote_time": snapshot.quote_time, "market_day": bars[-1].day,
    }


def build_report(results: list[dict[str, Any]], slot_label: str, now: datetime) -> str:
    triggered = sum(1 for item in results if item["risk"]["triggered"])
    writeback_failed = sum(1 for item in results if item.get("writeback_error"))
    lines = [
        f"【持仓止盈止损更新 {slot_label}】",
        f"更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}｜腾讯证券快照/前复权日线",
        f"持仓：{len(results)} 只｜已触发：{triggered} 只",
    ]
    if writeback_failed:
        lines.append(f"⚠ 表格写回暂未授权：{writeback_failed} 只；本次计算与推送正常，已保留自动重试")
    for index, item in enumerate(results, 1):
        fields, risk = item["fields"], item["risk"]
        code = str(fields.get("证券代码") or "")
        name = str(fields.get("证券名称") or item["snapshot"].name or code)
        lines.extend(
            [
                "",
                f"{index}. {name}（{code}） 现价 {price_text(risk['latest'])}｜成本 {price_text(risk['cost'])}｜盈亏 {percent_text(risk['profit_rate'])}",
                f"海指导：ATR止盈 {price_text(risk['sea_take'])}｜系统止损 {price_text(risk['sea_stop'])}｜信号 {risk['sea_signal']}",
                f"狼大：13日 {price_text(risk['wolf_13'])}｜日通道 {price_text(risk['wolf_day'])}｜周通道 {price_text(risk['wolf_week'])}｜趋势线 {price_text(risk['wolf_trend'])}",
                f"狼大当前保护线 {price_text(risk['wolf_protection'])}｜信号 {risk['wolf_signal']}",
                f"系统最终线：止盈 {price_text(risk['final_take'])}｜止损 {price_text(risk['final_stop'])}｜计划 {risk['final_action']}",
                f"行情时间：{risk['quote_time'].strftime('%Y-%m-%d %H:%M:%S')}｜日线截止：{risk['market_day'].isoformat()}",
            ]
        )
        for warning in risk["warnings"]:
            lines.append("⚠ " + warning)
    lines.extend(
        [
            "",
            "说明：海指导ATR止盈线=持仓后最高价-2×ATR14；狼大13日线=近13日低点×0.97；日/周通道线=记忆点×0.97/0.95。",
            "你只需维护证券名称和成本价；其余字段由脚本更新。海指导止损线属于系统化补充规则，不是其固定原话。以上为纪律提醒，不代替交易决定。",
            f"表格：{SOURCE_URL}",
        ]
    )
    return "\n".join(lines)


def validate_schema(client: FeishuBitable) -> None:
    names = {str(item.get("field_name") or "") for item in client.fields(HOLDINGS_TABLE_ID)}
    missing = sorted(REQUIRED_FIELDS - names)
    if missing:
        raise RuntimeError("持仓主表缺少字段：" + "、".join(missing))


def calculate_all(client: FeishuBitable, market: TencentMarket, *, write: bool) -> list[dict[str, Any]]:
    records = client.records(HOLDINGS_TABLE_ID)
    results: list[dict[str, Any]] = []
    for record in records:
        fields = dict(record.get("fields") or {})
        name = str(fields.get("证券名称") or "").strip()
        if not name or "示例" in name or "演示" in name:
            log(f"跳过空白或示例记录：{record.get('record_id')}")
            continue
        code = str(fields.get("证券代码") or "").strip()
        if not code:
            code, canonical_name, market_name = market.resolve_name(name)
            fields["证券代码"] = code
            fields["市场"] = market_name
            log(f"自动识别证券：{name} -> {canonical_name}({code})")
        snapshot = market.snapshot(code, str(fields.get("市场") or ""))
        risk = risk_calculation(fields, snapshot, datetime.now(CST))
        writeback_error = ""
        if write:
            updates = dict(risk["updates"])
            updates["证券代码"] = code
            if fields.get("市场"):
                updates["市场"] = fields["市场"]
            try:
                client.update_record(HOLDINGS_TABLE_ID, str(record.get("record_id")), updates)
            except Exception as exc:
                writeback_error = str(exc)
                risk["warnings"].append("飞书表格暂未写回；本次计算与推送不受影响")
                log(f"{name} 写回失败，将继续生成报告：{exc}")
        results.append(
            {
                "record_id": record.get("record_id"),
                "fields": fields,
                "snapshot": snapshot,
                "risk": risk,
                "writeback_error": writeback_error,
            }
        )
        log(f"{fields.get('证券名称') or code} 更新完成：price={risk['latest']} sea={risk['sea_signal']} wolf={risk['wolf_signal']}")
    if not results:
        raise RuntimeError("持仓主表没有可计算的持仓；请至少填写证券名称和成本价")
    return results


def enqueue_report(content: str, slot_label: str, now: datetime, notify: bool) -> bool:
    event_id = f"portfolio_risk:{now.date().isoformat()}:{slot_label}"
    with message_hub.connect_db(MESSAGE_HUB_DB_FILE) as db:
        created = message_hub.insert_event(
            db,
            event_id=event_id,
            source="portfolio",
            event_type="stop_profit_loss_report",
            event_time=now.isoformat(timespec="seconds"),
            author="NAS持仓风控",
            title=f"持仓止盈止损更新 {slot_label}",
            content=content,
            source_url=SOURCE_URL,
            tags=["持仓", "止盈", "止损", slot_label],
            metadata={"slot": slot_label, "app_token": APP_TOKEN, "table_id": HOLDINGS_TABLE_ID},
            dedupe_key=event_id,
            notify=notify,
        )
        db.commit()
    return created


def run(slot_label: str, *, write: bool = True, notify: bool = True) -> dict[str, Any]:
    now = datetime.now(CST)
    client = FeishuBitable()
    client.authenticate()
    validate_schema(client)
    results = calculate_all(client, TencentMarket(), write=write)
    report = build_report(results, slot_label, now)
    created = False
    if write:
        created = enqueue_report(report, slot_label, now, notify=notify)
    return {"holdings": len(results), "triggered": sum(x["risk"]["triggered"] for x in results), "created": created, "report": report}


def slot_key(day: date, hour: int) -> str:
    return f"{day.isoformat()}@{hour:02d}"


def run_slot(hour: int) -> bool:
    now = datetime.now(CST)
    state = load_state()
    state.setdefault("slots", {})
    key = slot_key(now.date(), hour)
    if (state["slots"].get(key) or {}).get("status") == "sent":
        return False
    try:
        result = run(f"{hour:02d}:00", write=True, notify=True)
        state = load_state()
        state.setdefault("slots", {})[key] = {
            "status": "sent", "time": datetime.now(CST).isoformat(),
            "holdings": result["holdings"], "triggered": result["triggered"],
        }
        cutoff = (now.date() - timedelta(days=90)).isoformat()
        state["slots"] = {k: v for k, v in state["slots"].items() if k[:10] >= cutoff}
        save_state(state)
        log(f"时段完成：{key} holdings={result['holdings']} triggered={result['triggered']} queued={int(result['created'])}")
        return True
    except Exception as exc:
        state = load_state()
        state.setdefault("slots", {})[key] = {
            "status": "failed", "time": datetime.now(CST).isoformat(), "error": f"{type(exc).__name__}: {exc}"
        }
        save_state(state)
        raise


def loop() -> None:
    log("持仓止盈止损服务启动：" + "、".join(f"{hour:02d}:00" for hour in PUSH_HOURS))
    while True:
        now = datetime.now(CST)
        for hour in PUSH_HOURS:
            if now.hour >= hour:
                try:
                    run_slot(hour)
                except Exception as exc:
                    log(f"时段 {hour:02d}:00 失败：{type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


def self_test() -> None:
    assert TencentMarket.symbol("000063", "A股") == "sz000063"
    assert TencentMarket.symbol("600519", "A股") == "sh600519"
    assert TencentMarket.symbol("HK1208", "港股") == "hk01208"
    start = date(2026, 1, 1)
    bars = tuple(Bar(start + timedelta(days=i), 10 + i, 10 + i, 11 + i, 9 + i, 100) for i in range(70))
    assert moving_average(bars, 5) == 77
    assert atr(bars) == 2
    snapshot = MarketSnapshot("sz000063", "测试", 79, datetime(2026, 3, 11, 15, tzinfo=CST), bars)
    result = risk_calculation({"证券名称": "测试", "成本价": 70, "海-止盈模式": "跟踪止盈", "海-止损线": 60, "狼-记忆点": 70, "狼-趋势止盈线": 75}, snapshot, datetime.now(CST))
    assert result["updates"]["最新价"] == 79
    assert result["wolf_protection"] > 0
    assert result["final_take"] > 0
    assert result["final_stop"] > 0
    assert result["updates"]["我的最终止盈价"] == price_round(result["final_take"])
    assert result["updates"]["我的最终止损价"] == price_round(result["final_stop"])
    print("portfolio_risk self-test: OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书持仓止盈止损自动更新与推送")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--run-slot", type=int, choices=PUSH_HOURS)
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--notify", action="store_true", help="与 --run-now 同用时加入飞书推送队列")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.dry_run:
        print(run("DRY-RUN", write=False, notify=False)["report"])
    elif args.run_now:
        result = run("MANUAL", write=True, notify=args.notify)
        print(json.dumps({k: v for k, v in result.items() if k != "report"}, ensure_ascii=False))
        print(result["report"])
    elif args.run_slot is not None:
        run_slot(args.run_slot)
    elif args.loop:
        loop()
    else:
        parser.error("请指定 --loop、--run-slot、--run-now、--dry-run 或 --self-test")


if __name__ == "__main__":
    main()
