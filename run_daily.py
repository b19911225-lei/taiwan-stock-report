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
# 結束代碼：0 成功｜1 AI 文字未通過檢查｜2 錯誤｜3 Telegram 發送失敗｜4 已發送但歷史紀錄沒存回 GitHub
#           10 非交易日｜11 今天已經發過｜12 還沒到收盤資料更新的時間，不判定休市
#           13 資料不完整、加了 --defer-incomplete，先不發（等下一次執行）

import argparse
import io
import json
import re
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
    WATCHLIST, WATCH_THEME, MARKET_SCAN_COUNT, SCAN_THEME, MIN_PICK_COUNT, MIN_FILL_RR,
)
from quality import DataQuality

EXIT_OK, EXIT_AI_INVALID, EXIT_ERROR, EXIT_SEND_FAILED, EXIT_PUSH_FAILED = 0, 1, 2, 3, 4
EXIT_NOT_TRADING, EXIT_ALREADY_SENT, EXIT_NOT_READY, EXIT_DEFERRED = 10, 11, 12, 13

# 台北時間這個時刻之前，加權指數沒有今天的資料不代表休市（可能還沒收盤或還沒更新）
CLOSED_DECISION_HOUR = 17
# 超過這個比例的股票沒有今日價格，或候選股的法人資料全缺，就算「資料不完整」
INCOMPLETE_PRICE_RATIO = 0.5

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

# 推薦分級（見 config.py 的 TARGET_STOCK_COUNT／MIN_PICK_COUNT）
TIER_LABELS = {"A": "嚴選", "B": "次選", "C": "候補"}


def log(msg):
    print(msg, flush=True)


def now_tw():
    return datetime.now(ZoneInfo(TIMEZONE))


def today_tw():
    return now_tw().strftime("%Y-%m-%d")


def resolve_trading_date(today, wait_minutes=0, deadline=None):
    """回傳 (加權指數最新日期, 加權指數資料)。等到 today 的資料出現或時間到為止。"""
    if deadline is None:
        deadline = time.time() + wait_minutes * 60
    while True:
        taiex = fetch.get_taiex_price(today)
        latest = fetch.latest_date_of(taiex)
        if latest == today or time.time() >= deadline:
            return latest, taiex
        log(f"  加權指數最新是 {latest}，還沒有 {today} 的資料，{DATA_WAIT_INTERVAL_SEC} 秒後再試")
        time.sleep(max(1, min(DATA_WAIT_INTERVAL_SEC, deadline - time.time())))


def wait_for_institutional(date, deadline):
    """法人資料通常比股價晚更新；用台積電的法人資料判斷今天的資料出來了沒。"""
    while True:
        daily, _ = fetch.get_institutional_investors("2330", date, days=3)
        latest = fetch.latest_date_of(daily)
        if latest == date:
            return True
        if time.time() >= deadline:
            log(f"  法人資料最新是 {latest}，等不到 {date}，先繼續（報告會標示不完整，晚上的備援會補發）")
            return False
        log(f"  法人資料最新是 {latest}，{DATA_WAIT_INTERVAL_SEC} 秒後再試")
        time.sleep(max(1, min(DATA_WAIT_INTERVAL_SEC, deadline - time.time())))


def load_ai_output(date):
    """讀 ai_output.json；容許外面多包一層 Markdown 程式碼框。回傳 (內容或 None, 錯誤訊息或 None)。"""
    path = history.day_dir(date) / "ai_output.json"
    if not path.exists():
        return None, None
    text = path.read_text(encoding="utf-8").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"ai_output.json 不是合法的 JSON（第 {e.lineno} 行：{e.msg}）"


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


def _tier_note(r):
    if r["tier"] == "B":
        return f"次選：RR {r['targets']['rr_ratio']:.2f} 未達 {MIN_RR_RATIO:g}"
    if r["tier"] == "C":
        return f"候補：動能訊號只有 {r['momentum_count']} 個"
    return None


