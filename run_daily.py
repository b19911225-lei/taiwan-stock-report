# run_daily.py - 每日流程入口（Routine 和 GitHub Actions 共用）
#
# Routine（主要）：
#   python3.13 run_daily.py check-date --wait-minutes 8   今天有沒有交易資料；休市就發一行通知
#   python3.13 run_daily.py collect                       抓資料、算好所有數字 → history/日期/picks.json、ai_input.json
#   （Routine 的 Claude 照 docs/AI_WRITING_RULES.md 寫 history/日期/ai_output.json）
#   python3.13 run_daily.py validate-ai                   檢查 AI 文字，不合格就改到合格
#   python3.13 run_daily.py finalize                      核對、組報告、送 Telegram、存回 GitHub
#
# GitHub Actions 手動備援 / 本機：
#   python run_daily.py all --ai api     （--ai none 只送程式產生的報告）
#
# 共用參數：--date YYYY-MM-DD（補跑某天）、--dry-run（不送 Telegram、不存回 GitHub）、
#           --force（已經發過也重發）、--no-git（不同步歷史分支）
#
# 結束代碼：0 成功｜1 AI 文字未通過檢查｜2 錯誤｜3 Telegram 發送失敗｜10 非交易日｜11 今天已經發過

import argparse
import io
import sys
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

# 在 Windows 上強制 stdout 使用 UTF-8，避免 cp950 UnicodeEncodeError
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import analyzer
import data_fetcher as fetch
import history
import performance
import reporter
import verify
from ai_validation import validate
from config import (
    STOCK_UNIVERSE, STOCK_NAMES, TIMEZONE, TOP_VOLUME_COUNT, HOT_THEME, MIN_RR_RATIO,
    TARGET_STOCK_COUNT, DEBATE_STOCK_COUNT, PERFORMANCE_LOOKBACK_DAYS, DATA_WAIT_INTERVAL_SEC,
)
from quality import DataQuality

EXIT_OK, EXIT_AI_INVALID, EXIT_ERROR, EXIT_SEND_FAILED = 0, 1, 2, 3
EXIT_NOT_TRADING, EXIT_ALREADY_SENT = 10, 11

SIGNAL_MEANINGS = {
    "M1": "今日量大於 20 日均量 1.5 倍",
    "M2": "今日量大於 5 日均量 1.3 倍",
    "M3": "外資連續買超 3 日以上",
    "M4": "投信 + 自營商買超近 3 日逐日增加",
    "M5": "KD 近 3 日黃金交叉",
    "M6": "RSI 上穿 50 或大於 60",
}

PICK_FIELDS = [
    "close", "today_chg", "chg_5d", "score", "score_breakdown", "signals", "momentum_count",
    "vol_vs_20d", "vol_vs_5d", "inst_available", "foreign_buy_days", "foreign_net_5d_lots",
    "sitc_net_5d_lots", "dealer_net_5d_lots", "rsi14", "kd_cross", "ma5", "ma20", "atr", "high_60d",
]


def log(msg):
    print(msg, flush=True)


def today_tw():
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def resolve_trading_date(today, wait_minutes=0):
    """回傳 (加權指數最新日期, 加權指數資料)。等到 today 的資料出現或時間到為止。"""
    deadline = time.time() + wait_minutes * 60
    while True:
        taiex = fetch.get_taiex_price(today)
        latest = fetch.latest_date_of(taiex)
        if latest == today or time.time() >= deadline:
            return latest, taiex
        log(f"  加權指數最新是 {latest}，還沒有 {today} 的資料，{DATA_WAIT_INTERVAL_SEC} 秒後再試")
        time.sleep(max(1, min(DATA_WAIT_INTERVAL_SEC, deadline - time.time())))


def market_summary(taiex):
    if taiex is None or taiex.empty:
        return {"close": None}
    last = taiex.iloc[-1]
    close = float(last["close"])
    out = {"close": round(close, 2), "chg": None, "chg_pct": None, "money_100m": None}
    if len(taiex) >= 2:
        prev = float(taiex.iloc[-2]["close"])
        if prev:
            out["chg"] = round(close - prev, 2)
            out["chg_pct"] = round((close - prev) / prev * 100, 2)
    for col in ("Trading_money", "Trading_Money"):
        if col in taiex.columns and float(last[col] or 0) > 0:
            out["money_100m"] = round(float(last[col]) / 1e8, 0)
            break
    return out


