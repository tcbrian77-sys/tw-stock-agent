"""
notify.py
用途：發送 Telegram 通知的工具模組
用法：
  from notify import send
  send("訊息內容")
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def send(message: str) -> bool:
    """發送訊息到 Telegram，成功回傳 True，失敗回傳 False。"""
    if not TOKEN or not CHAT_ID:
        print("❌ 找不到 TELEGRAM_TOKEN 或 TELEGRAM_CHAT_ID，請確認 .env 設定")
        return False

    try:
        response = requests.post(API_URL, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",   # 支援 <b>粗體</b>、<code>等寬字</code> 等格式
        }, timeout=10)

        if response.json().get("ok"):
            return True
        else:
            print(f"❌ Telegram 回傳錯誤：{response.json().get('description')}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"❌ 網路錯誤：{e}")
        return False


if __name__ == "__main__":
    # 直接執行這個檔案時，發送測試訊息
    ok = send(
        "🤖 <b>台股監控系統</b> 啟動測試\n\n"
        "✅ Telegram 通知正常運作\n"
        "📡 FinMind API 已連線\n"
        "準備就緒，等待訊號中..."
    )
    if ok:
        print("✅ 測試訊息發送成功，請查看 Telegram！")
    else:
        print("❌ 發送失敗，請檢查設定")
