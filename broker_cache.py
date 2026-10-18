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
from finmind_fallback import is_level_error, load_stock_universe, fetch_dataset_per_stock
import twse_source

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

BROKERS_DIR = os.path.join(os.path.dirname(__file__), "data", "brokers")
API_URL = "https://api.finmindtrade.com/api/v4/data"


def fetch_dataset(dataset: str, target_date: str) -> pd.DataFrame:
    """向 FinMind 要求指定 dataset 的資料。
    帳號若已降級（免費/register 等級），全市場批次查詢會被拒絕，
    此時自動改為逐檔查詢（較慢，約數小時）。
    """
    response = requests.get(API_URL, params={
        "dataset": dataset,
        "start_date": target_date,
        "end_date": target_date,
        "token": TOKEN,
    }, timeout=60)
    data = response.json()

    if data.get("msg") == "success":
        return pd.DataFrame(data["data"])

    if is_level_error(data):
        print(f"⚠️  {dataset} 帳號等級不足，無法使用全市場批次查詢。")

        twse_fetchers = {
            "TaiwanStockInstitutionalInvestorsBuySell": twse_source.fetch_institutional_for_date,
            "TaiwanStockMarginPurchaseShortSale": twse_source.fetch_margin_for_date,
        }
        fetcher = twse_fetchers.get(dataset)

        if fetcher:
            print("   改用 TWSE + TPEx 官方開放資料（免費、全市場、無需金鑰，支援任何歷史日期）...")
            try:
                df = fetcher(target_date)
                if not df.empty:
                    print(f"   ✅ TWSE+TPEx 取得 {len(df):,} 筆")
                    return df
                print("   ℹ️  TWSE+TPEx 兩邊都查無資料，視為假日/休市日，不觸發逐檔查詢")
                return df
            except Exception as e:
                print(f"   ⚠️  TWSE+TPEx 來源失敗：{e}，改用逐檔查詢")
        else:
            print("   （非今日資料或無對應來源，改用逐檔查詢）")

        print("   改為逐檔向 FinMind 查詢（約需數小時，請耐心等候）...")
        stock_ids = load_stock_universe()
        return fetch_dataset_per_stock(
            dataset, TOKEN, stock_ids,
            start_date=target_date, end_date=target_date, label=dataset,
        )

    response.raise_for_status()
    raise RuntimeError(f"FinMind 回傳錯誤：{data.get('msg')}")


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
