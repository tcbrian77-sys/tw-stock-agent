"""
evening_scan.py
用途：每日 20:00 基本面評分選股，發送晚間選股報告到 Telegram
用法：python3 evening_scan.py        ← 正常執行（發送 Telegram）
      python3 evening_scan.py --dry  ← 模擬執行（只印出，不發送）
crontab：0 20 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 evening_scan.py
"""

import os
import sys
import glob
import requests
import pandas as pd
from datetime import date, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from notify import send
from finmind_fallback import is_level_error, load_stock_universe, fetch_dataset_per_stock
import twse_source

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

BASE_DIR        = os.path.dirname(__file__)
PRICES_DIR      = os.path.join(BASE_DIR, "data", "prices")
DATA_DIR        = os.path.join(BASE_DIR, "data")
STOCK_INFO_PATH = os.path.join(BASE_DIR, "data", "stock_info.csv")
API_URL         = "https://api.finmindtrade.com/api/v4/data"
TW_TZ           = ZoneInfo("Asia/Taipei")
TOP_N           = 50

RANK_EMOJIS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


# ── 股票名稱 ───────────────────────────────────────────────

def load_stock_names() -> dict:
    if os.path.exists(STOCK_INFO_PATH):
        df = pd.read_csv(STOCK_INFO_PATH, dtype={"stock_id": str})
        if "stock_name" in df.columns:
            return dict(zip(df["stock_id"], df["stock_name"]))
    return {}


# ── 篩選池：昨日金額前50 ∩ 股數前50 ─────────────────────────

def get_intersection_pool():
    """從最新 prices CSV 取一般股（4位數字）交集篩選池。"""
    files = sorted(glob.glob(os.path.join(PRICES_DIR, "prices_*.csv")))
    if not files:
        raise RuntimeError("找不到 prices CSV，請先執行 stock_cache.py")

    df = pd.read_csv(files[-1], dtype={"stock_id": str})
    df = df[df["stock_id"].str.match(r"^\d{4}$")].copy()
    df["Trading_money"]  = pd.to_numeric(df["Trading_money"],  errors="coerce").fillna(0)
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce").fillna(0)

    top_money  = set(df.nlargest(TOP_N, "Trading_money")["stock_id"])
    top_volume = set(df.nlargest(TOP_N, "Trading_Volume")["stock_id"])
    pool       = sorted(top_money & top_volume)

    print(f"📊 篩選池：金額前{TOP_N} ∩ 股數前{TOP_N} = {len(pool)} 檔交集")
    return pool, len(pool)


# ── 月營收快取（已有，直接讀）─────────────────────────────────

def load_revenue_cache() -> pd.DataFrame:
    ym    = date.today().strftime("%Y%m")
    cache = os.path.join(DATA_DIR, f"fundamental_filter_{ym}.csv")
    if not os.path.exists(cache):
        raise RuntimeError(f"找不到月營收快取 {cache}，請先執行 scan.py")

    df = pd.read_csv(cache, dtype={"stock_id": str})
    # 只保留台灣本業資料
    if "country" in df.columns:
        df = df[df["country"] == "Taiwan"]
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    print(f"📂 月營收快取：{len(df):,} 筆，{df['stock_id'].nunique():,} 檔")
    return df


# ── TaiwanStockPER 快取 ────────────────────────────────────

