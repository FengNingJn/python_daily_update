#!/usr/bin/env python31
# -*- coding: utf-8 -*-
"""NGA 全量/增量爬虫 → 统一归档到 nga_daily_report.md"""
import os, re, json, time, sys
from datetime import datetime, timedelta
from collections import defaultdict
from bs4 import BeautifulSoup
import requests

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(OUTPUT_DIR, "nga_cookies.json")
DELAY = 0.5
TODAY = datetime.now().strftime("%Y-%m-%d")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept-Language": "zh-CN,zh;q=0.9"}

# ═══ 跟踪目标 ═══
TRACKED_THREADS = [
    ("46581190", "亨通光电真爱楼"),
    ("47047228", "澜起科技500元"),
]
TRACKED_UIDS = {
    "150058": "狼大", "60916468": "灰兔尾", "21321600": "幸运阿sai",
    "61395264": "村上吹树", "66662897": "fuelish", "42162697": "包子music",
    "41505780": "绝望之诗", "66278813": "文乌", "67145714": "Plezl",
    "65329649": "zippo578", "557398": "海指导",
}
UID_ORDER = ["150058", "60916468", "21321600", "61395264", "66662897", "42162697", "41505780", "66278813", "67145714", "65329649", "557398"]

# 用户已知主帖（searchpost=1 可能漏掉的帖子）
KNOWN_USER_THREADS = {
    "150058": [("45974302", "狼大-科学技术打头阵")],
}

# ═══ 工具 ═══
def load_cookies():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    return {}

def create_session():
    s = requests.Session(); s.headers.update(HEADERS)
    for k, v in load_cookies().items(): s.cookies.set(k, v)
    return s

def fetch(session, url, timeout=20):
    for _ in range(3):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 403: return None
            if r.status_code == 200: return r.content.decode('gbk', errors='replace')
            time.sleep(2)
        except: time.sleep(1)
    return None

def clean_reply(text):
    m = re.search(r'\[uid=\d+\](.+?)\[/uid\]', text)
    if m: return f"回复 **{m.group(1)}**"
    return re.sub(r'\[/?b\]|\[pid=\d+[^\]]*\]Reply\[/pid\]|Post by', '', text).strip()

def download_images(html_elems, tid):
    """提取 img 标签，下载图片到本地 images/ 目录，返回替换后的 HTML 字符串"""
    IMG_DIR = os.path.join(OUTPUT_DIR, "images")
    os.makedirs(IMG_DIR, exist_ok=True)
    
    img_map = {}  # {old_src: local_markdown}
    for img in html_elems.find_all('img'):
        src = img.get('src', '')
        if not src:
            continue
        # 解析 URL
        if src.startswith('./'):
            src = 'https://img.ngabbs.com/' + src[2:]
        elif src.startswith('http'):
            pass  # already absolute
        else:
            src = 'https://img.ngabbs.com/' + src.lstrip('/')
        
        # 生成本地文件名
        ext = os.path.splitext(src.split('/')[-1])[1] or '.jpg'
        if not ext.lower() in ('.jpg','.jpeg','.png','.gif','.webp'):
            continue
        fname = f"{tid}_{hash(src) & 0xffffffff:x}{ext}"
        local_path = os.path.join(IMG_DIR, fname)
        
        # 下载（只下一次）
        if not os.path.exists(local_path):
            try:
                r = requests.get(src, headers=HEADERS, timeout=15)
                if r.status_code == 200:
                    with open(local_path, 'wb') as f:
                        f.write(r.content)
            except:
                pass
        
        if os.path.exists(local_path):
            img_map[src] = f"images/{fname}"
            img['data-saved'] = fname  # mark as processed
    
    return img_map

