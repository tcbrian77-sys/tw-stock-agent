# 台股自動監控 Agent

## 專案目標

打造一套台股自動監控系統，包含四隻腳本，每日自動排程執行，訊號觸發時發 Telegram 通知。

## 四隻腳本說明

| 腳本 | 執行時間 | 功能 |
|------|----------|------|
| `stock_cache.py` | 收盤後 18:00 | 抓全市場日收盤資料，存 CSV |
| `broker_cache.py` | 收盤後 22:00 | 抓三大法人＋融資融券，存 CSV |
| `scan.py` | 盤前 08:00 | 讀取 CSV，跑選股策略，產生候選清單 |
| `monitor.py` | 盤中 09:00-13:30（每 10 分鐘）| 盤中即時監控，條件觸發發 Telegram |

## 資料來源

- **FinMind API**：台股日成交資料（dataset: TaiwanStockPrice）
- **yfinance**：美股指數（^DJI, ^IXIC, ^SOX）、台股大盤（^TWII）、台幣匯率（TWD=X）
- 免費版限制：每小時約 600 次請求
- 全市場一天資料 = 一次 API 呼叫即可取得

## 資料夾結構

```
tw-stock-agent/
├── data/
│   ├── prices/                          ← 每日收盤 CSV（格式：prices_YYYY-MM-DD.csv）
│   ├── brokers/                         ← 籌碼 CSV
│   └── fundamental_filter_YYYYMM.csv   ← 月營收歷史快取（同月只抓一次）
├── logs/                ← 執行紀錄
├── watchlist_conditions.json  ← scan.py 產出，monitor.py 讀取
├── morning_watchlist_YYYYMMDD.txt ← 每日盤前選股報告（純文字）
├── .env                 ← API token（機密，不上 git）
├── .gitignore
├── CLAUDE.md            ← 本檔案
├── stock_cache.py
├── broker_cache.py
├── scan.py
└── monitor.py
```

## 環境設定

- Python 3.x
- 需要套件：`pandas`, `requests`, `python-dotenv`
- API token 存放在 `.env` 檔，格式：`FINMIND_TOKEN=你的token`

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
- [x] 第六階段：crontab 排程自動化（完成，2026-06 重新設定）
  - [x] 四支腳本全部排程，週一到週五自動執行
  - [x] 各腳本 log 輸出至 logs/ 資料夾
  - [x] pmset 設定 07:55 自動喚醒（`sudo pmset repeat wakeorpoweron MTWRF 07:55:00`）
  - [x] 排程工具：Python subprocess 寫入（set_crontab.py），避免 heredoc 問題
- [x] 第七階段：morning_brief.py 每日早報（2026-06 完成）
  - [x] 美股三大指數昨收（^DJI, ^IXIC, ^SOX）via yfinance
  - [x] 台幣匯率（TWD=X）via yfinance
  - [x] 台股大盤點位（^TWII）via yfinance；成交量從本地 prices CSV 一般股加總
  - [x] 三大法人昨日買賣超（從本地 brokers CSV 讀取，單位億元）
  - [x] scan.py 加入 Telegram 發送（--dry 跳過）
  - [x] crontab 更新：morning_brief 08:05，scan.py 改為 08:20

| 時間 | 腳本 | Log |
|------|------|-----|
| 08:05 | morning_brief.py | logs/morning_brief.log |
| 08:20 | scan.py | logs/scan.log |
| */10 09–13 | monitor.py | logs/monitor.log |
| 14:30 | stock_cache.py | logs/stock_cache.log |
| 22:00 | broker_cache.py | logs/broker_cache.log |

🎉 全部完成！系統每個交易日自動運行。

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
