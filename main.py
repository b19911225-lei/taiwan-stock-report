# main.py - 主程式入口

import sys
import io
import time
from datetime import datetime
from pathlib import Path

# 在 Windows 上強制 stdout 使用 UTF-8，避免 cp950 UnicodeEncodeError
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import STOCK_UNIVERSE, TARGET_STOCK_COUNT, MIN_RR_RATIO
from data_fetcher import (
    get_stock_price, get_institutional_investors,
    get_taiex_price, is_trading_day, get_latest_trading_date,
    get_top_volume_stocks
)
from analyzer import apply_filters, calc_price_targets, calc_theme_avg_chg
from reporter import build_report, send_to_telegram, STOCK_NAMES

REPORT_DIR = Path(__file__).parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def main(dry_run=False):
    today = datetime.today().strftime("%Y-%m-%d")
    print(f"\n{'='*50}")
    print(f"  台股選股報告系統  {today}")
    print(f"{'='*50}")

    # 確認交易日
    trading = is_trading_day(today)
    if not trading:
        latest = get_latest_trading_date()
        print(f"[!] {today} 非交易日，改用最近交易日：{latest}")
        today = latest

    # 取得大盤資料
    print("\n[1/4] 取得大盤資料...")
    market_df = get_taiex_price(today)
    if market_df is not None and not market_df.empty:
        row = market_df.iloc[-1]
        print(f"  大盤日期：{row.get('date','N/A')}  收盤：{row.get('close','N/A')}")
    else:
        print("  [!] 大盤資料取得失敗，繼續執行")
        market_df = None

    # 動態合併當日成交量前10名
    print("\n[1.5/4] 抓取當日成交量前10名...")
    dynamic_universe = dict(STOCK_UNIVERSE)  # 複製固定清單
    top_vol = get_top_volume_stocks(today, top_n=10)
    new_count = 0
    for sid, name, vol in top_vol:
        if sid not in dynamic_universe:
            dynamic_universe[sid] = ["T0_當日熱門"]
            new_count += 1
            if sid not in STOCK_NAMES:
                STOCK_NAMES[sid] = name  # 動態補充名稱
        print(f"  量排行: {sid} {name} {vol:,}張{'  [新加入]' if sid not in STOCK_UNIVERSE else ''}")
    print(f"  新加入股票池: {new_count} 支，總計: {len(dynamic_universe)} 支")

    # 第一階段：取得所有股票原始資料（計算主題漲跌幅用）
    print(f"\n[2/4] 抓取 {len(dynamic_universe)} 支股票資料...")
    raw_results = {}
    for idx, (stock_id, themes) in enumerate(dynamic_universe.items(), 1):
        print(f"  [{idx:2d}/{len(dynamic_universe)}] {stock_id}...", end=" ", flush=True)
        price_df = get_stock_price(stock_id, days=65)
        if price_df.empty or len(price_df) < 5:
            print("[x] 無資料")
            continue
        latest = price_df.iloc[-1]
        prev   = price_df.iloc[-2] if len(price_df) >= 2 else latest
        close  = latest["close"]
        prev_c = prev["close"]
        chg    = (close - prev_c) / prev_c * 100 if prev_c else 0
        raw_results[stock_id] = {
            "price_df": price_df,
            "today_chg": round(chg, 2),
            "close": close,
        }
        print(f"[OK] 收盤:{close} 漲跌:{chg:+.2f}%")

    # 計算主題平均漲跌
    theme_avg_chg = calc_theme_avg_chg({k: v for k, v in raw_results.items()})

    # 第二階段：完整篩選
    print(f"\n[3/4] 執行選股篩選...")
    results = {}
    ok_count = 0
    rr_ok_count = 0

    for stock_id, raw in raw_results.items():
        price_df = raw["price_df"]
        inst_df  = get_institutional_investors(stock_id, days=10)

        filter_result = apply_filters(stock_id, price_df, inst_df, theme_avg_chg,
                                      stock_themes=dynamic_universe)
        filter_result["today_chg"] = raw["today_chg"]
        filter_result["close"]     = raw["close"]

        if filter_result["passed"]:
            ok_count += 1
            high_60d = price_df["high"].tail(60).max()
            targets  = calc_price_targets(raw["close"], high_60d)
            if targets and targets["rr_ratio"] >= MIN_RR_RATIO:
                filter_result["targets"] = targets
                rr_ok_count += 1
                print(f"  [++] {stock_id} 評分:{filter_result['score']} RR:{targets['rr_ratio']:.1f} 訊號:{filter_result['signals']}")
            else:
                filter_result["targets"] = None
                print(f"  [~]  {stock_id} 通過篩選但 RR不足")
        else:
            reason = filter_result.get("exclude_reason", "")
            print(f"  [--] {stock_id} [{reason}]")

        results[stock_id] = filter_result

    print(f"\n  成功取得資料：{len(raw_results)} 支")
    print(f"  通過篩選：{ok_count} 支")
    print(f"  RR>={MIN_RR_RATIO} 進入名單：{rr_ok_count} 支")

    # 建立報告
    print(f"\n[4/4] 產生並推送報告...")
    messages = build_report(results, market_df, today, dynamic_universe=dynamic_universe)

    # 儲存報告檔案
    report_file = REPORT_DIR / f"report_{today}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        for i, msg in enumerate(messages):
            f.write(f"=== 訊息 {i+1} ===\n{msg}\n\n")
    print(f"  報告已儲存：{report_file}")

    if not dry_run:
        send_results = send_to_telegram(messages)
        for i, status in enumerate(send_results):
            print(f"  Telegram 訊息{i+1}：{status}")
        push_ok = all("SUCCESS" in s for s in send_results)
    else:
        print("  [dry_run 模式，跳過推送]")
        push_ok = None

    print(f"\n{'='*50}")
    print(f"  執行完成")
    print(f"  資料日期：{today}")
    print(f"  入選名單：{rr_ok_count} 支")
    status_str = "成功" if push_ok else ("失敗" if push_ok is False else "跳過(dry-run)")
    print(f"  推送狀態：{status_str}")
    print(f"{'='*50}\n")

    return results


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
