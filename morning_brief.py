"""
morning_brief.py
用途：每日 08:05 發送台股早報到 Telegram
用法：python3 morning_brief.py        ← 正常執行（發送到 Telegram）
      python3 morning_brief.py --dry  ← 模擬執行（只印出，不發送）
crontab 設定：5 8 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 morning_brief.py
"""

import os
import sys
import glob
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from notify import send

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BASE_DIR    = os.path.dirname(__file__)
PRICES_DIR  = os.path.join(BASE_DIR, "data", "prices")
BROKERS_DIR = os.path.join(BASE_DIR, "data", "brokers")
TW_TZ       = ZoneInfo("Asia/Taipei")

WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]
NA_STR      = "⚠️ 資料暫時無法取得"


# ── 美股 & 台幣匯率（yfinance）────────────────────────────

def _safe_yf(symbol: str):
    """取某 ticker 昨日收盤，回傳 (close, pct) 或 None。"""
    try:
        h = yf.Ticker(symbol).history(period="10d")
        if h.empty:
            return None
        # 排除今日尚未完成的 bar（DatetimeIndex.date 回傳 numpy date 陣列）
        today_tw = date.today()
        h_valid = h[h.index.tz_convert(TW_TZ).date < today_tw]
        if h_valid.empty:
            h_valid = h
        if len(h_valid) < 2:
            return None
        close = float(h_valid.iloc[-1]["Close"])
        prev  = float(h_valid.iloc[-2]["Close"])
        pct   = (close - prev) / prev * 100
        return close, pct
    except Exception as e:
        print(f"⚠️  {symbol} 失敗：{e}")
        return None


def fetch_us_markets() -> dict:
    """抓美股三大指數昨收，回傳 {名稱: (close, pct) 或 None}。"""
    syms = {
        "道瓊":     "^DJI",
        "那斯達克":  "^IXIC",
        "費城半導體": "^SOX",
    }
    return {name: _safe_yf(sym) for name, sym in syms.items()}


def fetch_twd():
    """台幣匯率（只取匯率，不算漲跌）。"""
    try:
        h = yf.Ticker("TWD=X").history(period="5d")
        if h.empty:
            return None
        today_tw = date.today()
        h_valid = h[h.index.tz_convert(TW_TZ).date < today_tw]
        if h_valid.empty:
            h_valid = h
        return float(h_valid.iloc[-1]["Close"])
    except Exception as e:
        print(f"⚠️  TWD=X 失敗：{e}")
        return None


# ── 台股大盤（^TWII 取點位，本地 CSV 取成交量）───────────

def fetch_taiex() -> dict:
    """
    點數 & 漲跌幅：^TWII via yfinance（Y9999 不在本地 prices CSV）
    成交量 & 20日均量：本地 prices CSV 所有股票 Trading_money 加總
    """
    result = {"close": None, "pct": None, "vol_yi": None, "avg20_yi": None}

    # 指數點位
    r = _safe_yf("^TWII")
    if r:
        result["close"] = round(r[0], 1)
        result["pct"]   = round(r[1], 2)

    # 成交量（本地 CSV 一般股加總）
    try:
        today_str = date.today().isoformat()
        # 排除今日尚未完整的 CSV（stock_cache 在 14:30 跑，早報時今日未收盤）
        files = sorted(
            f for f in glob.glob(os.path.join(PRICES_DIR, "prices_*.csv"))
            if os.path.basename(f) < f"prices_{today_str}.csv"
        )
        if not files:
            return result

        recent = files[-20:]
        daily_money = []
        for f in recent:
            df = pd.read_csv(f, dtype={"stock_id": str})
            # 只計算 4 位純數字代碼的一般股（排除權證、ETF 等）
            reg = df[df["stock_id"].str.match(r"^\d{4}$")]
            daily_money.append(float(reg["Trading_money"].sum()))

        result["vol_yi"]   = round(daily_money[-1] / 1e8)
        result["avg20_yi"] = round(sum(daily_money) / len(daily_money) / 1e8)
    except Exception as e:
        print(f"⚠️  成交量讀取失敗：{e}")

    return result


# ── 三大法人（本地 broker CSV）────────────────────────────

