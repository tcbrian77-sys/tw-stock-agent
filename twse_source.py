"""
twse_source.py
用途：免費且不需帳號的全市場資料來源，取代 FinMind 付費批次查詢。
      資料來自台灣證券交易所（TWSE）與證券櫃買中心（TPEx）官方 Open API，
      兩者皆為公開資料、無需金鑰、無每日限額，且一次呼叫即可拿到全市場（當日）。

      TWSE 只有「今天」的快照（STOCK_DAY_ALL / MI_MARGN 等），沒有歷史日期參數；
      如需補過去某一天，需改用 classic 查詢（www.twse.com.tw/rwd/...?date=）。
      本模組目前只實作「今天」版本，因為 stock_cache.py / broker_cache.py
      本來就是每天抓當天收盤資料，剛好對應。

      輸出欄位刻意對齊原本 FinMind 資料集的欄位名稱，讓下游程式（scan.py、
      monitor.py、morning_brief.py...）不需要修改就能繼續使用。
"""

import time
import requests
import pandas as pd
from datetime import date

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TWSE_CLASSIC = "https://www.twse.com.tw/rwd/zh"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tw-stock-agent/1.0)"}


def _roc_to_ad(roc_date: str) -> str:
    """民國日期（例："1151016"）轉西元日期字串（"2026-10-16"）。"""
    roc_date = str(roc_date)
    y = int(roc_date[:3]) + 1911
    m = roc_date[3:5]
    d = roc_date[5:7]
    return f"{y}-{m}-{d}"


def _get(url: str, params: dict = None) -> list:
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _to_float(x) -> float:
    if x in (None, "", "--"):
        return 0.0
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# 收盤價（取代 FinMind TaiwanStockPrice）
# ═══════════════════════════════════════════════════════════════

def _fetch_twse_prices_for_date(target_date: str) -> pd.DataFrame:
    """TWSE 全市場收盤資料，用「日期查詢」而非「最新快照」。

    注意：openapi.twse.com.tw 的 STOCK_DAY_ALL 是「最新快取」，實測發現
    它更新的時間比 TPEx 的對應資料晚，常常收盤後好幾個小時還停留在前一天，
    導致兩邊合併時日期對不齊。改用這個 classic 的 date-indexed 查詢
    （www.twse.com.tw/rwd/.../MI_INDEX?date=），同一天的資料確定後就查得到，
    不會有「快取還沒更新」的問題。
    """
    ymd = target_date.replace("-", "")
    payload = _get(f"{TWSE_CLASSIC}/afterTrading/MI_INDEX", params={
        "date": ymd, "type": "ALLBUT0999", "response": "json",
    })
    table = next(
        (t for t in payload.get("tables", []) if "每日收盤行情" in str(t.get("title", ""))),
        None,
    )
    if table is None or not table.get("data"):
        return pd.DataFrame()

    rows = []
    for r in table["data"]:
        tag = r[9]
        sign = -1 if "green" in tag else (1 if "red" in tag else 0)
        rows.append({
            "date": target_date,
            "stock_id": r[0],
            "Trading_Volume": _to_float(r[2]),
            "Trading_money": _to_float(r[4]),
            "open": _to_float(r[5]),
            "max": _to_float(r[6]),
            "min": _to_float(r[7]),
            "close": _to_float(r[8]),
            "spread": sign * _to_float(r[10]),
            "Trading_turnover": _to_float(r[3]),
        })
    return pd.DataFrame(rows)


def _fetch_tpex_prices() -> pd.DataFrame:
    rows = [{
        "date": _roc_to_ad(r["Date"]),
        "stock_id": r["SecuritiesCompanyCode"],
        "Trading_Volume": _to_float(r["TradingShares"]),
        "Trading_money": _to_float(r["TransactionAmount"]),
        "open": _to_float(r["Open"]),
        "max": _to_float(r["High"]),
        "min": _to_float(r["Low"]),
        "close": _to_float(r["Close"]),
        "spread": _to_float(r["Change"]),
        "Trading_turnover": _to_float(r["TransactionNumber"]),
    } for r in _get(f"{TPEX_OPENAPI}/tpex_mainboard_daily_close_quotes")]
    return pd.DataFrame(rows)


