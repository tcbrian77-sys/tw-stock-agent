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
TPEX_CLASSIC = "https://www.tpex.org.tw/www/zh-tw"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tw-stock-agent/1.0)"}


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


def _to_price(x) -> float:
    """OHLC 價格欄位專用：TWSE/TPEx 用 "--" 表示當日無成交價可供計算
    （例如零股/鉅額交易，有成交量但沒有一般的開高低收），不是真的 0 元。
    回傳 NaN 而非 0.0，避免被技術指標誤判成暴跌。"""
    if x in (None, "", "--"):
        return float("nan")
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else float("nan")
    except ValueError:
        return float("nan")


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
            "open": _to_price(r[5]),
            "max": _to_price(r[6]),
            "min": _to_price(r[7]),
            "close": _to_price(r[8]),
            "spread": sign * _to_float(r[10]),
            "Trading_turnover": _to_float(r[3]),
        })
    return pd.DataFrame(rows)


def _ad_to_roc_slash(ad_date: str) -> str:
    """西元日期（"2026-10-13"）轉民國日期斜線格式（"115/10/13"）。"""
    y, m, d = ad_date.split("-")
    return f"{int(y) - 1911}/{m}/{d}"


def _fetch_tpex_prices_for_date(target_date: str) -> pd.DataFrame:
    """TPEx 全市場收盤資料，用「日期查詢」（classic dailyQuotes），
    可查任何歷史日期，不是只有「最新」。
    """
    payload = _get(f"{TPEX_CLASSIC}/afterTrading/dailyQuotes", params={
        "date": _ad_to_roc_slash(target_date), "response": "json",
    })
    table = next(
        (t for t in payload.get("tables", []) if "上櫃股票行情" in str(t.get("title", ""))),
        None,
    )
    if table is None or not table.get("data"):
        return pd.DataFrame()

    rows = []
    for r in table["data"]:
        rows.append({
            "date": target_date,
            "stock_id": r[0],
            "Trading_Volume": _to_float(r[8]),
            "Trading_money": _to_float(r[9]),
            "open": _to_price(r[4]),
            "max": _to_price(r[5]),
            "min": _to_price(r[6]),
            "close": _to_price(r[2]),
            "spread": _to_float(r[3]),
            "Trading_turnover": _to_float(r[10]),
        })
    return pd.DataFrame(rows)


def fetch_prices_for_date(target_date: str, max_wait_minutes: int = 0,
                           poll_interval_sec: int = 60) -> pd.DataFrame:
    """TWSE + TPEx 全市場指定日期收盤資料，合併為 FinMind 相容欄位。

    兩邊都用「日期查詢」（TWSE: MI_INDEX?date=，TPEx: dailyQuotes?date=），
    可查任何歷史交易日，不是只有「最新」，也不會有兩邊資料對不起來的問題。

    若兩邊都「乾淨地」查無資料（沒有例外，單純沒有資料），代表這天大概
    是假日/休市日，直接回傳空的 DataFrame，不會當作錯誤、也不會重試——
    呼叫端應該把這個情況當作「這天沒有交易」處理，而不是觸發昂貴的
    FinMind 逐檔查詢備援（之前這裡會誤判成「來源失敗」，導致對假日
    白白重試逐檔查詢數小時）。

    只有「查詢日期＝今天」時才會啟用等待重試（max_wait_minutes > 0），
    因為今天的資料預期一定會出現，只是可能還沒公佈；歷史日期則不等待，
    空值就直接回傳。
    """
    deadline = time.time() + max_wait_minutes * 60

    while True:
        twse_df = _fetch_twse_prices_for_date(target_date)
        tpex_df = _fetch_tpex_prices_for_date(target_date)

        if not twse_df.empty and not tpex_df.empty:
            return pd.concat([twse_df, tpex_df], ignore_index=True)

        if max_wait_minutes <= 0 or time.time() >= deadline:
            if twse_df.empty and tpex_df.empty:
                return pd.DataFrame()  # 兩邊都沒有 → 當作假日/休市日
            raise RuntimeError(
                f"TWSE/TPEx {target_date} 資料不完整"
                f"（TWSE {'有' if not twse_df.empty else '無'}資料，"
                f"TPEx {'有' if not tpex_df.empty else '無'}資料）"
            )

        print(f"   ⏳ {target_date} 資料尚未齊備，{poll_interval_sec} 秒後重試...")
        time.sleep(poll_interval_sec)


def fetch_prices_today(max_wait_minutes: int = 30, poll_interval_sec: int = 60) -> pd.DataFrame:
    """今日收盤資料，fetch_prices_for_date 的便利包裝。"""
    return fetch_prices_for_date(date.today().isoformat(), max_wait_minutes, poll_interval_sec)


# ═══════════════════════════════════════════════════════════════
# 三大法人買賣超（取代 FinMind TaiwanStockInstitutionalInvestorsBuySell）
# ═══════════════════════════════════════════════════════════════

def fetch_institutional_today() -> pd.DataFrame:
    return fetch_institutional_for_date(date.today().isoformat())


