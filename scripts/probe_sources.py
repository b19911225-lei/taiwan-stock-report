# scripts/probe_sources.py - 資料源檢查
#
# 對每個外部資料源抓一次真實資料，確認格式和程式的假設一致（欄位名稱、法人類別、日期格式…）。
# 不會發送 Telegram 訊息、不會推送 git。輸出是給人看的報告，最後一行是總結。
#
# 用法：python scripts/probe_sources.py [YYYY-MM-DD]   （日期預設是加權指數最新的交易日）
# 結束代碼：0 全部通過｜1 有項目和程式的假設不符｜2 有資料源連不上

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import data_fetcher as f  # noqa: E402
import verify  # noqa: E402
from config import STOCK_UNIVERSE  # noqa: E402

results = []   # (狀態, 項目, 說明)；狀態：OK / MISMATCH / UNREACHABLE / INFO（只是資訊，不影響結果）


def record(status, name, detail):
    results.append((status, name, detail))
    print(f"[{status}] {name}：{detail}", flush=True)


def probe(name):
    def deco(fn):
        def run(*args):
            print(f"\n=== {name} ===", flush=True)
            try:
                fn(*args)
            except Exception as e:
                record("UNREACHABLE", name, f"{type(e).__name__}: {e}")
                traceback.print_exc()
        return run
    return deco


@probe("FinMind 加權指數")
def p_taiex(date):
    df = f.get_taiex_price(date)
    if df.empty:
        return record("UNREACHABLE", "加權指數", "沒有資料")
    print("欄位：", list(df.columns))
    print(df.tail(2).to_string())
    money = [c for c in ("Trading_money", "Trading_Money") if c in df.columns]
    record("OK" if money else "MISMATCH", "加權指數成交金額欄位",
           f"找到 {money}" if money else f"沒有 Trading_money，現有欄位 {list(df.columns)}")
    record("OK", "加權指數日期", f"最新 {f.latest_date_of(df)}")


@probe("FinMind 個股價格")
def p_price(date):
    raw = f._fetch("TaiwanStockPrice", stock_id="2330", start_date="2026-09-01", end_date=date)
    print("原始欄位：", list(raw.columns))
    df = f.normalize_price_df(raw)
    record("OK" if not df.empty else "MISMATCH", "個股價格欄位",
           f"正規化後 {len(df)} 筆，最新 {f.latest_date_of(df)}" if not df.empty else "正規化後是空的")