def total_pages(session, tid, authorid=None):
    def curr(p):
        url = f"https://ngabbs.com/read.php?tid={tid}"
        if authorid: url += f"&authorid={authorid}"
        if p > 1: url += f"&page={p}"
        html = fetch(session, url)
        if not html or 'ERROR:15' in html[:500]: return -1
        m = re.search(r'__CURRENT_PAGE\s*=\s*(\d+)', html); return int(m.group(1)) if m else -1
    if curr(1) != 1: return 1
    hi = 2
    while True:
        a = curr(hi)
        if a < 0: return 1
        if a < hi: return a
        if hi > 500000: return hi
        hi *= 2; time.sleep(0.15)

def find_start(session, tid, authorid, target_date):
    total = total_pages(session, tid, authorid)
    if total <= 100: return 1
    lo, hi = 1, total
    while lo < hi:
        mid = (lo + hi) // 2
        url = f"https://ngabbs.com/read.php?tid={tid}"
        if authorid: url += f"&authorid={authorid}"
        if mid > 1: url += f"&page={mid}"
        html = fetch(session, url)
        if not html: lo = mid + 1; continue
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}', html)
        if m and m.group(1) >= target_date: hi = mid
        else: lo = mid + 1
        time.sleep(0.15)
    return max(1, lo)

# ═══ 帖子提取 ═══
def extract_post(table, tid=""):
    al = table.find('a', href=re.compile(r'uid='))
    uid = ""; 
    if al: m = re.search(r'uid=(\d+)', al.get('href', '')); uid = m.group(1) if m else ""
    floor = ""; pl = table.find('a', href=re.compile(r'pid='))
    if pl: floor = pl.get_text(strip=True)
    dt = ""; tp = re.compile(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})')
    for e in table.find_all(['span', 'td', 'div']):
        m = tp.search(e.get_text(strip=True))
        if m: dt = m.group(1); break
    pc = table.find(class_='postcontent')
    if not pc: return None
    
    # 下载图片并替换为 markdown
    img_map = download_images(pc, tid)
    for old_src, local_path in img_map.items():
        for img in pc.find_all('img'):
            if img.get('src', '').startswith('./') or 'img.ngabbs.com' in str(img.get('src', '')):
                if img.get('data-saved'):
                    # 用 markdown 图片标记替换 img 标签
                    alt = img.get('alt', 'image')
                    img_md = f"\n![{alt}]({local_path})\n"
                    img.replace_with(BeautifulSoup(img_md, 'html.parser'))
                    # 只替换第一个匹配的
                    continue
    
    quoted = ""; bq = pc.find('blockquote')
    if bq: quoted = clean_reply(bq.get_text('\n', strip=True)); bq.decompose()
    for br in pc.find_all('br'): br.replace_with('\n')
    text = re.sub(r'\n{3,}', '\n\n', pc.get_text('\n', strip=True)).strip()
    return {'floor': floor, 'uid': uid, 'time': dt, 'content': text, 'quoted': quoted}

# ═══ 爬取 ═══
def crawl(session, tid, author_uid=None, since_date=None):
    total = total_pages(session, tid, author_uid)
    start = find_start(session, tid, author_uid, since_date) if since_date else 1
    print(f"    [{start}/{total}]", end="")
    
    posts, seen = [], set()
    for page in range(start, total + 1):
        if page > start: time.sleep(DELAY)
        url = f"https://ngabbs.com/read.php?tid={tid}"
        if author_uid: url += f"&authorid={author_uid}"
        if page > 1: url += f"&page={page}"
        html = fetch(session, url)
        if not html: continue
        
        page_had_before = False
        for table in BeautifulSoup(html, 'lxml').find_all('table', class_=re.compile(r'forumbox')):
            p = extract_post(table, tid)
            if not p: continue
            if author_uid and p['uid'] != author_uid: continue
            if since_date and p['time'][:10] < since_date:
                page_had_before = True; continue
            key = p['content'][:80]
            if key not in seen: seen.add(key); posts.append(p)
        
        if since_date and page_had_before and len(seen) > 0:
            break  # already passed the date boundary
        
        if (page - start) % 50 == 0: print(f" {page}/{total}")
    print(f" -> {len(posts)}条")
    return posts

