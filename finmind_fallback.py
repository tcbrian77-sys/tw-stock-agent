"""
finmind_fallback.py
用途：FinMind 免費/register 等級無法使用「全市場批次查詢」（不帶 stock_id 的請求），
      但逐檔查詢（帶 data_id）仍可使用。本模組提供共用的偵測與逐檔救援機制，
      並做速率節流以符合免費額度（官方約 600 次/小時，這裡保留緩衝）。

用法：
  from finmind_fallback import is_level_error, load_stock_universe, fetch_dataset_per_stock
"""

import os
import time
import requests
import pandas as pd

API_URL = "https://api.finmindtrade.com/api/v4/data"
BASE_DIR = os.path.dirname(__file__)
STOCK_INFO_PATH = os.path.join(BASE_DIR, "data", "stock_info.csv")

RATE_PER_HOUR = 500                      # 保留緩衝，低於官方 600 次/小時上限
MIN_INTERVAL = 3600.0 / RATE_PER_HOUR    # 每檔請求之間至少間隔的秒數


def is_level_error(payload: dict) -> bool:
    """判斷 FinMind 回應是否為帳號等級不足（需要升級付費版）的錯誤訊息。"""
    msg = (payload or {}).get("msg", "")
    return isinstance(msg, str) and "update your user level" in msg.lower()


def load_stock_universe() -> list:
    """回傳一般股（4 位數字代碼）清單，供逐檔查詢使用。"""
    if not os.path.exists(STOCK_INFO_PATH):
        raise FileNotFoundError(
            f"找不到 {STOCK_INFO_PATH}，請先執行一次 monitor.py 以產生股票清單快取"
        )
    df = pd.read_csv(STOCK_INFO_PATH, dtype={"stock_id": str})
    ids = df["stock_id"].dropna().unique().tolist()
    return sorted(s for s in ids if s.isdigit() and len(s) == 4)


def fetch_dataset_per_stock(dataset: str, token: str, stock_ids: list,
                             start_date: str, end_date: str = None,
                             label: str = "") -> pd.DataFrame:
    """逐檔向 FinMind 請求指定 dataset，附帶速率節流，回傳合併後的 DataFrame。

    注意：免費/register 等級一次只能查單檔（帶 data_id），這裡用節流迴圈取代
    原本的全市場批次呼叫。數量大時會花較長時間（約每小時 500 檔）。
    """
    frames = []
    total = len(stock_ids)
    t_last = 0.0

    for i, sid in enumerate(stock_ids, 1):
        wait = MIN_INTERVAL - (time.time() - t_last)
        if wait > 0:
            time.sleep(wait)
        t_last = time.time()

        params = {
            "dataset": dataset,
            "data_id": sid,
            "start_date": start_date,
            "token": token,
        }
        if end_date:
            params["end_date"] = end_date

        try:
            r = requests.get(API_URL, params=params, timeout=30)
            payload = r.json()
        except Exception as e:
            print(f"\n⚠️  {sid} 請求失敗：{e}")
            continue

        if payload.get("msg") == "success" and payload.get("data"):
            frames.append(pd.DataFrame(payload["data"]))

        if i % 25 == 0 or i == total:
            tag = f"[{label}] " if label else ""
            print(f"\r  {tag}逐檔進度：{i}/{total}", end="", flush=True)

    print()
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
