# data_fetcher.py - FinMind / 證交所 / 新聞資料抓取
#
# 原則（見 docs/SPEC.md）：
# - 抓不到就回空值，由呼叫端記進「今日缺少」，不在這裡補數字。
# - 證交所被擋時常回 HTTP 200 的擋頁或沒有轉址目標的 307，不能只看狀態碼。
# - 不要寫死 verify=certifi.where()：Routine 的雲端主機走代理，CA 由
#   REQUESTS_CA_BUNDLE 指定，寫死會讓 TLS 驗證失敗。

import html
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from config import FINMIND_TOKEN

LOG_DIR = Path(__file__).parent / "logs"

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_TOP20_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
TWSE_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
CNYES_NEWS_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"

FINMIND_SLEEP_SEC = 0.5
USER_AGENT = "Mozilla/5.0 (taiwan-stock-report)"

# 法人名稱：FinMind 回英文代號，證交所和舊資料是中文，兩種都要認得。
# 外資自營商不另外計算：證交所註明「外資自營商買賣金額已計入自營商買賣金額」，
# 加進來會重複計算。
INSTITUTION_ALIASES = {
    "foreign": {"foreign_investor", "外資", "外資及陸資", "外資及陸資(不含外資自營商)"},
    "sitc": {"investment_trust", "投信"},
    "dealer": {"dealer_self", "dealer_hedging", "dealer", "自營商", "自營商(自行買賣)", "自營商(避險)"},
    "ignore": {"foreign_dealer_self", "外資自營商", "total", "合計"},
}


class TwseBlocked(Exception):
    """證交所回擋頁、無轉址目標的 3xx，或不是 JSON 的內容。"""


def _log_error(msg):
    LOG_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open(LOG_DIR / f"error_{date_str}.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] {msg}\n")


def _date_range(end_date, days):
    """end_date 往前抓 days 個交易日；多抓一些日曆天以確保足夠交易日。"""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = end - timedelta(days=int(days * 1.6) + 10)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _fetch(dataset, stock_id=None, start_date=None, end_date=None, retries=3):
    params = {"dataset": dataset}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if stock_id:
        params["data_id"] = stock_id
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    if FINMIND_TOKEN:
        # header 是 FinMind 目前的驗證方式；token / api_token 參數是舊寫法（原程式用 api_token），一併帶上以相容
        params["token"] = FINMIND_TOKEN
        params["api_token"] = FINMIND_TOKEN

    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != 200:
                raise ValueError(f"API status {data.get('status')}: {data.get('msg')}")
            return pd.DataFrame(data.get("data", []))
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        finally:
            time.sleep(FINMIND_SLEEP_SEC)

    _log_error(f"[{dataset}] {stock_id} 失敗（{retries}次重試後）: {last_error}")
    return pd.DataFrame()


def get_stock_price(stock_id, end_date, days=65):
    start, end = _date_range(end_date, days)
    df = _fetch("TaiwanStockPrice", stock_id=stock_id, start_date=start, end_date=end)
    return normalize_price_df(df, days)


def normalize_price_df(df, days=65):
    needed = ["date", "open", "max", "min", "close", "Trading_Volume"]
    if df.empty or not all(c in df.columns for c in needed):
        return pd.DataFrame()
    df = df[needed].copy()
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = df["volume"].fillna(0) / 1000  # 股 → 張
    # 沒成交的日子 FinMind 會給 0 價，不能拿去算指標
    df = df[df["close"] > 0]
    df = df.sort_values("date").reset_index(drop=True)
    return df.tail(days).reset_index(drop=True)


def get_institutional_investors(stock_id, end_date, days=10):
    start, end = _date_range(end_date, days)
    df = _fetch("TaiwanStockInstitutionalInvestors", stock_id=stock_id, start_date=start, end_date=end)
    return normalize_institutional_df(df)


def normalize_institutional_df(df):
    """回傳每日一列：date, foreign, sitc, dealer（淨買超股數），以及無法辨識的名稱。"""
    empty = pd.DataFrame(columns=["date", "foreign", "sitc", "dealer"])
    if df.empty or not {"date", "name", "buy", "sell"}.issubset(df.columns):
        return empty, []
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)
    df["net"] = df["buy"] - df["sell"]

    lookup = {}
    for cat, names in INSTITUTION_ALIASES.items():
        for n in names:
            lookup[n.lower()] = cat
    df["cat"] = df["name"].astype(str).str.strip().str.lower().map(lookup)
    unknown = sorted(df.loc[df["cat"].isna(), "name"].astype(str).unique().tolist())

    df = df[df["cat"].isin(["foreign", "sitc", "dealer"])]
    if df.empty:
        return empty, unknown
    pivot = df.pivot_table(index="date", columns="cat", values="net", aggfunc="sum").fillna(0)
    for cat in ["foreign", "sitc", "dealer"]:
        if cat not in pivot.columns:
            pivot[cat] = 0.0
    pivot = pivot[["foreign", "sitc", "dealer"]].sort_index().reset_index()
    pivot.columns.name = None
    return pivot, unknown


