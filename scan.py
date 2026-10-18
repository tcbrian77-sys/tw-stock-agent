"""
scan.py
用途：盤前選股，三層過濾架構（基本面 → 量能 → 技術訊號）
用法：
  python3 scan.py       ← 用最新資料選股，結果發送 Telegram
  python3 scan.py --dry ← 只印結果，不發 Telegram
"""

import os
import sys
import json
import glob
import requests
import pandas as pd
from datetime import date
from dotenv import load_dotenv
from notify import send
from finmind_fallback import is_level_error, load_stock_universe, fetch_dataset_per_stock

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

BASE_DIR     = os.path.dirname(__file__)
PRICES_DIR   = os.path.join(BASE_DIR, "data", "prices")
DATA_DIR     = os.path.join(BASE_DIR, "data")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist_conditions.json")
API_URL      = "https://api.finmindtrade.com/api/v4/data"

MIN_DAYS          = 20    # 技術指標最少需要幾個交易日
HISTORY_DAYS      = 60    # 讀取最近幾個交易日 CSV
REVENUE_START     = "2019-01-01"  # 7 年歷史，足以確認「歷史新高」且初次下載約 90 次 API 呼叫
MIN_REVENUE_MONTHS = 24   # 歷史資料不足 24 個月直接跳過


# ═══════════════════════════════════════════════════════════════
# 第一層：月營收基本面篩選
# ═══════════════════════════════════════════════════════════════

def _fundamental_cache_path() -> str:
    return os.path.join(DATA_DIR, f"fundamental_filter_{date.today().strftime('%Y%m')}.csv")


def _revenue_month_dates() -> list:
    """從 REVENUE_START 到本月，產生每個月份的公告日（每月 1 日）清單。
    FinMind 月營收的 date 欄位 = 公告月份的第一天（例如 12 月營收 → date 2026-01-01）。
    """
    parts = [int(x) for x in REVENUE_START.split("-")]
    cur = date(parts[0], parts[1], 1)
    end = date.today().replace(day=1)
    result = []
    while cur <= end:
        result.append(cur.strftime("%Y-%m-%d"))
        m = cur.month + 1
        y = cur.year + (1 if m > 12 else 0)
        m = m if m <= 12 else 1
        cur = date(y, m, 1)
    return result


def _fetch_revenue_from_api() -> pd.DataFrame:
    """逐月向 FinMind 抓全市場月營收。
    API 在不帶 stock_id 時只回傳指定 start_date 當月截面，
    因此必須逐月呼叫（每次約 2,000 筆，一次呼叫 2-3 秒）。
    帳號若已降級（免費/register 等級），全市場批次查詢會被拒絕，
    此時改為逐檔查詢：單檔查詢只帶 start_date（不帶 end_date）會一次回傳
    該股從 start_date 到最新的「全部」月份，因此只需每檔查詢一次。
    """
    months = _revenue_month_dates()
    print(f"📡 逐月下載月營收（{REVENUE_START} 起，共 {len(months)} 個月）...")
    all_dfs = []

    for i, m_date in enumerate(months):
        print(f"\r  進度：{i+1}/{len(months)}  ({m_date})", end="", flush=True)
        try:
            r = requests.get(API_URL, params={
                "dataset": "TaiwanStockMonthRevenue",
                "start_date": m_date,
                "token": TOKEN,
            }, timeout=60)
            payload = r.json()
        except requests.RequestException as e:
            print(f"\n⚠️  {m_date} 請求失敗：{e}，跳過")
            continue

        if is_level_error(payload):
            print(f"\n⚠️  帳號等級不足，無法使用全市場批次查詢（第 {i+1} 個月即遭拒絕）。")
            print("   改為逐檔查詢完整歷史（每檔一次呼叫即可拿到全部月份，約需數小時）...")
            stock_ids = load_stock_universe()
            df = fetch_dataset_per_stock(
                "TaiwanStockMonthRevenue", TOKEN, stock_ids,
                start_date=REVENUE_START, label="月營收",
            )
            if df.empty:
                print("❌ 無法從 FinMind 取得任何月營收資料")
                sys.exit(1)
            unique_dates = df["date"].nunique() if "date" in df.columns else "?"
            print(f"✅ 下載完成：{len(df):,} 筆，{df['stock_id'].nunique():,} 檔，{unique_dates} 個月份")
            return df

        if r.status_code != 200 or payload.get("msg") != "success":
            # 未來月份可能回傳 empty，正常跳過
            continue

        rows = payload.get("data", [])
        if rows:
            all_dfs.append(pd.DataFrame(rows))

    print()  # 換行

    if not all_dfs:
        print("❌ 無法從 FinMind 取得任何月營收資料")
        sys.exit(1)

    df = pd.concat(all_dfs, ignore_index=True)
    unique_dates = df["date"].nunique() if "date" in df.columns else "?"
    print(f"✅ 下載完成：{len(df):,} 筆，{df['stock_id'].nunique():,} 檔，{unique_dates} 個月份")
    return df


