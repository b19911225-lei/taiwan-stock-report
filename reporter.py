# reporter.py - 產生 Telegram 訊息與推送
#
# 所有數字都從 picks.json 來（程式算好的），AI 只提供文字段落；
# 動態文字一律 html.escape，避免 AI 或新聞裡的 < > & 讓 Telegram HTML 解析失敗。

import html
import time
from datetime import datetime
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MIN_RR_RATIO, MIN_PICK_COUNT, MIN_FILL_RR

LOG_DIR = Path(__file__).parent / "logs"
TELEGRAM_LIMIT = 4000

SIGNAL_LEGEND = "M1 量>20日均1.5倍｜M2 量>5日均1.3倍｜M3 外資連買≥3日｜M4 主力連增｜M5 KD金叉｜M6 RSI強勢"
VERDICT_EMOJI = {"強力做多": "🟢🔥", "謹慎做多": "🟡✅", "中性觀察": "⚪👀", "規避": "🔴⚠️"}
TIER_BADGE = {"嚴選": "🥇嚴選", "次選": "🥈次選", "候補": "🥉候補"}
WEEKDAYS = "一二三四五六日"


def esc(text):
    return html.escape(str(text), quote=False)


def esc_url(url):
    """href 裡的網址：連引號一起跳脫，避免新聞網址裡的 " 弄壞整則訊息（Telegram 會整則拒收）。"""
    return html.escape(str(url), quote=True)


def fmt_num(x, nd=2):
    """價格格式：千分位、最多 nd 位小數、去掉多餘的 0。"""
    if x is None:
        return "N/A"
    s = f"{float(x):,.{nd}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_pct(x):
    if x is None:
        return "N/A"
    arrow = "▲" if x > 0 else ("▼" if x < 0 else "─")
    return f"{arrow}{abs(x):.2f}%"


def sentiment(chg_pct):
    if chg_pct is None:
        return ""
    if chg_pct >= 1.5:  return "🚀 強勢上攻"
    if chg_pct >= 0.5:  return "📈 偏多格局"
    if chg_pct >= -0.5: return "😐 盤整觀望"
    if chg_pct >= -1.5: return "📉 偏弱震盪"
    return "🔻 空方主導"


def pick_number_strings(pick):
    """報告上每檔一定會出現的數字字串（verify.py 用來核對報告有沒有寫錯）。"""
    t = pick["targets"]
    return {
        "收盤": fmt_num(pick["close"]),
        "進場下緣": fmt_num(t["entry_low"]),
        "進場上緣": fmt_num(t["entry_high"]),
        "目標": fmt_num(t["target1"]),
        "停損": fmt_num(t["stop_loss"]),
        "RR": f"{t['rr_ratio']:.2f}",
    }


def _brief_comment(p):
    sigs = p.get("signals", [])
    parts = []
    if "M3" in sigs:
        parts.append(f"外資連買{p.get('foreign_buy_days', 0)}日")
    if "M5" in sigs:
        parts.append("KD金叉")
    if "M6" in sigs and p.get("rsi14") is not None:
        parts.append(f"RSI強勢({p['rsi14']:.0f})")
    if "M1" in sigs:
        parts.append(f"量能放大{p.get('vol_vs_20d', 0):.1f}倍")
    if parts:
        return "、".join(parts[:2])
    return "多訊號共振" if len(sigs) >= 2 else ""


def _signed(x, nd=2):
    return "N/A" if x is None else f"{x:+,.{nd}f}"


def _market_lines(m):
    lines = ["🏦 <b>大盤</b>"]
    if m.get("close") is None:
        lines.append("加權指數：無資料")
        return lines
    pts = f"，{_signed(m['chg'])} 點" if m.get("chg") is not None else ""
    lines.append(f"加權指數 {fmt_num(m['close'])}（{fmt_pct(m.get('chg_pct'))}{pts}）")
    second = []
    if m.get("money_100m") is not None:
        second.append(f"成交金額 {fmt_num(m['money_100m'], 0)} 億")
    if sentiment(m.get("chg_pct")):
        second.append(sentiment(m.get("chg_pct")))
    if second:
        lines.append("｜".join(second))
    b = m.get("breadth") or {}
    if b.get("up") is not None and b.get("down") is not None:
        up = f"上漲 {b['up']} 家" + (f"（漲停 {b['limit_up']}）" if b.get("limit_up") is not None else "")
        down = f"下跌 {b['down']} 家" + (f"（跌停 {b['limit_down']}）" if b.get("limit_down") is not None else "")
        lines.append(f"{up}｜{down}" + (f"｜持平 {b['flat']} 家" if b.get("flat") is not None else ""))
    inst = m.get("institutional_100m")
    if inst:
        lines.append(f"三大法人：外資 {_signed(inst['foreign'])} 億｜投信 {_signed(inst['sitc'])} 億｜"
                     f"自營商 {_signed(inst['dealer'])} 億")
    top = m.get("top_turnover") or []
    if top:
        lines.append("成交重心：" + "、".join(f"{esc(r['name'])} {fmt_pct(r.get('chg_pct'))}" for r in top))
    return lines


