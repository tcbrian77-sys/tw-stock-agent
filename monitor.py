"""
monitor.py
用途：盤中監控，檢查候選股票是否觸發條件，觸發時發 Telegram
用法：python3 monitor.py          ← 正常執行（交易時間才動作）
      python3 monitor.py --force  ← 強制執行（測試用，忽略交易時間限制）
crontab 設定：*/10 9-13 * * 1-5 cd ~/tw-stock-agent && python3 monitor.py
"""

import os
import sys
import glob
import json
import requests
import pandas as pd
from datetime import date, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from notify import send

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

BASE_DIR              = os.path.dirname(__file__)
WATCHLIST_PATH         = os.path.join(BASE_DIR, "watchlist_conditions.json")
ALERTED_PATH           = os.path.join(BASE_DIR, "alerted.json")
FLOW_ALERTED_PATH      = os.path.join(BASE_DIR, "selling_alerted.json")
STOCK_INFO_PATH        = os.path.join(BASE_DIR, "data", "stock_info.csv")
PRICES_DIR             = os.path.join(BASE_DIR, "data", "prices")
BROKERS_DIR            = os.path.join(BASE_DIR, "data", "brokers")
API_URL                = "https://api.finmindtrade.com/api/v4/data"
TW_TZ                  = ZoneInfo("Asia/Taipei")

# 三大法人合計買超/賣超金額門檻（台幣）；只要超過此門檻就通知
EXCESSIVE_FLOW_TWD = 100_000_000

# 各策略的漲幅觸發門檻（%）
TRIGGER_PCT = {
    "布林突破": 2.0,
    "超賣反彈": 1.5,
}

# 各策略的出場參數
EXIT_RULES = {
    "布林突破": {
        "stop_loss_pct":   5.0,
        "take_profit_pct": 8.0,
        "force_days":      5,
        "two_stage":       True,
    },
    "超賣反彈": {
        "stop_loss_pct":   4.0,
        "take_profit_pct": 6.0,
        "force_days":      3,
        "two_stage":       False,
    },
}


# ── 近 5 日均量（本地 CSV）────────────────────────────────

def get_5d_avg_volume(stock_id: str) -> int:
    """從本地 prices CSV 讀近 5 個交易日均量，回傳張數（股數 ÷ 1000）。"""
    try:
        files = sorted(
            f for f in os.listdir(PRICES_DIR)
            if f.startswith("prices_") and f.endswith(".csv")
        )[-5:]
    except FileNotFoundError:
        return 0

    vols = []
    for fname in files:
        try:
            df = pd.read_csv(os.path.join(PRICES_DIR, fname), dtype={"stock_id": str})
            row = df[df["stock_id"] == stock_id]
            if not row.empty:
                vols.append(float(row.iloc[0]["Trading_Volume"]))
        except Exception:
            continue

    if not vols:
        return 0
    return int(sum(vols) / len(vols) / 1000)


# ── 時間判斷 ──────────────────────────────────────────────

def is_trading_hours() -> bool:
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    end   = now.replace(hour=14, minute=0,  second=0, microsecond=0)
    return start <= now <= end


# ── 股票名稱（快取於 data/stock_info.csv）────────────────

