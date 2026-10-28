# 台股自動監控 Agent

> [!info] 2026-10-28 更新：系統已完全遷移到雲端自動化，且新增 YouTuber 監控功能。
> 完整架構、cron-job.org job 清單、機密復原方式，見 Obsidian 筆記「台股監控 Agent (tw-stock-agent)」。本檔案保留原始設計細節（scan.py 三層過濾邏輯等）並更新成目前實際狀態。

## 專案目標

兩個功能，共用同一套雲端自動化基礎設施，訊號觸發時發 Telegram 到同一個共用群組（含老婆）：
1. **台股盤中監控**：全市場三大法人買賣超、技術面訊號
2. **YouTuber 新片監控**：`youtube_watch.py`，監看 3 個台股 YouTuber，新片自動轉錄＋ Claude 摘要

## 雲端架構（不依賴本機電腦）

```
cron-job.org（外部排程）→ GitHub Actions workflow_dispatch（雲端 runner）
→ 執行 .py 腳本 → 發 Telegram → commit 資料/狀態檔回 repo
```

**本機 crontab 已於 2026-10-27 清空**（遷移前的舊排程，曾跟雲端版本並行造成重複發送風險；備份在 `legacy_local_automation_backup/`，已 gitignore）。GitHub Actions 全部用 `workflow_dispatch`，**沒有用原生 `schedule:`**（實測原生排程常漏跳/延遲數小時）。

## 腳本說明

| 腳本 | 執行時間（台北） | 功能 |
|------|----------|------|
| `morning_brief.py` | 08:05 | 每日早報 |
| `scan.py` | 08:20 | 讀取 CSV，跑選股策略，產生候選清單 |
| `monitor.py` | 09:00-14:00，每 15 分 | 盤中監控：候選股訊號 ＋ 三大法人買賣超總覽 |
| `stock_cache.py` | 14:30 | 抓全市場日收盤資料，存 CSV |
| `evening_scan.py` | 20:00 | 晚間選股 |
| `broker_cache.py` | 22:00 | 抓三大法人＋融資融券，存 CSV |
| `youtube_watch.py` | 全天每 20 分 | YouTuber 新片監控（見下方獨立章節） |

## 資料來源

- **TWSE／TPEx 官方免費 Open Data**（`twse_source.py`）：主要資料源。FinMind 付費訂閱已過期，改用交易所官方開放資料，支援任意歷史日期查詢（TWSE: `MI_INDEX`，TPEx: `dailyQuotes`，皆用日期查詢端點，不用「最新」端點，避免延遲）
- **FinMind 逐檔查詢**（`finmind_fallback.py`）：TWSE+TPEx 都查不到時的備援，受限 500 次/小時，僅免費額度
- **yfinance**：美股指數（^DJI, ^IXIC, ^SOX）、台股大盤（^TWII）、台幣匯率（TWD=X）
- TWSE+TPEx 兩邊都查無資料 → 視為休市日（週末或臨時休市如颱風假），不觸發耗時的逐檔查詢

## 資料夾結構

```
tw-stock-agent/
├── data/
│   ├── prices/                          ← 每日收盤 CSV（格式：prices_YYYY-MM-DD.csv）
│   ├── brokers/                         ← 籌碼 CSV（institutional_/margin_YYYY-MM-DD.csv）
│   └── fundamental_filter_YYYYMM.csv   ← 月營收歷史快取（同月只抓一次）
├── logs/                ← 本機測試用，雲端 Actions log 不存這裡（已 gitignore）
├── legacy_local_automation_backup/  ← 舊版本機 crontab 備份（已 gitignore，不上 git）
├── .github/workflows/   ← 7 個 GitHub Actions workflow（每腳本一個）
├── watchlist_conditions.json  ← scan.py 產出，monitor.py 讀取
├── alerted.json          ← monitor.py 候選股逐檔通知去重
├── selling_alerted.json  ← monitor.py 三大法人總覽去重（存「資料日期」）
├── youtube_seen.json     ← youtube_watch.py 已處理影片 ID 記錄
├── morning_watchlist_YYYYMMDD.txt ← 每日盤前選股報告（純文字）
├── .env                 ← API token（機密，不上 git；見下方「機密」）
├── .gitignore
├── CLAUDE.md            ← 本檔案
├── notify.py            ← Telegram 發送共用模組
├── twse_source.py       ← TWSE/TPEx 官方資料來源
├── finmind_fallback.py  ← FinMind 逐檔備援
├── stock_cache.py
├── broker_cache.py
├── scan.py
├── evening_scan.py
├── morning_brief.py
├── monitor.py
└── youtube_watch.py
```

## 環境設定

- Python 3.x
- 套件：見 `requirements.txt`（`pandas`, `requests`, `python-dotenv`, `yfinance`, `youtube-transcript-api`, `yt-dlp`）
- `.env` 內容：`FINMIND_TOKEN`、`TELEGRAM_TOKEN`、`TELEGRAM_CHAT_ID`、`ANTHROPIC_API_KEY`、`GROQ_API_KEY`、`OPENAI_API_KEY`
- 同樣的值也存在 GitHub repo Secrets（雲端用）。**GitHub Secrets 存進去後無法讀回值**，`.env` 遺失時本機要重新產生金鑰，不能指望從 GitHub 拿回來

