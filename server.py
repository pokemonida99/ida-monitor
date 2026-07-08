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
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fetch_pr import _bigrams

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
TAGS_PATH = BASE_DIR / "tags.json"
INDEX_PATH = BASE_DIR / "index.html"

TAIPEI_TZ = timezone(timedelta(hours=8))
DAYS = 30


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def build_summary(start=None, end=None) -> dict:
    """start/end 為 YYYY-MM-DD 字串；未指定則預設近 30 天。"""
    tags_cfg = json.loads(TAGS_PATH.read_text(encoding="utf-8"))["tags"]

    today = datetime.now(TAIPEI_TZ).date()
    end_d = _parse_date(end) or today
    start_d = _parse_date(start) or end_d - timedelta(days=DAYS - 1)
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    if (end_d - start_d).days > 366:  # 上限一年，避免回應過大
        start_d = end_d - timedelta(days=366)
    n = (end_d - start_d).days + 1
    dates = [(start_d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    start = dates[0]
    end_s = dates[-1]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 每日每標籤聲量
    rows = conn.execute(
        "SELECT tag, pub_date, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? AND pub_date <= ? GROUP BY tag, pub_date",
        (start, end_s),
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
        "SELECT sentiment, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? AND pub_date <= ? GROUP BY sentiment",
        (start, end_s),
    ).fetchall()
    sentiment = {r["sentiment"]: r["n"] for r in sent_rows}

    # 各標籤情緒
    tag_sent_rows = conn.execute(
        "SELECT tag, sentiment, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? AND pub_date <= ? GROUP BY tag, sentiment",
        (start, end_s),
    ).fetchall()
    tag_sentiment = {}
    for r in tag_sent_rows:
        tag_sentiment.setdefault(r["tag"], {})[r["sentiment"]] = r["n"]

    # 主要媒體來源 Top 12
    source_rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM articles "
        "WHERE pub_date >= ? AND pub_date <= ? AND source != '' "
        "GROUP BY source ORDER BY n DESC LIMIT 12",
        (start, end_s),
    ).fetchall()
    sources = [{"source": r["source"], "count": r["n"]} for r in source_rows]

    # 最新文章（近 200 則）
    art_rows = conn.execute(
        "SELECT tag, title, link, source, published, pub_date, sentiment FROM articles "
        "WHERE pub_date >= ? AND pub_date <= ? ORDER BY published DESC LIMIT 200",
        (start, end_s),
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


def build_alerts() -> dict:
    """負面警訊：近 14 天需注意的新聞，提醒撰寫輿情。

    每則附上重要度（標題含產發署直接相關詞 = 2，一般產業 = 1）
    與事件群組編號（標題高度相似的報導聚合成同一事件，方便整批處理）。
    """
    cfg = json.loads((BASE_DIR / "alert_rules.json").read_text(encoding="utf-8"))
    rules = cfg["categories"]
    priority_words = cfg.get("priority_words", [])
    start = (datetime.now(TAIPEI_TZ).date() - timedelta(days=14)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, category, title, link, source, pub_date, handled "
        "FROM alert_articles WHERE pub_date >= ? ORDER BY pub_date DESC, id DESC LIMIT 1000",
        (start,),
    ).fetchall()
    conn.close()

    articles = []
    clusters = []  # [{"category", "bg", "group"}]
    for r in rows:
        a = dict(r)
        a["importance"] = 2 if any(w in a["title"] for w in priority_words) else 1
        bg = _bigrams(a["title"])
        group = None
        for cl in clusters:
            if cl["category"] != a["category"]:
                continue
            base = min(len(bg), len(cl["bg"])) or 1
            if len(bg & cl["bg"]) / base >= 0.55:
                group = cl["group"]
                break
        if group is None:
            group = len(clusters)
            clusters.append({"category": a["category"], "bg": bg, "group": group})
        a["group"] = group
        articles.append(a)

    return {
        "generated_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "categories": [{"category": r["category"], "color": r["color"]} for r in rules],
        "articles": articles,
    }


# 背景更新狀態（單一程序內共享）
REFRESH = {
    "running": False, "stage": "", "ok": None,
    "started_at": None, "finished_at": None, "output": "",
}
REFRESH_LOCK = threading.Lock()


def _run_refresh():
    stages = [
        ("聲量資料（約 1～2 分鐘）", "fetch_news.py", ["--only"]),
        ("新聞稿擴散（約 1 分鐘）", "fetch_pr.py", []),
        ("負面警訊（約 30 秒）", "fetch_alerts.py", []),
    ]
    outputs = []
    ok = True
    for i, (name, script, args) in enumerate(stages, 1):
        REFRESH["stage"] = f"[{i}/3] {name}"
        try:
            proc = subprocess.run(
                [sys.executable, str(BASE_DIR / script), *args],
                capture_output=True, text=True, timeout=600,
            )
            outputs.append(proc.stdout.strip())
            if proc.returncode != 0:
                outputs.append(proc.stderr.strip())
                ok = False
        except Exception as e:
            outputs.append(f"{name} 失敗：{e}")
            ok = False
    REFRESH.update(
        running=False, ok=ok, stage="完成" if ok else "部分失敗",
        finished_at=datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        output="\n".join(o for o in outputs if o),
    )


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
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._send_json(build_summary(
                    (q.get("start") or [None])[0],
                    (q.get("end") or [None])[0],
                ))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/pr"):
            try:
                self._send_json(build_pr_summary())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/alerts"):
            try:
                self._send_json(build_alerts())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/refresh_status"):
            self._send_json(REFRESH)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/alert_done":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
                ids = data.get("ids") or [data["id"]]
                handled = 1 if data.get("handled") else 0
                handled_at = (datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
                              if handled else None)
                conn = sqlite3.connect(DB_PATH)
                conn.executemany(
                    "UPDATE alert_articles SET handled = ?, handled_at = ? WHERE id = ?",
                    [(handled, handled_at, int(i)) for i in ids],
                )
                conn.commit()
                conn.close()
                self._send_json({"ok": True, "count": len(ids)})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/refresh":
            with REFRESH_LOCK:
                if REFRESH["running"]:
                    self._send_json({"started": False, "running": True})
                    return
                REFRESH.update(
                    running=True, ok=None, stage="準備中…", output="",
                    started_at=datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
                    finished_at=None,
                )
                threading.Thread(target=_run_refresh, daemon=True).start()
            self._send_json({"started": True})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):
        pass  # 安靜模式


def main():
    args = [a for a in sys.argv[1:] if a != "--lan"]
    port = int(args[0]) if args else 8765
    # --lan：開放同一內網的同仁連線（例如 http://你的內網IP:8765）
    host = "0.0.0.0" if "--lan" in sys.argv else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    scope = "內網共用" if host == "0.0.0.0" else "僅本機"
    print(f"輿情觀測器已啟動（{scope}）：http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