def fetch_prices_today(max_wait_minutes: int = 30, poll_interval_sec: int = 60) -> pd.DataFrame:
    """TWSE + TPEx 全市場今日收盤資料，合併為 FinMind 相容欄位。

    TWSE 用日期查詢（MI_INDEX?date=），資料一旦公佈即可查到，沒有「最新
    快取還沒更新」的問題。TPEx 只有「最新快照」端點，實測通常很快就會更新
    成當日資料，但保留一個等待重試機制以防萬一（預設最多等 30 分鐘）。
    若兩邊日期還是對不起來，丟出例外讓呼叫端決定如何處理（例如改用其他
    資料源），避免把不同日期的資料混存成一個檔案。
    """
    today = date.today().isoformat()
    deadline = time.time() + max_wait_minutes * 60

    twse_df = _fetch_twse_prices_for_date(today)
    if twse_df.empty:
        raise RuntimeError(f"TWSE 尚無 {today} 的收盤資料（可能太早執行，稍後再試）")

    while True:
        tpex_df = _fetch_tpex_prices()
        tpex_latest = tpex_df["date"].max() if not tpex_df.empty else None

        if tpex_latest == today:
            return pd.concat([twse_df, tpex_df], ignore_index=True)

        if time.time() >= deadline:
            raise RuntimeError(
                f"TPEx 今日資料尚未更新（最新：{tpex_latest}，預期：{today}），放棄等待"
            )

        print(f"   ⏳ TPEx 日期尚未更新（最新={tpex_latest}），{poll_interval_sec} 秒後重試...")
        time.sleep(poll_interval_sec)


# ═══════════════════════════════════════════════════════════════
# 三大法人買賣超（取代 FinMind TaiwanStockInstitutionalInvestorsBuySell）
# ═══════════════════════════════════════════════════════════════