def _us_lines(us):
    if not us:
        return []
    dates = sorted({u["date"][5:].replace("-", "/") for u in us})
    label = dates[0] if len(dates) == 1 else f"{dates[0]}～{dates[-1]}"
    return [f"🌎 <b>國際股市（{label} 收盤）</b>",
            "｜".join(f"{esc(u['name'])} {fmt_pct(u.get('chg_pct'))}" for u in us)]


def _broadcast_lines(ai_clean):
    lines = []
    if (ai_clean or {}).get("headline"):
        lines.append(f"🎙️ <b>{esc(ai_clean['headline'])}</b>")
    if (ai_clean or {}).get("market_commentary"):
        lines.append(esc(ai_clean["market_commentary"]))
    return lines


def _news_links(payload, ids):
    news = {n["id"]: n for n in payload.get("news", [])}
    return " ".join(f'<a href="{esc_url(news[i]["url"])}">[{esc(i)}]</a>' for i in ids if i in news and news[i].get("url"))


def _theme_lines(themes):
    lines = ["🔥 <b>主題強弱</b>"]
    for i, t in enumerate(themes):
        star = "★" if i == 0 and t["avg_chg_pct"] is not None else "  "
        value = "無資料" if t["avg_chg_pct"] is None else fmt_pct(t["avg_chg_pct"])
        lines.append(f"{star}{esc(t['name'])}：{value}")
    return lines


def _pick_lines(rank, p):
    t = p["targets"]
    nums = pick_number_strings(p)
    theme_tag = "/".join(p.get("themes", [])[:2]) or "熱門量能"
    badge = TIER_BADGE.get(p.get("tier_label", "嚴選"), "")
    line1 = (f"{rank}. <b>{esc(p['stock_id'])} {esc(p['name'])}</b>｜{badge}｜{esc(theme_tag)}｜"
             f"{nums['收盤']} {fmt_pct(p.get('today_chg'))}｜評分{p['score']}｜{' '.join(p.get('signals', []))}")
    line2 = (f"   進場 {nums['進場下緣']}~{nums['進場上緣']}｜目標 {nums['目標']}（{esc(t['target_basis'])}）"
             f"｜停損 {nums['停損']}｜RR {nums['RR']}")
    extra = [x for x in (_brief_comment(p), p.get("tier_note"),
                         "" if p.get("inst_available", True) else "法人資料缺") if x]
    return f"{line1}\n{line2}" + (f"\n   {esc('｜'.join(extra))}" if extra else "")


def _tier_summary(picks):
    """有用次選／候補補足，或檔數不到 MIN_PICK_COUNT 時，說明為什麼。"""
    strict = sum(1 for p in picks if p.get("tier_label", "嚴選") == "嚴選")
    if strict < len(picks):
        return (f"嚴選（全部條件都符合）只有 {strict} 檔，用次選（RR 未達 {MIN_RR_RATIO:g}）、"
                f"候補（動能訊號只有 1 個）補到 {len(picks)} 檔，風險較高，請搭配停損")
    if len(picks) < MIN_PICK_COUNT:
        return f"今天符合條件的只有 {len(picks)} 檔，寧缺勿濫，不硬湊到 {MIN_PICK_COUNT} 檔"
    return None


def _watch_lines(payload, ai_clean):
    items = payload.get("watchlist") or []
    if not items:
        return []
    news = {n["id"]: n for n in payload.get("news", [])}
    comments = (ai_clean or {}).get("watchlist", {})
    lines = ["⭐ <b>自選股</b>"]
    for w in items:
        head = f"⭐ <b>{esc(w['stock_id'])} {esc(w['name'])}</b>"
        if w.get("unavailable"):
            lines.append(f"{head}｜{esc(w['unavailable'])}")
            continue
        inst = "" if w.get("inst_available", True) else "｜法人資料缺"
        lines.append(f"{head}｜{fmt_num(w.get('close'))} {fmt_pct(w.get('today_chg'))}｜評分 {w.get('score')}/100{inst}")
        if w.get("pick_rank"):
            lines.append(f"   ✅ 已列入今日推薦第 {w['pick_rank']} 檔")
        else:
            reasons = w.get("fail_reasons") or ["條件都符合，但評分不在今日推薦名單內"]
            lines.append(f"   未入選：{esc('；'.join(reasons))}")
        t = w.get("targets")
        levels = (f"｜參考停損 {fmt_num(t['stop_loss'])}｜目標 {fmt_num(t['target1'])}｜RR {t['rr_ratio']:.2f}"
                  if t else "")
        rsi = w.get("rsi14")
        lines.append(f"   訊號 {' '.join(w.get('signals') or []) or '無'}｜RSI {rsi if rsi is not None else 'N/A'}"
                     f"｜外資連買 {w.get('foreign_buy_days', 0)} 日{levels}")
        c = comments.get(w["stock_id"])
        if c:
            links = _news_links(payload, c.get("news_ids", []))
            lines.append(f"   💬 {esc(c['comment'])}" + (f" {links}" if links else ""))
        for nid in (w.get("related_news") or [])[:2]:
            n = news.get(nid)
            if n and n.get("url"):
                lines.append(f'   📰 <a href="{esc_url(n["url"])}">{esc(n["title"][:40])}</a>')
    return lines


