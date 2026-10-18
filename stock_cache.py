"""
stock_cache.py
用途：抓指定日期的全市場日收盤資料，存成 CSV
用法：
  python3 stock_cache.py            ← 抓今天
  python3 stock_cache.py 2025-06-10 ← 抓指定日期
"""

import sys
import os
import requests
import pandas as pd
from datetime import date
from dotenv import load_dotenv
from finmind_fallback import is_level_error, load_stock_universe, fetch_dataset_per_stock
import twse_source

# 讀取 .env 檔裡的 FINMIND_TOKEN
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

PRICES_DIR = os.path.join(os.path.dirname(__file__), "data", "prices")
API_URL = "https://api.finmindtrade.com/api/v4/data"


def fetch_prices(target_date: str) -> pd.DataFrame:
    """向 FinMind 要求指定日期的全市場收盤資料。
    帳號若已降級（免費/register 等級），全市場批次查詢會被拒絕，
    此時自動改為逐檔查詢（較慢，約數小時）。
    """
    print(f"📡 正在向 FinMind 請求 {target_date} 的資料...")

    params = {
        "dataset": "TaiwanStockPrice",
        "start_date": target_date,
        "end_date": target_date,
        "token": TOKEN,
    }

    response = requests.get(API_URL, params=params, timeout=60)
    data = response.json()

    if data.get("msg") == "success":
        return pd.DataFrame(data["data"])

    if is_level_error(data):
        print("⚠️  帳號等級不足（FinMind 付費訂閱可能已過期），無法使用全市場批次查詢。")

        if target_date == date.today().isoformat():
            print("   改用 TWSE + TPEx 官方開放資料（免費、全市場、無需金鑰）...")
            try:
                df = twse_source.fetch_prices_today(max_wait_minutes=30, poll_interval_sec=60)
                if not df.empty:
                    print(f"   ✅ TWSE+TPEx 取得 {len(df):,} 筆")
                    return df
                print("   ⚠️  TWSE+TPEx 回傳空值，改用逐檔查詢")
            except Exception as e:
                print(f"   ⚠️  TWSE+TPEx 來源失敗：{e}，改用逐檔查詢")
        else:
            print("   （非今日資料，TWSE+TPEx 來源僅支援當日快照，改用逐檔查詢）")

        print("   改為逐檔向 FinMind 查詢（約需數小時，請耐心等候）...")
        stock_ids = load_stock_universe()
        return fetch_dataset_per_stock(
            "TaiwanStockPrice", TOKEN, stock_ids,
            start_date=target_date, end_date=target_date, label="收盤價",
        )

    response.raise_for_status()  # 其他 HTTP 錯誤（如 401、500）就直接停止
    raise RuntimeError(f"FinMind 回傳錯誤：{data.get('msg')}")


def save_prices(df: pd.DataFrame, target_date: str) -> str:
    """把資料存成 CSV，回傳存檔路徑。"""
    os.makedirs(PRICES_DIR, exist_ok=True)
    filepath = os.path.join(PRICES_DIR, f"prices_{target_date}.csv")
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    return filepath


def main():
    # 決定要抓哪一天的資料
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = date.today().isoformat()  # 例如 "2025-06-13"

    print(f"🗓  目標日期：{target_date}")

    # 檢查 token 是否存在
    if not TOKEN:
        print("❌ 找不到 FINMIND_TOKEN，請確認 .env 檔案是否存在且格式正確。")
        sys.exit(1)

    # 抓資料
    df = fetch_prices(target_date)

    # 判斷是否為休市日或資料尚未更新
    if df.empty:
        print(f"⚠️  {target_date} 查無資料。")
        print("   可能原因：")
        print("   1. 這天是假日或休市日（週末、國定假日）")
        print("   2. 當天收盤資料尚未上傳（通常 18:00 後才有）")
        print("   3. 日期格式錯誤，請用 YYYY-MM-DD，例如 2025-06-10")
        sys.exit(0)

    # 存檔
    filepath = save_prices(df, target_date)

    # 顯示結果摘要
    print(f"✅ 完成！共 {len(df):,} 筆資料，涵蓋 {df['stock_id'].nunique():,} 檔股票")
    print(f"📁 存檔位置：{filepath}")

    # 顯示台積電(2330)的資料做快速確認
    tsmc = df[df["stock_id"] == "2330"]
    if not tsmc.empty:
        row = tsmc.iloc[0]
        print(f"\n📊 台積電(2330) 快速確認：")
        print(f"   收盤價：{row.get('close', 'N/A')}")
        print(f"   成交量：{row.get('Trading_Volume', row.get('trading_volume', 'N/A'))}")
    else:
        print("\n⚠️  台積電(2330)資料不在此日期內")


if __name__ == "__main__":
    main()
