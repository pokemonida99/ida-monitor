#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追蹤產業發展署新聞稿的媒體擴散狀況。

1. 爬取產發署官網新聞稿列表（前兩頁，約 20 則）
2. 對觀測期內（發布後 10 天）的新聞稿，用標題關鍵詞搜尋 Google News＋Bing News
   （Google 會把同事件報導聚合、RSS 常漏轉載，雙引擎互補提高召回）
3. 記錄發布日 D0 ~ D+10 的相關報導，統計報導則數、原始媒體數與平台數
   （轉載平台 Yahoo、LINE TODAY、PChome 等各算一則報導，與同仁人工回報口徑一致）

用法：python3 fetch_pr.py（fetch_news.py 執行完也會自動呼叫）
"""
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
TAIPEI_TZ = timezone(timedelta(hours=8))

IDA_LIST_URL = "https://www.ida.gov.tw/ctlr?PRO=news.rwdNewsList&type=news&page={page}"
IDA_BASE = "https://www.ida.gov.tw"
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
BING_RSS_URL = "https://www.bing.com/news/search?q={query}&format=RSS&setmkt=zh-TW"

# Bing 結果只有網址，平台名由網域判斷（原始媒體交給 enrich 的 DOMAIN_MEDIA）
PLATFORM_LABELS = {
    "tw.news.yahoo.com": "Yahoo新聞",
    "today.line.me": "LINE TODAY",
    "news.pchome.com.tw": "PChome",
    "msn.com": "MSN",
    "match.net.tw": "match生活網",
    "n.yam.com": "yam蕃薯藤",
    "yamnews.yam.com": "yam蕃薯藤",
}

OBSERVE_DAYS = 10   # 新聞稿發布後持續回補報導的天數
MAX_OFFSET = 10     # 收錄 D0 ~ D+10（儀表板 D+4 以後彙總成一欄顯示）

# 標題斷詞用的停用詞（機關名、無鑑別度用語）
STOPWORDS = [
    "經濟部產業發展署", "產業發展署", "經濟部", "產發署", "產業署",
    "今日", "今天", "正式", "舉辦", "辦理", "召開", "登場", "啟動",
    "歡迎", "邀請", "邀您", "共同", "攜手", "推動", "說明", "公告",
]

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS press_releases (
            id INTEGER PRIMARY KEY,          -- 官網新聞 id
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            category TEXT,
            release_date TEXT NOT NULL,      -- YYYY-MM-DD
            first_seen TEXT,
            last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS pr_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            source TEXT,
            pub_date TEXT,                   -- YYYY-MM-DD
            day_offset INTEGER,              -- 0=發布當天, 1=隔天...
            fetched_at TEXT,
            resolved_url TEXT,               -- 解碼後的原始文章網址
            outlet TEXT,                     -- 原始媒體（報社/通訊社）
            reporter TEXT,                   -- 記者姓名
            UNIQUE(pr_id, link)
        );
        """
    )
    # 舊資料庫升級
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pr_articles)")}
    for col in ("resolved_url", "outlet", "reporter"):
        if col not in cols:
            conn.execute(f"ALTER TABLE pr_articles ADD COLUMN {col} TEXT")
    conn.commit()