def select_picks(results):
    """嚴選依評分取前 TARGET_STOCK_COUNT 檔；不到 MIN_PICK_COUNT 檔時用次選、候補補足（RR 至少 MIN_FILL_RR）。"""
    def ranked(tier, min_rr=None):
        ids = [s for s, r in results.items() if r.get("tier") == tier
               and (min_rr is None or (r.get("targets") and r["targets"]["rr_ratio"] >= min_rr))]
        return sorted(ids, key=lambda s: results[s]["score"], reverse=True)

    pick_ids = ranked("A")[:TARGET_STOCK_COUNT]
    for tier in ("B", "C"):
        if len(pick_ids) >= MIN_PICK_COUNT:
            break
        pick_ids += ranked(tier, MIN_FILL_RR)[:MIN_PICK_COUNT - len(pick_ids)]
    return pick_ids


def collect(date, taiex):
    q = DataQuality()

    stock_info = fetch.get_stock_info()
    if not stock_info:
        q.add_note("官方股票清單抓不到，股票名稱用內建表")
    else:
        not_found, mismatched = verify.verify_universe(stock_info)
        if not_found:
            q.add_note(f"股票池有 {len(not_found)} 個代號在官方清單查無：{'、'.join(not_found[:8])}"
                       f"{'…' if len(not_found) > 8 else ''}")
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

    # 全市場掃描：當日上漲、成交金額最大的普通股（同一份資料也拿來算漲跌家數、成交重心）
    quotes, breadth, err = fetch.get_twse_daily(date)
    if err:
        q.add_note(err)
    scan = analyzer.pick_market_scan(quotes, set(universe), MARKET_SCAN_COUNT)
    for sid, _ in scan:
        universe[sid] = [SCAN_THEME]
    if scan:
        log(f"  市場掃描加入 {len(scan)} 檔：{'、'.join(f'{s} {n}' for s, n in scan)}")

    for sid in WATCHLIST:
        themes = universe.setdefault(sid, [])
        if WATCH_THEME not in themes:
            themes.append(WATCH_THEME)

    def name_of(sid):
        return (stock_info.get(sid) or hot_names.get(sid) or (quotes.get(sid) or {}).get("name")
                or STOCK_NAMES.get(sid) or sid)

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
    results, cards, unknown_inst = {}, {}, set()
    stats = {"universe": len(universe), "fetched": len(raw), "scan_added": len(scan), "prefilter_passed": 0,
             "scored_passed": 0, "rr_ok": 0}
    for sid, r in raw.items():
        ev, reason = analyzer.evaluate(r["df"])
        pre_ok = ev is not None and not ev["exclusions"] and all(ev["trend"].values())
        if not pre_ok:
            results[sid] = {"passed": False,
                            "exclude_reason": reason or (ev["exclusions"] or ["趨勢不符"])[0]}
            if sid not in WATCHLIST or ev is None:
                continue          # 自選股沒通過也要打分數（評分卡寫原因），其他的就不抓法人了
        else:
            stats["prefilter_passed"] += 1
        inst_daily, unknown = fetch.get_institutional_investors(sid, date, days=10)
        unknown_inst.update(unknown)
        inst = analyzer.calc_institutional_signals(inst_daily, date)
        if not inst["available"] and pre_ok:
            q.missing_inst.append(sid)
        card = analyzer.score_card(sid, ev, inst, theme_avg, universe)
        cards[sid] = card
        if not pre_ok:
            continue
        rr = card["targets"]["rr_ratio"] if card["targets"] else None
        strong = card["momentum_count"] >= 2
        if strong:
            stats["scored_passed"] += 1
        tier = ("A" if rr is not None and rr >= MIN_RR_RATIO else "B") if strong else "C"
        stats["rr_ok"] += int(tier == "A")
        results[sid] = {**card, "passed": strong, "exclude_reason": None if strong else "動能不足", "tier": tier}
        mark = {"A": "++", "B": "~ ", "C": ". "}[tier]
        log(f"  [{mark}] {sid} 評分:{card['score']} RR:{rr} 訊號:{card['signals']}")

    if unknown_inst:
        q.add_note(f"法人資料有無法辨識的類別：{'、'.join(sorted(unknown_inst))}（請更新 INSTITUTION_ALIASES）")
    incomplete = []
    if stats["prefilter_passed"] and len(q.missing_inst) == stats["prefilter_passed"]:
        incomplete.append("所有候選股的法人資料都缺（可能尚未更新）")
    if len(q.missing_price) > len(universe) * INCOMPLETE_PRICE_RATIO:
        incomplete.append(f"{len(q.missing_price)}/{len(universe)} 檔沒有今日價格")
    if incomplete:
        q.add_note("資料不完整：" + "；".join(incomplete) + "。晚上的備援會在資料齊全後補發完整版")

    pick_ids = select_picks(results)
    picks = [{
        "stock_id": sid,
        "name": name_of(sid),
        "themes": [t.split("_", 1)[1] for t in universe[sid]],
        **{k: results[sid].get(k) for k in PICK_FIELDS},
        "targets": results[sid]["targets"],
        "tier": results[sid]["tier"],
        "tier_label": TIER_LABELS[results[sid]["tier"]],
        "tier_note": _tier_note(results[sid]),
    } for sid in pick_ids]

    watchlist = []
    for sid in WATCHLIST:
        entry = {"stock_id": sid, "name": name_of(sid),
                 "pick_rank": pick_ids.index(sid) + 1 if sid in pick_ids else None}
        if sid in cards:
            c = cards[sid]
            entry.update({k: c.get(k) for k in PICK_FIELDS})
            entry.update(trend=c["trend"], fail_reasons=c["fail_reasons"], targets=c["targets"])
        elif sid in q.missing_price:
            entry["unavailable"] = f"沒有 {date} 的價格資料"
        else:
            entry["unavailable"] = "歷史價格資料不足，算不出指標"
        watchlist.append(entry)

    themes = sorted(
        ({"theme": t, "name": t.split("_", 1)[1], "avg_chg_pct": None if v is None else round(v * 100, 2)}
         for t, v in theme_avg.items()),
        key=lambda x: (x["avg_chg_pct"] is not None, x["avg_chg_pct"] or 0), reverse=True)

    news, err = fetch.get_tw_stock_news()
    if err:
        q.add_note(err)
    # 推薦股和自選股的個股新聞（FinMind），接在鉅亨網新聞後面、編號連續
    titles = {n["title"][:20] for n in news}
    for sid in dict.fromkeys(pick_ids + list(WATCHLIST)):
        for item in fetch.get_stock_news(sid, date):
            if item["title"][:20] in titles:
                continue
            titles.add(item["title"][:20])
            news.append({"id": f"N{len(news) + 1}", "source_id": "", "title": item["title"], "summary": "",
                         "time": "", "url": item["url"], "category": "個股", "source": item["source"],
                         "stocks": [sid]})
    related = {sid: fetch.related_news_ids(news, sid, name_of(sid)) for sid in dict.fromkeys(pick_ids + list(WATCHLIST))}
    for p in picks:
        p["related_news"] = related.get(p["stock_id"], [])
    for w in watchlist:
        w["related_news"] = related.get(w["stock_id"], [])

    market = market_summary(taiex)
    market["breadth"] = breadth or None
    market["top_turnover"] = analyzer.top_turnover(quotes, 5)
    market["institutional_100m"], err = fetch.get_market_institutional(date)
    if err:
        q.add_note(err)
    us_markets, err = fetch.get_us_markets(date)
    if err:
        q.add_note(err)

    payload = {
        "date": date,
        "generated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
        "market": market,
        "us_markets": us_markets,
        "themes": themes,
        "picks": picks,
        "debate_ids": pick_ids[:DEBATE_STOCK_COUNT],
        "watchlist": watchlist,
        "stats": stats,
        "incomplete": incomplete,
        "exclude_counts": dict(Counter(r["exclude_reason"] for r in results.values() if not r.get("passed"))),
        "quality": q.to_dict(),
        "performance": performance.summarize(past, price_lookup, PERFORMANCE_LOOKBACK_DAYS),
        "news": news,
    }
    return payload, build_ai_input(payload)