def get_user_threads(session, uid):
    """通过 searchpost=1 获取用户所有参与过的帖子，比只看他创建的帖更全面"""
    threads, seen = [], set()
    for page in range(1, 20):
        html = fetch(session, f"https://ngabbs.com/thread.php?authorid={uid}&searchpost=1&fid=0&page={page}")
        if not html or len(html) < 5000: continue
        found = False
        for a in BeautifulSoup(html, 'lxml').find_all('a', href=True):
            m = re.search(r'tid=(\d+)', a['href'])
            if m:
                tid = m.group(1)
                if tid not in seen:
                    t = a.get_text(strip=True)
                    if t and len(t) > 2 and 'NGA' not in t and '本页' not in t:
                        seen.add(tid)
                        threads.append({'tid': tid, 'title': t[:80]})
                        found = True
        if not found: break
        if page % 3 == 0: time.sleep(DELAY)
    return threads

# ═══ 合并输出 ═══
def merge_to_report(all_data, mode="全量"):
    report_path = os.path.join(OUTPUT_DIR, "nga_daily_report.md")
    
    lines = [f"\n\n---\n\n# 全量历史归档（按日期+按人）\n"]
    total = sum(len(vv) for v in all_data.values() for vv in v.values())
    lines.append(f"> 更新: {TODAY} | 模式: {mode} | {len(all_data)}天 | {total}条\n\n")
    
    for date in sorted(all_data.keys()):
        by_uid = all_data[date]
        day_total = sum(len(v) for v in by_uid.values())
        lines.append(f"### {date} ({day_total}条)\n")
        
        for uid in UID_ORDER:
            if uid not in by_uid: continue
            posts = by_uid[uid]
            seen = set(); unique = []
            for p in posts:
                k = p['content'][:60]
                if k not in seen: seen.add(k); unique.append(p)
            
            name = TRACKED_UIDS.get(uid, f"UID:{uid}")
            lines.append(f"**{name}** ({len(unique)}条)\n")
            for p in unique[:20]:
                c = p['content'].replace('\n', ' \n ')
                l = f"- [{p['time'][11:16]}] {c}"
                if p['quoted']: l += f"  [引用: {p['quoted'][:80]}]"
                lines.append(l)
            lines.append("")
        lines.append("---\n")
    
    new_section = '\n'.join(lines)
    
    if os.path.exists(report_path):
        with open(report_path, 'r', encoding='utf-8') as f:
            existing = f.read()
        old = existing.find("\n\n---\n\n# 全量历史归档")
        if old > 0: existing = existing[:old] + new_section
        else: existing += new_section
        with open(report_path, 'w', encoding='utf-8') as f: f.write(existing)
    else:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("# NGA 历史归档\n\n" + new_section)

# ═══════════════════════════════════════════════
# 每日市场数据采集
# ═══════════════════════════════════════════════

