"""
broker_cache.py
用途：抓指定日期的三大法人買賣超 + 融資融券，存成 CSV
用法：
  python3 broker_cache.py            ← 抓今天
  python3 broker_cache.py 2025-06-06 ← 抓指定日期
"""

import sys
import os
import requests
import pandas as pd
from datetime import date
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

BROKERS_DIR = os.path.join(os.path.dirname(__file__), "data", "brokers")
API_URL = "https://api.finmindtrade.com/api/v4/data"


def fetch_dataset(dataset: str, target_date: str) -> pd.DataFrame:
    """向 FinMind 要求指定 dataset 的資料，一次呼叫拿全市場。"""
    response = requests.get(API_URL, params={
        "dataset": dataset,
        "start_date": target_date,
        "end_date": target_date,
        "token": TOKEN,
    }, timeout=60)
    response.raise_for_status()

    data = response.json()
    if data.get("msg") != "success":
        raise RuntimeError(f"FinMind 回傳錯誤：{data.get('msg')}")

    return pd.DataFrame(data["data"])


def fetch_institutional(target_date: str) -> pd.DataFrame:
    """抓三大法人買賣超，並加上 net（買超金額）欄位。"""
    print(f"📡 正在抓三大法人資料（{target_date}）...")
    df = fetch_dataset("TaiwanStockInstitutionalInvestorsBuySell", target_date)
    if df.empty:
        return df

    # net = 買進 - 賣出，正數代表買超，負數代表賣超
    df["net"] = df["buy"] - df["sell"]
    return df


def fetch_margin(target_date: str) -> pd.DataFrame:
    """抓融資融券餘額。"""
    print(f"📡 正在抓融資融券資料（{target_date}）...")
    df = fetch_dataset("TaiwanStockMarginPurchaseShortSale", target_date)
    return df


def save_csv(df: pd.DataFrame, filename: str) -> str:
    """存 CSV，回傳路徑。"""
    os.makedirs(BROKERS_DIR, exist_ok=True)
    filepath = os.path.join(BROKERS_DIR, filename)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    return filepath


def main():
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = date.today().isoformat()

    print(f"🗓  目標日期：{target_date}")

    if not TOKEN:
        print("❌ 找不到 FINMIND_TOKEN，請確認 .env 設定")
        sys.exit(1)

    # --- 三大法人 ---
    df_inst = fetch_institutional(target_date)
    if df_inst.empty:
        print("⚠️  三大法人查無資料（可能是休市日或資料尚未更新）")
    else:
        path = save_csv(df_inst, f"institutional_{target_date}.csv")
        stocks = df_inst["stock_id"].nunique()
        print(f"✅ 三大法人：{len(df_inst):,} 筆，涵蓋 {stocks:,} 檔，存至 {path}")

        # 快速確認：台積電外資買賣超
        tsmc = df_inst[(df_inst["stock_id"] == "2330") & (df_inst["name"] == "Foreign_Investor")]
        if not tsmc.empty:
            row = tsmc.iloc[0]
            action = "買超" if row["net"] > 0 else "賣超"
            print(f"   📊 台積電外資：{action} {abs(row['net']):,} 股")

    # --- 融資融券 ---
    df_margin = fetch_margin(target_date)
    if df_margin.empty:
        print("⚠️  融資融券查無資料（可能是休市日或資料尚未更新）")
    else:
        path = save_csv(df_margin, f"margin_{target_date}.csv")
        print(f"✅ 融資融券：{len(df_margin):,} 筆，存至 {path}")

        # 快速確認：台積電融資餘額
        tsmc_m = df_margin[df_margin["stock_id"] == "2330"]
        if not tsmc_m.empty:
            balance = tsmc_m.iloc[0].get("MarginPurchaseTodayBalance", "N/A")
            print(f"   📊 台積電融資餘額：{balance:,} 股")


if __name__ == "__main__":
    main()