## 開發進度

- [x] 第一階段：資料管線（完成）
  - [x] 專案結構建立
  - [x] .env 安全存放 API token（FinMind 付費帳號）
  - [x] stock_cache.py：一次 API 呼叫抓全市場日收盤資料，存 CSV
  - [x] 驗證：2025-06-06 共 2,254 檔一般股票，台積電收盤價 995 元正確
- [x] 第二階段：Telegram 通知（完成）
  - [x] 建立 Telegram Bot，取得 Token 與 Chat ID
  - [x] notify.py：send() 函式，支援 HTML 格式
  - [x] 驗證：測試訊息成功送達
- [x] 第三階段：broker_cache.py 籌碼資料（完成）
  - [x] 三大法人買賣超（TaiwanStockInstitutionalInvestorsBuySell）
  - [x] 融資融券（TaiwanStockMarginPurchaseShortSale）
- [x] 第四階段：scan.py 選股策略（完成，已整合三層過濾）
  - [x] fetch_history.py：一次性補抓 62 個交易日歷史資料
  - [x] 三層過濾架構（2026-06 整合）
  - [x] 輸出 watchlist_conditions.json（含基本面欄位）與 morning_watchlist_YYYYMMDD.txt
- [x] 第五階段：monitor.py 盤中監控（完成）
  - [x] 讀取 watchlist_conditions.json（2026-06 改名）
  - [x] 觸發條件：今日漲幅 ≥ 2%
  - [x] alerted.json 避免重複通知
- [x] 第五階段 3B：monitor.py 升級（2026-06 完成）
  - [x] 監控上限移除（原 20 檔上限，現全部監控）
  - [x] 策略差異化觸發門檻：布林突破 ≥ 2%，超賣反彈 ≥ 1.5%
  - [x] 股票名稱查詢（TaiwanStockInfo，快取於 data/stock_info.csv）
  - [x] Telegram 訊息格式升級（含股票名稱、漲幅說明、YoY）
  - [x] 即時價格改用 TaiwanStockPriceTick（付費帳號，盤中 tick 資料）
    - 每檔獨立查詢（data_id=stock_id），取最新一筆 deal_price 為現價
    - TaiwanStockPrice 僅有收盤資料，盤中無法使用
  - [x] Telegram 通知加入出場條件（2026-06 完成）
    - 停損/停利價格以觸發當下現價計算
    - 近5日均量從本地 data/prices/ CSV 讀取，不打 API
    - 策略A（布林突破）：停損 -5%、停利①+8% 減半、停利②跌破5日均線全出、強制出場5日
    - 策略B（超賣反彈）：停損 -4%、停利 +6% 全出、強制出場3日
    - 訊息末行加「⚡ 以上為參考訊號，請自行判斷進出場」
- [x] 第六階段：crontab 排程自動化（**2026-06 設定，2026-10-27 已停用並清空**）
  - [x] 原本：四支腳本全部排程，週一到週五自動執行，本機 crontab + pmset 喚醒
  - [x] 2026-10-27 發現本機 crontab 遷移後仍在跑，跟雲端版本並行、各自獨立去重狀態檔，有重複發 Telegram 風險 → 清空本機 crontab（備份於 `legacy_local_automation_backup/`），改成純雲端排程（見「第九階段」）
- [x] 第七階段：morning_brief.py 每日早報（2026-06 完成）
  - [x] 美股三大指數昨收（^DJI, ^IXIC, ^SOX）via yfinance
  - [x] 台幣匯率（TWD=X）via yfinance
  - [x] 台股大盤點位（^TWII）via yfinance；成交量從本地 prices CSV 一般股加總
  - [x] 三大法人昨日買賣超（從本地 brokers CSV 讀取，單位億元）
  - [x] scan.py 加入 Telegram 發送（--dry 跳過）
  - [x] crontab 更新：morning_brief 08:05，scan.py 改為 08:20

- [x] 第八階段：evening_scan.py 晚間選股（2026-06 完成）
  - [x] 篩選池：昨日成交金額前50 ∩ 股數前50 的交集（一般股 4 位數字代碼）
  - [x] 四條件評分（各 25 分，資料缺失時等比換算為 100）：
    - 條件一：最近 3 期 YoY 加速（25=全程加速，15=部分加速，0=持平/減速）
    - 條件二：最新月營收 vs 歷史新高（≥100%=25，95-99%=15，90-94%=5，<90%=0）
    - 條件三：毛利率 vs 去年同季（↑>1pp=25，持平±1pp=10，↓=0）—— TaiwanStockFinancialStatements
    - 條件四：PE 本益比（<15=25，15-20=15，20-25=5，≥25或虧損=0）—— TaiwanStockPER
  - [x] 快取：月營收用現有 fundamental_filter_YYYYMM.csv；
          PER → data/stock_per_YYYYMM.csv；財報 → data/financial_statements_YYYYMM.csv
  - [x] 輸出：總分最高 5 檔，以排名 emoji 格式發送 Telegram
  - [x] crontab 新增 20:00 evening_scan.py（已於第九階段改為雲端排程）