def _levels(t):
    if not t:
        return None
    return {"entry_low": t["entry_low"], "entry_high": t["entry_high"], "target1": t["target1"],
            "target_basis": t["target_basis"], "stop_loss": t["stop_loss"], "rr_ratio": t["rr_ratio"]}


def _stock_numbers(p):
    return {
        "close": p.get("close"), "today_chg_pct": p.get("today_chg"), "chg_5d_pct": p.get("chg_5d"),
        "score": p.get("score"), "score_breakdown": p.get("score_breakdown"),
        "signals": {s: SIGNAL_MEANINGS[s] for s in p.get("signals") or []},
        "vol_vs_20d": p.get("vol_vs_20d"), "vol_vs_5d": p.get("vol_vs_5d"),
        "rsi14": p.get("rsi14"), "kd_cross": p.get("kd_cross"),
        "ma5": p.get("ma5"), "ma20": p.get("ma20"), "atr14": p.get("atr"), "high_60d": p.get("high_60d"),
        "institutional": {
            "available": p.get("inst_available"),
            "foreign_buy_days": p.get("foreign_buy_days"),
            "foreign_net_5d_lots": p.get("foreign_net_5d_lots"),
            "sitc_net_5d_lots": p.get("sitc_net_5d_lots"),
            "dealer_net_5d_lots": p.get("dealer_net_5d_lots"),
        },
        "program_levels": _levels(p.get("targets")),
    }


