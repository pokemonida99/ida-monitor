#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""輿情觀測器網頁伺服器（純標準庫）。

用法：python3 server.py [port]，預設 8765。
路由：
  GET  /             儀表板頁面
  GET  /api/summary  30 天彙總資料（JSON）
  POST /api/refresh  立即執行一次抓取，回傳結果
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
TAGS_PATH = BASE_DIR / "tags.json"
INDEX_PATH = BASE_DIR / "index.html"

TAIPEI_TZ = timezone(timedelta(hours=8))
DAYS = 30


def build_summary() -> dict:
    tags_cfg = json.loads(TAGS_PATH.read_text(encoding="utf-8"))["tags"]

    today = datetime.now(TAIPEI_TZ).date()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS - 1, -1, -1)]
    start = dates[0]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 每日每標籤聲量
    rows = conn.execute(
        "SELECT tag, pub_date, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? GROUP BY tag, pub_date",
        (start,),
    ).fetchall()
    daily = {}
    for r in rows:
        daily.setdefault(r["tag"], {})[r["pub_date"]] = r["n"]

    series = []
    totals = []
    for cfg in tags_cfg:
        tag = cfg["tag"]
        counts = [daily.get(tag, {}).get(d, 0) for d in dates]
        series.append({"tag": tag, "color": cfg["color"], "counts": counts})
        totals.append({"tag": tag, "color": cfg["color"], "total": sum(counts)})

    # 情緒分布
    sent_rows = conn.execute(
        "SELECT sentiment, COUNT(*) AS n FROM articles WHERE pub_date >= ? GROUP BY sentiment",
        (start,),
    ).fetchall()
    sentiment = {r["sentiment"]: r["n"] for r in sent_rows}

    # 各標籤情緒
    tag_sent_rows = conn.execute(
        "SELECT tag, sentiment, COUNT(*) AS n FROM articles WHERE pub_date >= ? GROUP BY tag, sentiment",
        (start,),
    ).fetchall()
    tag_sentiment = {}
    for r in tag_sent_rows:
        tag_sentiment.setdefault(r["tag"], {})[r["sentiment"]] = r["n"]

    # 主要媒體來源 Top 12
    source_rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? AND source != '' GROUP BY source ORDER BY n DESC LIMIT 12",
        (start,),
    ).fetchall()
    sources = [{"source": r["source"], "count": r["n"]} for r in source_rows]

    # 最新文章（近 200 則）
    art_rows = conn.execute(
        "SELECT tag, title, link, source, published, pub_date, sentiment FROM articles "
        "WHERE pub_date >= ? ORDER BY published DESC LIMIT 200",
        (start,),
    ).fetchall()
    articles = [dict(r) for r in art_rows]

    # KPI
    today_str = today.strftime("%Y-%m-%d")
    week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_week = (today - timedelta(days=14)).strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE pub_date = ?", (today_str,)
    ).fetchone()[0]
    this_week = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE pub_date > ?", (week_ago,)
    ).fetchone()[0]
    last_week = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE pub_date > ? AND pub_date <= ?",
        (prev_week, week_ago),
    ).fetchone()[0]
    last_fetch = conn.execute(
        "SELECT run_at, new_count, note FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    return {
        "generated_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "days": dates,
        "series": series,
        "totals": sorted(totals, key=lambda x: -x["total"]),
        "sentiment": sentiment,
        "tag_sentiment": tag_sentiment,
        "sources": sources,
        "articles": articles,
        "kpi": {
            "total_30d": sum(t["total"] for t in totals),
            "today": today_count,
            "this_week": this_week,
            "last_week": last_week,
            "top_tag": max(totals, key=lambda x: x["total"])["tag"] if totals else "-",
        },
        "last_fetch": dict(last_fetch) if last_fetch else None,
    }


def build_pr_summary() -> dict:
    """新聞稿擴散追蹤：每則新聞稿 D0~D+3 的報導數與媒體數。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    prs = conn.execute(
        "SELECT id, title, url, category, release_date FROM press_releases "
        "ORDER BY release_date DESC LIMIT 30"
    ).fetchall()
    result = []
    for pr in prs:
        arts = conn.execute(
            "SELECT title, link, source, pub_date, day_offset, "
            "resolved_url, outlet, reporter FROM pr_articles "
            "WHERE pr_id = ? ORDER BY day_offset, outlet, source",
            (pr["id"],),
        ).fetchall()
        counts = [0, 0, 0, 0]
        media = set()
        reporters = []
        for a in arts:
            if 0 <= a["day_offset"] <= 3:
                counts[a["day_offset"]] += 1
            m = a["outlet"] or a["source"]
            if m:
                media.add(m)
            if a["reporter"]:
                for name in a["reporter"].split("、"):
                    if name not in reporters:
                        reporters.append(name)
        result.append({
            "id": pr["id"],
            "title": pr["title"],
            "url": pr["url"],
            "category": pr["category"],
            "release_date": pr["release_date"],
            "counts": counts,
            "total": len(arts),
            "media": len(media),
            "reporters": reporters,
            "articles": [dict(a) for a in arts],
        })
    conn.close()
    return {
        "generated_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "press_releases": result,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/summary"):
            try:
                self._send_json(build_summary())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/pr"):
            try:
                self._send_json(build_pr_summary())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/refresh":
            try:
                proc = subprocess.run(
                    [sys.executable, str(BASE_DIR / "fetch_news.py")],
                    capture_output=True, text=True, timeout=600,
                )
                self._send_json({
                    "ok": proc.returncode == 0,
                    "output": (proc.stdout + proc.stderr).strip(),
                })
            except Exception as e:
                self._send_json({"ok": False, "output": str(e)}, 500)
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):
        pass  # 安靜模式


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"輿情觀測器已啟動：http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