def _quality_lines(payload, verify_issues=None):
    q = payload.get("quality", {})
    stats = payload.get("stats", {})
    lines = ["🧾 <b>資料檢查</b>"]
    missing = []
    if q.get("missing_price"):
        ids = q["missing_price"]
        missing.append(f"價格 {len(ids)} 檔（{'、'.join(ids[:8])}{'…' if len(ids) > 8 else ''}）")
    if q.get("missing_inst"):
        ids = q["missing_inst"]
        missing.append(f"法人 {len(ids)} 檔（{'、'.join(ids[:8])}{'…' if len(ids) > 8 else ''}，未計籌碼分）")
    lines.append("今日缺少：" + ("；".join(missing) if missing else "無"))
    for note in q.get("notes", []):
        lines.append(f"⚠️ {esc(note)}")
    for issue in verify_issues or []:
        lines.append(f"❗ {esc(issue)}")
    for check in q.get("checks", []):
        lines.append(f"✅ {esc(check)}")
    if stats:
        scan = f"（含市場掃描 {stats['scan_added']}）" if stats.get("scan_added") else ""
        lines.append(
            f"篩選：股票池 {stats.get('universe', 0)}{scan}｜有資料 {stats.get('fetched', 0)}｜"
            f"趨勢通過 {stats.get('prefilter_passed', 0)}｜評分通過 {stats.get('scored_passed', 0)}｜"
            f"RR≥{MIN_RR_RATIO:g} {stats.get('rr_ok', 0)}")
    return lines


def _debate_lines(payload, ai_clean, ai_note):
    picks = payload.get("picks", [])
    candidates = [p for p in picks if p["stock_id"] in set(payload.get("debate_ids", []))]
    lines = [f"⚔️ <b>多空辯證 Top {len(candidates)}</b>"]
    if ai_note:
        lines.append(f"<i>{esc(ai_note)}</i>")
    stocks = (ai_clean or {}).get("stocks", {})
    for i, p in enumerate(candidates, 1):
        lines.append("")
        tier = "" if p.get("tier_label", "嚴選") == "嚴選" else f"（{esc(p['tier_label'])}）"
        lines.append(f"<b>{i}. {esc(p['stock_id'])} {esc(p['name'])}</b>{tier}")
        a = stocks.get(p["stock_id"])
        if a:
            emoji = VERDICT_EMOJI.get(a["verdict"], "❓")
            lines.append(f"判決：{emoji} {esc(a['verdict'])}｜信心 {a['confidence']}/10")
            lines.append("🐂 " + "；".join(esc(x) for x in a["bull_points"]))
            lines.append("🐻 " + "；".join(esc(x) for x in a["bear_points"]))
            links = _news_links(payload, a.get("news_ids", []))
            lines.append(f"📝 {esc(a['summary'])}" + (f" {links}" if links else ""))
        else:
            kd = "KD金叉、" if p.get("kd_cross") else ""
            rsi = p.get("rsi14")
            lines.append(f"  動能：{'、'.join(p.get('signals', []))}")
            lines.append(f"  技術：{kd}RSI={rsi if rsi is not None else 'N/A'}、收盤站上MA20")
            lines.append(f"  外資連買：{p.get('foreign_buy_days', 0)}日"
                         + ("" if p.get("inst_available", True) else "（法人資料缺）"))
    return lines


def _highlight_lines(payload, ai_clean):
    highlights = (ai_clean or {}).get("highlights", [])
    if not highlights:
        return []
    lines = ["📰 <b>焦點新聞</b>"]
    for h in highlights:
        links = _news_links(payload, h.get("news_ids", []))
        lines.append(f"• {esc(h['text'])}" + (f" {links}" if links else ""))
    return lines