def collect(date, taiex):
    q = DataQuality()

    stock_info = fetch.get_stock_info()
    if not stock_info:
        q.add_note("官方股票清單抓不到，股票名稱用內建表")
    else:
        not_found, mismatched = verify.verify_universe(stock_info)
        if not_found:
            q.add_note(f"股票池有 {len(not_found)} 個代號在官方清單查無：{'、'.join(not_found)}")
        if mismatched:
            q.add_note(f"股票池有 {len(mismatched)} 檔名稱和官方不符（{'、'.join(s for s, _, _ in mismatched)}），"
                       f"請執行 python verify.py universe")

    universe = {sid: list(themes) for sid, themes in STOCK_UNIVERSE.items()}
    top_vol, err = fetch.get_top_volume_stocks(date, TOP_VOLUME_COUNT)
    if err:
        q.add_note(err)
    hot_names = {}
    for sid, name, vol in top_vol:
        hot_names[sid] = name
        if sid not in universe:
            universe[sid] = [HOT_THEME]
        log(f"  量排行: {sid} {name} {vol:,}張")

    def name_of(sid):
        return stock_info.get(sid) or hot_names.get(sid) or STOCK_NAMES.get(sid) or sid

    # 過去推薦的股票也要抓價格，才能追蹤績效
    past = history.past_picks(date, PERFORMANCE_LOOKBACK_DAYS)
    extra = sorted({p["stock_id"] for _, picks in past for p in picks} - set(universe))

    log(f"\n抓取 {len(universe)} 檔股票池 + {len(extra)} 檔追蹤中的過去推薦...")
    price_lookup, raw = {}, {}
    for sid in list(universe) + extra:
        df = fetch.get_stock_price(sid, date, days=65)
        if df.empty or len(df) < 5:
            if sid in universe:
                q.missing_price.append(sid)
                log(f"  {sid} [x] 無資料")
            continue
        price_lookup[sid] = df
        if sid not in universe:
            continue
        if fetch.latest_date_of(df) != date:
            q.missing_price.append(sid)
            log(f"  {sid} [x] 沒有 {date} 的資料（最新 {fetch.latest_date_of(df)}）")
            continue
        close, prev = float(df.iloc[-1]["close"]), float(df.iloc[-2]["close"])
        raw[sid] = {"df": df, "close": close,
                    "today_chg": round((close - prev) / prev * 100, 2) if prev else 0.0}

    theme_avg = analyzer.calc_theme_avg_chg(raw)
    results, unknown_inst = {}, set()
    stats = {"universe": len(universe), "fetched": len(raw), "prefilter_passed": 0,
             "scored_passed": 0, "rr_ok": 0}
    for sid, r in raw.items():
        pre = analyzer.prefilter(r["df"])
        if not pre["passed"]:
            results[sid] = {"passed": False, "exclude_reason": pre["exclude_reason"]}
            continue
        stats["prefilter_passed"] += 1
        inst_daily, unknown = fetch.get_institutional_investors(sid, date, days=10)
        unknown_inst.update(unknown)
        inst = analyzer.calc_institutional_signals(inst_daily, date)
        if not inst["available"]:
            q.missing_inst.append(sid)
        res = analyzer.score(sid, pre, inst, theme_avg, universe)
        if res["passed"]:
            stats["scored_passed"] += 1
            res["targets"] = analyzer.calc_price_targets(res["close"], res["high_60d"], res["ma20"], res["atr"])
            res["rr_ok"] = bool(res["targets"]) and res["targets"]["rr_ratio"] >= MIN_RR_RATIO
            stats["rr_ok"] += int(res["rr_ok"])
            rr = res["targets"]["rr_ratio"] if res["targets"] else None
            log(f"  [{'++' if res['rr_ok'] else '~ '}] {sid} 評分:{res['score']} RR:{rr} 訊號:{res['signals']}")
        results[sid] = res

    if unknown_inst:
        q.add_note(f"法人資料有無法辨識的類別：{'、'.join(sorted(unknown_inst))}（請更新 INSTITUTION_ALIASES）")
    if stats["prefilter_passed"] and len(q.missing_inst) == stats["prefilter_passed"]:
        q.add_note("所有候選股的法人資料都缺，可能是資料源尚未更新")

    pick_ids = sorted((s for s, r in results.items() if r.get("rr_ok")),
                      key=lambda s: results[s]["score"], reverse=True)[:TARGET_STOCK_COUNT]
    picks = [{
        "stock_id": sid,
        "name": name_of(sid),
        "themes": [t.split("_", 1)[1] for t in universe[sid]],
        **{k: results[sid].get(k) for k in PICK_FIELDS},
        "targets": results[sid]["targets"],
    } for sid in pick_ids]

    themes = sorted(
        ({"theme": t, "name": t.split("_", 1)[1], "avg_chg_pct": round(v * 100, 2)} for t, v in theme_avg.items()),
        key=lambda x: x["avg_chg_pct"], reverse=True)

    news, err = fetch.get_tw_stock_news()
    if err:
        q.add_note(err)

    payload = {
        "date": date,
        "generated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
        "market": market_summary(taiex),
        "themes": themes,
        "picks": picks,
        "debate_ids": pick_ids[:DEBATE_STOCK_COUNT],
        "stats": stats,
        "exclude_counts": dict(Counter(r["exclude_reason"] for r in results.values() if not r.get("passed"))),
        "quality": q.to_dict(),
        "performance": performance.summarize(past, price_lookup, PERFORMANCE_LOOKBACK_DAYS),
        "news": news,
    }
    return payload, build_ai_input(payload)


