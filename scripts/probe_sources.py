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

results = []   # (狀態, 項目, 說明)；狀態：OK / MISMATCH / UNREACHABLE


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


@probe("FinMind 法人買賣超")
def p_inst(date):
    raw = f._fetch("TaiwanStockInstitutionalInvestors", stock_id="2330", start_date="2026-10-01", end_date=date)
    if raw.empty:
        return record("UNREACHABLE", "法人買賣超", "沒有資料")
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

    print("\n=== 總結 ===")
    for status, name, detail in results:
        print(f"{status:12s} {name}：{detail}")
    if any(s == "UNREACHABLE" for s, _, _ in results):
        return 2
    return 1 if any(s == "MISMATCH" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
