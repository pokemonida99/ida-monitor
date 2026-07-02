# 產業發展署輿情觀測器

監測經濟部產業發展署相關議題的新聞聲量，提供 30 天趨勢儀表板，並每天自動更新 4 次。

## 儀表板

開瀏覽器進入 **http://127.0.0.1:8765**（伺服器由 launchd 常駐，開機自動啟動）。

功能（兩個頁籤）：

**📊 聲量總覽**
- 近 30 天總聲量、今日則數、週對週變化、最熱標籤（KPI 卡）
- 30 天每日聲量趨勢線圖（點圖例可切換標籤）
- 標籤聲量排行、情緒分布（正面/中立/負面）、主要媒體來源
- 最新新聞列表，可依標籤、情緒、關鍵字篩選
- 「立即更新資料」按鈕可手動抓取（約 1～2 分鐘）

**📤 新聞稿擴散**
- 自動爬取產發署官網新聞稿（`fetch_pr.py`）
- 每則新聞稿發布日（D0）到第三天（D+3）的媒體報導數統計
- 追蹤「多少家媒體、多少記者發出新聞」：總報導數＋不同媒體家數
- 點新聞稿列可展開報導明細（哪家媒體、哪天、什麼標題）
- 判斷方式：以新聞稿標題關鍵詞搜 Google News，再用引號詞組／關鍵字／
  雙字組相似度過濾不相關報導；發布後 10 天內每次排程持續回補

## 觀測標籤（tags.json）

依經濟部與產業發展署重點政策設計，共 9 個標籤：

| 標籤 | 政策依據 |
|---|---|
| 產業發展署 | 機關本身的新聞露出 |
| 無人機產業 | 無人載具產業發展統籌型計畫（114–119 年，目標產值 400 億） |
| 智慧製造 | 製造業 AI 應用開發及擴散計畫、產業升級創新平台 |
| 半導體 | 五大信賴產業之一，2028 年增加 2.6 兆產值目標 |
| AI產業 | 五大信賴產業之一，2026 年數位經濟兆元目標 |
| 淨零轉型 | 淨零科技方案（2023–2026）、碳費制度 |
| 五大信賴產業 | 賴政府核心產業政策（半導體、AI、軍工、安控、次世代通訊） |
| 電動車產業 | 車輛產業電動化轉型 |
| 關稅與貿易 | 美國關稅對製造業衝擊（高度輿情敏感議題） |

改標籤：編輯 `tags.json`（每個標籤可設多個搜尋關鍵字與顏色），存檔後執行 `bash install.sh` 重新部署。

## 架構

- 資料源：Google News RSS 搜尋（繁中/台灣，近 30 天），免費、免 API key
- `fetch_news.py`：抓取 → 標題情緒判斷（簡易詞典）→ 去重存入 SQLite（`data.db`）
- `server.py`：純 Python 標準庫的網頁伺服器（無需安裝套件），提供儀表板與 JSON API
- `index.html`：Chart.js 儀表板

## 自動排程（launchd）

- `com.ida.monitor.fetch`：每天 **08:00、12:00、16:00、20:00** 自動抓新資料
- `com.ida.monitor.server`：儀表板伺服器開機常駐（port 8765）

⚠️ 因 macOS 隱私保護不允許背景程序讀「文件」資料夾，**執行版安裝在 `~/ida-monitor`**，
這個資料夾只是原始碼。改程式後跑 `bash install.sh` 即可重新部署（歷史資料庫不會被覆蓋）。

常用指令：

```bash
bash install.sh                                        # 部署 + 重新載入排程
launchctl kickstart gui/$(id -u)/com.ida.monitor.fetch # 立刻手動抓一次
tail ~/ida-monitor/logs/fetch.log                      # 看抓取記錄
launchctl list | grep com.ida.monitor                  # 檢查排程狀態
# 移除排程：
launchctl bootout gui/$(id -u)/com.ida.monitor.fetch
launchctl bootout gui/$(id -u)/com.ida.monitor.server
```

改排程時間：編輯 `install.sh` 裡 `StartCalendarInterval` 區塊，再跑 `bash install.sh`
（排程設定檔由 install.sh 動態產生，路徑自動偵測，換電腦不用改）。

## 搬到另一台電腦（Mac）

1. 把整個 `ida-monitor` 資料夾複製過去（AirDrop／隨身碟／雲端都可以）。
   要保留歷史資料就連 `data.db` 一起帶；不帶的話會自動重抓最近 30 天。
2. 在新電腦的終端機執行：

   ```bash
   cd ida-monitor
   bash install.sh
   ```

   就這樣。腳本會自動安裝到新電腦的 `~/ida-monitor`、設好排程、啟動儀表板
   （首次安裝且沒帶 data.db 時會立刻抓一次資料）。
3. 開瀏覽器進 http://127.0.0.1:8765。

需要 macOS 內建的 python3（沒有的話跑 `xcode-select --install`）。
Windows／Linux 沒有 launchd：程式本身（python3 標準庫）可以直接跑，
排程改用 Linux 的 cron 或 Windows 的工作排程器呼叫 `fetch_news.py` 即可。
