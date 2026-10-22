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
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import FINMIND_TOKEN, TIMEZONE

LOG_DIR = Path(__file__).parent / "logs"

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_TOP20_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
TWSE_DAILY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
CNYES_CATEGORY_URL = "https://api.cnyes.com/media/api/v1/newslist/category/{}"
CNYES_NEWS_URL = CNYES_CATEGORY_URL.format("tw_stock")

# 新聞分類（2026-10-22 資料源檢查確認可用）：(鉅亨網分類, 報告上的標籤, 則數)
NEWS_CATEGORIES = (("tw_quo", "盤勢", 10), ("tw_stock", "台股", 30), ("wd_stock", "國際", 10))
# 分析師 EPS／目標價預估：AI 撰寫規則不能引用，放進去只會佔篇幅
NEWS_SKIP_PATTERNS = ("鉅亨速報 - Factset",)
# 股票名稱是常見詞，新聞標題出現不代表講這家公司（只用代號或新聞的股票標籤配對）
COMMON_WORD_NAMES = {"全新", "統一", "大成", "中興", "國產", "台灣", "新光", "大同"}

# 美股（報告上叫「國際股市」）：FinMind USStockPrice，2026-10-22 資料源檢查確認可用
US_MARKETS = (("^DJI", "道瓊"), ("^GSPC", "S&P 500"), ("^IXIC", "那斯達克"),
              ("^SOX", "費城半導體"), ("TSM", "台積電 ADR"))

FINMIND_SLEEP_SEC = 0.5
USER_AGENT = "Mozilla/5.0 (taiwan-stock-report)"

# 法人名稱：FinMind 回英文代號（實測：Foreign_Investor、Foreign_Dealer_Self、Investment_Trust、
# Dealer_self、Dealer_Hedging），證交所是中文，兩種都要認得。
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

    msg = f"[{dataset}] {stock_id} 失敗（{retries}次重試後）: {last_error}"
    _log_error(msg)
    print(f"  [FinMind] {msg}", file=sys.stderr, flush=True)   # 讓 Routine / Actions 的 log 看得到原因
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


# 個股三大法人買賣超。原程式用的 TaiwanStockInstitutionalInvestors 實測回 HTTP 422（資料集不存在），
# 所以從上線起一筆法人資料都沒拿到過（2026-10-18 scripts/probe_sources.py 確認）。
INSTITUTIONAL_DATASET = "TaiwanStockInstitutionalInvestorsBuySell"


def get_institutional_investors(stock_id, end_date, days=10):
    start, end = _date_range(end_date, days)
    df = _fetch(INSTITUTIONAL_DATASET, stock_id=stock_id, start_date=start, end_date=end)
    return normalize_institutional_df(df)


def _institution_lookup():
    return {n.lower(): cat for cat, names in INSTITUTION_ALIASES.items() for n in names}


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
    df["cat"] = df["name"].astype(str).str.strip().str.lower().map(_institution_lookup())
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


def get_market_institutional(date):
    """
    整體市場三大法人買賣超（億元）：{"foreign", "sitc", "dealer"}。
    分類和個股一樣：外資不含外資自營商、自營商 = 自行買賣 + 避險。回傳 (結果或 None, 錯誤訊息或 None)。
    """
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    df = _fetch("TaiwanStockTotalInstitutionalInvestors", start_date=start, end_date=date)
    if df.empty or not {"date", "name", "buy", "sell"}.issubset(df.columns):
        return None, "整體三大法人買賣超抓不到"
    df = df[df["date"].astype(str).str[:10] == date]
    if df.empty:
        return None, f"整體三大法人買賣超還沒有 {date} 的資料"
    lookup = _institution_lookup()
    out = {"foreign": 0.0, "sitc": 0.0, "dealer": 0.0}
    matched = set()
    for _, row in df.iterrows():
        cat = lookup.get(str(row["name"]).strip().lower())
        if cat in out:
            out[cat] += float(row["buy"] or 0) - float(row["sell"] or 0)
            matched.add(cat)
    if matched != set(out):
        names = sorted(df["name"].astype(str).unique())
        return None, f"整體三大法人類別認不得（{'、'.join(names)}），請更新 INSTITUTION_ALIASES"
    return {k: round(v / 1e8, 2) for k, v in out.items()}, None


def get_us_markets(date):
    """
    報告日「之前」最近一個美股交易日的收盤和漲跌幅（台股收盤時，美股停在前一晚）。
    回傳 ([{id, name, date, close, chg_pct}], 錯誤訊息或 None)。
    """
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    out, missing = [], []
    for sid, name in US_MARKETS:
        df = _fetch("USStockPrice", stock_id=sid, start_date=start, end_date=date)
        if df.empty or not {"date", "Close"}.issubset(df.columns):
            missing.append(name)
            continue
        df = df[df["date"].astype(str).str[:10] < date].sort_values("date")
        closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(closes) < 2 or not closes.iloc[-2]:
            missing.append(name)
            continue
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        out.append({"id": sid, "name": name, "date": str(df.loc[closes.index[-1], "date"])[:10],
                    "close": round(last, 2), "chg_pct": round((last - prev) / prev * 100, 2)})
    return out, (f"美股資料缺：{'、'.join(missing)}" if missing else None)