def load_per_cache() -> pd.DataFrame:
    """全市場 PER，一次 API 呼叫，快取於 data/stock_per_YYYYMM.csv。"""
    ym      = date.today().strftime("%Y%m")
    cache_f = os.path.join(DATA_DIR, f"stock_per_{ym}.csv")

    if os.path.exists(cache_f):
        df = pd.read_csv(cache_f, dtype={"stock_id": str})
        print(f"📂 PER 快取：{len(df):,} 筆")
        return df

    # 往前找最近有資料的交易日
    print("📡 抓取 TaiwanStockPER...")
    for delta in range(7):
        check_date = (date.today() - pd.Timedelta(days=delta)).isoformat()
        try:
            r = requests.get(API_URL, params={
                "dataset":    "TaiwanStockPER",
                "start_date": check_date,
                "end_date":   check_date,
                "token":      TOKEN,
            }, timeout=30)
            payload = r.json()

            if is_level_error(payload):
                print("⚠️  帳號等級不足，無法使用全市場批次查詢。")
                print("   改用 TWSE + TPEx 官方開放資料（免費、全市場、無需金鑰）...")
                try:
                    df = twse_source.fetch_per_today()
                except Exception as e:
                    df = pd.DataFrame()
                    print(f"   ⚠️  TWSE+TPEx 來源失敗：{e}")

                if not df.empty:
                    df["stock_id"] = df["stock_id"].astype(str)
                    df.to_csv(cache_f, index=False)
                    print(f"✅ PER 快取完成（TWSE+TPEx）：{len(df):,} 筆")
                    return df

                print("   TWSE+TPEx 來源無資料，改用逐檔查詢（約需數小時，最後手段）...")
                stock_ids = load_stock_universe()
                df = fetch_dataset_per_stock(
                    "TaiwanStockPER", TOKEN, stock_ids,
                    start_date=check_date, end_date=check_date, label="PER",
                )
                if not df.empty:
                    df["stock_id"] = df["stock_id"].astype(str)
                    df.to_csv(cache_f, index=False)
                    print(f"✅ PER 快取完成（逐檔）：{len(df):,} 筆（{check_date}）")
                    return df
                continue

            if payload.get("msg") == "success" and payload.get("data"):
                df = pd.DataFrame(payload["data"])
                df["stock_id"] = df["stock_id"].astype(str)
                df.to_csv(cache_f, index=False)
                print(f"✅ PER 快取完成：{len(df):,} 筆（{check_date}）")
                return df
        except Exception as e:
            print(f"⚠️  PER {check_date} 失敗：{e}")

    print("⚠️  無法取得 PER 資料")
    return pd.DataFrame()


# ── TaiwanStockFinancialStatements 快取 ───────────────────────

def load_fin_cache(pool_ids: list) -> pd.DataFrame:
    """
    逐檔抓取財報（GrossProfit + Revenue），快取於 data/financial_statements_YYYYMM.csv。
    同月份只抓一次；新出現的 stock_id 補抓後追加。
    """
    ym      = date.today().strftime("%Y%m")
    cache_f = os.path.join(DATA_DIR, f"financial_statements_{ym}.csv")
    NEEDED  = {"GrossProfit", "Revenue"}

    if os.path.exists(cache_f):
        df        = pd.read_csv(cache_f, dtype={"stock_id": str})
        cached_ids = set(df["stock_id"].unique())
        missing   = [s for s in pool_ids if s not in cached_ids]
    else:
        df      = pd.DataFrame()
        missing = list(pool_ids)

    if missing:
        print(f"📡 抓取財報（{len(missing)} 檔）...")
        new_rows = []
        for i, sid in enumerate(missing, 1):
            print(f"\r  {i}/{len(missing)} {sid}  ", end="", flush=True)
            try:
                r = requests.get(API_URL, params={
                    "dataset":    "TaiwanStockFinancialStatements",
                    "stock_id":   sid,
                    "start_date": "2024-01-01",
                    "end_date":   date.today().isoformat(),
                    "data_id":    sid,
                    "token":      TOKEN,
                }, timeout=30)
                payload = r.json()
                if payload.get("msg") == "success" and payload.get("data"):
                    rows = [row for row in payload["data"] if row["type"] in NEEDED]
                    new_rows.extend(rows)
            except Exception as e:
                print(f"\n⚠️  {sid} 失敗：{e}")
        print()

        if new_rows:
            new_df = pd.DataFrame(new_rows)
            new_df["stock_id"] = new_df["stock_id"].astype(str)
            df = pd.concat([df, new_df], ignore_index=True) if not df.empty else new_df
            df.to_csv(cache_f, index=False)
            print(f"✅ 財報快取更新：{len(df):,} 筆")
        else:
            print("⚠️  無新增財報資料")
    else:
        print(f"📂 財報快取：{len(df):,} 筆")

    if df.empty:
        return df
    return df[df["type"].isin(NEEDED)].copy()


# ── 評分函式 ────────────────────────────────────────────────