def _watch_tomorrow_lines(ai_clean):
    points = (ai_clean or {}).get("watch_tomorrow", [])
    if not points:
        return []
    return ["🔭 <b>明日觀察</b>"] + [f"• {esc(t)}" for t in points]


def _performance_lines(perf):
    lines = [f"📈 <b>推薦追蹤（近 {perf.get('lookback_days', 20)} 個交易日）</b>"] if perf else []
    if not perf:
        return lines
    if not perf.get("total"):
        lines.append("尚無歷史紀錄（從這一版上線後開始累積）")
        return lines
    lines.append(f"共 {perf['total']} 檔｜達標 {perf['hit']}｜停損 {perf['stopped']}｜持有中 {perf['holding']}")
    win = perf.get("win_rate_pct")
    avg = perf.get("avg_return_pct")
    lines.append(f"已結案勝率 {'N/A' if win is None else f'{win:.1f}%'}｜"
                 f"平均報酬 {'N/A' if avg is None else f'{avg:+.2f}%'}")
    return lines


def _title(date):
    try:
        wd = f"（{WEEKDAYS[datetime.strptime(date, '%Y-%m-%d').weekday()]}）"
    except ValueError:
        wd = ""
    return f"📊 <b>台股盤後選股日報｜{esc(date)}{wd}</b>"


def build_messages(payload, ai_clean=None, ai_note=None, verify_issues=None, resend_note=None):
    date = payload["date"]
    picks = payload.get("picks", [])

    msg1 = [_title(date)]
    if resend_note:
        msg1.append(f"<i>{esc(resend_note)}</i>")
    broadcast = _broadcast_lines(ai_clean)
    if broadcast:
        msg1 += [""] + broadcast
    msg1.append("")
    msg1 += _market_lines(payload.get("market", {})) + [""]
    us = _us_lines(payload.get("us_markets"))
    if us:
        msg1 += us + [""]
    msg1 += _theme_lines(payload.get("themes", [])) + [""]
    msg1.append(f"📋 <b>今日推薦（{len(picks)} 檔）</b>")
    if picks:
        msg1 += [_pick_lines(i, p) for i, p in enumerate(picks, 1)]
        msg1.append(f"<i>{esc(SIGNAL_LEGEND)}</i>")  # 圖例裡有「>」，Telegram HTML 模式要跳脫
        note = _tier_summary(picks)
        if note:
            msg1.append(f"<i>{esc(note)}</i>")
    else:
        passed = payload.get("stats", {}).get("prefilter_passed")
        msg1.append("（今日沒有符合趨勢條件的個股）" if not passed
                    else f"（{passed} 檔符合趨勢條件，但 RR 都不到 {MIN_FILL_RR:g} 或算不出價位，今日不推薦）")
    watch = _watch_lines(payload, ai_clean)
    if watch:
        msg1 += [""] + watch
    msg1 += [""] + _quality_lines(payload, verify_issues)

    msg2 = []
    if payload.get("debate_ids"):
        msg2 += _debate_lines(payload, ai_clean, ai_note) + [""]
    elif ai_note:
        msg2 += [f"<i>{esc(ai_note)}</i>", ""]
    for section in (_highlight_lines(payload, ai_clean), _watch_tomorrow_lines(ai_clean)):
        if section:
            msg2 += section + [""]
    msg2 += _performance_lines(payload.get("performance")) + [""]
    msg2.append("🤖 數字由程式計算，文字由 AI 撰寫並經程式檢查｜僅供參考，非投資建議")

    messages = []
    for m in ("\n".join(msg1), "\n".join(msg2)):
        messages += split_message(m)
    return messages


def closed_message(date):
    return f"📅 {date} 沒有今日交易資料（休市，或資料源尚未更新），今天不發選股報告。"


def split_message(text, limit=TELEGRAM_LIMIT):
    """超過 Telegram 上限時，從空行或換行處切開，不截斷內容。"""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in text.split("\n"):
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return [p[:limit] for p in parts]


def send_to_telegram(messages):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return ["ERROR:未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"] * len(messages)
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"send_{datetime.now().strftime('%Y-%m-%d')}.log"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    results = []
    for i, msg in enumerate(messages):
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        status = "ERROR:未送出"
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=30)
                body = resp.json()
                if body.get("ok"):
                    status = "SUCCESS"
                    break
                status = f"FAIL:{body.get('description')}"
                if resp.status_code == 400:
                    break  # 格式錯誤重送也不會好
            except Exception as e:
                # 例外訊息可能含有網址裡的 token，只記類型
                status = f"ERROR:{type(e).__name__}"
            time.sleep(2 ** attempt)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] 訊息{i + 1}: {status}\n")
        results.append(status)
        if i < len(messages) - 1:
            time.sleep(1)
    return results
