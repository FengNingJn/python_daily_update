#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日市场数据收集 -> 追加到 nga_daily_report.md
用法: python daily_market.py [--date 2026-06-26]
"""
import os, json, sys, argparse
from datetime import datetime, timedelta
import requests

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

parser = argparse.ArgumentParser()
parser.add_argument('--date', type=str, default='', help='日期 YYYY-MM-DD')
args = parser.parse_args()
TODAY = args.date if args.date else datetime.now().strftime("%Y-%m-%d")

session = requests.Session()
session.trust_env = False
session.headers.update({'User-Agent': 'Mozilla/5.0'})


def fetch_json(url, referer='https://data.eastmoney.com/'):
    try:
        r = session.get(url, headers={'Referer': referer}, timeout=15)
        return r.json()
    except:
        return {}


def get_indices():
    indices = {
        '上证指数': '1.000001', '深证成指': '0.399001', '创业板指': '0.399006',
        '科创50': '1.000688', '沪深300': '1.000300', '中证1000': '1.000852',
    }
    result = {}
    for name, code in indices.items():
        data = fetch_json(
            f'https://push2.eastmoney.com/api/qt/stock/get?secid={code}'
            f'&fields=f43,f44,f45,f47,f48,f170,f169'
        ).get('data')
        if data:
            result[name] = {
                'price': data.get('f43', 0) / 100,
                'high': data.get('f44', 0) / 100,
                'low': data.get('f45', 0) / 100,
                'volume': data.get('f47', 0),
                'amount_yi': data.get('f48', 0) / 1e8,
                'change_pct': data.get('f170', 0) / 100,
            }
    return result


def get_futures():
    contracts = {
        'IF(沪深300)': 'CFF_RE_IF2607',
        'IH(上证50)': 'CFF_RE_IH2607',
        'IM(中证1000)': 'CFF_RE_IM2607',
    }
    result = {}
    s = requests.Session()
    s.trust_env = False
    s.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'})
    for name, code in contracts.items():
        try:
            r = s.get(f'https://hq.sinajs.cn/list={code}', timeout=10)
            txt = r.text.strip()
            if not txt or '=""' in txt:
                continue
            parts = txt.split('"')[1].split(',')
            if len(parts) < 14:
                continue
            open_p = float(parts[0]) if parts[0] else 0
            high = float(parts[1]) if parts[1] else 0
            low = float(parts[2]) if parts[2] else 0
            price = float(parts[3]) if parts[3] else 0
            volume = int(float(parts[4])) if parts[4] else 0
            amount = float(parts[5]) / 1e8 if parts[5] else 0
            position = int(float(parts[6])) if parts[6] else 0
            prev_settle = float(parts[13]) if parts[13] else 0
            chg = price - prev_settle if prev_settle else 0
            chg_pct = (chg / prev_settle) * 100 if prev_settle else 0
            result[name] = {
                'price': price, 'open': open_p, 'high': high, 'low': low,
                'prev_settle': prev_settle, 'change': chg, 'change_pct': chg_pct,
                'volume': volume, 'amount_yi': amount, 'position': position,
            }
        except:
            pass
    return result


def get_sector_flow():
    data = fetch_json(
        'https://push2.eastmoney.com/api/qt/clist/get?'
        'pn=1&pz=86&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2'
        '&fields=f12,f14,f62,f66,f184'
    )
    rows = data.get('data', {}).get('diff', [])
    if not rows:
        return None
    inflow = sorted(rows, key=lambda r: r.get('f62', 0), reverse=True)[:5]
    outflow = sorted(rows, key=lambda r: r.get('f62', 0))[:5]
    return {
        'inflow': [(r['f14'], r['f62'] / 1e8, r.get('f66', 0) / 1e8) for r in inflow],
        'outflow': [(r['f14'], r['f62'] / 1e8, r.get('f66', 0) / 1e8) for r in outflow],
    }


def get_margin():
    data = fetch_json(
        'https://push2.eastmoney.com/api/qt/stock/get?secid=130.MARGIN&fields=f43,f170'
    ).get('data')
    if data and data.get('f43'):
        return {'balance': data['f43'] / 1e8, 'change_pct': data.get('f170', 0) / 100}
    return None


def get_limit_up():
    data = fetch_json(
        'https://push2.eastmoney.com/api/qt/clist/get?'
        'pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
        '&fields=f2,f3,f12,f14,f8,f9,f10,f20'
    )
    rows = data.get('data', {}).get('diff', []) if data else []
    return [{
        'name': r['f14'], 'code': r['f12'], 'pct': r['f3'],
        'turnover': r.get('f8', 0) / 100 if r.get('f8') else 0,
        'mcap': r.get('f20', 0) / 1e8 if r.get('f20') else 0,
    } for r in rows[:15]]


def generate_report():
    print(f"[每日市场数据] {TODAY}")
    print("=" * 60)

    lines = []
    lines.append(f"\n\n## 每日市场数据 - {TODAY}\n")

    indices = get_indices()
    if indices:
        lines.append("### 主要指数\n")
        lines.append("| 指数 | 收盘 | 涨跌幅 | 最高 | 最低 | 成交额(亿) |")
        lines.append("|------|------|--------|------|------|-----------|")
        for name in ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '中证1000']:
            d = indices.get(name)
            if d:
                lines.append(f"| {name} | {d['price']:.1f} | {d['change_pct']:+.2f}% | {d['high']:.1f} | {d['low']:.1f} | {d['amount_yi']:.0f} |")
        lines.append("")

    futs = get_futures()
    if futs:
        lines.append("### 股指期货主力合约\n")
        lines.append("| 合约 | 最新 | 涨跌 | 涨幅 | 开盘 | 最高 | 最低 | 昨结 | 持仓(手) | 成交额(亿) |")
        lines.append("|------|------|------|------|------|------|------|------|---------|-----------|")
        for name in ['IF(沪深300)', 'IH(上证50)', 'IM(中证1000)']:
            d = futs.get(name)
            if d:
                lines.append(
                    f"| {name} | {d['price']:.1f} | {d['change']:+.1f} | {d['change_pct']:+.2f}% | "
                    f"{d['open']:.1f} | {d['high']:.1f} | {d['low']:.1f} | {d['prev_settle']:.1f} | "
                    f"{d['position']} | {d['amount_yi']:.0f} |"
                )
        lines.append("")
        lines.append("**期货方向**: ")
        dirs = []
        for name in ['IF(沪深300)', 'IH(上证50)', 'IM(中证1000)']:
            d = futs.get(name)
            if d:
                arrow = "[空]" if d['change'] < 0 else "[多]" if d['change'] > 0 else "[平]"
                dirs.append(f"{name} {arrow}{abs(d['change_pct']):.1f}%")
        lines.append(" | ".join(dirs))
        lines.append("")

    flow = get_sector_flow()
    if flow:
        lines.append("### 行业资金流向\n")
        lines.append("**流入前5：**\n")
        for name, net, super_large in flow['inflow']:
            lines.append(f"- {name}: 主力净流入 **+{net:.1f}亿** (超大单 +{super_large:.1f}亿)")
        lines.append("\n**流出前5：**\n")
        for name, net, super_large in flow['outflow']:
            lines.append(f"- {name}: 主力净流出 **{net:.1f}亿** (超大单 {super_large:.1f}亿)")
        lines.append("")

    margin = get_margin()
    if margin:
        lines.append("### 融资融券\n")
        alert = ""
        if abs(margin['change_pct']) > 2:
            alert = f" [WARN] 大幅{'流入' if margin['change_pct'] > 0 else '流出'}!"
        lines.append(f"- 融资余额: **{margin['balance']:.0f}亿** ({margin['change_pct']:+.2f}%){alert}")
        lines.append("")
    else:
        lines.append("### 融资融券\n")
        lines.append("- 数据未更新（非交易日）\n")
        lines.append("")

    limits = get_limit_up()
    if limits:
        lines.append("### 涨停板（涨幅榜）\n")
        lines.append("| 代码 | 名称 | 涨幅 | 换手率 | 总市值(亿) |")
        lines.append("|------|------|------|--------|-----------|")
        for l in limits[:12]:
            lines.append(f"| {l['code']} | {l['name']} | {l['pct']:.1f}% | {l['turnover']:.1f}% | {l['mcap']:.0f} |")
        lines.append("")

    lines.append("### 复盘速览\n")
    if indices:
        sh = indices.get('上证指数', {})
        sz = indices.get('深证成指', {})
        lines.append(f"- 上证 {sh.get('price',0):.1f} ({sh.get('change_pct',0):+.2f}%) | 深证 {sz.get('price',0):.1f} ({sz.get('change_pct',0):+.2f}%)")
    if futs:
        if_pct = futs.get('IF(沪深300)', {}).get('change_pct', 0)
        dir_word = '下跌' if if_pct < 0 else '上涨'
        lines.append(f"- 期指全线{dir_word}, IF主力 {'-' if if_pct<0 else '+'}{abs(if_pct):.2f}%")
    if flow:
        top_in = flow['inflow'][0] if flow['inflow'] else ('-', 0, 0)
        top_out = flow['outflow'][0] if flow['outflow'] else ('-', 0, 0)
        lines.append(f"- 资金流入: {top_in[0]} +{top_in[1]:.1f}亿 | 流出: {top_out[0]} {top_out[1]:.1f}亿")
    lines.append(f"- 数据时间: {TODAY}（周末/节假日数据为上一交易日收盘）")
    lines.append("")

    report_text = '\n'.join(lines)
    print(report_text)

    report_path = os.path.join(OUTPUT_DIR, 'nga_daily_report.md')
    if os.path.exists(report_path):
        with open(report_path, 'r', encoding='utf-8') as f:
            existing = f.read()
        marker = f'\n\n## 每日市场数据 - {TODAY}'
        old = existing.find(marker)
        if old < 0:
            old = existing.find('\n\n## 每日市场数据 - ')
        if old > 0:
            existing = existing[:old]
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(existing + report_text)

    print(f"\n[OK] 已追加到 nga_daily_report.md")


if __name__ == '__main__':
    generate_report()
