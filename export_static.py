#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把儀表板資料匯出成靜態檔案，供 cPanel 等無法常駐程式的主機使用。

用法：python3 export_static.py [輸出目錄]
輸出：index.html、summary.json、pr.json
（index.html 找不到 /api/summary 時會自動改讀這兩個 JSON）

cPanel 部署：把本專案上傳到家目錄，再設 Cron Job（一天 4 次）：
  python3 ~/ida-monitor/fetch_news.py && \
  python3 ~/ida-monitor/export_static.py ~/public_html/yuqing
"""
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from server import build_summary, build_pr_summary, build_alerts  # noqa: E402


def main() -> int:
    dest = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else BASE_DIR / "public"
    dest.mkdir(parents=True, exist_ok=True)

    (dest / "summary.json").write_text(
        json.dumps(build_summary(), ensure_ascii=False), encoding="utf-8")
    (dest / "pr.json").write_text(
        json.dumps(build_pr_summary(), ensure_ascii=False), encoding="utf-8")
    (dest / "alerts.json").write_text(
        json.dumps(build_alerts(), ensure_ascii=False), encoding="utf-8")
    shutil.copy(BASE_DIR / "index.html", dest / "index.html")

    print(f"已匯出靜態儀表板 → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