def build_ai_input(payload):
    candidates = []
    for p in payload["picks"]:
        if p["stock_id"] not in payload["debate_ids"]:
            continue
        t = p["targets"]
        candidates.append({
            "stock_id": p["stock_id"], "name": p["name"], "themes": p["themes"],
            "close": p["close"], "today_chg_pct": p["today_chg"], "chg_5d_pct": p["chg_5d"],
            "score": p["score"], "score_breakdown": p["score_breakdown"],
            "signals": {s: SIGNAL_MEANINGS[s] for s in p["signals"]},
            "vol_vs_20d": p["vol_vs_20d"], "vol_vs_5d": p["vol_vs_5d"],
            "rsi14": p["rsi14"], "kd_cross": p["kd_cross"],
            "ma5": p["ma5"], "ma20": p["ma20"], "atr14": p["atr"], "high_60d": p["high_60d"],
            "institutional": {
                "available": p["inst_available"],
                "foreign_buy_days": p["foreign_buy_days"],
                "foreign_net_5d_lots": p["foreign_net_5d_lots"],
                "sitc_net_5d_lots": p["sitc_net_5d_lots"],
                "dealer_net_5d_lots": p["dealer_net_5d_lots"],
            },
            "program_levels": {
                "entry_low": t["entry_low"], "entry_high": t["entry_high"],
                "target1": t["target1"], "target_basis": t["target_basis"],
                "stop_loss": t["stop_loss"], "rr_ratio": t["rr_ratio"],
            },
        })
    return {
        "date": payload["date"],
        "instructions": "依 docs/AI_WRITING_RULES.md 撰寫 history/<date>/ai_output.json；數字只能照抄本檔。",
        "market": payload["market"],
        "themes": payload["themes"],
        "candidates": candidates,
        "other_picks": [{"stock_id": p["stock_id"], "name": p["name"]}
                        for p in payload["picks"] if p["stock_id"] not in payload["debate_ids"]],
        "news": [{k: n[k] for k in ("id", "title", "summary", "time")} for n in payload["news"]],
        "missing": payload["quality"],
    }


# ─── 指令 ───

def _pull(args):
    if args.no_git:
        history.HISTORY_DIR.mkdir(exist_ok=True)
        return
    ok, msg = history.sync_pull()
    log(f"歷史分支：{msg}")


def _push(args, message):
    if args.no_git or args.dry_run:
        return True
    ok, msg = history.sync_push(message)
    log(f"歷史分支：{msg}")
    return ok