def _load_revenue() -> pd.DataFrame:
    cache = _fundamental_cache_path()
    if os.path.exists(cache):
        print(f"📂 讀取月營收快取：{os.path.basename(cache)}")
        df = pd.read_csv(cache, dtype={"stock_id": str})
        print(f"   {len(df):,} 筆，{df['stock_id'].nunique():,} 檔股票")
        return df

    df = _fetch_revenue_from_api()
    os.makedirs(DATA_DIR, exist_ok=True)
    df.to_csv(cache, index=False)
    print(f"💾 已快取至 {os.path.basename(cache)}")
    return df


def filter_fundamental(revenue_df: pd.DataFrame) -> tuple:
    """
    條件 a：最新一期月營收 > 過去所有月份最高值（歷史新高）
    條件 b：最新一期 YoY > 過去 12 期任何一期的 YoY（YoY 創近期新高）
    YoY = (最新期營收 / 去年同月營收) - 1，自行計算，不依賴 API 欄位
    回傳 (通過的 stock_id list, {stock_id: metadata dict})
    """
    # 確認必要欄位存在
    required = {"stock_id", "revenue", "revenue_year", "revenue_month"}
    missing = required - set(revenue_df.columns)
    if missing:
        print(f"❌ 月營收資料缺少欄位：{missing}，現有欄位：{list(revenue_df.columns)}")
        sys.exit(1)

    df = revenue_df.copy()
    df["stock_id"] = df["stock_id"].astype(str)
    df = df[df["stock_id"].str.match(r"^\d{4}$")].copy()
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df["revenue_year"]  = pd.to_numeric(df["revenue_year"],  errors="coerce")
    df["revenue_month"] = pd.to_numeric(df["revenue_month"], errors="coerce")
    df = df.dropna(subset=["revenue", "revenue_year", "revenue_month"])

    # 建立 YYYYMM 期別整數（方便排序與查找去年同月）
    df["period"] = (df["revenue_year"] * 100 + df["revenue_month"]).astype(int)

    passed  = []
    metadata = {}

    for stock_id, g in df.groupby("stock_id", sort=False):
        g = g.sort_values("period").drop_duplicates("period").reset_index(drop=True)

        if len(g) < MIN_REVENUE_MONTHS:
            continue

        latest      = g.iloc[-1]
        lat_period  = int(latest["period"])
        lat_rev     = float(latest["revenue"])
        lat_year    = lat_period // 100
        lat_month   = lat_period % 100

        # ── YoY 計算（最新期） ──
        prev_period = (lat_year - 1) * 100 + lat_month
        prev_rows   = g[g["period"] == prev_period]
        if prev_rows.empty:
            continue
        prev_rev = float(prev_rows.iloc[0]["revenue"])
        if prev_rev <= 0:
            continue
        latest_yoy = (lat_rev - prev_rev) / prev_rev

        # ── 條件 a：營收歷史新高 ──
        hist_max = float(g["revenue"].iloc[:-1].max())
        is_rev_high = lat_rev > hist_max

        # ── 條件 b：YoY 創近 12 期新高 ──
        recent_12 = g.iloc[-13:-1]  # 最新期之前的 12 期
        prior_yoys = []
        for _, row in recent_12.iterrows():
            yr = int(row["period"]) // 100
            mo = int(row["period"]) % 100
            comp_period = (yr - 1) * 100 + mo
            comp_rows = g[g["period"] == comp_period]
            if not comp_rows.empty:
                comp_rev = float(comp_rows.iloc[0]["revenue"])
                if comp_rev > 0:
                    prior_yoys.append((float(row["revenue"]) - comp_rev) / comp_rev)

        if not prior_yoys:
            continue
        is_yoy_high = latest_yoy > max(prior_yoys)

        if is_rev_high and is_yoy_high:
            passed.append(stock_id)
            metadata[stock_id] = {
                "latest_period":  lat_period,
                "revenue":        int(lat_rev),
                "yoy":            round(latest_yoy * 100, 2),  # 百分比
                "revenue_all_time_high": True,
                "yoy_12m_high":   True,
            }

    print(f"✅ 第一層（月營收雙新高）：{len(passed):,} 檔通過")
    return passed, metadata