def _score_c1_acceleration(stock_id: str, rev_df: pd.DataFrame):
    """條件一：最近 3 期 YoY 加速趨勢。"""
    df = rev_df[rev_df["stock_id"] == stock_id].sort_values(
        ["revenue_year", "revenue_month"]
    )
    if len(df) < 14:  # 需要至少 13 個月以計算 3 期 YoY
        return None, None

    # 建立 (year, month) → revenue 的查找字典
    rev_map = {
        (int(row["revenue_year"]), int(row["revenue_month"])): row["revenue"]
        for _, row in df.iterrows()
        if pd.notna(row["revenue"]) and row["revenue"] > 0
    }

    # 取最近 3 期月份並計算 YoY
    recent = df.tail(3)
    yoys   = []
    for _, row in recent.iterrows():
        y, m    = int(row["revenue_year"]), int(row["revenue_month"])
        prev_rev = rev_map.get((y - 1, m))
        curr_rev = row["revenue"]
        if prev_rev and prev_rev > 0 and pd.notna(curr_rev):
            yoys.append((curr_rev - prev_rev) / prev_rev * 100)

    if len(yoys) < 3:
        return None, None

    y1, y2, y3  = yoys[0], yoys[1], yoys[2]
    acc1, acc2  = (y2 > y1), (y3 > y2)

    if acc1 and acc2:
        score = 25
    elif acc1 or acc2:
        score = 15
    else:
        score = 0

    return score, f"{y1:+.1f}%→{y2:+.1f}%→{y3:+.1f}%"


def _score_c2_rev_high(stock_id: str, rev_df: pd.DataFrame):
    """條件二：最新月營收接近或突破歷史新高。"""
    df = rev_df[rev_df["stock_id"] == stock_id]
    if df.empty:
        return None, None

    max_rev    = df["revenue"].max()
    latest_rev = df.sort_values(["revenue_year", "revenue_month"]).iloc[-1]["revenue"]

    if max_rev <= 0 or pd.isna(max_rev):
        return None, None

    ratio = latest_rev / max_rev * 100

    if ratio >= 100:
        score = 25
    elif ratio >= 95:
        score = 15
    elif ratio >= 90:
        score = 5
    else:
        score = 0

    return score, f"{ratio:.1f}%"


def _score_c3_gross_margin(stock_id: str, fin_df: pd.DataFrame):
    """條件三：最新季毛利率 vs 去年同季。"""
    if fin_df.empty:
        return None, None

    df = fin_df[fin_df["stock_id"] == stock_id].copy()
    if df.empty:
        return None, None

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    pivot = df.pivot_table(index="date", columns="type", values="value", aggfunc="last")

    if "GrossProfit" not in pivot.columns or "Revenue" not in pivot.columns:
        return None, None

    pivot = pivot.dropna(subset=["GrossProfit", "Revenue"])
    pivot = pivot[pivot["Revenue"] > 0].sort_index()
    if len(pivot) < 2:
        return None, None

    pivot["gm"] = pivot["GrossProfit"] / pivot["Revenue"] * 100

    latest_date = pivot.index[-1]
    latest_gm   = pivot["gm"].iloc[-1]

    # 找去年同季（日期差約 365 天，容許 ±60 天）
    latest_ts = pd.Timestamp(latest_date)
    target_ts = latest_ts - pd.DateOffset(years=1)
    diffs = [(abs((pd.Timestamp(q) - target_ts).days), q) for q in pivot.index[:-1]]
    if not diffs:
        return None, None
    closest_days, closest_q = min(diffs)
    if closest_days > 60:
        return None, None

    prev_gm = pivot.loc[closest_q, "gm"]
    diff    = latest_gm - prev_gm

    if diff > 1:
        score  = 25
        trend  = f"↑{diff:.1f}pp"
    elif diff >= -1:
        score  = 10
        trend  = f"持平{diff:+.1f}pp"
    else:
        score  = 0
        trend  = f"↓{abs(diff):.1f}pp"

    return score, f"{latest_gm:.1f}%（{trend}）"