def build_ai_input(payload):
    candidates = []
    for p in payload["picks"]:
        if p["stock_id"] not in payload["debate_ids"]:
            continue
        candidates.append({
            "stock_id": p["stock_id"], "name": p["name"], "themes": p["themes"],
            "tier": p.get("tier_label", "嚴選"), "tier_note": p.get("tier_note"),
            **_stock_numbers(p),
            "related_news": p.get("related_news", []),
        })
    watchlist = []
    for w in payload.get("watchlist", []):
        item = {"stock_id": w["stock_id"], "name": w["name"], "pick_rank": w.get("pick_rank")}
        if w.get("unavailable"):
            item["unavailable"] = w["unavailable"]
        else:
            item.update(_stock_numbers(w))
            item.update(trend_ok={k: bool(v) for k, v in (w.get("trend") or {}).items()},
                        not_selected_reasons=w.get("fail_reasons", []))
        item["related_news"] = w.get("related_news", [])
        watchlist.append(item)
    return {
        "date": payload["date"],
        "instructions": "依 docs/AI_WRITING_RULES.md 撰寫 history/<date>/ai_output.json；數字只能照抄本檔。",
        "market": payload["market"],
        "us_markets": payload.get("us_markets", []),
        "themes": payload["themes"],
        "candidates": candidates,
        "other_picks": [{"stock_id": p["stock_id"], "name": p["name"]}
                        for p in payload["picks"] if p["stock_id"] not in payload["debate_ids"]],
        "watchlist": watchlist,
        "news": [{k: n.get(k, "") for k in ("id", "category", "title", "summary", "time")}
                 for n in payload["news"]],
        "missing": payload["quality"],
    }


# ─── 指令 ───

def _pull(args):
    """同步歷史分支；失敗就停（不知道今天發過沒，硬跑可能重複發送）。"""
    if args.no_git:
        history.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        return True
    ok, msg = history.sync_pull()
    log(f"歷史分支：{msg}")
    if not ok:
        log("ERROR 歷史分支同步失敗，為避免重複發送先停止（本機測試請加 --no-git）")
    return ok


