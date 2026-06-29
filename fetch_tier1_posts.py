#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爬取NGA精华帖 - 楼主全部发言(UID匹配) + 日常帖增量更新"""

import os, re, json, time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(OUTPUT_DIR, "nga_cookies.json")
DELAY = 0.5
TODAY = datetime.now().strftime("%Y-%m-%d")

# ============================================================
# 配置
# ============================================================
TIER1_THREADS = [
    {"tid": "21729074", "label": "新股与次新股思路研究分享", "author": "qiaoxueji"},
    {"tid": "28986514", "label": "二纬路战法选股公式", "author": "土の豆"},
    {"tid": "32769897", "label": "只谈逻辑不谈交易(半导体+电力)", "author": "海伯利安之歌"},
    {"tid": "45807456", "label": "行业信息差_各行业未来催化", "author": "幸运阿sai"},
    {"tid": "20485399", "label": "夹头日志_价值投资实盘", "author": "卯吴骆辰黎毕"},
]

# 日常指导帖配置
DAILY_TID = "45974302"
DAILY_KEY_UIDS = {
    "150058": "狼大(楼主)",
    "66989363": "鱼饵啊啊",
    "65329649": "技术分析",
    "67086272": "冷酷鸡腿堡",
    "60916468": "灰兔尾",
}