def _fetch_json(url, referer='https://data.eastmoney.com/'):
    s = requests.Session()
    s.trust_env = False
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    try:
        r = s.get(url, headers={'Referer': referer}, timeout=15)
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
        data = _fetch_json(
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
    """Sina CFF: [0]=今开 [1]=最高 [2]=最低 [3]=最新 [4]=成交量 [5]=成交额 [6]=持仓量 [13]=昨结算"""
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
    data = _fetch_json(
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
    # 先用实时API
    data = _fetch_json(
        'https://push2.eastmoney.com/api/qt/stock/get?secid=130.MARGIN&fields=f43,f170'
    ).get('data')
    if data and data.get('f43'):
        return {'balance': data['f43'] / 1e8, 'change_pct': data.get('f170', 0) / 100}
    # 非交易日回退到K线历史
    d = _fetch_json(
        'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=130.MARGIN'
        '&fields1=f1,f2,f3,f4&fields2=f51,f52,f53&klt=101&fqt=0&lmt=3'
    )
    if d:
        klines = d.get('data')
        if klines and klines.get('klines'):
            ks = klines['klines']
            if ks and len(ks) >= 2:
                last = ks[-1].split(',')
                prev = ks[-2].split(',')
                if len(last) >= 3:
                    bal = float(last[2]) / 1e8
                    bal_prev = float(prev[2]) / 1e8 if len(prev) >= 3 else bal
                    chg = (bal - bal_prev) / bal_prev * 100 if bal_prev else 0
                    return {'balance': bal, 'change_pct': chg}
    return None


def get_limit_up():
    data = _fetch_json(
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


# ═══════════════════════════════════════════════
# 市场数据报告生成
# ═══════════════════════════════════════════════

def generate_market_report():
    """生成每日市场数据表格，返回 markdown 文本"""
    print(f"\n[市场数据] 采集 {TODAY} 数据...")
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
        lines.append("| 合约 | 最新 | 涨跌 | 涨幅 | 开盘 | 最高 | 最低 | 昨结 | 持仓(手) |")
        lines.append("|------|------|------|------|------|------|------|------|---------|")
        for name in ['IF(沪深300)', 'IH(上证50)', 'IM(中证1000)']:
            d = futs.get(name)
            if d:
                lines.append(
                    f"| {name} | {d['price']:.1f} | {d['change']:+.1f} | {d['change_pct']:+.2f}% | "
                    f"{d['open']:.1f} | {d['high']:.1f} | {d['low']:.1f} | {d['prev_settle']:.1f} | "
                    f"{d['position']} |"
                )
        lines.append("")

    flow = get_sector_flow()
    if flow:
        lines.append("### 行业资金流向\n")
        lines.append("**流入前5：**\n")
        for name, net, super_large in flow['inflow']:
            lines.append(f"- {name}: 主力净流入 **+{net:.1f}亿**")
        lines.append("\n**流出前5：**\n")
        for name, net, super_large in flow['outflow']:
            lines.append(f"- {name}: 主力净流出 **{net:.1f}亿**")
        lines.append("")

    margin = get_margin()
    if margin:
        lines.append("### 融资融券\n")
        alert = ""
        if abs(margin['change_pct']) > 2:
            alert = f" [WARN] 大幅{'流入' if margin['change_pct'] > 0 else '流出'}!"
        lines.append(f"- 融资余额: **{margin['balance']:.0f}亿** ({margin['change_pct']:+.2f}%){alert}")
        lines.append("")

    limits = get_limit_up()
    if limits:
        lines.append("### 涨停板\n")
        lines.append("| 代码 | 名称 | 涨幅 | 换手率 | 总市值(亿) |")
        lines.append("|------|------|------|--------|-----------|")
        for l in limits[:10]:
            lines.append(f"| {l['code']} | {l['name']} | {l['pct']:.1f}% | {l['turnover']:.1f}% | {l['mcap']:.0f} |")
        lines.append("")

    return '\n'.join(lines), indices, futs, flow, margin, limits


# ═══════════════════════════════════════════════
# 量价分析 + 明日展望
# ═══════════════════════════════════════════════

def analyze_tomorrow(all_data, indices, futs, flow, margin, limits):
    """结合狼大发言和量价数据，生成明日走势分析"""
    lines = []
    lines.append(f"\n\n## 明日展望 - {TODAY} 夜盘\n")

    # ── 1. 提取狼大今日发言要点 ──
    wolf_today = []
    wolf_yesterday = []
    today_dates = [TODAY, (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]
    for date in sorted(all_data.keys(), reverse=True):
        if date in today_dates or date >= today_dates[-1]:
            for p in all_data[date].get('150058', []):
                if date == TODAY:
                    wolf_today.append(p)
                else:
                    wolf_yesterday.append(p)
        if len(wolf_today) >= 10:
            break

    if wolf_today:
        lines.append("### 狼大今日观点\n")
        for p in wolf_today[:8]:
            c = p['content'].replace('\n', ' \n ')
            lines.append(f"> [{p['time'][11:16]}] {c}")
        lines.append("")

    # ── 2. 量价关系分析 ──
    lines.append("### 量价关系\n")
    sh = indices.get('上证指数', {}) if indices else {}
    sz = indices.get('深证成指', {}) if indices else {}
    hs300 = indices.get('沪深300', {}) if indices else {}

    sh_chg = sh.get('change_pct', 0)
    sh_amt = sh.get('amount_yi', 0)

    # 判断放量/缩量：成交额 > 1.5万亿 算放量
    vol_level = "放量" if sh_amt > 15000 else "缩量" if sh_amt < 10000 else "平量"
    direction = "上涨" if sh_chg > 0.5 else "下跌" if sh_chg < -0.5 else "震荡"

    lines.append(f"- 上证: {direction} {sh_chg:+.2f}%, 成交{sh_amt:.0f}亿 ({vol_level})")

    # 量价组合判断
    signals = []
    if direction == "下跌" and vol_level == "放量":
        signals.append("放量下跌 = 恐慌抛压，短期偏空，关注次日是否缩量止跌")
    elif direction == "下跌" and vol_level == "缩量":
        signals.append("缩量下跌 = 惜售情绪，可能接近短期底部")
    elif direction == "上涨" and vol_level == "放量":
        signals.append("放量上涨 = 资金主动买入，趋势偏强")
    elif direction == "上涨" and vol_level == "缩量":
        signals.append("缩量上涨 = 跟风盘不足，反弹高度有限")
    else:
        signals.append(f"{direction}{vol_level} = 方向不明确，观望为主")

    for s in signals:
        lines.append(f"  → {s}")
    lines.append("")

    # ── 3. 期货信号 ──
    if futs:
        lines.append("### 期货信号\n")
        if_fut = futs.get('IF(沪深300)', {})
        ih_fut = futs.get('IH(上证50)', {})
        im_fut = futs.get('IM(中证1000)', {})

        if if_fut and hs300:
            basis = if_fut.get('price', 0) - hs300.get('price', 0)
            basis_pct = (basis / hs300.get('price', 1)) * 100
            basis_label = "贴水" if basis < -10 else "升水" if basis > 10 else "平水"
            lines.append(f"- IF vs 沪深300: {basis_label} {basis:+.1f}点 ({basis_pct:+.2f}%)")
            if basis < -20:
                lines.append(f"  → 大幅贴水，期市资金偏空")
            elif basis > 20:
                lines.append(f"  → 大幅升水，期市资金偏多")

        for name, d in [('IF', if_fut), ('IH', ih_fut), ('IM', im_fut)]:
            if d:
                arrow = "[空]" if d.get('change', 0) < 0 else "[多]" if d.get('change', 0) > 0 else "[平]"
                lines.append(f"- {name}: {arrow} {d.get('change_pct', 0):+.2f}% | 持仓{d.get('position', 0)}手")
        lines.append("")

    # ── 4. 资金方向 ──
    if flow:
        lines.append("### 资金方向\n")
        top_in = flow['inflow'][0] if flow['inflow'] else ('-', 0, 0)
        top_out = flow['outflow'][0] if flow['outflow'] else ('-', 0, 0)
        in_sum = sum(x[1] for x in flow['inflow'])
        out_sum = sum(abs(x[1]) for x in flow['outflow'])
        lines.append(f"- 流入前五合计: +{in_sum:.1f}亿 | 流出前五合计: -{out_sum:.1f}亿")
        lines.append(f"- 最大流入: {top_in[0]} +{top_in[1]:.1f}亿")
        lines.append(f"- 最大流出: {top_out[0]} -{abs(top_out[1]):.1f}亿")

        # 方向提示
        if '半导体' in str(flow['inflow']) or '光电子' in str(flow['inflow']) or '面板' in str(flow['inflow']):
            lines.append("  → 科技方向有资金关注")
        if '光伏' in str(flow['inflow']) or '新能源' in str(flow['inflow']):
            lines.append("  → 新能源方向有资金流入")
        if '国防' in str(flow['outflow']) or '军工' in str(flow['outflow']):
            lines.append("  → 军工方向资金流出")
        lines.append("")

    # ── 5. 情绪指标 ──
    if limits:
        lines.append("### 情绪指标\n")
        up20 = [l for l in limits if l['pct'] >= 19.9]
        up10 = [l for l in limits if 10 <= l['pct'] < 19.9]
        lines.append(f"- 涨停(>=20%): {len(up20)}只 | 大涨(10-20%): {len(up10)}只")

        if len(up20) >= 8:
            lines.append("  → 涨停家数多，短线情绪亢奋")
        elif len(up20) >= 3:
            lines.append("  → 短线情绪正常")
        else:
            lines.append("  → 涨停稀少，短线情绪低迷")

        # 方向提取
        sectors = set()
        for l in up20[:5]:
            n = l['name']
            if '光' in n or '硅' in n or '半导' in n:
                sectors.add('半导体/光电子')
            if '装备' in n or '精密' in n:
                sectors.add('高端制造')
            if '新材' in n or '材料' in n:
                sectors.add('新材料')
        if sectors:
            lines.append(f"  → 涨停方向: {', '.join(sectors)}")
        lines.append("")

    # ── 6. 综合展望 ──
    lines.append("### 综合展望\n")

    # 打分
    score = 0
    reasons = []

    if sh_chg > 0.5:
        score += 1; reasons.append("指数收涨")
    elif sh_chg < -0.5:
        score -= 1; reasons.append("指数收跌")
    else:
        reasons.append("指数横盘")

    if direction == "下跌" and vol_level == "缩量":
        score += 1; reasons.append("缩量下跌(惜售)")
    elif direction == "下跌" and vol_level == "放量":
        score -= 1; reasons.append("放量下跌(恐慌)")
    elif direction == "上涨" and vol_level == "放量":
        score += 1; reasons.append("放量上涨(强势)")

    if futs:
        if_fut = futs.get('IF(沪深300)', {})
        if if_fut.get('change', 0) < -0.5:
            score -= 1; reasons.append("期货偏空")
        elif if_fut.get('change', 0) > 0.5:
            score += 1; reasons.append("期货偏多")

    if limits and len(up20) >= 8:
        score += 1; reasons.append("短线情绪好")
    elif limits and len(up20) <= 2:
        score -= 1; reasons.append("短线情绪差")

    if margin and abs(margin.get('change_pct', 0)) > 2:
        if margin['change_pct'] > 0:
            score += 1; reasons.append("杠杆资金大幅流入")
        else:
            score -= 1; reasons.append("杠杆资金大幅流出")

    lines.append(f"**综合评分: {score}** ({'+' if score>=0 else ''}{score})")
    lines.append(f"因素: {' | '.join(reasons)}")
    lines.append("")

    if score >= 2:
        outlook = "偏乐观。量价配合较好，资金面和情绪面共振向上，明日大概率延续强势。关注早盘量能确认。"
    elif score >= 0:
        outlook = "中性偏谨慎。市场方向尚不明确，建议控制仓位观望。关注次日开盘30分钟量价方向。"
    elif score >= -1:
        outlook = "偏谨慎。空头信号较多，短期有继续调整压力。关注关键支撑位能否守住。"
    else:
        outlook = "偏空。多指标共振向下，建议降低仓位。等待缩量企稳信号后再考虑入场。"

    lines.append(f"> {outlook}")
    lines.append("")

    # 关键点位
    if sh:
        lines.append(f"- 上证关键支撑: {sh.get('low', 0):.0f} | 阻力: {sh.get('high', 0):.0f}")
    if hs300:
        lines.append(f"- 沪深300支撑: {hs300.get('low', 0):.0f} | 阻力: {hs300.get('high', 0):.0f}")
    lines.append("")

    lines.append("*免责: 以上为AI基于数据指标自动分析，仅供参考，不构成投资建议。*")
    lines.append("")

    return '\n'.join(lines)


# ═══ 主流程 ═══
def run(since_date=None):
    mode = "增量" if since_date else "全量"
    session = create_session()
    print(f"{'='*50}")
    print(f"NGA {mode}更新 | {TODAY}")
    if since_date: print(f"起始: {since_date}")
    print(f"{'='*50}")
    
    all_data = defaultdict(lambda: defaultdict(list))
    
    # 1. 指定帖子
    for tid, label in TRACKED_THREADS:
        print(f"\n[帖] {label} (tid={tid})")
        for p in crawl(session, tid):
            all_data[p['time'][:10]][p['uid']].append(p)
    
    # 2. 每个用户（全量保留完整历史）
    for uid, name in TRACKED_UIDS.items():
        print(f"\n[人] {name} (UID={uid})")
        threads = get_user_threads(session, uid)
        # fallback: 如果 searchpost=1 没抓到，用已知主帖
        if not threads and uid in KNOWN_USER_THREADS:
            threads = [{'tid': t[0], 'title': t[1]} for t in KNOWN_USER_THREADS[uid]]
        print(f"  参与帖: {len(threads)}")
        for i, t in enumerate(threads):
            print(f"  [{i+1}/{len(threads)}] {t['title'][:35]}...", end="")
            # 用户发言全量保留，不用增量过滤
            for p in crawl(session, t['tid'], author_uid=uid, since_date=since_date):
                all_data[p['time'][:10]][uid].append(p)
            if i < len(threads) - 1: time.sleep(DELAY)
    
    # 3. 合并输出
    print(f"\n[写] 合并归档到 nga_daily_report.md ...")
    merge_to_report(all_data, mode)
    
    # 统计
    total_posts = sum(len(vv) for v in all_data.values() for vv in v.values())
    print(f"\n{'='*50}")
    print(f"完成! {len(all_data)}天 {total_posts}条")
    for uid in UID_ORDER:
        cnt = sum(len(all_data[d].get(uid, [])) for d in all_data)
        if cnt: print(f"  {TRACKED_UIDS.get(uid, uid)}: {cnt}条")
    
    # 4. 市场数据采集 & 量价分析
    market_text, indices, futs, flow, margin, limits = generate_market_report()
    analysis_text = analyze_tomorrow(all_data, indices, futs, flow, margin, limits)
    
    # 追加到报告
    report_path = os.path.join(OUTPUT_DIR, "nga_daily_report.md")
    if os.path.exists(report_path):
        with open(report_path, 'r', encoding='utf-8') as f:
            existing = f.read()
        # 删除旧的同日市场数据 & 展望
        old_m = existing.find(f'\n\n## 每日市场数据 - {TODAY}')
        if old_m < 0:
            old_m = existing.find('\n\n## 每日市场数据 - ')
        old_a = existing.find(f'\n\n## 明日展望 - {TODAY}')
        if old_a < 0:
            old_a = existing.find('\n\n## 明日展望 - ')
        cut = min(old_m, old_a) if old_m > 0 and old_a > 0 else max(old_m, old_a)
        if cut > 0:
            existing = existing[:cut]
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(existing + market_text + analysis_text)
    
    print(f"\n[OK] 市场数据+明日展望已追加")

if __name__ == '__main__':
    if not load_cookies(): print("[FAIL] 未找到 nga_cookies.json"); exit(1)
    
    inc = '--inc' in sys.argv or '-i' in sys.argv
    since = None
    if inc:
        since = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        for a in sys.argv:
            if a.startswith('--days='): since = (datetime.now() - timedelta(days=int(a.split('=')[1]))).strftime("%Y-%m-%d")
            elif a.startswith('--since='): since = a.split('=')[1]
    
    run(since_date=since)