def _push(args, message):
    if args.no_git or args.dry_run:
        return True
    ok, msg = history.sync_push(message)
    log(f"歷史分支：{msg}")
    return ok


def cmd_check_date(args):
    today = args.date or today_tw()
    if not _pull(args):
        return EXIT_ERROR
    if history.is_sent(today) and not args.force:
        log(f"ALREADY_SENT {today}：今天的報告已經發過（要重發請加 --force）")
        return EXIT_ALREADY_SENT
    weekday = datetime.strptime(today, "%Y-%m-%d").weekday()
    deadline = time.time() + (args.wait_minutes * 60 if weekday < 5 else 0)
    latest, _ = resolve_trading_date(today, deadline=deadline)
    if latest == today:
        log(f"TRADING_DAY {today}")
        wait_for_institutional(today, deadline)
        return EXIT_OK
    if latest is None:
        log("ERROR 抓不到加權指數，FinMind 可能連不上（Routine 請確認網路白名單有 api.finmindtrade.com）")
        return EXIT_ERROR
    if weekday >= 5:
        log(f"NOT_TRADING {today} 是週末")
        return EXIT_NOT_TRADING
    if today == today_tw() and now_tw().hour < CLOSED_DECISION_HOUR:
        log(f"NOT_READY 現在還不到 {CLOSED_DECISION_HOUR}:00，加權指數沒有今天的資料不代表休市，不發通知")
        return EXIT_NOT_READY

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
        if not _push(args, f"closed {today}"):
            log("  休市通知已發送，但標記沒有存回 GitHub，晚上的備援可能會再發一次")
    return EXIT_NOT_TRADING


def cmd_collect(args):
    today = args.date or today_tw()
    if not _pull(args):
        return EXIT_ERROR
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
    if ai_input is None:
        log(f"ERROR 找不到 history/{date}/ai_input.json，請先跑 collect")
        return EXIT_ERROR
    ai_output, err = load_ai_output(date)
    if err:
        log(f"AI_INVALID {err}")
        return EXIT_AI_INVALID
    if ai_output is None:
        log(f"AI_INVALID 找不到 history/{date}/ai_output.json")
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
    # 沒有推薦股也照樣呼叫：盤勢播報、焦點新聞、自選股評論還是要寫
    out, err, usage = write_with_api(ai_input)
    if usage:
        log(f"  API 用量：{usage}")
    if out is None:
        log(f"  AI 撰寫失敗：{err}（報告會只列程式數據）")
        return EXIT_AI_INVALID
    history.save_json(date, "ai_output.json", out)
    return cmd_validate_ai(args)


def twse_closes_for(date):
    """證交所收盤價（核對用）：先抓指定日期的全部收盤行情，抓不到再用 STOCK_DAY_ALL（只有最新一天）。"""
    quotes, _, err = fetch.get_twse_daily(date)
    if quotes:
        return {sid: q["close"] for sid, q in quotes.items()}, date, None
    closes, twse_date, err2 = fetch.get_twse_closes()
    return closes, twse_date, (err2 if not closes else None)