def fetch_institutional_for_date(target_date: str) -> pd.DataFrame:
    """TWSE + TPEx 全市場指定日期三大法人買賣超，合併為 FinMind 相容欄位
    （date, stock_id, name, buy, sell, net）。
    name 分類：Foreign_Investor, Foreign_Dealer_Self, Investment_Trust,
               Dealer_self, Dealer_Hedging（與 FinMind 命名一致）。

    注意：TWSE（T86）支援任何歷史日期查詢，但 TPEx 目前只找到「最新」
    端點（tpex_3insti_daily_trading），沒有日期參數。所以只有在查詢
    「今天」時才會加入 TPEx 的資料；查歷史日期時，TPEx 部分會略過
    （否則會把「今天」的 TPEx 資料誤標成歷史日期，造成資料錯誤）。
    """
    rows = []

    # --- TWSE（classic 查詢，date 參數用西元 YYYYMMDD，不是民國年；支援任何歷史日期）---
    payload = _get(f"{TWSE_CLASSIC}/fund/T86", params={
        "date": target_date.replace("-", ""), "selectType": "ALL", "response": "json",
    })
    if not payload.get("data"):
        return pd.DataFrame()  # 查無資料，視為假日/休市日
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
            rows.append({"date": target_date, "stock_id": sid, "name": name,
                         "buy": b, "sell": s, "net": b - s})

    # --- TPEx（openapi，只有「今天」，沒有歷史查詢）---
    if target_date == date.today().isoformat():
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
                rows.append({"date": target_date, "stock_id": sid, "name": name,
                             "buy": b, "sell": s, "net": b - s})

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════
# 融資融券（取代 FinMind TaiwanStockMarginPurchaseShortSale）
# ═══════════════════════════════════════════════════════════════

def fetch_margin_today() -> pd.DataFrame:
    """今日融資融券餘額，fetch_margin_for_date 的便利包裝。"""
    return fetch_margin_for_date(date.today().isoformat())


def fetch_margin_for_date(target_date: str) -> pd.DataFrame:
    """TWSE + TPEx 全市場指定日期融資融券餘額，合併為 FinMind 相容欄位。
    兩邊都用「日期查詢」（classic），可查任何歷史交易日。單位為「張」
    （1張=1000股），與 FinMind 原本的資料單位一致。
    """
    rows = []

    twse_payload = _get(f"{TWSE_CLASSIC}/marginTrading/MI_MARGN", params={
        "date": target_date.replace("-", ""), "selectType": "ALL", "response": "json",
    })
    twse_table = next(
        (t for t in twse_payload.get("tables", []) if "彙總" in str(t.get("title", ""))),
        None,
    )
    if twse_table is None or not twse_table.get("data"):
        return pd.DataFrame()  # 查無資料，視為假日/休市日
    for r in twse_table["data"]:
        rows.append({
            "date": target_date,
            "stock_id": r[0].strip(),
            "MarginPurchaseBuy":              _to_float(r[2]),
            "MarginPurchaseSell":             _to_float(r[3]),
            "MarginPurchaseCashRepayment":    _to_float(r[4]),
            "MarginPurchaseYesterdayBalance": _to_float(r[5]),
            "MarginPurchaseTodayBalance":     _to_float(r[6]),
            "MarginPurchaseLimit":            _to_float(r[7]),
            "ShortSaleBuy":                   _to_float(r[8]),
            "ShortSaleSell":                  _to_float(r[9]),
            "ShortSaleCashRepayment":         _to_float(r[10]),
            "ShortSaleYesterdayBalance":      _to_float(r[11]),
            "ShortSaleTodayBalance":          _to_float(r[12]),
            "ShortSaleLimit":                 _to_float(r[13]),
            "OffsetLoanAndShort":             _to_float(r[14]),
            "Note":                           r[15] if len(r) > 15 else "",
        })

    tpex_payload = _get(f"{TPEX_CLASSIC}/margin/balance", params={
        "date": _ad_to_roc_slash(target_date), "response": "json",
    })
    for t in tpex_payload.get("tables", []):
        for r in t.get("data", []):
            rows.append({
                "date": target_date,
                "stock_id": r[0].strip(),
                "MarginPurchaseBuy":              _to_float(r[3]),
                "MarginPurchaseSell":             _to_float(r[4]),
                "MarginPurchaseCashRepayment":    _to_float(r[5]),
                "MarginPurchaseYesterdayBalance": _to_float(r[2]),
                "MarginPurchaseTodayBalance":     _to_float(r[6]),
                "MarginPurchaseLimit":            _to_float(r[9]),
                "ShortSaleBuy":                   _to_float(r[12]),
                "ShortSaleSell":                  _to_float(r[11]),
                "ShortSaleCashRepayment":         _to_float(r[13]),
                "ShortSaleYesterdayBalance":      _to_float(r[10]),
                "ShortSaleTodayBalance":          _to_float(r[14]),
                "ShortSaleLimit":                 _to_float(r[17]),
                "OffsetLoanAndShort":             _to_float(r[18]),
                "Note":                           r[19] if len(r) > 19 else "",
            })

    return pd.DataFrame(rows)