def get_stock_news(stock_id, date, limit=4):
    """FinMind 個股新聞（報告日當天，最新的在前）。每則有 title, source, url。抓不到回空清單。"""
    df = _fetch("TaiwanStockNews", stock_id=stock_id, start_date=date)
    if df.empty or "title" not in df.columns:
        return []
    if "date" in df.columns:
        df = df[df["date"].astype(str).str[:10] <= date].sort_values("date", ascending=False)
    out, seen = [], set()
    for _, row in df.iterrows():
        title = _clean_text(row.get("title"), 120)
        source = str(row.get("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        if not title or title[:20] in seen:
            continue
        seen.add(title[:20])
        link = row.get("link")
        out.append({"title": title, "source": source, "url": str(link) if pd.notna(link) and link else ""})
        if len(out) >= limit:
            break
    return out


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


def parse_twse_date(value):
    """證交所的日期可能是民國 1151016、115/10/16，或西元 20261016、2026-10-16。回傳 YYYY-MM-DD 或 None。"""
    digits = re.sub(r"\D", "", str(value or ""))
    try:
        if len(digits) == 7:        # 民國年
            return f"{int(digits[:3]) + 1911:04d}-{digits[3:5]}-{digits[5:7]}"
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    except ValueError:
        pass
    return None


def _num(text):
    v = pd.to_numeric(str(text).replace(",", "").strip(), errors="coerce")
    return None if pd.isna(v) else float(v)


def get_twse_daily(date):
    """
    證交所指定日期的全部收盤行情（MI_INDEX type=ALLBUT0999；上櫃股不在裡面）。
    回傳 ({代號: {name, close, change, chg_pct, volume_lots, value_100m}},
          {up, limit_up, down, limit_down, flat}, 錯誤訊息或 None)。
    STOCK_DAY_ALL 在 17:50 常常還是前一天的資料；這份可以指定日期。
    """
    try:
        data = _twse_get_json(TWSE_DAILY_URL, {"response": "json", "date": date.replace("-", ""),
                                               "type": "ALLBUT0999"})
    except Exception as e:
        _log_error(f"MI_INDEX 失敗: {e}")
        return {}, {}, f"證交所全部收盤行情抓不到（{e}）"
    if data.get("stat") != "OK":
        return {}, {}, f"證交所全部收盤行情沒有 {date} 的資料（stat={data.get('stat')}）"
    quotes, breadth = parse_twse_daily(data)
    if not quotes:
        return {}, breadth, "證交所全部收盤行情格式無法辨識"
    return quotes, breadth, None


def parse_twse_daily(data):
    quotes, breadth = {}, {}
    for table in data.get("tables") or []:
        fields = table.get("fields") or []
        rows = table.get("data") or []
        if "證券代號" in fields and "收盤價" in fields:
            idx = {f: i for i, f in enumerate(fields)}
            sign_col = next((f for f in fields if f.startswith("漲跌(")), None)
            for row in rows:
                sid = str(row[idx["證券代號"]]).strip()
                close = _num(row[idx["收盤價"]])
                if not sid or close is None or close <= 0:      # 「--」是沒有成交
                    continue
                diff = _num(row[idx["漲跌價差"]]) if "漲跌價差" in idx else None
                sign = re.sub(r"<[^>]+>", "", str(row[idx[sign_col]])).strip() if sign_col else ""
                if diff is None or sign not in ("+", "-", ""):
                    change = None                               # 「X」是除權息，漲跌不是和前一天收盤比
                else:
                    change = -diff if sign == "-" else (diff if sign == "+" else 0.0)
                prev = close - change if change is not None else None
                vol = _num(row[idx["成交股數"]]) if "成交股數" in idx else None
                value = _num(row[idx["成交金額"]]) if "成交金額" in idx else None
                quotes[sid] = {
                    "name": str(row[idx["證券名稱"]]).strip() if "證券名稱" in idx else sid,
                    "close": close,
                    "change": None if change is None else round(change, 2),
                    "chg_pct": round(change / prev * 100, 2) if prev else None,
                    "volume_lots": int(vol // 1000) if vol else 0,
                    "value_100m": round(value / 1e8, 2) if value else 0.0,
                }
        elif "漲跌證券數" in str(table.get("title") or "") and "股票" in fields:
            col = fields.index("股票")
            for row in rows:
                m = re.match(r"([\d,]+)(?:\(([\d,]+)\))?", str(row[col]).strip())
                if not m:
                    continue
                n = int(m.group(1).replace(",", ""))
                limit = int(m.group(2).replace(",", "")) if m.group(2) else None
                label = str(row[0])
                if label.startswith("上漲"):
                    breadth.update(up=n, limit_up=limit)
                elif label.startswith("下跌"):
                    breadth.update(down=n, limit_down=limit)
                elif label.startswith("持平"):
                    breadth["flat"] = n
    return quotes, breadth


def get_twse_closes():
    """
    證交所上市股票最新一日收盤價（核對用）。上櫃股不在這份清單裡。
    回傳 ({代號: 收盤價}, 資料日期 YYYY-MM-DD 或 None（格式裡沒有日期）, 錯誤訊息或 None)。
    """
    try:
        data = _twse_get_json(TWSE_DAY_ALL_URL)
    except Exception as e:
        _log_error(f"STOCK_DAY_ALL 失敗: {e}")
        return {}, None, f"證交所收盤價抓不到（{e}）"
    if not isinstance(data, list):
        return {}, None, "證交所收盤價格式無法辨識"
    closes, dates = {}, set()
    for row in data:
        code = str(row.get("Code", "")).strip()
        price = pd.to_numeric(str(row.get("ClosingPrice", "")).replace(",", ""), errors="coerce")
        if code and not pd.isna(price):
            closes[code] = float(price)
        d = parse_twse_date(row.get("Date"))
        if d:
            dates.add(d)
    return closes, (max(dates) if dates else None), None


# ─── 新聞（鉅亨網台股新聞）───

def _clean_text(text, limit=200):
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def get_tw_stock_news(hours=24, categories=NEWS_CATEGORIES):
    """
    鉅亨網幾個分類的新聞（盤勢、台股、國際），合併、去重、最新的在前。
    回傳 (新聞清單, 錯誤訊息或 None)。每則有 id, title, summary, time, url, category, stocks。
    """
    items, failed = [], []
    for cat, label, n in categories:
        try:
            resp = requests.get(CNYES_CATEGORY_URL.format(cat), params={"limit": n}, timeout=20,
                                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            items += [(label, item) for item in (resp.json().get("items") or {}).get("data") or []]
        except Exception as e:
            _log_error(f"新聞抓取失敗（{cat}）: {e}")
            failed.append(f"{label}（{type(e).__name__}）")
    if not items:
        return [], f"新聞抓不到：{'、'.join(failed)}"
    news = parse_cnyes_items(items, hours=hours)
    return news, (f"部分新聞抓不到：{'、'.join(failed)}" if failed else None)


def parse_cnyes_news(data, hours=24, now_ts=None, category="台股"):
    items = (data.get("items") or {}).get("data") or []
    return parse_cnyes_items([(category, i) for i in items], hours=hours, now_ts=now_ts)


def parse_cnyes_items(items, hours=24, now_ts=None):
    """items：[(分類標籤, 鉅亨網新聞), ...]。"""
    now_ts = now_ts or time.time()
    rows, seen = [], set()
    for category, item in items:
        news_id = str(item.get("newsId", "")).strip()
        title = _clean_text(item.get("title"), 120)
        if not news_id or not title or news_id in seen:
            continue
        if any(p in title for p in NEWS_SKIP_PATTERNS):
            continue
        ts = float(item.get("publishAt") or 0)
        if ts and now_ts - ts > hours * 3600:
            continue
        seen.add(news_id)
        rows.append((ts, category, news_id, title, item))
    rows.sort(key=lambda r: r[0], reverse=True)
    out = []
    for ts, category, news_id, title, item in rows:
        out.append({
            "id": f"N{len(out) + 1}",
            "source_id": news_id,
            "title": title,
            "summary": _clean_text(item.get("summary"), 150),
            # 一律用台北時間（雲端主機和 Actions 是 UTC）
            "time": datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE)).strftime("%m/%d %H:%M") if ts else "",
            "url": f"https://news.cnyes.com/news/id/{news_id}",
            "category": category,
            "stocks": sorted(set(re.findall(r"(?<!\d)(\d{4})(?!\d)", str(item.get("stock") or "")))),
        })
    return out


def related_news_ids(news, stock_id, name, all_names=()):
    """
    和這檔股票有關的新聞編號：新聞的股票標籤有這個代號、標題有代號，或標題有股票名稱
    （名稱去掉 *-KY；名稱是常見詞的只認代號；標題裡的「南亞科」不算「南亞」）。
    all_names：所有股票名稱，用來排除包含這個名稱的其他股票。
    """
    short = re.sub(r"\*?-KY$|\*", "", str(name or "")).strip()
    use_name = len(short) >= 2 and short not in COMMON_WORD_NAMES
    longer = sorted({re.sub(r"\*?-KY$|\*", "", str(n)).strip() for n in all_names} - {short}, key=len, reverse=True)
    longer = [n for n in longer if short and short in n]
    code = re.compile(rf"(?<!\d){re.escape(stock_id)}(?!\d)")

    def name_hit(title):
        for n in longer:
            title = title.replace(n, "")
        return short in title

    return [n["id"] for n in news
            if stock_id in n.get("stocks", []) or code.search(n["title"]) or (use_name and name_hit(n["title"]))]


def dump_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