def _finmind_raw(dataset, **params):
    """直接呼叫 FinMind，印出 status 和 msg（_fetch 失敗時只會回空表）。"""
    q = {"dataset": dataset, **params}
    headers = {}
    if f.FINMIND_TOKEN:
        q["token"] = f.FINMIND_TOKEN
        headers["Authorization"] = f"Bearer {f.FINMIND_TOKEN}"
    r = requests.get(f.FINMIND_URL, params=q, headers=headers, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {"status": None, "msg": r.text[:200]}
    rows = body.get("data") or []
    print(f"  {dataset}: HTTP {r.status_code} status={body.get('status')} msg={body.get('msg')!r} 筆數={len(rows)}")
    return rows


@probe("FinMind 法人買賣超")
def p_inst(date):
    rows = _finmind_raw(f.INSTITUTIONAL_DATASET, data_id="2330", start_date="2026-10-01", end_date=date)
    if not rows:
        return record("UNREACHABLE", "法人買賣超", f"{f.INSTITUTIONAL_DATASET} 沒有資料")
    raw = f._fetch(f.INSTITUTIONAL_DATASET, stock_id="2330", start_date="2026-10-01", end_date=date)
    print("原始欄位：", list(raw.columns))
    names = sorted(raw["name"].astype(str).unique().tolist()) if "name" in raw.columns else []
    print("name 欄的值：", names)
    print(raw.tail(6).to_string())
    daily, unknown = f.normalize_institutional_df(raw)
    print("正規化後：\n", daily.tail(3).to_string())
    if unknown:
        record("MISMATCH", "法人類別", f"認不得：{unknown}（請加進 INSTITUTION_ALIASES）")
    elif daily.empty:
        record("MISMATCH", "法人類別", f"一個都認不得：{names}")
    else:
        record("OK", "法人類別", f"全部認得：{names}")
    record("OK", "法人資料日期", f"最新 {f.latest_date_of(daily)}")


@probe("FinMind 官方股票清單")
def p_info(date):
    raw = f._fetch("TaiwanStockInfo")
    print("原始欄位：", list(raw.columns), "筆數：", len(raw))
    info = f.get_stock_info()
    if not info:
        return record("MISMATCH", "官方股票清單", "解析後是空的")
    not_found, mismatched = verify.verify_universe(info)
    record("OK", "官方股票清單", f"{len(info)} 檔")
    for sid in sorted(STOCK_UNIVERSE):
        print(f"  {sid} {info.get(sid, '（查無）')}")
    if not_found or mismatched:
        record("MISMATCH", "股票池代號", f"查無 {not_found}；名稱不符 {mismatched}")
    else:
        record("OK", "股票池代號", f"{len(STOCK_UNIVERSE)} 檔代號和名稱都一致")


@probe("證交所成交量排行 MI_INDEX20")
def p_top(date):
    data = f._twse_get_json(f.TWSE_TOP20_URL, {"response": "json", "date": date.replace("-", "")})
    print("stat：", data.get("stat"), "fields：", data.get("fields"))
    rows = f.parse_top_volume(data, 10)
    record("OK" if rows else "MISMATCH", "成交量排行", f"解析出 {len(rows)} 檔：{rows[:3]}")


@probe("證交所收盤價 STOCK_DAY_ALL")
def p_day_all(date):
    data = f._twse_get_json(f.TWSE_DAY_ALL_URL)
    first = data[0] if isinstance(data, list) and data else {}
    print("第一筆：", first)
    closes, d, err = f.get_twse_closes()
    if err:
        return record("UNREACHABLE", "證交所收盤價", err)
    record("OK" if closes else "MISMATCH", "證交所收盤價", f"{len(closes)} 檔，2330 = {closes.get('2330')}")
    record("OK" if d else "MISMATCH", "證交所收盤價日期",
           f"資料日期 {d}" if d else f"找不到日期欄位，現有欄位 {list(first)}")


@probe("鉅亨網新聞")
def p_news(date):
    resp = requests.get(f.CNYES_NEWS_URL, params={"limit": 5}, timeout=20, headers={"User-Agent": f.USER_AGENT})
    print("HTTP", resp.status_code)
    data = resp.json()
    print("最上層欄位：", list(data))
    items = (data.get("items") or {}).get("data") or []
    print("第一則欄位：", list(items[0]) if items else None)
    news = f.parse_cnyes_news(data, hours=24 * 7)
    record("OK" if news else "MISMATCH", "新聞", f"解析出 {len(news)} 則：{[n['title'][:20] for n in news[:2]]}")


@probe("Telegram bot")
def p_telegram(date):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return record("MISMATCH", "Telegram", "沒有設定 TELEGRAM_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)   # 只查 bot 資料，不發訊息
    ok = r.ok and r.json().get("ok")
    record("OK" if ok else "MISMATCH", "Telegram token", "有效" if ok else f"HTTP {r.status_code}")


def _explore(name, fn):
    """評估中的資料源：只印出格式，失敗記 INFO，不影響檢查結果。"""
    print(f"\n=== （評估）{name} ===", flush=True)
    try:
        record("INFO", name, fn())
    except Exception as e:
        record("INFO", name, f"失敗 {type(e).__name__}: {e}")


def p_explore(date):
    from datetime import datetime, timedelta
    week_ago = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

    def mi_index():
        data = f._twse_get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                                {"response": "json", "date": date.replace("-", ""), "type": "ALLBUT0999"})
        print("stat：", data.get("stat"), "最上層欄位：", list(data))
        for i, t in enumerate(data.get("tables") or []):
            print(f"  table[{i}] title={t.get('title')!r} fields={t.get('fields')} 筆數={len(t.get('data') or [])}")
            for row in (t.get("data") or [])[:2]:
                print("     ", row)
        return f"stat={data.get('stat')} tables={len(data.get('tables') or [])}"

    def total_inst():
        rows = _finmind_raw("TaiwanStockTotalInstitutionalInvestors", start_date=week_ago, end_date=date)
        for r in rows[-8:]:
            print("     ", r)
        return f"{len(rows)} 筆"

    def stock_news():
        rows = _finmind_raw("TaiwanStockNews", data_id="2330", start_date=date)
        for r in rows[:3]:
            print("     ", r)
        return f"{len(rows)} 筆"

    def us_index():
        out = []
        for sid in ("^GSPC", "^IXIC", "^SOX", "^DJI", "TSM", "SPY"):
            rows = _finmind_raw("USStockPrice", data_id=sid, start_date=week_ago, end_date=date)
            if rows:
                print(f"  {sid} 最後一筆：", rows[-1])
            out.append(f"{sid}:{len(rows)}")
        return " ".join(out)

    def info_4577():
        raw = f._fetch("TaiwanStockInfo")
        rows = raw[raw["stock_id"].astype(str) == "4577"].to_dict("records") if not raw.empty else []
        print("  4577：", rows)
        df = f.get_stock_price("4577", date)
        print(df.tail(2).to_string() if not df.empty else "  4577 沒有價格")
        return f"官方清單 {len(rows)} 筆，價格 {len(df)} 筆"

    def cnyes_categories():
        out = []
        for cat in ("headline", "tw_stock", "wd_stock", "us_stock", "tw_quo", "tw_macro"):
            r = requests.get(f"https://api.cnyes.com/media/api/v1/newslist/category/{cat}",
                             params={"limit": 5}, timeout=20, headers={"User-Agent": f.USER_AGENT})
            items = (r.json().get("items") or {}).get("data") or [] if r.ok else []
            print(f"  {cat}: HTTP {r.status_code}", [i.get("title", "")[:30] for i in items[:3]])
            if items:
                print("    第一則欄位：", sorted(items[0]))
            out.append(f"{cat}:{r.status_code}/{len(items)}")
        return " ".join(out)

    def tpex_quotes():
        r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                         timeout=30, headers={"User-Agent": f.USER_AGENT, "Accept": "application/json"})
        data = r.json() if r.ok else []
        print(f"  HTTP {r.status_code} 筆數 {len(data)}")
        for row in data[:2]:
            print("     ", row)
        hit = [row for row in data if str(row.get("SecuritiesCompanyCode", "")).strip() == "4577"]
        print("  4577：", hit)
        return f"HTTP {r.status_code} {len(data)} 筆"

    for name, fn in (("證交所全部收盤行情 MI_INDEX", mi_index), ("FinMind 整體三大法人", total_inst),
                     ("FinMind 個股新聞", stock_news), ("FinMind 美股指數", us_index),
                     ("4577 基本資料", info_4577), ("鉅亨網其他分類", cnyes_categories),
                     ("櫃買中心收盤行情", tpex_quotes)):
        _explore(name, fn)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    if not date:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        date = f.latest_date_of(f.get_taiex_price(today)) or today
    print(f"檢查日期：{date}")
    for p in (p_taiex, p_price, p_inst, p_info, p_top, p_day_all, p_news, p_telegram):
        p(date)
    p_explore(date)

    print("\n=== 總結 ===")
    for status, name, detail in results:
        print(f"{status:12s} {name}：{detail}")
    if any(s == "UNREACHABLE" for s, _, _ in results):
        return 2
    return 1 if any(s == "MISMATCH" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