# ═══════════════════════════════════════════════════════════════
# 第二層：成交量前 50
# ═══════════════════════════════════════════════════════════════

def filter_top_volume(candidate_ids: list, price_df: pd.DataFrame, top_n: int = 50) -> list:
    """從候選清單中取最新交易日成交量前 top_n 檔。"""
    latest_date = price_df["date"].max()
    today_prices = price_df[price_df["date"] == latest_date].copy()
    filtered = today_prices[today_prices["stock_id"].isin(candidate_ids)]
    top = filtered.nlargest(top_n, "Trading_money")["stock_id"].tolist()
    print(f"✅ 第二層（金額前 {top_n}）：{len(top)} 檔通過")
    return top


# ═══════════════════════════════════════════════════════════════
# 技術指標
# ═══════════════════════════════════════════════════════════════

def load_price_history() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(PRICES_DIR, "prices_*.csv")))
    if not files:
        print("❌ data/prices/ 沒有資料，請先執行 stock_cache.py 或 fetch_history.py")
        sys.exit(1)

    files = files[-HISTORY_DAYS:]
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df[df["stock_id"].astype(str).str.match(r"^\d{4}$")].copy()
    df["stock_id"] = df["stock_id"].astype(str)
    df["date"]     = pd.to_datetime(df["date"])
    df = df.sort_values(["stock_id", "date"])

    latest = df["date"].max().date()
    print(f"📂 載入價格資料：{df['stock_id'].nunique():,} 檔，"
          f"{df['date'].nunique()} 個交易日，最新 {latest}")
    return df


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def _calc_indicators(group: pd.DataFrame) -> pd.DataFrame:
    g = group.copy().reset_index(drop=True)

    # 個別交易日若無正常成交價（例如零股/鉅額交易，TWSE/TPEx 標示為
    # "--"），close 會是 NaN。單一缺值若不處理，會讓接下來約 20 個交易日
    # 的滾動指標（布林、RSI）整段變成 NaN。用前一日收盤價補值（視為當天
    # 價格持平），是金融時間序列常見的缺值處理方式。
    g[["open", "max", "min", "close"]] = g[["open", "max", "min", "close"]].ffill()

    g["sma20"]    = g["close"].rolling(20).mean()
    g["std20"]    = g["close"].rolling(20).std()
    g["bb_upper"] = g["sma20"] + 2 * g["std20"]

    g["vol_ma5"]  = g["Trading_Volume"].rolling(5).mean()
    g["vol_ratio"]= g["Trading_Volume"] / g["vol_ma5"]

    g["rsi"]      = _calc_rsi(g["close"])
    g["pct_change"] = g["close"].pct_change() * 100

    # 止跌訊號用欄位
    g["body"]         = abs(g["close"] - g["open"])
    g["lower_shadow"] = g[["open", "close"]].min(axis=1) - g["min"]

    return g


# ═══════════════════════════════════════════════════════════════
# 第三層：技術策略
# ═══════════════════════════════════════════════════════════════

def _strategy_bb_breakout(g: pd.DataFrame):
    """
    策略 A：布林突破
    - 收盤 > 布林上軌（20日）
    - 量比 > 1.5x
    - RSI 50–75（有動能，未過熱）
    """
    if len(g) < MIN_DAYS:
        return None
    t = g.iloc[-1]
    if (t["close"] > t["bb_upper"]
            and t["vol_ratio"] > 1.3
            and 50 <= t["rsi"] <= 75):
        return _signal(t, "布林突破")
    return None


def _strategy_oversold_bounce(g: pd.DataFrame):
    """
    策略 B：超賣反彈
    - RSI < 30（嚴格超賣）
    - 量比 > 0.8x
    - 最近 3 日有止跌訊號（收紅 K 或下影線 > 實體 1.5 倍）
    """
    if len(g) < MIN_DAYS:
        return None
    t = g.iloc[-1]
    if t["rsi"] >= 30 or t["vol_ratio"] <= 0.6:
        return None

    def _is_bottom(row) -> bool:
        is_green = row["close"] >= row["open"]
        long_shadow = (row["body"] > 0
                       and row["lower_shadow"] > row["body"] * 1.5)
        return is_green or long_shadow

    recent3 = g.iloc[-3:]
    if any(_is_bottom(recent3.iloc[i]) for i in range(len(recent3))):
        return _signal(t, "超賣反彈")
    return None