def get_taiex_price(end_date):
    start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=15)).strftime("%Y-%m-%d")
    df = _fetch("TaiwanStockPrice", stock_id="TAIEX", start_date=start, end_date=end_date)
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def latest_date_of(df):
    if df is None or df.empty:
        return None
    return pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")


def get_stock_info():
    """官方股票清單：{代號: 名稱}。抓不到回空 dict。"""
    df = _fetch("TaiwanStockInfo")
    if df.empty or not {"stock_id", "stock_name"}.issubset(df.columns):
        return {}
    if "date" in df.columns:
        df = df.sort_values("date")
    names = {}
    for sid, name in zip(df["stock_id"].astype(str), df["stock_name"].astype(str)):
        names[sid.strip()] = name.strip()
    return names


# ─── 證交所 ───

BLOCK_MARKERS = ("安全性考量", "無法呈現", "Request Rejected", "The requested URL was rejected")


def _twse_get_json(url, params=None, retries=2):
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=20, allow_redirects=False,
                                headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            text = resp.text or ""
            if 300 <= resp.status_code < 400:
                raise TwseBlocked(f"HTTP {resp.status_code}（轉址到 {resp.headers.get('Location') or '無目標'}）")
            if any(m in text for m in BLOCK_MARKERS):
                raise TwseBlocked(f"HTTP {resp.status_code} 但內容是擋頁")
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                raise TwseBlocked(f"HTTP {resp.status_code} 但內容不是 JSON：{text[:60]!r}")
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(3)
    raise last_error


def get_top_volume_stocks(date, top_n=10):
    """
    當日成交量前 top_n 名普通股（排除 ETF）。
    回傳 (清單, 錯誤訊息或 None)；清單元素是 (代號, 名稱, 成交張數)。
    """
    try:
        data = _twse_get_json(TWSE_TOP20_URL, {"response": "json", "date": date.replace("-", "")})
    except Exception as e:
        _log_error(f"MI_INDEX20 失敗: {e}")
        return [], f"證交所成交量排行抓不到（{e}）"
    if data.get("stat") != "OK" or "data" not in data:
        return [], f"證交所成交量排行沒有資料（stat={data.get('stat')}）"
    return parse_top_volume(data, top_n), None


def parse_top_volume(data, top_n=10):
    fields = data.get("fields", [])
    rows = data.get("data", [])
    df = pd.DataFrame(rows, columns=fields)
    code_col = next((c for c in df.columns if "代號" in c), None)
    name_col = next((c for c in df.columns if "名稱" in c), None)
    vol_col = next((c for c in df.columns if "成交股數" in c), None)
    if not code_col or not vol_col:
        return []
    out = []
    for _, row in df.iterrows():
        sid = str(row[code_col]).strip()
        if not re.fullmatch(r"\d{4}", sid) or sid.startswith("00"):
            continue
        vol = pd.to_numeric(str(row[vol_col]).replace(",", ""), errors="coerce")
        if pd.isna(vol):
            continue
        name = str(row[name_col]).strip() if name_col else sid
        out.append((sid, name, int(vol) // 1000))
    out.sort(key=lambda x: x[2], reverse=True)
    return out[:top_n]


def get_twse_closes():
    """
    證交所上市股票最新一日收盤價（核對用）。
    回傳 ({代號: 收盤價}, 錯誤訊息或 None)。上櫃股不在這份清單裡。
    """
    try:
        data = _twse_get_json(TWSE_DAY_ALL_URL)
    except Exception as e:
        _log_error(f"STOCK_DAY_ALL 失敗: {e}")
        return {}, f"證交所收盤價抓不到（{e}）"
    if not isinstance(data, list):
        return {}, "證交所收盤價格式無法辨識"
    closes = {}
    for row in data:
        code = str(row.get("Code", "")).strip()
        price = pd.to_numeric(str(row.get("ClosingPrice", "")).replace(",", ""), errors="coerce")
        if code and not pd.isna(price):
            closes[code] = float(price)
    return closes, None


# ─── 新聞（鉅亨網台股新聞）───

def _clean_text(text, limit=200):
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def get_tw_stock_news(limit=30, hours=24):
    """回傳 (新聞清單, 錯誤訊息或 None)。每則有 id, title, summary, time, url。"""
    try:
        resp = requests.get(CNYES_NEWS_URL, params={"limit": limit}, timeout=20,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log_error(f"新聞抓取失敗: {e}")
        return [], f"新聞抓不到（{type(e).__name__}）"
    return parse_cnyes_news(data, hours=hours), None


def parse_cnyes_news(data, hours=24, now_ts=None):
    items = (data.get("items") or {}).get("data") or []
    now_ts = now_ts or time.time()
    out = []
    for item in items:
        news_id = str(item.get("newsId", "")).strip()
        title = _clean_text(item.get("title"), 120)
        if not news_id or not title:
            continue
        ts = item.get("publishAt") or 0
        if ts and now_ts - float(ts) > hours * 3600:
            continue
        out.append({
            "id": f"N{len(out) + 1}",
            "source_id": news_id,
            "title": title,
            "summary": _clean_text(item.get("summary"), 200),
            "time": datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M") if ts else "",
            "url": f"https://news.cnyes.com/news/id/{news_id}",
        })
    return out


def dump_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