def fetch_institutional_today() -> pd.DataFrame:
    """TWSE + TPEx 全市場今日三大法人買賣超，合併為 FinMind 相容欄位
    （date, stock_id, name, buy, sell, net）。
    name 分類：Foreign_Investor, Foreign_Dealer_Self, Investment_Trust,
               Dealer_self, Dealer_Hedging（與 FinMind 命名一致）。
    """
    today = date.today().isoformat()
    rows = []

    # --- TWSE（classic 查詢，date 參數用西元 YYYYMMDD，不是民國年）---
    payload = _get(f"{TWSE_CLASSIC}/fund/T86", params={
        "date": date.today().strftime("%Y%m%d"), "selectType": "ALL", "response": "json",
    })
    if not payload.get("data"):
        raise RuntimeError(f"TWSE T86 今日（{today}）資料尚未發布（可能太早執行，稍後再試）")
    for r in payload.get("data", []):
        sid = r[0].strip()
        cats = [
            ("Foreign_Investor",   r[2], r[3]),
            ("Foreign_Dealer_Self", r[5], r[6]),
            ("Investment_Trust",   r[8], r[9]),
            ("Dealer_self",        r[12], r[13]),
            ("Dealer_Hedging",     r[15], r[16]),
        ]
        for name, buy, sell in cats:
            b, s = _to_float(buy), _to_float(sell)
            rows.append({"date": today, "stock_id": sid, "name": name,
                         "buy": b, "sell": s, "net": b - s})

    # --- TPEx（openapi，只有今天）---
    for r in _get(f"{TPEX_OPENAPI}/tpex_3insti_daily_trading"):
        sid = r["SecuritiesCompanyCode"]
        cats = [
            ("Foreign_Investor",
             r.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy"),
             r.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell")),
            ("Foreign_Dealer_Self",
             r.get("Foreign Dealers-Total Buy"), r.get("Foreign Dealers-TotalSell")),
            ("Investment_Trust",
             r.get("SecuritiesInvestmentTrustCompanies-TotalBuy"),
             r.get("SecuritiesInvestmentTrustCompanies-TotalSell")),
            ("Dealer_self",
             r.get("Dealers-TotalBuy"), r.get("Dealers-TotalSell")),
        ]
        for name, buy, sell in cats:
            b, s = _to_float(buy), _to_float(sell)
            rows.append({"date": today, "stock_id": sid, "name": name,
                         "buy": b, "sell": s, "net": b - s})

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════
# 融資融券（取代 FinMind TaiwanStockMarginPurchaseShortSale）
# ═══════════════════════════════════════════════════════════════

def fetch_margin_today() -> pd.DataFrame:
    """TWSE + TPEx 全市場今日融資融券餘額，合併為 FinMind 相容欄位。"""
    today = date.today().isoformat()
    rows = []

    twse_margin = _get(f"{TWSE_OPENAPI}/exchangeReport/MI_MARGN")
    if not twse_margin:
        raise RuntimeError("TWSE MI_MARGN 回傳空值（可能太早執行，稍後再試）")
    for r in twse_margin:
        rows.append({
            "date": today,
            "stock_id": r.get("股票代號", "").strip(),
            "MarginPurchaseBuy":              _to_float(r.get("融資買進")),
            "MarginPurchaseSell":             _to_float(r.get("融資賣出")),
            "MarginPurchaseCashRepayment":    _to_float(r.get("融資現金償還")),
            "MarginPurchaseYesterdayBalance": _to_float(r.get("融資前日餘額")),
            "MarginPurchaseTodayBalance":     _to_float(r.get("融資今日餘額")),
            "MarginPurchaseLimit":            _to_float(r.get("融資限額")),
            "ShortSaleBuy":                   _to_float(r.get("融券買進")),
            "ShortSaleSell":                  _to_float(r.get("融券賣出")),
            "ShortSaleCashRepayment":         _to_float(r.get("融券現券償還")),
            "ShortSaleYesterdayBalance":      _to_float(r.get("融券前日餘額")),
            "ShortSaleTodayBalance":          _to_float(r.get("融券今日餘額")),
            "ShortSaleLimit":                 _to_float(r.get("融券限額")),
            "OffsetLoanAndShort":             _to_float(r.get("資券互抵")),
            "Note":                           r.get("註記", ""),
        })

    for r in _get(f"{TPEX_OPENAPI}/tpex_mainboard_margin_balance"):
        rows.append({
            "date": today,
            "stock_id": r.get("SecuritiesCompanyCode", "").strip(),
            "MarginPurchaseBuy":              _to_float(r.get("MarginPurchase")),
            "MarginPurchaseSell":             _to_float(r.get("MarginSales")),
            "MarginPurchaseCashRepayment":    _to_float(r.get("CashRedemption")),
            "MarginPurchaseYesterdayBalance": _to_float(r.get("MarginPurchaseBalancePreviousDay")),
            "MarginPurchaseTodayBalance":     _to_float(r.get("MarginPurchaseBalance")),
            "MarginPurchaseLimit":            _to_float(r.get("MarginPurchaseQuota")),
            "ShortSaleBuy":                   _to_float(r.get("ShortConvering")),
            "ShortSaleSell":                  _to_float(r.get("ShortSale")),
            "ShortSaleCashRepayment":         _to_float(r.get("StockRedemption")),
            "ShortSaleYesterdayBalance":      _to_float(r.get("ShortSaleBalancePreviousDay")),
            "ShortSaleTodayBalance":          _to_float(r.get("ShortSaleBalance")),
            "ShortSaleLimit":                 _to_float(r.get("ShortSaleQuota")),
            "OffsetLoanAndShort":             _to_float(r.get("Offsetting")),
            "Note":                           r.get("Note", ""),
        })

    return pd.DataFrame(rows)