def cmd_check_date(args):
    today = args.date or today_tw()
    _pull(args)
    if history.is_sent(today) and not args.force:
        log(f"ALREADY_SENT {today}：今天的報告已經發過（要重發請加 --force）")
        return EXIT_ALREADY_SENT
    weekday = datetime.strptime(today, "%Y-%m-%d").weekday()
    latest, _ = resolve_trading_date(today, args.wait_minutes if weekday < 5 else 0)
    if latest == today:
        log(f"TRADING_DAY {today}")
        return EXIT_OK
    if latest is None:
        log("ERROR 抓不到加權指數，FinMind 可能連不上（Routine 請確認網路白名單有 api.finmindtrade.com）")
        return EXIT_ERROR
    if weekday >= 5:
        log(f"NOT_TRADING {today} 是週末")
        return EXIT_NOT_TRADING

    marker = history.closed_marker(today)
    if marker.exists() and not args.force:
        log(f"NOT_TRADING {today}：休市通知已經發過")
        return EXIT_NOT_TRADING
    text = reporter.closed_message(today)
    log(f"NOT_TRADING {today}（加權指數最新 {latest}）")
    if args.dry_run:
        log(text)
        return EXIT_NOT_TRADING
    status = reporter.send_to_telegram([text])[0]
    log(f"  Telegram：{status}")
    if status == "SUCCESS":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f'{{"latest_trading_date": "{latest}"}}\n', encoding="utf-8")
        _push(args, f"closed {today}")
    return EXIT_NOT_TRADING


def cmd_collect(args):
    today = args.date or today_tw()
    _pull(args)
    if history.is_sent(today) and not args.force:
        log(f"ALREADY_SENT {today}")
        return EXIT_ALREADY_SENT
    latest, taiex = resolve_trading_date(today)
    if latest != today:
        log(f"NOT_TRADING {today}（加權指數最新 {latest}），請先跑 check-date")
        return EXIT_NOT_TRADING
    log(f"\n{'=' * 50}\n  台股選股資料收集  {today}\n{'=' * 50}")
    payload, ai_input = collect(today, taiex)
    history.save_json(today, "picks.json", payload)
    history.save_json(today, "ai_input.json", ai_input)
    s = payload["stats"]
    log(f"\n股票池 {s['universe']}｜有資料 {s['fetched']}｜趨勢通過 {s['prefilter_passed']}｜"
        f"評分通過 {s['scored_passed']}｜RR≥{MIN_RR_RATIO:g} {s['rr_ok']}")
    log(f"觀察清單 {len(payload['picks'])} 檔，辯證 {len(payload['debate_ids'])} 檔，新聞 {len(payload['news'])} 則")
    q = payload["quality"]
    log(f"今日缺少：價格 {q['missing_price']}；法人 {q['missing_inst']}；其他 {q['notes']}")
    log(f"COLLECTED history/{today}/picks.json、history/{today}/ai_input.json")
    return EXIT_OK


def cmd_validate_ai(args):
    date = args.date or today_tw()
    ai_input = history.load_json(date, "ai_input.json")
    ai_output = history.load_json(date, "ai_output.json")
    if ai_input is None:
        log(f"ERROR 找不到 history/{date}/ai_input.json，請先跑 collect")
        return EXIT_ERROR
    if ai_output is None:
        log(f"ERROR 找不到 history/{date}/ai_output.json")
        return EXIT_AI_INVALID
    _, issues = validate(ai_output, ai_input)
    for i in issues:
        log(f"  [不合格] {i}")
    log("AI_VALID" if not issues else f"AI_INVALID 共 {len(issues)} 個問題，請修改 ai_output.json 後再檢查")
    return EXIT_OK if not issues else EXIT_AI_INVALID


def cmd_write_ai(args):
    from ai_writer import write_with_api
    date = args.date or today_tw()
    ai_input = history.load_json(date, "ai_input.json")
    if ai_input is None:
        log(f"ERROR 找不到 history/{date}/ai_input.json")
        return EXIT_ERROR
    if not ai_input["candidates"]:
        history.save_json(date, "ai_output.json", {"date": date, "highlights": [], "stocks": []})
        log("沒有辯證名單，只寫空的 ai_output.json")
        return EXIT_OK
    out, err, usage = write_with_api(ai_input)
    if usage:
        log(f"  API 用量：{usage}")
    if out is None:
        log(f"  AI 撰寫失敗：{err}（報告會只列程式數據）")
        return EXIT_AI_INVALID
    history.save_json(date, "ai_output.json", out)
    return cmd_validate_ai(args)