def load_stock_names() -> dict:
    """回傳 {stock_id: stock_name}。本地快取不存在時才呼叫 API。"""
    if os.path.exists(STOCK_INFO_PATH):
        df = pd.read_csv(STOCK_INFO_PATH, dtype={"stock_id": str})
        if "stock_name" in df.columns:
            return dict(zip(df["stock_id"], df["stock_name"]))

    print("📡 抓取股票名稱（TaiwanStockInfo）...")
    try:
        r = requests.get(API_URL, params={
            "dataset": "TaiwanStockInfo",
            "token": TOKEN,
        }, timeout=60)
        payload = r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"⚠️  股票名稱 API 失敗：{e}")
        return {}

    if payload.get("msg") != "success" or not payload.get("data"):
        print("⚠️  TaiwanStockInfo 回傳異常，將略過股票名稱")
        return {}

    df = pd.DataFrame(payload["data"])

    # FinMind 欄位可能為 stock_name 或 company_name，找第一個存在的
    name_col = next((c for c in ["stock_name", "company_name", "name"] if c in df.columns), None)
    if name_col is None:
        print(f"⚠️  找不到名稱欄位，現有欄位：{list(df.columns)}")
        return {}

    df = df[["stock_id", name_col]].rename(columns={name_col: "stock_name"})
    df["stock_id"] = df["stock_id"].astype(str)
    df = df.drop_duplicates("stock_id")

    os.makedirs(os.path.dirname(STOCK_INFO_PATH), exist_ok=True)
    df.to_csv(STOCK_INFO_PATH, index=False)
    print(f"✅ 股票名稱快取完成（{len(df):,} 檔）")
    return dict(zip(df["stock_id"], df["stock_name"]))


# ── 已通知狀態管理 ────────────────────────────────────────

def load_alerted() -> set:
    today = date.today().isoformat()
    if not os.path.exists(ALERTED_PATH):
        return set()
    with open(ALERTED_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get(today, []))


def save_alerted(alerted: set):
    today = date.today().isoformat()
    with open(ALERTED_PATH, "w", encoding="utf-8") as f:
        json.dump({today: list(alerted)}, f, ensure_ascii=False)