def fetch_institutional() -> dict:
    """
    回傳 {外資, 投信, 自營, 合計} 億元。
    net 欄位單位：NTD 元，÷ 1e8 得億元。
    外資 = Foreign_Investor + Foreign_Dealer_Self
    投信 = Investment_Trust
    自營 = Dealer_self + Dealer_Hedging + Dealer
    """
    try:
        files = sorted(glob.glob(os.path.join(BROKERS_DIR, "institutional_*.csv")))
        if not files:
            return {}
        df = pd.read_csv(files[-1], dtype={"stock_id": str})

        foreign = df[df["name"].isin(["Foreign_Investor", "Foreign_Dealer_Self"])]["net"].sum()
        trust   = df[df["name"] == "Investment_Trust"]["net"].sum()
        dealer  = df[df["name"].isin(["Dealer_self", "Dealer_Hedging", "Dealer"])]["net"].sum()
        total   = foreign + trust + dealer

        return {
            "外資": round(foreign / 1e8, 1),
            "投信": round(trust   / 1e8, 1),
            "自營": round(dealer  / 1e8, 1),
            "合計": round(total   / 1e8, 1),
        }
    except Exception as e:
        print(f"⚠️  三大法人讀取失敗：{e}")
        return {}


# ── 訊息組裝 ─────────────────────────────────────────────

def build_brief() -> str:
    today    = date.today()
    weekday  = WEEKDAYS_ZH[today.weekday()]
    date_str = today.strftime("%Y/%m/%d")

    lines = [f"📊 台股早報 {date_str}（{weekday}）", ""]

    # 美股
    lines.append("🌏 外部環境（昨收）")
    us = fetch_us_markets()
    for name in ["道瓊", "那斯達克", "費城半導體"]:
        r = us.get(name)
        if r:
            close, pct = r
            sign = "+" if pct >= 0 else ""
            lines.append(f"{name} {close:,.0f}（{sign}{pct:.2f}%）")
        else:
            lines.append(f"{name} {NA_STR}")

    twd = fetch_twd()
    lines.append(f"台幣 {twd:.2f}元／美元" if twd else f"台幣 {NA_STR}")

    # 台股大盤
    lines += ["", "📈 台股大盤昨日"]
    taiex = fetch_taiex()
    if taiex["close"] is not None:
        sign = "+" if taiex["pct"] >= 0 else ""
        lines.append(f"加權指數 {taiex['close']:,.1f}（{sign}{taiex['pct']}%）")
    else:
        lines.append(f"加權指數 {NA_STR}")

    if taiex["vol_yi"] is not None and taiex["avg20_yi"] is not None:
        v, a = taiex["vol_yi"], taiex["avg20_yi"]
        flag = "放量" if v > a else "縮量"
        lines.append(f"成交量 {v}億（近20日均量 {a}億，{flag}）")
    else:
        lines.append(f"成交量 {NA_STR}")

    # 三大法人
    lines += ["", "🏦 三大法人昨日"]
    inst = fetch_institutional()
    if inst:
        for name in ["外資", "投信", "自營"]:
            v = inst[name]
            sign = "+" if v >= 0 else ""
            lines.append(f"{name} {sign}{v:.1f}億")
        total = inst["合計"]
        sign  = "+" if total >= 0 else ""
        direction = "買超" if total >= 0 else "賣超"
        lines.append(f"合計 {sign}{total:.1f}億（{direction}）")
    else:
        lines.append(NA_STR)

    lines += ["", "📋 選股清單將於 08:20 發出"]

    return "\n".join(lines)


# ── 主程式 ───────────────────────────────────────────────

def main():
    dry = "--dry" in sys.argv
    now_str = datetime.now(TW_TZ).strftime("%H:%M")
    print(f"📊 [{now_str}] 台股早報{'（模擬，不發送）' if dry else ''}...")

    msg = build_brief()
    print("\n" + "─" * 50)
    print(msg)
    print("─" * 50)

    if dry:
        print("\n（--dry 模式，未實際發送）")
        return

    ok = send(msg)
    print("✅ 早報發送成功" if ok else "❌ 早報發送失敗")


if __name__ == "__main__":
    main()
