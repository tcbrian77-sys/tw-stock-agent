"""
fetch_history.py
用途：一次性歷史資料下載，把過去 N 天的收盤價全部補齊
用法：python3 fetch_history.py
注意：只需要跑一次，之後由 stock_cache.py 每日維護
"""

import os
import time
import requests
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("FINMIND_TOKEN")

PRICES_DIR = os.path.join(os.path.dirname(__file__), "data", "prices")
API_URL = "https://api.finmindtrade.com/api/v4/data"
DAYS_TO_FETCH = 90        # 抓 90 個日曆天（約含 60 個交易日）
SLEEP_BETWEEN = 6         # 每次 API 呼叫間隔秒數（避免超過每小時 600 次限制）


def fetch_one_day(target_date: str) -> pd.DataFrame:
    r = requests.get(API_URL, params={
        "dataset": "TaiwanStockPrice",
        "start_date": target_date,
        "end_date": target_date,
        "token": TOKEN,
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("msg") != "success":
        return pd.DataFrame()
    return pd.DataFrame(data["data"])


def main():
    os.makedirs(PRICES_DIR, exist_ok=True)

    end_date = date.today()
    start_date = end_date - timedelta(days=DAYS_TO_FETCH)

    # 列出這段期間所有日曆天
    all_dates = []
    d = start_date
    while d <= end_date:
        # 跳過週末（台股週一到週五交易）
        if d.weekday() < 5:
            all_dates.append(d.isoformat())
        d += timedelta(days=1)

    print(f"📅 準備下載 {start_date} ~ {end_date}（共 {len(all_dates)} 個工作日）")
    print(f"⏱  預估時間：約 {len(all_dates) * SLEEP_BETWEEN // 60} 分鐘\n")

    skipped, downloaded, holiday = 0, 0, 0

    for i, target_date in enumerate(all_dates, 1):
        filepath = os.path.join(PRICES_DIR, f"prices_{target_date}.csv")

        # 已有 CSV 就跳過，不重複下載
        if os.path.exists(filepath):
            print(f"[{i:2}/{len(all_dates)}] {target_date} ⏭  已有資料，跳過")
            skipped += 1
            continue

        df = fetch_one_day(target_date)

        if df.empty:
            print(f"[{i:2}/{len(all_dates)}] {target_date} 🔴 休市日，跳過")
            holiday += 1
        else:
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            stocks = df[df["stock_id"].str.match(r"^\d{4}$")]["stock_id"].nunique()
            print(f"[{i:2}/{len(all_dates)}] {target_date} ✅ {stocks:,} 檔股票")
            downloaded += 1

        # 還沒到最後一筆才需要等待
        if i < len(all_dates):
            time.sleep(SLEEP_BETWEEN)

    print(f"\n🎉 完成！下載 {downloaded} 天、跳過 {skipped} 天（已有）、休市 {holiday} 天")
    print(f"📁 資料位置：{PRICES_DIR}")


if __name__ == "__main__":
    main()
