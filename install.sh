#!/bin/bash
# 安裝 / 更新輿情觀測器到 ~/ida-monitor 並設定 launchd 自動排程（macOS）。
#
# 在任何一台 Mac 上執行：bash install.sh
# － 路徑自動偵測（$HOME），排程設定檔由本腳本動態產生，換電腦不用改任何東西
# － macOS 的隱私保護不允許背景程序讀「文件」資料夾，所以執行版放在 ~/ida-monitor
# － 改了程式或 tags.json 之後，重新執行 bash install.sh 即可部署
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/ida-monitor"
UID_NUM=$(id -u)
PYTHON=/usr/bin/python3   # macOS 內建（需安裝 Xcode Command Line Tools）

if ! "$PYTHON" --version >/dev/null 2>&1; then
  echo "找不到 python3，請先執行：xcode-select --install" >&2
  exit 1
fi

mkdir -p "$DEST/logs"
cp "$SRC"/{fetch_news.py,fetch_pr.py,fetch_alerts.py,enrich.py,server.py,export_static.py,index.html,tags.json,alert_rules.json} "$DEST/"
# 資料庫只在目的地不存在時才複製，避免覆蓋累積的歷史資料
[ -f "$DEST/data.db" ] || { [ -f "$SRC/data.db" ] && cp "$SRC/data.db" "$DEST/"; }

# 動態產生 launchd 設定：儀表板伺服器常駐（資料更新用網頁上的「立即更新」按鈕手動觸發）
mkdir -p ~/Library/LaunchAgents

cat > ~/Library/LaunchAgents/com.ida.monitor.server.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ida.monitor.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DEST}/server.py</string>
        <string>8765</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DEST}/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>${DEST}/logs/server.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$UID_NUM/com.ida.monitor.server" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" ~/Library/LaunchAgents/com.ida.monitor.server.plist

# 全新安裝（沒有資料庫）時，立刻抓一次資料
if [ ! -f "$DEST/data.db" ]; then
  echo "首次安裝，立即抓取資料（約 2～3 分鐘）…"
  "$PYTHON" "$DEST/fetch_news.py" || true
fi

sleep 2
echo "--- launchd 狀態（第二欄 0 = 正常）---"
launchctl list | grep com.ida.monitor
echo "--- 儀表板 ---"
curl -s -o /dev/null -w "http://127.0.0.1:8765 → HTTP %{http_code}\n" http://127.0.0.1:8765/