def _score_c4_pe(stock_id: str, per_df: pd.DataFrame):
    """條件四：本益比。"""
    if per_df.empty:
        return None, None

    row = per_df[per_df["stock_id"] == stock_id]
    if row.empty:
        return None, None

    pe = float(row.iloc[0]["PER"])

    if pe <= 0:
        return 0, f"PE 虧損"
    elif pe < 15:
        return 25, f"PE {pe:.1f}"
    elif pe < 20:
        return 15, f"PE {pe:.1f}"
    elif pe < 25:
        return 5,  f"PE {pe:.1f}"
    else:
        return 0,  f"PE {pe:.1f}"


def score_stock(stock_id: str, rev_df, fin_df, per_df):
    """四條件評分，資料缺失時等比換算為100分。"""
    raw = {
        "c1": _score_c1_acceleration(stock_id, rev_df),
        "c2": _score_c2_rev_high(stock_id, rev_df),
        "c3": _score_c3_gross_margin(stock_id, fin_df),
        "c4": _score_c4_pe(stock_id, per_df),
    }

    valid = {k: v for k, v in raw.items() if v[0] is not None}
    if not valid:
        return 0, {k: (0, "無資料") for k in raw}

    raw_total    = sum(v[0] for v in valid.values())
    max_possible = len(valid) * 25
    final_score  = round(raw_total / max_possible * 100)

    # 補齊缺失條件為 "無資料"
    result = {k: v if v[0] is not None else (None, "無資料") for k, v in raw.items()}
    return final_score, result


# ── Telegram 訊息 ─────────────────────────────────────────

def build_message(ranked: list, names: dict, pool_size: int) -> str:
    today = date.today().strftime("%Y/%m/%d")
    lines = [
        f"📊 晚間選股報告 {today}",
        "基本面加速成長策略｜明日參考",
        "",
    ]

    for i, (sid, score, det) in enumerate(ranked):
        emoji = RANK_EMOJIS[i]
        name  = names.get(sid, "")

        c1 = det["c1"][1]
        c2 = det["c2"][1]
        c3 = det["c3"][1]
        c4 = det["c4"][1]

        lines.append(f"{emoji} {sid} {name}  總分 {score}/100")
        lines.append(f"   營收加速 {c1}｜接近新高 {c2}｜毛利 {c3}｜{c4}")
        if i < len(ranked) - 1:
            lines.append("")

    lines += [
        "",
        f"📌 篩選池：昨日金額前{TOP_N}∩股數前{TOP_N}（共{pool_size}檔交集）",
        "⚡ 以上為參考訊號，請自行判斷進出場",
    ]
    return "\n".join(lines)


# ── 主程式 ───────────────────────────────────────────────

def main():
    dry     = "--dry" in sys.argv
    now_str = datetime.now(TW_TZ).strftime("%H:%M")
    print(f"🌙 [{now_str}] 晚間選股啟動...")

    pool_ids, pool_size = get_intersection_pool()
    if not pool_ids:
        print("❌ 交集為空，無法繼續")
        return

    rev_df = load_revenue_cache()
    per_df = load_per_cache()
    fin_df = load_fin_cache(pool_ids)
    names  = load_stock_names()

    print(f"\n🔢 評分中（{len(pool_ids)} 檔）...")
    results = []
    for sid in pool_ids:
        score, det = score_stock(sid, rev_df, fin_df, per_df)
        results.append((sid, score, det))
        c1s = det['c1'][0] if det['c1'][0] is not None else '-'
        c2s = det['c2'][0] if det['c2'][0] is not None else '-'
        c3s = det['c3'][0] if det['c3'][0] is not None else '-'
        c4s = det['c4'][0] if det['c4'][0] is not None else '-'
        print(f"  {sid} {names.get(sid,''):8s} 總分={score:3d}  "
              f"c1={c1s} c2={c2s} c3={c3s} c4={c4s}")

    results.sort(key=lambda x: x[1], reverse=True)
    ranked = results[:5]

    msg = build_message(ranked, names, pool_size)
    print("\n" + "─" * 50)
    print(msg)
    print("─" * 50)

    if dry:
        print("\n（--dry 模式，未實際發送）")
        return

    ok = send(msg)
    print("✅ 晚間選股已發送 Telegram" if ok else "❌ Telegram 發送失敗")


if __name__ == "__main__":
    main()