def load_flow_alerted_date():
    """三大法人買賣超總覽是「整份報告」，不是逐檔通知，所以用資料日期本身
    當去重 key：同一份收盤資料（same inst_date）當天只送一次，不會每 15
    分鐘重複發送同樣的總覽。等 broker_cache.py 收盤後更新成新的一天，
    inst_date 改變了才會再送一次。
    """
    if not os.path.exists(FLOW_ALERTED_PATH):
        return None
    with open(FLOW_ALERTED_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("last_sent_date")


def save_flow_alerted_date(flow_date: str):
    with open(FLOW_ALERTED_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_sent_date": flow_date}, f, ensure_ascii=False)


# ── 三大法人全市場買超/賣超檢查 ──────────────────────────────

def _latest_dated_csv(dir_path: str, prefix: str):
    """回傳某資料夾裡最新一份 {prefix}_YYYY-MM-DD.csv 的 (路徑, 日期字串)。"""
    files = sorted(glob.glob(os.path.join(dir_path, f"{prefix}_*.csv")))
    if not files:
        return None, None
    latest = files[-1]
    fname = os.path.basename(latest)
    file_date = fname[len(prefix) + 1 : -4]
    return latest, file_date


def check_excessive_flow(names: dict) -> tuple:
    """全市場三大法人合計買超/賣超金額（台幣），回傳 (buys, sells, 資料日期)。
    buys/sells 皆已依金額大小排序（大到小）。

    注意：三大法人買賣超是收盤後才有的「當日最終數字」（broker_cache.py
    每天 22:00 台北時間才更新），不是盤中即時資料。盤中每 15 分鐘檢查到的，
    是「最近一次收盤後」的資料，同一天盤中重複檢查會看到同樣的結果，直到
    當天收盤、broker_cache.py 跑完才會更新成當天的數字。
    """
    inst_path, inst_date = _latest_dated_csv(BROKERS_DIR, "institutional")
    price_path, price_date = _latest_dated_csv(PRICES_DIR, "prices")

    if inst_path is None or price_path is None:
        print("⚠️  找不到籌碼或價格快取，略過買賣超檢查")
        return [], [], None

    inst_df = pd.read_csv(inst_path, dtype={"stock_id": str})
    price_df = pd.read_csv(price_path, dtype={"stock_id": str})

    net_by_stock = inst_df.groupby("stock_id")["net"].sum()
    close_by_stock = dict(zip(price_df["stock_id"], price_df["close"]))

    buys, sells = [], []

    for sid, net_shares in net_by_stock.items():
        if net_shares == 0:
            continue
        close = close_by_stock.get(sid)
        if not close or close <= 0:
            continue
        value_twd = abs(net_shares) * close
        if value_twd <= EXCESSIVE_FLOW_TWD:
            continue
        entry = {
            "stock_id": sid,
            "name": names.get(sid, ""),
            "net_shares": net_shares,
            "value_twd": value_twd,
            "direction": "buy" if net_shares > 0 else "sell",
        }
        (buys if net_shares > 0 else sells).append(entry)

    buys.sort(key=lambda r: r["value_twd"], reverse=True)
    sells.sort(key=lambda r: r["value_twd"], reverse=True)
    return buys, sells, inst_date


# ── 5 日 / 10 日股價趨勢 ──────────────────────────────────

def compute_price_trends(stock_ids: list, lookback_days: int = 11) -> dict:
    """讀取最近 lookback_days 天的本地收盤價快取，計算每檔股票的 5 日、10 日
    漲跌幅（%）。資料不足時對應欄位回傳 None，不中斷整體檢查。
    """
    files = sorted(glob.glob(os.path.join(PRICES_DIR, "prices_*.csv")))[-lookback_days:]
    if not files:
        return {}

    wanted = set(stock_ids)
    frames = []
    for f in files:
        df = pd.read_csv(f, usecols=["date", "stock_id", "close"], dtype={"stock_id": str})
        frames.append(df[df["stock_id"].isin(wanted)])
    if not frames:
        return {}

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["stock_id", "date"])

    trends = {}
    for sid, g in combined.groupby("stock_id"):
        closes = g["close"].tolist()
        latest = closes[-1] if closes else None

        def pct(n_days_back):
            idx = -(n_days_back + 1)
            if latest is None or len(closes) < n_days_back + 1 or closes[idx] in (0, None):
                return None
            return (latest - closes[idx]) / closes[idx] * 100

        trends[sid] = {"pct_5d": pct(5), "pct_10d": pct(10)}
    return trends


def _fmt_pct(pct):
    return f"{pct:+.1f}%" if pct is not None else "資料不足"


TOP_N_FLOW = 10


def build_overview_message(top_buys: list, top_sells: list, trends: dict, report_date: str) -> str:
    """頭條訊息：前十大買超／賣超，附帶 5 日、10 日股價趨勢比較。"""
    lines = [
        f"📊 三大法人買賣超總覽（{report_date} 收盤資料）",
        f"門檻：合計買超／賣超金額 > NT${EXCESSIVE_FLOW_TWD:,.0f}",
        "",
        f"🟢 買超 Top {TOP_N_FLOW}（含 5日/10日股價趨勢）",
    ]
    if not top_buys:
        lines.append("（無超過門檻的買超股票）")
    for s in top_buys[:TOP_N_FLOW]:
        t = trends.get(s["stock_id"], {})
        lines.append(
            f"- {s['stock_id']} {s['name']}  買超 NT${s['value_twd']:,.0f}  "
            f"5日 {_fmt_pct(t.get('pct_5d'))} ／ 10日 {_fmt_pct(t.get('pct_10d'))}"
        )

    lines += ["", f"🔴 賣超 Top {TOP_N_FLOW}（含 5日/10日股價趨勢）"]
    if not top_sells:
        lines.append("（無超過門檻的賣超股票）")
    for s in top_sells[:TOP_N_FLOW]:
        t = trends.get(s["stock_id"], {})
        lines.append(
            f"- {s['stock_id']} {s['name']}  賣超 NT${s['value_twd']:,.0f}  "
            f"5日 {_fmt_pct(t.get('pct_5d'))} ／ 10日 {_fmt_pct(t.get('pct_10d'))}"
        )

    lines += [
        "",
        f"（共 {len(top_buys)} 檔買超／{len(top_sells)} 檔賣超超過門檻，僅顯示 Top {TOP_N_FLOW}）",
        "⚡ 以上為參考訊號，請自行判斷進出場",
    ]
    return "\n".join(lines)


# ── 讀取 watchlist ─────────────────────────────────────────

def load_watchlist() -> list:
    """讀取 scan.py 產出的候選清單（全部，不設上限）。"""
    if not os.path.exists(WATCHLIST_PATH):
        print("❌ 找不到 watchlist_conditions.json，請先執行 scan.py")
        sys.exit(1)

    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        data = json.load(f)

    signals   = data.get("signals", [])
    scan_date = data.get("date", "")
    today     = date.today().isoformat()

    if scan_date != today:
        print(f"⚠️  watchlist_conditions.json 是 {scan_date} 的資料（今天是 {today}），建議先執行 scan.py")

    return signals


# ── 抓即時價格 ────────────────────────────────────────────

def fetch_today_prices(stock_ids: list) -> dict:
    """抓盤中即時成交價（TaiwanStockPriceTick，盤中即時，付費帳號）。
    每檔獨立查詢，取最新一筆 deal_price 為現價，累加 volume 為今日成交量。
    """
    today  = date.today().isoformat()
    result = {}

    for sid in stock_ids:
        try:
            r = requests.get(API_URL, params={
                "dataset":    "TaiwanStockPriceTick",
                "stock_id":   sid,
                "start_date": today,
                "end_date":   today,
                "data_id":    sid,
                "token":      TOKEN,
            }, timeout=30)
        except requests.RequestException as e:
            print(f"⚠️  {sid} 請求失敗：{e}")
            continue

        if r.status_code != 200:
            print(f"⚠️  {sid} HTTP {r.status_code}")
            continue

        payload = r.json()
        if payload.get("msg") != "success" or not payload.get("data"):
            continue

        ticks     = payload["data"]
        latest    = ticks[-1]
        total_vol = sum(float(t.get("volume", 0)) for t in ticks)

        result[sid] = {
            "close":          float(latest["deal_price"]),
            "Trading_Volume": total_vol,
            "tick_time":      latest.get("Time", ""),
        }

    if not result:
        print("⚠️  今日 Tick 資料尚未更新（可能市場未開盤）")

    return result


# ── 觸發條件判斷 ──────────────────────────────────────────

def check_trigger(signal: dict, price_data: dict):
    """各策略採用不同漲幅門檻：布林突破 ≥ 2%，超賣反彈 ≥ 1.5%。"""
    sid      = signal["stock_id"]
    strategy = signal["strategy"]

    if sid not in price_data:
        return None

    p             = price_data[sid]
    current_close = float(p.get("close", 0))
    current_vol   = float(p.get("Trading_Volume", 0))
    scan_close    = float(signal["close"])

    if scan_close <= 0 or current_close <= 0:
        return None

    pct       = (current_close - scan_close) / scan_close * 100
    threshold = TRIGGER_PCT.get(strategy, 2.0)

    if pct >= threshold:
        return {
            "stock_id":        sid,
            "strategy":        strategy,
            "scan_close":      scan_close,
            "current_close":   current_close,
            "pct":             round(pct, 2),
            "vol_zhang":       int(current_vol // 1000),
            "vol_5d_avg_zhang": get_5d_avg_volume(sid),
            "rsi":             signal.get("rsi"),
            "yoy":             signal.get("yoy"),
            "threshold":       threshold,
        }
    return None


# ── Telegram 訊息組裝 ─────────────────────────────────────

def build_message(t: dict, name: str) -> str:
    sid      = t["stock_id"]
    strategy = t["strategy"]
    yoy      = t.get("yoy")
    price    = t["current_close"]
    vol_avg  = t.get("vol_5d_avg_zhang", 0)

    rules    = EXIT_RULES.get(strategy, EXIT_RULES["布林突破"])
    sl_pct   = rules["stop_loss_pct"]
    tp_pct   = rules["take_profit_pct"]
    days     = rules["force_days"]
    sl_price = round(price * (1 - sl_pct / 100), 1)
    tp_price = round(price * (1 + tp_pct / 100), 1)

    yoy_str = f"  ✓ YoY {yoy:+.1f}%" if yoy is not None else ""

    lines = [
        f"🔔 {sid} {name}【{strategy}】",
        f"現價 {price}  量 {t['vol_zhang']:,} 張",
        f"✓ 漲幅 +{t['pct']}%{yoy_str}",
        "",
        f"📌 建議停損：{sl_price}元（-{sl_pct:.0f}%）",
    ]

    if rules["two_stage"]:
        lines.append(f"🎯 停利①：{tp_price}元（+{tp_pct:.0f}%，達到後建議減半）")
        lines.append(f"🎯 停利②：跌破5日均線時建議全出")
        lines.append(f"⏰ 強制出場：{days}個交易日內未續漲")
    else:
        lines.append(f"🎯 建議停利：{tp_price}元（+{tp_pct:.0f}%，建議全出）")
        lines.append(f"⏰ 強制出場：{days}個交易日內無反彈")

    lines += [
        f"⚠️ 近5日均量：{vol_avg:,}張",
        "",
        "⚡ 以上為參考訊號，請自行判斷進出場",
    ]

    return "\n".join(lines)


# ── 主程式 ────────────────────────────────────────────────

def main():
    force   = "--force" in sys.argv
    now_str = datetime.now(TW_TZ).strftime("%H:%M")

    if not force and not is_trading_hours():
        print(f"⏰ {now_str}：非交易時間，不執行監控。加上 --force 可強制執行。")
        sys.exit(0)

    print(f"🔍 [{now_str}] 開始盤中監控...")

    names = load_stock_names()

    # ── 三大法人買超/賣超總覽（全市場，獨立於 watchlist，不受其空清單影響）──
    last_sent_date = load_flow_alerted_date()
    buys, sells, flow_date = check_excessive_flow(names)
    print(f"📊 買賣超檢查：買超 {len(buys)} 檔／賣超 {len(sells)} 檔超過門檻（資料日期 {flow_date}）")

    if flow_date and flow_date == last_sent_date:
        print(f"📭 {flow_date} 的總覽今天已經發送過，不重複發送")
    elif flow_date and (buys or sells):
        top_ids = [s["stock_id"] for s in (buys[:TOP_N_FLOW] + sells[:TOP_N_FLOW])]
        trends = compute_price_trends(top_ids)

        msg = build_overview_message(buys, sells, trends, flow_date)

        if send(msg):
            save_flow_alerted_date(flow_date)
            print("✅ 已發送買賣超總覽")
        else:
            print("❌ 買賣超總覽發送失敗")

    watchlist = load_watchlist()
    if not watchlist:
        print("📭 watchlist 為空，今日無候選股票")
        sys.exit(0)
    print(f"📋 監控清單：{len(watchlist)} 檔（全部監控，不設上限）")

    alerted = load_alerted()
    targets = [s for s in watchlist if s["stock_id"] not in alerted]
    print(f"🎯 待檢查：{len(targets)} 檔（排除 {len(watchlist) - len(targets)} 檔已通知）")

    if not targets:
        print("✅ 所有候選股票今日已通知完畢")
        sys.exit(0)

    stock_ids  = [s["stock_id"] for s in targets]
    price_data = fetch_today_prices(stock_ids)

    if not price_data:
        print("⏳ 暫無即時資料，下次再試")
        sys.exit(0)

    triggered = [r for s in targets if (r := check_trigger(s, price_data))]
    print(f"🚨 觸發通知：{len(triggered)} 檔")

    if triggered:
        newly_alerted = set()
        for t in triggered:
            sid  = t["stock_id"]
            name = names.get(sid, "")
            msg  = build_message(t, name)

            ok = send(msg)
            if ok:
                newly_alerted.add(sid)
                print(f"✅ 已發送：{sid} {name}  +{t['pct']}%  【{t['strategy']}】")
            else:
                print(f"❌ 發送失敗：{sid}")

        alerted |= newly_alerted
        save_alerted(alerted)
    else:
        print("📭 目前無股票達到觸發條件")


if __name__ == "__main__":
    main()