def fetch_pr_list(pages=2):
    """爬產發署新聞稿列表，回傳 [(id, title, url, category, date)]"""
    results = []
    for page in range(1, pages + 1):
        req = urllib.request.Request(IDA_LIST_URL.format(page=page), headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # 每筆資料的結構：idbDate 日期 →（可有分類標籤）→ idbSubject 內的連結與標題
        pattern = re.compile(
            r'idbDate">(\d{4}-\d{2}-\d{2})</span>.*?'
            r'(?:category-badge[^>]*>([^<]*)</span>.*?)?'
            r'PRO=news\.NewsView&id=(\d+)"[^>]*>([^<]+)</a>',
            re.S,
        )
        for m in pattern.finditer(html):
            rel_date, category, nid, title = m.groups()
            results.append((
                int(nid),
                title.strip(),
                f"{IDA_BASE}/ctlr?PRO=news.NewsView&id={nid}",
                (category or "").strip(),
                rel_date,
            ))
        time.sleep(1)
    return results


def extract_keywords(title: str):
    """回傳 (quoted_phrases, terms)：引號內詞組是強關鍵字，其餘取長字塊。"""
    quoted = re.findall(r"[「『]([^」』]{2,20})[」』]", title)
    t = title
    for q in quoted:
        t = t.replace(q, " ")
    for w in STOPWORDS:
        t = t.replace(w, " ")
    # 依非中英數字元切塊，保留長度 >= 3 的字塊（較有鑑別度）；
    # 純日期字塊（2026年、7月…）沒有鑑別度，會撈回整個月的無關新聞
    chunks = [c for c in re.split(r"[^\w]+", t)
              if len(c) >= 3 and not re.fullmatch(r"\d+[年月日號]?", c)]
    chunks.sort(key=len, reverse=True)
    return quoted, chunks[:4]


def _source_from_domain(domain: str) -> str:
    """由網域推平台名（Bing 結果用）。查不到就直接用網域。"""
    from enrich import DOMAIN_MEDIA
    table = {**{d: n for d, n in DOMAIN_MEDIA.items() if n}, **PLATFORM_LABELS}
    for d, name in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if domain == d or domain.endswith("." + d):
            return name
    return domain


def _numbers(text: str):
    """標題中的特徵數字（3 位數以上、非年份），千分位逗號移除。
    如「9,249家」→ 9249，是記者改寫標題時最常保留的元素。"""
    nums = set()
    for n in re.findall(r"\d[\d,，]*", text):
        n = re.sub(r"[,，]", "", n)
        if len(n) >= 3 and not re.fullmatch(r"(19|20)\d{2}", n):
            nums.add(n)
    return nums


def _lcs_len(a: str, b: str) -> int:
    """最長連續共同子字串長度（計畫名／活動名比對用）。"""
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ch == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _bigrams(text: str):
    """取中文字元雙字組＋英文單字（記者改寫標題時仍會保留大量詞彙）。
    英文詞 2 字母就收（AI、EV、IC 是產業新聞的關鍵詞）。
    數字不納入：年份、日期（2026、7月）在不相關新聞間太常見，會造成誤判。"""
    chars = re.sub(r"[^一-鿿]", "", text)
    grams = {chars[i:i + 2] for i in range(len(chars) - 1)}
    grams |= {w.lower() for w in re.findall(r"[A-Za-z]{2,}", text)}
    return grams


def is_relevant(article_title: str, quoted, terms, pr_core: str) -> bool:
    """報導標題與新聞稿「類似」就算：含引號詞組、含關鍵字塊，
    或雙字組重疊夠多（記者改寫標題仍會保留計畫名、機構名等詞彙）。"""
    flat = re.sub(r"\s+", "", article_title)  # 忽略空格差異
    if any(re.sub(r"\s+", "", q) in flat for q in quoted):
        return True
    if sum(1 for c in terms if c in article_title) >= 2:
        return True
    # 記者大幅改寫標題時，特徵數字（9,249家→9249）或計畫名等
    # 連續共同字串（如「AI輔導團」）仍會保留
    core_flat = re.sub(r"\s+", "", pr_core).lower()
    if _numbers(core_flat) & _numbers(flat):
        return True
    pr_bg = _bigrams(pr_core)
    shared = len(pr_bg & _bigrams(article_title)) if pr_bg else 0
    # LCS 先剔除數字：年份日期（2026年、7月）人人都有，不能當特徵
    lcs = _lcs_len(re.sub(r"\d+", "", core_flat), re.sub(r"\d+", "", flat.lower()))
    if lcs >= 5:
        return True
    if lcs >= 4 and shared >= 2:  # 較短的共同字串需雙字組佐證
        return True
    if not pr_bg:
        return False
    # 至少 6 組共同雙字組，或覆蓋新聞稿核心字 25%
    return shared >= max(6, int(len(pr_bg) * 0.25))


def build_queries(quoted, terms):
    """一則新聞稿發多組查詢擴大召回：
    引號詞組單獨查（活動/計畫名最準），關鍵字塊兩兩配對查，
    再加上較長的單一關鍵字塊（最寬，不相關的靠 is_relevant 過濾）。"""
    queries = [f'"{q}"' for q in quoted]
    if len(terms) >= 2:
        queries.append(f"{terms[0]} {terms[1]}")
        if len(terms) >= 3:
            queries.append(f"{terms[0]} {terms[2]}")
            queries.append(f"{terms[1]} {terms[2]}")
    for t in terms:
        if len(t) >= 4:
            queries.append(t)
    return queries[:7]


def search_coverage(pr_title: str, release: date):
    """搜尋新聞稿發布日前後的相關報導（雙引擎、多組查詢、合併去重）。
    Google News 會把同事件報導聚合、RSS 常只回傳代表作，
    Bing News 補上另一批來源（工商、經濟日報、商周、MSN…）。"""
    quoted, terms = extract_keywords(pr_title)
    queries = build_queries(quoted, terms)
    if not queries:
        return []
    # 新聞稿標題去掉機關名後的核心文字，供相似度比對
    pr_core = pr_title
    for w in STOPWORDS:
        pr_core = pr_core.replace(w, "")
    window = (f' after:{(release - timedelta(days=1)).strftime("%Y-%m-%d")}'
              f' before:{(release + timedelta(days=MAX_OFFSET + 1)).strftime("%Y-%m-%d")}')

    out = {}
    seen_titles = set()  # 同平台同標題只留一筆（跨引擎去重靠這組 key）
    seen_bigrams = []    # [(source, title_bigrams)]：引擎間標題微差的近似去重

    def consider(title, link, source, dt):
        if not title or not link or link in out:
            return
        clean_title = re.sub(r"\s*-\s*[^-]+$", "", title) if " - " in title else title
        if (source, clean_title) in seen_titles:
            return
        # 同來源、標題幾乎相同（兩引擎抓到同一篇但標題有一兩字差異）視為同篇
        bg = _bigrams(clean_title)
        for s, tbg in seen_bigrams:
            if s == source and bg and tbg and \
                    len(bg & tbg) / min(len(bg), len(tbg)) >= 0.85:
                return
        if not is_relevant(clean_title, quoted, terms, pr_core):
            return
        offset = (dt.date() - release).days
        # 記者會提前見報或引擎回報 UTC 造成的 D-1，歸入 D0
        if -1 <= offset <= MAX_OFFSET:
            seen_titles.add((source, clean_title))
            seen_bigrams.append((source, bg))
            out[link] = (clean_title, link, source,
                         dt.strftime("%Y-%m-%d"), max(offset, 0))

    for query in queries:
        # Google News（支援 after:/before: 限縮時間）
        try:
            q = urllib.parse.quote(query + window)
            req = urllib.request.Request(RSS_URL.format(query=q), headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read())
            for item in root.iter("item"):
                source = (item.findtext("source") or "").strip()
                if source.endswith("gov.tw"):  # 官網自載不算媒體報導
                    continue
                # Google 來源偶爾直接給網域（news.cnyes.com），轉成媒體名以利去重
                if "." in source and re.fullmatch(r"[A-Za-z0-9.-]+", source):
                    source = _source_from_domain(source.lower())
                try:
                    dt = parsedate_to_datetime(item.findtext("pubDate")).astimezone(TAIPEI_TZ)
                except Exception:
                    continue
                consider((item.findtext("title") or "").strip(),
                         (item.findtext("link") or "").strip(), source, dt)
        except Exception:
            pass
        time.sleep(1)

        # Bing News（無時間運算子，靠發布日過濾；連結已是原始網址）
        try:
            q = urllib.parse.quote(query)
            req = urllib.request.Request(BING_RSS_URL.format(query=q), headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read())
            for item in root.iter("item"):
                link = (item.findtext("link") or "").strip()
                m = re.search(r"[?&]url=([^&]+)", link)
                url = urllib.parse.unquote(m.group(1)) if m else link
                domain = urllib.parse.urlparse(url).netloc.lower()
                domain = domain[4:] if domain.startswith("www.") else domain
                if not domain or domain.endswith("gov.tw"):
                    continue
                try:
                    dt = parsedate_to_datetime(item.findtext("pubDate")).astimezone(TAIPEI_TZ)
                except Exception:
                    continue
                consider((item.findtext("title") or "").strip(),
                         url, _source_from_domain(domain), dt)
        except Exception:
            pass
        time.sleep(1)
    return list(out.values())


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    now_iso = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    today = datetime.now(TAIPEI_TZ).date()

    try:
        prs = fetch_pr_list()
    except Exception as e:
        print(f"抓取新聞稿列表失敗：{e}", file=sys.stderr)
        return 1

    for nid, title, url, category, rel_date in prs:
        conn.execute(
            "INSERT OR IGNORE INTO press_releases (id, title, url, category, release_date, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (nid, title, url, category, rel_date, now_iso),
        )
    conn.commit()

    # 回補對象：仍在觀測期內（發布後 OBSERVE_DAYS 天）的新聞稿，
    # 加上 30 天內從未搜尋過的（首次執行時做一次性回補，Google News 約保留 30 天）
    # --backfill：重搜 40 天內全部新聞稿並重建報導清單
    # （先清掉舊結果再搜，判斷邏輯改版後可洗掉先前的誤判）
    cutoff = (today - timedelta(days=OBSERVE_DAYS)).strftime("%Y-%m-%d")
    month_ago = (today - timedelta(days=40)).strftime("%Y-%m-%d")
    backfill = "--backfill" in sys.argv
    if backfill:
        active = conn.execute(
            "SELECT id, title, release_date FROM press_releases "
            "WHERE release_date >= ?", (month_ago,),
        ).fetchall()
    else:
        active = conn.execute(
            "SELECT id, title, release_date FROM press_releases "
            "WHERE release_date >= ? OR (release_date >= ? AND last_checked IS NULL)",
            (cutoff, month_ago),
        ).fetchall()

    new_count = 0
    for pr_id, title, rel_str in active:
        release = date.fromisoformat(rel_str)
        try:
            items = search_coverage(title, release)
        except Exception as e:
            print(f"搜尋失敗 [{title[:20]}…]：{e}", file=sys.stderr)
            continue
        if backfill:  # 搜尋成功才重建，避免搜掛時把舊資料清空
            conn.execute("DELETE FROM pr_articles WHERE pr_id = ?", (pr_id,))
        existing = {
            (r[0], r[1]) for r in conn.execute(
                "SELECT source, title FROM pr_articles WHERE pr_id = ?", (pr_id,)
            )
        }
        for a_title, link, source, pub_date, offset in items:
            if (source, a_title) in existing:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO pr_articles "
                "(pr_id, title, link, source, pub_date, day_offset, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pr_id, a_title, link, source, pub_date, offset, now_iso),
            )
            new_count += cur.rowcount
        conn.execute(
            "UPDATE press_releases SET last_checked = ? WHERE id = ?",
            (now_iso, pr_id),
        )
        conn.commit()
        time.sleep(1)

    # 解析新報導的原始媒體與記者（每次最多 60 篇，避免單次執行過久）
    from enrich import enrich_article
    pending = conn.execute(
        "SELECT id, link, source FROM pr_articles WHERE resolved_url IS NULL LIMIT 60"
    ).fetchall()
    enriched = 0
    for row_id, link, source in pending:
        try:
            url, outlet, reporter = enrich_article(link, source)
        except Exception:
            url, outlet, reporter = link, source, None
        conn.execute(
            "UPDATE pr_articles SET resolved_url = ?, outlet = ?, reporter = ? WHERE id = ?",
            (url, outlet, reporter, row_id),
        )
        conn.commit()
        enriched += 1
        time.sleep(1)

    # 同一平台＋同標題的重複連結只留一筆；
    # 不同平台的轉載（Yahoo、LINE TODAY、PChome…）視為獨立報導保留，
    # 與人工回報「每個刊出平台各算一則」的口徑一致
    conn.execute(
        "DELETE FROM pr_articles WHERE id NOT IN ("
        "SELECT MIN(id) FROM pr_articles GROUP BY pr_id, COALESCE(source, ''), title)"
    )
    conn.commit()
    conn.close()
    print(f"[{now_iso}] 新聞稿 {len(prs)} 則（觀測中 {len(active)}），"
          f"新增報導 {new_count} 則，解析媒體/記者 {enriched} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
