#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""負面警訊監測：斷供、漲價、裁員、公協會發言、貿易制裁等。

依 alert_rules.json 的類別搜尋 Google News（近 14 天），
存入 alert_articles 供儀表板「負面警訊」頁籤提醒同仁撰寫輿情。

用法：python3 fetch_alerts.py（fetch_news.py 執行完也會自動呼叫）
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
RULES_PATH = BASE_DIR / "alert_rules.json"
TAIPEI_TZ = timezone(timedelta(hours=8))

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
WINDOW_DAYS = 14


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            source TEXT,
            pub_date TEXT,
            handled INTEGER DEFAULT 0,   -- 0=待處理 1=已寫輿情/已處理
            fetched_at TEXT,
            UNIQUE(category, link)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_date ON alert_articles(pub_date);
        """
    )
    conn.commit()


def fetch_query(query: str):
    q = urllib.parse.quote(f"{query} when:{WINDOW_DAYS}d")
    req = urllib.request.Request(RSS_URL.format(query=q), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link:
            continue
        clean_title = re.sub(r"\s*-\s*[^-]+$", "", title) if " - " in title else title
        try:
            dt = parsedate_to_datetime(pub).astimezone(TAIPEI_TZ)
        except Exception:
            dt = datetime.now(TAIPEI_TZ)
        items.append((clean_title, link, source, dt.strftime("%Y-%m-%d")))
    return items


def main() -> int:
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))["categories"]
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    now_iso = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")

    new_count = 0
    errors = []
    for rule in rules:
        cat = rule["category"]
        triggers = rule.get("triggers", [])
        seen_titles = {
            r[0] for r in conn.execute(
                "SELECT title FROM alert_articles WHERE category = ?", (cat,))
        }
        for query in rule["queries"]:
            try:
                items = fetch_query(query)
            except Exception as e:
                errors.append(f"{cat}/{query}: {e}")
                continue
            for title, link, source, pub_date in items:
                if title in seen_titles:  # 同標題不同連結（轉載）只留一筆
                    continue
                # 標題必須含類別觸發詞，過濾搜尋引擎的鬆散比對雜訊
                if triggers and not any(t in title for t in triggers):
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO alert_articles "
                    "(category, title, link, source, pub_date, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (cat, title, link, source, pub_date, now_iso),
                )
                if cur.rowcount:
                    seen_titles.add(title)
                    new_count += cur.rowcount
            conn.commit()
            time.sleep(1)

    conn.close()
    note = "; ".join(errors) if errors else "ok"
    print(f"[{now_iso}] 負面警訊新增 {new_count} 則，狀態：{note}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
