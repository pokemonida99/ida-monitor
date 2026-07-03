#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取 Google News RSS 新聞，依標籤存入 SQLite。

只用 Python 標準庫，方便用 cron / launchd 排程執行。
用法：python3 fetch_news.py
"""
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
TAGS_PATH = BASE_DIR / "tags.json"

TAIPEI_TZ = timezone(timedelta(hours=8))

RSS_URL = (
    "https://news.google.com/rss/search?"
    "q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)

# 簡易情緒詞典（輿情正負面判斷）
POSITIVE_WORDS = [
    "成長", "突破", "創新高", "看好", "利多", "補助", "擴大", "領先", "肯定",
    "合作", "升級", "加碼", "回溫", "轉機", "受惠", "拓展", "商機", "亮眼",
    "獲利", "增加", "推動", "加速", "強化", "支持",
]
NEGATIVE_WORDS = [
    "衰退", "下滑", "裁員", "虧損", "危機", "衝擊", "疑慮", "批評", "質疑",
    "延宕", "跳票", "弊案", "抗議", "反對", "停工", "倒閉", "外移", "斷鏈",
    "減少", "重挫", "警訊", "隱憂", "爭議", "罰",
]


def sentiment_of(text: str) -> str:
    pos = sum(text.count(w) for w in POSITIVE_WORDS)
    neg = sum(text.count(w) for w in NEGATIVE_WORDS)
    if pos > neg:
        return "正面"
    if neg > pos:
        return "負面"
    return "中立"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            source TEXT,
            published TEXT,          -- ISO 日期時間（台北時區）
            pub_date TEXT,           -- YYYY-MM-DD，供每日聲量統計
            sentiment TEXT,
            fetched_at TEXT,
            UNIQUE(tag, link)
        );
        CREATE INDEX IF NOT EXISTS idx_articles_tag_date ON articles(tag, pub_date);
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            new_count INTEGER,
            note TEXT
        );
        """
    )
    conn.commit()


def fetch_rss(query: str):
    """回傳 [(title, link, source, published_dt)]"""
    q = urllib.parse.quote(f"{query} when:30d")
    url = RSS_URL.format(query=q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link:
            continue
        try:
            dt = parsedate_to_datetime(pub).astimezone(TAIPEI_TZ)
        except Exception:
            dt = datetime.now(TAIPEI_TZ)
        # Google News 標題常是「標題 - 媒體名」，把媒體名拆掉避免影響情緒判斷
        clean_title = re.sub(r"\s*-\s*[^-]+$", "", title) if " - " in title else title
        items.append((clean_title, link, source, dt))
    return items


def main() -> int:
    tags_cfg = json.loads(TAGS_PATH.read_text(encoding="utf-8"))["tags"]
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    now_iso = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    total_new = 0
    errors = []

    for cfg in tags_cfg:
        tag = cfg["tag"]
        for query in cfg["queries"]:
            try:
                items = fetch_rss(query)
            except Exception as e:
                errors.append(f"{tag}/{query}: {e}")
                continue
            for title, link, source, dt in items:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (tag, title, link, source, published, pub_date, sentiment, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tag,
                        title,
                        link,
                        source,
                        dt.isoformat(timespec="seconds"),
                        dt.strftime("%Y-%m-%d"),
                        sentiment_of(title),
                        now_iso,
                    ),
                )
                total_new += cur.rowcount
            conn.commit()
            time.sleep(1)  # 對 Google News 禮貌性間隔

    note = "; ".join(errors) if errors else "ok"
    conn.execute(
        "INSERT INTO fetch_log (run_at, new_count, note) VALUES (?, ?, ?)",
        (now_iso, total_new, note),
    )
    conn.commit()
    conn.close()
    print(f"[{now_iso}] 新增 {total_new} 則，狀態：{note}")

    # 接著更新新聞稿擴散追蹤與負面警訊
    try:
        import fetch_pr
        fetch_pr.main()
    except Exception as e:
        print(f"新聞稿追蹤更新失敗：{e}", file=sys.stderr)
    try:
        import fetch_alerts
        fetch_alerts.main()
    except Exception as e:
        print(f"負面警訊更新失敗：{e}", file=sys.stderr)

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