- [x] 第九階段：遷移到雲端自動化（2026-10 完成）
  - [x] FinMind 付費訂閱過期 → 建立 `twse_source.py`（TWSE/TPEx 官方免費資料）取代，`finmind_fallback.py` 當逐檔備援
  - [x] 6 支腳本各自建立 GitHub Actions workflow（`workflow_dispatch`），不用原生 `schedule:`
  - [x] 用 cron-job.org 外部定時呼叫 GitHub API 觸發對應 workflow（job id 見 Obsidian 筆記）
  - [x] 每個 workflow 執行完把資料/狀態檔分別 `git add` 後 commit 回 repo（避免某檔案不存在時整批 `git add` 失敗）
  - [x] monitor.py 排程改為 09:00-14:00 每 15 分（原本 09:00-13:30 每 10 分）
  - [x] 2026-10-27：清空本機 crontab，確認系統 100% 雲端運行，筆電開關機不影響

- [x] 第十階段：三大法人買賣超總覽（2026-10 完成）
  - [x] monitor.py 全市場掃描三大法人合計買超/賣超，門檻 **NT$100,000,000**（原為 $8M 美金換算，已改成直接用台幣，不用再查匯率）
  - [x] 顯示 **Top 10** 買超／Top 10 賣超 + 5日/10日股價趨勢比較（原本 Top 5 + 完整名單分頁訊息，使用者反饋太長看不完，已取消完整名單，改單則訊息）
  - [x] 單位改用「張」（股數 ÷ 1000），比原始股數好讀
  - [x] 去重用「資料日期」（`selling_alerted.json`），同一天收盤資料只發一次總覽

- [x] 第十一階段：YouTuber 新片監控（2026-10 完成，`youtube_watch.py`）
  - [x] 監看 3 個頻道：老王愛說笑、一綺聊股(HAMILIU)、理財達人秀(EBCmoneyshow)
  - [x] RSS feed 偵測新片（`youtube_seen.json` 記錄已處理 ID，首次執行只記錄不發送避免洗版）
  - [x] 逐字稿：先試原生字幕（`youtube-transcript-api`）→ 沒字幕才用 yt-dlp 下載音訊 + Whisper（優先 Groq `whisper-large-v3`，OpenAI `whisper-1` 當備援但目前沒額度）
  - [x] 直播預告/進行中自動跳過，等結束再處理
  - [x] Claude（`claude-sonnet-5`）產生中文摘要／分析／建議，發送 Telegram
  - [x] cron-job.org 排程：全天每 20 分鐘觸發一次

🎉 系統目前完全雲端自動運行，不依賴本機電腦或任何終端機。

## scan.py 三層過濾架構（2026-06 版）

### 第一層：月營收基本面（FinMind TaiwanStockMonthRevenue）

- **快取**：`data/fundamental_filter_YYYYMM.csv`，同月只抓一次
- **API 行為注意**：不帶 stock_id 時，每次呼叫只回傳一個月截面，必須逐月呼叫
  - 起始：`REVENUE_START = "2019-01-01"`（約 90 個月，初次下載 3-4 分鐘）
  - `date` 欄位 = 公告月份第一天（例如 12 月營收 → date = 隔年 1/1）
- **篩選條件**（同時成立才通過）：
  - 條件 a：最新月營收 > 歷史所有月份最高值
  - 條件 b：最新 YoY > 過去 12 期任何一期 YoY（YoY 自算，不用 API 欄位）
- 歷史資料 < 24 個月的股票跳過

### 第二層：成交量前 50

- 從第一層通過的股票中，取最新交易日**成交金額**（Trading_money）前 50 大
- 直接讀本地 `data/prices/` CSV，不打 API
- 改用金額排名（非股數），使大型股如台積電可進入候選池

### 第三層：技術訊號

**策略 A：布林突破**
- 收盤 > 布林上軌（20日）
- 量比 > 1.3x（今日量 / 近 5 日均量）
- RSI 50–75

**策略 B：超賣反彈**
- RSI < 30
- 量比 > 0.6x
- 最近 3 日有止跌訊號（收紅 K 或下影線 > 實體 1.5 倍）

### 輸出

- `watchlist_conditions.json`：每檔含 revenue、yoy、revenue_all_time_high 欄位
- `morning_watchlist_YYYYMMDD.txt`：三層統計摘要 + 候選清單

## 參考資料

- 架構參考：https://homuchen.com/posts/claude-code-stock-agent-monitor-alert/
- FinMind 文件：https://finmind.github.io/
- 完整系統交接筆記（cron-job.org job 清單、機密復原步驟、無關的其他 launchctl agent 說明）：Obsidian `Projects/台股監控 Agent (tw-stock-agent).md`
