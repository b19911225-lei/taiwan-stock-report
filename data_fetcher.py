# data_fetcher.py - FinMind 資料抓取

import time
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from config import FINMIND_TOKEN

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

BASE_URL = "https://api.finmindtrade.com/api/v4/data"


def _get_date_range(days):
    end = datetime.today()
    # 多抓一些日曆天以確保足夠交易日
    start = end - timedelta(days=int(days * 1.8))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _fetch(dataset, stock_id=None, start_date=None, end_date=None, retries=3):
    params = {
        "api_token": FINMIND_TOKEN,
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date,
    }
    if stock_id:
        params["data_id"] = stock_id

    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != 200:
                raise ValueError(f"API status {data.get('status')}: {data.get('msg')}")
            df = pd.DataFrame(data.get("data", []))
            return df
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    # 所有 retry 都失敗才回傳空值
    _log_error(f"[{dataset}] {stock_id} 失敗（{retries}次重試後）: {last_error}")
    return pd.DataFrame()


def _log_error(msg):
    date_str = datetime.today().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"error_{date_str}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] {msg}\n")


def get_stock_price(stock_id, days=65):
    start, end = _get_date_range(days)
    df = _fetch("TaiwanStockPrice", stock_id=stock_id, start_date=start, end_date=end)
    if df.empty:
        return df
    df = df[["date", "open", "max", "min", "close", "Trading_Volume"]].copy()
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) / 1000  # 轉換為張
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].apply(
        pd.to_numeric, errors="coerce"
    )
    time.sleep(1)
    return df.tail(days).reset_index(drop=True)


def get_institutional_investors(stock_id, days=10):
    start, end = _get_date_range(days + 5)
    df = _fetch("TaiwanStockInstitutionalInvestors", stock_id=stock_id, start_date=start, end_date=end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 轉換買賣超欄位
    for col in ["buy", "sell"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "buy" in df.columns and "sell" in df.columns:
        df["net"] = df["buy"] - df["sell"]
    time.sleep(1)
    return df.tail(days * 3).reset_index(drop=True)


def get_market_overview(date=None):
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    df = _fetch("TaiwanStockTotalReturnIndex", start_date=start, end_date=date)
    if df.empty:
        # 備用：嘗試加權指數
        df = _fetch("TaiwanStockPrice", stock_id="TAIEX", start_date=start, end_date=date)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    time.sleep(1)
    return df


def get_taiex_price(date=None):
    """取得大盤加權指數資料"""
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    df = _fetch("TaiwanStockPrice", stock_id="TAIEX", start_date=start, end_date=date)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    time.sleep(1)
    return df


def is_trading_day(date=None):
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")
    df = get_stock_price("2330", days=5)
    if df.empty:
        return False
    latest = df.iloc[-1]["date"]
    target = pd.to_datetime(date)
    result = latest.date() == target.date()
    if not result:
        _log_error(f"{date} 非交易日，最新資料日期為 {latest.date()}")
    return result


def get_latest_trading_date():
    """取得最近一個有資料的交易日"""
    df = get_stock_price("2330", days=5)
    if df.empty:
        return datetime.today().strftime("%Y-%m-%d")
    return df.iloc[-1]["date"].strftime("%Y-%m-%d")


def get_top_volume_stocks(date, top_n=10):
    """
    抓取指定日期成交量前 top_n 名的普通股（排除 ETF）。
    使用 TWSE 公開 API，回傳 [(stock_id, name, volume_lots), ...] 清單。
    """
    import certifi

    date_fmt = date.replace("-", "")  # 20260512
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    params = {"response": "json", "date": date_fmt}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=20, verify=certifi.where())
            resp.raise_for_status()
            data = resp.json()

            if data.get("stat") != "OK" or "data" not in data:
                _log_error(f"TWSE MI_INDEX20 非預期回應: {data.get('stat')}")
                return []

            fields = data.get("fields", [])
            rows   = data.get("data", [])
            df = pd.DataFrame(rows, columns=fields)

            # 欄位：證券代號、證券名稱、成交股數...
            code_col   = [c for c in df.columns if "代號" in c or "code" in c.lower()]
            name_col   = [c for c in df.columns if "名稱" in c or "name" in c.lower()]
            vol_col    = [c for c in df.columns if "成交股數" in c or "股數" in c or "volume" in c.lower()]

            if not code_col:
                _log_error(f"TWSE 回傳欄位無法辨識: {fields}")
                return []

            df = df.rename(columns={
                code_col[0]: "stock_id",
                name_col[0] if name_col else code_col[0]: "name",
                vol_col[0]  if vol_col  else code_col[0]: "vol_str",
            })

            # 清理成交股數（含逗號）
            if "vol_str" in df.columns:
                df["vol"] = pd.to_numeric(
                    df["vol_str"].astype(str).str.replace(",", ""), errors="coerce"
                ).fillna(0)
            else:
                df["vol"] = 0

            # 只保留 4 碼普通股，排除 ETF（00 開頭）
            df = df[df["stock_id"].astype(str).str.match(r"^\d{4}$")]
            df = df[~df["stock_id"].astype(str).str.startswith("00")]
            df = df.sort_values("vol", ascending=False).head(top_n)

            result = []
            for _, row in df.iterrows():
                sid  = str(row["stock_id"]).strip()
                name = str(row.get("name", sid)).strip()
                vol  = int(row["vol"]) // 1000  # 股 → 張
                result.append((sid, name, vol))

            time.sleep(1)
            return result

        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                _log_error(f"get_top_volume_stocks 失敗: {e}")
                return []