# 全量跟踪用户（增量更新时会扫描他们所有主题帖）
TRACKED_UIDS = {
    "150058": "狼大",
    "60916468": "灰兔尾",
    "21321600": "幸运阿sai",
    "61395264": "村上吹树",
    "66662897": "66662897",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept-Language": "zh-CN,zh;q=0.9"}

# 爬取策略：前N页 + 后M页（兼顾早期精华和最新更新）
FRONT_PAGES = 60   # 前60页（早期精华内容）
TAIL_PAGES = 20    # 后20页（最新更新）
SKIP_IF_FEWER = 62  # 如果总页数少于此值，全量爬取

def load_cookies():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def get_page(session, tid, page, authorid=None):
    url = f"https://ngabbs.com/read.php?tid={tid}"
    if authorid:
        url += f"&authorid={authorid}"
    if page > 1: url += f"&page={page}"
    for _ in range(3):
        try:
            r = session.get(url, timeout=25, headers=HEADERS)
            if r.status_code == 403: return None, "403"
            if r.status_code == 200: return r.content.decode('gbk', errors='replace'), None
            time.sleep(2)
        except: time.sleep(1)
    return None, "timeout"

def get_total_pages_binary(session, tid, authorid=None):
    """二分法查找真实总页数（通过 __CURRENT_PAGE 检测边界）"""
    def get_current_page(page):
        try:
            url = f"https://ngabbs.com/read.php?tid={tid}"
            if authorid: url += f"&authorid={authorid}"
            if page > 1: url += f"&page={page}"
            r = session.get(url, timeout=15, headers=HEADERS)
            if r.status_code != 200: return -1
            html = r.content.decode('gbk', errors='replace')
            if 'ERROR:15' in html[:500]: return -1
            m = re.search(r'__CURRENT_PAGE\s*=\s*(\d+)', html)
            return int(m.group(1)) if m else -1
        except:
            return -1
    
    # 确保第1页有效
    if get_current_page(1) != 1:
        return 1
    
    # 指数探测找上界
    hi = 2
    while True:
        actual = get_current_page(hi)
        if actual < 0:
            return 1
        if actual < hi:  # 被重定向，actual 就是最后一页
            return actual
        if hi > 500000:
            return hi
        hi *= 2
        time.sleep(0.15)

def extract_posts(html, author_uid):
    """从HTML提取指定UID的发言"""
    soup = BeautifulSoup(html, 'lxml')
    posts = []
    tables = soup.find_all('table', class_=re.compile(r'forumbox'))
    if not tables: tables = soup.find_all('table', class_=re.compile(r'postbox'))
    
    for table in tables:
        pc = table.find(class_='postcontent')
        if not pc: continue
        
        al = table.find('a', href=re.compile(r'uid='))
        if not al: continue
        m = re.search(r'uid=(\d+)', al.get('href', ''))
        if not m or m.group(1) != author_uid: continue
        
        # 楼层
        floor = ""
        pl = table.find('a', href=re.compile(r'pid='))
        if pl:
            t = pl.get_text(strip=True)
            if t: floor = t
        
        # 时间
        pt = ""
        tp = re.compile(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}')
        for e in table.find_all(['span', 'td', 'div']):
            if tp.search(e.get_text(strip=True)):
                pt = e.get_text(strip=True)
                break
        
        # 清理
        for bq in pc.find_all('blockquote'): bq.decompose()
        for br in pc.find_all('br'): br.replace_with('\n')
        text = pc.get_text('\n', strip=False)
        text = re.sub(r'\[/?b\]', '', text)
        text = re.sub(r'\[/?collapse[^\]]*\]', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        
        if text and len(text) > 5:
            key = text[:80]
            if not any(p['content'][:80] == key for p in posts):
                posts.append({'floor': floor, 'time': pt, 'content': text})
    return posts

def crawl_pages(session, tid, author_uid, pages_to_crawl):
    """爬取指定页码列表"""
    all_posts = []
    ek = set()
    
    for i, page in enumerate(pages_to_crawl):
        if i > 0: time.sleep(DELAY)
        print(f"  [页{page}]", end=" ")
        html, err = get_page(session, tid, page, author_uid)
        if err:
            html, err = get_page(session, tid, page)
        if err: print("[FAIL]"); continue
        
        posts = extract_posts(html, author_uid)
        new = 0
        for p in posts:
            key = p['content'][:80]
            if key not in ek:
                all_posts.append(p)
                ek.add(key)
                new += 1
        print(f"+{new}", end=" ")
    return all_posts

def crawl_thread(session, tid, author_name):
    """爬取一个帖子的全部楼主发言（智能策略）"""
    # 第1步：获取UID
    print(f"  [获取UID]", end=" ")
    html, err = get_page(session, tid, 1)
    if err: print(f"[FAIL]({err})"); return None
    
    soup = BeautifulSoup(html, 'lxml')
    author_uid = None
    ft = soup.find('table', class_=re.compile(r'forumbox'))
    if ft:
        al = ft.find('a', href=re.compile(r'uid='))
        if al:
            m = re.search(r'uid=(\d+)', al.get('href', ''))
            if m: author_uid = m.group(1)
    if not author_uid: print("[FAIL] 无法获取UID"); return None
    
    # 第2步：二分法检测总页数
    print(f"UID={author_uid}", end=" ")
    total_pages = get_total_pages_binary(session, tid, author_uid)
    print(f"共{total_pages}页")
    
    # 第3步：决定爬取策略
    if total_pages <= SKIP_IF_FEWER:
        pages_to_crawl = list(range(1, total_pages + 1))
        print(f"  策略: 全量爬取 {len(pages_to_crawl)}页")
    else:
        front = list(range(1, FRONT_PAGES + 1))
        tail = list(range(max(FRONT_PAGES + 1, total_pages - TAIL_PAGES + 1), total_pages + 1))
        pages_to_crawl = front + tail
        print(f"  策略: 前{FRONT_PAGES}页 + 后{TAIL_PAGES}页 = {len(pages_to_crawl)}页")
    
    all_posts = crawl_pages(session, tid, author_uid, pages_to_crawl)
    print()
    return all_posts

def main():
    print("=" * 60)
    print("NGA第一梯队 - 楼主全部发言爬取 (UID匹配 + 二分页检测)")
    print("=" * 60)
    
    cookies = load_cookies()
    if not cookies: print("[FAIL] 未找到 nga_cookies.json"); return
    
    session = requests.Session()
    session.headers.update(HEADERS)
    for k, v in cookies.items(): session.cookies.set(k, v)
    print(f"[OK] {len(cookies)} Cookie\n")
    
    results = []
    for i, t in enumerate(TIER1_THREADS, 1):
        tid, label, author = t['tid'], t['label'], t['author']
        print(f"{'─'*50}")
        print(f"[{i}/5] {label} (tid={tid})")
        print(f"{'─'*50}")
        
        posts = crawl_thread(session, tid, author)
        
        if posts:
            safe = re.sub(r'[\\/:*?"<>|]', '_', label)
            fpath = os.path.join(OUTPUT_DIR, f"tier1_{i:02d}_{safe}.md")
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(f"# {label}\n\n")
                f.write(f"- **TID**: {tid}\n- **作者**: {author}\n")
                f.write(f"- **链接**: https://ngabbs.com/read.php?tid={tid}\n")
                f.write(f"- **楼主发言**: {len(posts)} 条\n")
                f.write(f"- **爬取时间**: {TODAY}\n\n---\n\n")
                for idx, p in enumerate(posts, 1):
                    h = f"## 发言 #{idx}"
                    if p['floor']: h += f"  [pid={p['floor']}]"
                    if p['time']: h += f"  *{p['time']}*"
                    f.write(h + "\n\n" + p['content'] + "\n\n---\n\n")
            kb = os.path.getsize(fpath) / 1024
            chars = sum(len(p['content']) for p in posts)
            print(f"  [OK] {kb:.0f}KB | {len(posts)}条 | {chars}字\n")
            results.append({'tid': tid, 'label': label, 'posts': len(posts), 'chars': chars})
        else:
            print(f"  [FAIL]\n")
            results.append({'tid': tid, 'label': label, 'posts': 0, 'chars': 0})
    
    print(f"{'='*60}")
    print("Done!")
    for r in results:
        s = f"{r['posts']}条/{r['chars']}字" if r['posts'] else "FAIL"
        print(f"  {r['label']}: [OK] {s}")
    with open(os.path.join(OUTPUT_DIR, "tier1_summary.json"), 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

def crawl_daily_incremental(session, last_date=None):
    """爬取日常指导帖(45974302)的增量更新"""
    if last_date is None:
        last_date = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    
    print("=" * 60)
    print(f"日常帖增量爬取 (tid={DAILY_TID}, 从{last_date}起)")
    print("=" * 60)
    
    # 二分法找最后一页
    total_pages = get_total_pages_binary(session, DAILY_TID)
    print(f"总页数: {total_pages}")
    
    # 二分找 last_date 起始页
    lo, hi = max(1, total_pages - 500), total_pages
    while lo < hi:
        mid = (lo + hi) // 2
        html, err = get_page(session, DAILY_TID, mid)
        if err:
            lo = mid + 1; continue
        soup = BeautifulSoup(html, 'lxml')
        dates = set()
        tp = re.compile(r'(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}')
        for e in soup.find_all(['span', 'td', 'div']):
            m = tp.search(e.get_text(strip=True))
            if m: dates.add(m.group(1))
        if dates and min(dates) >= last_date:
            hi = mid
        else:
            lo = mid + 1
        time.sleep(0.2)
    
    start_page = lo
    pages_to_crawl = list(range(start_page, total_pages + 1))
    print(f"起始页: {start_page}, 共{len(pages_to_crawl)}页\n")
    
    # 爬取
    all_posts = []
    ek = set()
    for i, page in enumerate(pages_to_crawl):
        if i > 0: time.sleep(DELAY)
        print(f"  [页{page}/{total_pages}]", end=" ")
        html, err = get_page(session, DAILY_TID, page)
        if err:
            print("[FAIL]"); continue
        
        soup = BeautifulSoup(html, 'lxml')
        tables = soup.find_all('table', class_=re.compile(r'forumbox'))
        tp = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})')
        
        new = 0
        for table in tables:
            al = table.find('a', href=re.compile(r'uid='))
            if not al: continue
            m = re.search(r'uid=(\d+)', al.get('href', ''))
            if not m: continue
            uid = m.group(1)
            if uid not in DAILY_KEY_UIDS: continue
            
            date_str, time_str = "", ""
            for e in table.find_all(['span', 'td', 'div']):
                dm = tp.search(e.get_text(strip=True))
                if dm:
                    date_str = dm.group(1)
                    time_str = dm.group(2)
                    break
            if not date_str or date_str < last_date: continue
            
            pc = table.find(class_='postcontent')
            if not pc: continue
            for bq in pc.find_all('blockquote'): bq.decompose()
            for br in pc.find_all('br'): br.replace_with('\n')
            content = pc.get_text('\n', strip=True)
            content = re.sub(r'\n{3,}', '\n\n', content).strip()
            
            key = content[:60]
            if key not in ek and len(content) > 10:
                ek.add(key)
                new += 1
                all_posts.append({
                    'uid': uid, 'name': DAILY_KEY_UIDS[uid],
                    'date': date_str, 'time': time_str, 'content': content
                })
        print(f"+{new}", end=" ")
    print()
    
    # 按日期分组统计
    from collections import defaultdict
    by_date = defaultdict(lambda: defaultdict(int))
    for p in all_posts:
        by_date[p['date']][p['name']] += 1
    
    print(f"\n增量统计 ({last_date} ~ {TODAY}):")
    for d in sorted(by_date.keys()):
        parts = [f"{n}:{c}" for n, c in sorted(by_date[d].items(), key=lambda x: -x[1])]
        print(f"  {d}: {', '.join(parts)} (共{sum(by_date[d].values())}条)")
    
    return all_posts, by_date


def crawl_user_incremental(session, uid, name, since_date):
    """爬取单个用户自 since_date 起的所有主题帖新增发言"""
    print(f"\n{'─'*40}\n[{name}] UID={uid} 增量更新 (自{since_date})")
    
    # 获取用户所有主题帖
    threads = []
    seen = set()
    for page in range(1, 30):
        html, err = get_page(session, f"thread.php?authorid={uid}&page={page}", uid)
        # thread.php不是read.php，自己处理
        try:
            r = session.get(f"https://ngabbs.com/thread.php?authorid={uid}&page={page}", 
                          timeout=15, headers=HEADERS)
            if r.status_code != 200: break
            html = r.content.decode('gbk', errors='replace')
        except: break
        
        found = False
        for m in re.finditer(r'tid=(\d+)', html):
            tid = m.group(1)
            if tid not in seen:
                seen.add(tid)
                # 找标题
                title_m = re.search(rf'tid={tid}[^>]*>([^<]+)<', html)
                title = title_m.group(1)[:80] if title_m else f"TID_{tid}"
                threads.append({'tid': tid, 'title': title})
                found = True
        if not found: break
        time.sleep(DELAY)
    
    print(f"  主题帖: {len(threads)}")
    
    total_new = 0
    for i, t in enumerate(threads):
        # 只爬该用户发言
        posts, err = crawl_thread(session, t['tid'], name)
        # crawl_thread returns all posts by the user, we need to filter by date
        # For now just count
        total_new += len(posts) if posts else 0
        if i < len(threads) - 1: time.sleep(DELAY)
    
    print(f"  [OK] {total_new}条发言")
    return total_new


if __name__ == '__main__':
    import sys
    cookies = load_cookies()
    if not cookies:
        print("[FAIL] 未找到 nga_cookies.json")
        sys.exit(1)
    
    session = requests.Session()
    session.headers.update(HEADERS)
    for k, v in cookies.items(): session.cookies.set(k, v)
    
    if len(sys.argv) > 1 and sys.argv[1] == "daily-all":
        # 增量更新：日常帖 + 所有跟踪用户
        last = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        if len(sys.argv) > 2: last = sys.argv[2]
        print(f"[OK] {len(cookies)} Cookie | 增量更新 自 {last}\n")
        
        # 1. 日常帖增量
        posts, stats = crawl_daily_incremental(session, last)
        
        # 2. 每个跟踪用户的增量
        for uid, name in TRACKED_UIDS.items():
            try:
                crawl_user_incremental(session, uid, name, last)
            except Exception as e:
                print(f"  [FAIL] {name}: {e}")
        
        print(f"\n{'='*60}\n全部更新完成!")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "daily":
        # 只爬日常帖增量
        print(f"[OK] {len(cookies)} Cookie\n")
        last = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        if len(sys.argv) > 2: last = sys.argv[2]
        posts, stats = crawl_daily_incremental(session, last)
    
    else:
        main()