def cmd_finalize(args):
    date = args.date or today_tw()
    prev = history.sent_info(date)
    if history.is_sent(date) and not args.force:
        log(f"ALREADY_SENT {date}")
        return EXIT_ALREADY_SENT
    payload = history.load_json(date, "picks.json")
    ai_input = history.load_json(date, "ai_input.json")
    if payload is None or ai_input is None:
        log(f"ERROR 找不到 history/{date}/picks.json，請先跑 collect")
        return EXIT_ERROR
    incomplete = payload.get("incomplete") or []
    resend_note = None
    if prev is not None and not args.force:
        # 稍早已經發過不完整的版本：資料還是不完整就不重發，齊全了才補發完整版
        if incomplete:
            log(f"ALREADY_SENT {date}：稍早已發過不完整的版本，資料仍不完整，不重發")
            return EXIT_ALREADY_SENT
        resend_note = "（資料更新後的完整版，取代稍早不完整的報告）"
    if incomplete and prev is None and args.defer_incomplete and not args.force:
        # 一天排了好幾次執行：前面幾次資料不完整就先不發，留給後面的執行，避免同一天收到兩份
        log(f"DEFERRED {date}：資料不完整（{'；'.join(incomplete)}），這次先不發，等下一次執行")
        return EXIT_DEFERRED

    ai_clean, ai_issues, ai_note = None, [], None
    ai_output, load_err = load_ai_output(date)
    if load_err:
        ai_issues = [load_err]
        ai_note = "AI 文字格式錯誤，以下只列程式數據"
    elif ai_output is None:
        ai_note = "AI 文字未提供，以下只列程式數據"
    else:
        try:
            ai_clean, ai_issues = validate(ai_output, ai_input)
        except Exception as e:  # 檢查程式本身出錯也不能擋住報告
            ai_clean, ai_issues = None, [f"AI 檢查失敗：{type(e).__name__}: {e}"]
        if ai_issues:
            ai_note = f"AI 文字有 {len(ai_issues)} 處未通過程式檢查，已拿掉該段落"
    history.save_json(date, "ai_validation.json", {"issues": ai_issues})

    q = DataQuality.from_dict(payload.get("quality"))
    verify_issues = []
    if payload["picks"]:
        twse, twse_date, err = twse_closes_for(date)
        if err:
            q.add_note(f"核對：{err}")
        elif twse_date and twse_date != date:
            q.add_note(f"證交所收盤價資料日期是 {twse_date}，不是 {date}，未核對收盤價")
        elif not twse_date and date != today_tw():
            q.add_note("補跑過去的日期：證交所只提供最新一日收盤價，未核對")
        else:
            result = verify.verify_closes(payload["picks"], twse)
            if result["ok"]:
                q.add_check(result["ok"])
            if result["skipped"]:
                q.add_note(result["skipped"])
            verify_issues += result["issues"]
    payload["quality"] = q.to_dict()

    messages = reporter.build_messages(payload, ai_clean, ai_note, verify_issues, resend_note)
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
            "sent_at": now_tw().isoformat(timespec="seconds"),
            "messages": len(messages),
            "ai_issues": len(ai_issues),
            "complete": not incomplete,
            "incomplete": incomplete,
        })
    pushed = _push(args, f"report {date}")
    if not ok:
        log("SEND_FAILED")
        return EXIT_SEND_FAILED
    if not pushed:
        log("SENT_BUT_NOT_SAVED 報告已發送，但紀錄沒有存回 GitHub（晚上的備援可能會重發）")
        return EXIT_PUSH_FAILED
    log("SENT" if not incomplete else "SENT_INCOMPLETE 已發送不完整的版本，資料齊全後會補發")
    return EXIT_OK


def cmd_all(args):
    rc = cmd_check_date(args)
    if rc != EXIT_OK:
        return rc
    rc = cmd_collect(args)
    if rc != EXIT_OK:
        return rc
    date = args.date or today_tw()
    payload = history.load_json(date, "picks.json") or {}
    if (payload.get("incomplete") and args.defer_incomplete and not args.force
            and history.sent_info(date) is None):
        # 在呼叫 Claude API 之前就停，延後的執行不花 API 費用
        log(f"DEFERRED {date}：資料不完整（{'；'.join(payload['incomplete'])}），這次先不發，等下一次執行")
        return EXIT_DEFERRED
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
        p.add_argument("--defer-incomplete", action="store_true",
                       help="資料不完整時先不發（後面還有排程會再跑時使用）")
    args = parser.parse_args(argv)
    handler = {
        "check-date": cmd_check_date, "collect": cmd_collect, "validate-ai": cmd_validate_ai,
        "write-ai": cmd_write_ai, "finalize": cmd_finalize, "all": cmd_all,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