def _signal(row, strategy: str) -> dict:
    return {
        "stock_id":   row["stock_id"],
        "strategy":   strategy,
        "close":      float(row["close"]),
        "vol_ratio":  round(float(row["vol_ratio"]), 1),
        "rsi":        round(float(row["rsi"]), 1),
        "pct_change": round(float(row["pct_change"]), 2),
    }


# ═══════════════════════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("🔍 盤前選股啟動（三層過濾架構）")
    print("=" * 60)

    # ── 第一層：月營收基本面 ──
    print("\n【第一層】月營收基本面篩選")
    revenue_df = _load_revenue()
    fundamental_ids, fundamental_meta = filter_fundamental(revenue_df)

    if not fundamental_ids:
        print("❌ 第一層無任何股票通過，停止執行")
        sys.exit(0)

    # ── 載入價格資料 ──
    print("\n【載入價格資料】")
    price_df = load_price_history()
    today_str = price_df["date"].max().strftime("%Y-%m-%d")

    # ── 第二層：量能前 50 ──
    print("\n【第二層】成交量前 50 篩選")
    top50_ids = filter_top_volume(fundamental_ids, price_df)

    if not top50_ids:
        print("❌ 第二層無任何股票通過，停止執行")
        sys.exit(0)

    # ── 第三層：技術訊號 ──
    print("\n【第三層】技術訊號掃描")
    results   = []
    target_df = price_df[price_df["stock_id"].isin(top50_ids)]

    for stock_id, group in target_df.groupby("stock_id", sort=False):
        g = _calc_indicators(group)
        for fn in [_strategy_bb_breakout, _strategy_oversold_bounce]:
            sig = fn(g)
            if sig:
                meta = fundamental_meta.get(stock_id, {})
                sig["revenue"] = meta.get("revenue")
                sig["yoy"]     = meta.get("yoy")
                sig["revenue_all_time_high"] = meta.get("revenue_all_time_high", False)
                results.append(sig)

    bb_results     = [r for r in results if r["strategy"] == "布林突破"]
    bounce_results = [r for r in results if r["strategy"] == "超賣反彈"]
    print(f"✅ 第三層：布林突破 {len(bb_results)} 檔，超賣反彈 {len(bounce_results)} 檔")

    # ── 存 watchlist_conditions.json ──
    watchlist = {
        "date":    today_str,
        "signals": results,
        "summary": {
            "fundamental_pass": len(fundamental_ids),
            "volume_top50":     len(top50_ids),
            "final_signals":    len(results),
        },
    }
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

    # ── 存 morning_watchlist_YYYYMMDD.txt ──
    date_compact = today_str.replace("-", "")
    txt_path     = os.path.join(BASE_DIR, f"morning_watchlist_{date_compact}.txt")

    lines = [
        f"🔍 盤前選股 {today_str}",
        "",
        f"📊 基本面通過：{len(fundamental_ids)} 檔（月營收雙條件）",
        f"📊 量能前50過濾後：{len(top50_ids)} 檔",
        f"📊 最終訊號：{len(results)} 檔",
        "",
    ]

    def _fmt(r: dict) -> str:
        yoy = r.get("yoy")
        yoy_str = f"  [YoY {yoy:+.1f}%]" if yoy is not None else ""
        return (f"- {r['stock_id']}  收 {r['close']}  量比 {r['vol_ratio']}x"
                f"  RSI {r['rsi']}  漲幅 {r['pct_change']:+.1f}%{yoy_str}")

    if bb_results:
        lines.append(f"📈 布林突破（{len(bb_results)} 檔）")
        lines.extend(_fmt(r) for r in bb_results)

    if bounce_results:
        if bb_results:
            lines.append("")
        lines.append(f"📉 超賣反彈（{len(bounce_results)} 檔）")
        lines.extend(_fmt(r) for r in bounce_results)

    if not results:
        lines.append("（今日無符合條件的候選股票）")

    txt_content = "\n".join(lines)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt_content)

    print(f"\n💾 已存至 watchlist_conditions.json（{len(results)} 檔）")
    print(f"💾 已存至 {os.path.basename(txt_path)}")
    print("\n" + "─" * 60)
    print(txt_content)
    print("─" * 60)

    # ── 發送 Telegram（--dry 跳過）────────────────────────
    if "--dry" not in sys.argv:
        ok = send(txt_content)
        print("✅ 選股清單已發送 Telegram" if ok else "❌ Telegram 發送失敗")


if __name__ == "__main__":
    main()