def cmd_finalize(args):
    date = args.date or today_tw()
    if history.is_sent(date) and not args.force:
        log(f"ALREADY_SENT {date}")
        return EXIT_ALREADY_SENT
    payload = history.load_json(date, "picks.json")
    ai_input = history.load_json(date, "ai_input.json")
    if payload is None or ai_input is None:
        log(f"ERROR 找不到 history/{date}/picks.json，請先跑 collect")
        return EXIT_ERROR

    ai_output = history.load_json(date, "ai_output.json")
    ai_clean, ai_issues, ai_note = None, [], None
    if ai_output is None:
        ai_note = "AI 文字未提供，以下只列程式數據"
    else:
        ai_clean, ai_issues = validate(ai_output, ai_input)
        if ai_issues:
            ai_note = f"AI 文字有 {len(ai_issues)} 處未通過程式檢查，已拿掉該段落"
    history.save_json(date, "ai_validation.json", {"issues": ai_issues})

    q = DataQuality.from_dict(payload.get("quality"))
    verify_issues = []
    if payload["picks"]:
        twse, err = fetch.get_twse_closes()
        if err:
            q.add_note(f"核對：{err}")
        else:
            summary, issues = verify.verify_closes(payload["picks"], twse)
            if summary:
                q.add_check(summary)
            verify_issues += issues
    payload["quality"] = q.to_dict()

    messages = reporter.build_messages(payload, ai_clean, ai_note, verify_issues)
    render_issues = verify.verify_messages(messages, payload["picks"])
    if render_issues:
        messages[-1] += "\n\n❗ 報告數字核對失敗：" + reporter.esc("；".join(render_issues))
    history.save_text(date, "report.txt",
                      "\n".join(f"=== 訊息 {i + 1} ===\n{m}\n" for i, m in enumerate(messages)))
    for i in ai_issues + verify_issues + render_issues:
        log(f"  [檢查] {i}")

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            log(f"\n=== 訊息 {i} ===\n{m}")
        log("\nDRY_RUN 沒有送出")
        return EXIT_OK

    statuses = reporter.send_to_telegram(messages)
    for i, s in enumerate(statuses, 1):
        log(f"  Telegram 訊息{i}：{s}")
    ok = all(s == "SUCCESS" for s in statuses)
    if ok:
        history.save_json(date, "sent.json", {
            "sent_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
            "messages": len(messages),
            "ai_issues": len(ai_issues),
        })
    _push(args, f"report {date}")
    log("SENT" if ok else "SEND_FAILED")
    return EXIT_OK if ok else EXIT_SEND_FAILED


def cmd_all(args):
    rc = cmd_check_date(args)
    if rc != EXIT_OK:
        return rc
    rc = cmd_collect(args)
    if rc != EXIT_OK:
        return rc
    if args.ai == "api":
        cmd_write_ai(args)
    return cmd_finalize(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="台股選股日報每日流程")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check-date", "collect", "validate-ai", "write-ai", "finalize", "all"):
        p = sub.add_parser(name)
        p.add_argument("--date", help="指定日期 YYYY-MM-DD（預設今天，台北時間）")
        p.add_argument("--dry-run", action="store_true", help="不送 Telegram、不推送歷史分支")
        p.add_argument("--force", action="store_true", help="已經發過也重做")
        p.add_argument("--no-git", action="store_true", help="不同步歷史分支（本機測試用）")
        p.add_argument("--wait-minutes", type=float, default=0, help="等資料更新的分鐘數")
        p.add_argument("--ai", choices=["api", "none"], default="none", help="all 指令的 AI 撰寫方式")
    args = parser.parse_args(argv)
    handler = {
        "check-date": cmd_check_date, "collect": cmd_collect, "validate-ai": cmd_validate_ai,
        "write-ai": cmd_write_ai, "finalize": cmd_finalize, "all": cmd_all,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
