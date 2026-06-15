import subprocess

crontab_content = (
    "# 台股監控系統排程\n"
    "\n"
    "# 早報：週一到週五 08:05\n"
    "5 8 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 morning_brief.py >> /Users/brian/tw-stock-agent/logs/morning_brief.log 2>&1\n"
    "\n"
    "# 盤前選股：週一到週五 08:20\n"
    "20 8 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 scan.py >> /Users/brian/tw-stock-agent/logs/scan.log 2>&1\n"
    "\n"
    "# 盤中監控：週一到週五 09:00-13:50，每 10 分鐘\n"
    "*/10 9-13 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 monitor.py >> /Users/brian/tw-stock-agent/logs/monitor.log 2>&1\n"
    "\n"
    "# 補收盤股價：週一到週五 14:30\n"
    "30 14 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 stock_cache.py >> /Users/brian/tw-stock-agent/logs/stock_cache.log 2>&1\n"
    "\n"
    "# 補籌碼資料：週一到週五 22:00\n"
    "0 22 * * 1-5 cd /Users/brian/tw-stock-agent && /usr/bin/python3 broker_cache.py >> /Users/brian/tw-stock-agent/logs/broker_cache.log 2>&1\n"
)

print("=== 即將寫入的 crontab 內容 ===")
print(crontab_content)

subprocess.run(["crontab", "-"], input=crontab_content, text=True, check=True)
print("✅ crontab 設定完成")
