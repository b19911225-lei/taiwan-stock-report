# reporter.py - 報告產生與 Telegram 推送

import time
import logging
from datetime import datetime
from pathlib import Path
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, THEME_STOCKS, STOCK_UNIVERSE

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

STOCK_NAMES = {
    "2330": "台積電", "3711": "日月光投控", "2325": "矽品", "3034": "聯詠",
    "6274": "台燿", "3008": "大立光", "2308": "台達電",
    "6533": "晶心科", "6247": "成電", "6530": "創意", "3105": "穩懋",
    "4977": "眾達電通", "3490": "單驥",
    "3081": "聯亞", "2455": "全訊", "2338": "光罩", "3508": "位速",
    "8150": "南茂", "6147": "頎邦", "3014": "台揚",
}


def _get_name(stock_id):
    return STOCK_NAMES.get(stock_id, stock_id)


def _sentiment(chg_pct):
    if chg_pct >= 1.5:   return "🚀 強勢上攻"
    if chg_pct >= 0.5:   return "📈 偏多格局"
    if chg_pct >= -0.5:  return "😐 盤整觀望"
    if chg_pct >= -1.5:  return "📉 偏弱震盪"
    return "🔻 空方主導"


def build_report(results, market_data, date):
    """建立報告，回傳訊息列表（最多2則）"""
    # ─── 大盤概況 ───
    taiex_str = "N/A"
    chg_str   = "N/A"
    vol_str   = "N/A"
    sentiment_str = ""

    if market_data is not None and not market_data.empty:
        try:
            row = market_data.iloc[-1]
            taiex = float(row.get("close", row.get("price", 0)))
            # 計算漲跌
            if len(market_data) >= 2:
                prev_row = market_data.iloc[-2]
                prev_close = float(prev_row.get("close", prev_row.get("price", taiex)))
                chg_pct = (taiex - prev_close) / prev_close * 100 if prev_close else 0
                chg_str = f"{chg_pct:+.2f}%"
                sentiment_str = _sentiment(chg_pct)
            taiex_str = f"{taiex:,.0f}"
            if "Trading_Volume" in row or "volume" in row:
                vol = float(row.get("volume", row.get("Trading_Volume", 0)))
                vol_str = f"{vol/1e8:.0f}" if vol > 1e8 else f"{vol:.0f}"
        except Exception:
            pass

    # ─── 主題強弱 ───
    theme_avg = {}
    for theme, stocks in THEME_STOCKS.items():
        chgs = [r["today_chg"] for s in stocks
                if (r := results.get(s)) and "today_chg" in r]
        theme_avg[theme] = sum(chgs) / len(chgs) if chgs else 0

    theme_sorted = sorted(theme_avg.items(), key=lambda x: x[1], reverse=True)
    theme_lines = []
    for i, (t, avg) in enumerate(theme_sorted):
        short = t.split("_", 1)[1] if "_" in t else t
        star  = "★" if i == 0 else "  "
        arrow = "▲" if avg > 0 else ("▼" if avg < 0 else "─")
        theme_lines.append(f"{star}{short}：{arrow}{abs(avg):.2f}%")

    # ─── 個股清單 ───
    passed = [(sid, r) for sid, r in results.items() if r.get("passed") and r.get("targets")]
    passed.sort(key=lambda x: x[1]["score"], reverse=True)
    passed = passed[:15]

    stock_lines = []
    for rank, (sid, r) in enumerate(passed, 1):
        name   = _get_name(sid)
        themes = [t.split("_", 1)[1] for t in STOCK_UNIVERSE.get(sid, [])]
        theme_tag = "/".join(themes[:2])
        close  = r.get("close", 0)
        chg    = r.get("today_chg", 0)
        score  = r.get("score", 0)
        sigs   = " ".join(r.get("signals", []))
        tgt    = r["targets"]
        arrow  = "▲" if chg >= 0 else "▼"
        line1 = (f"{rank}. <b>{sid} {name}</b> ｜{theme_tag}｜"
                 f"{close} {arrow}{abs(chg):.2f}% ｜評分{score}｜{sigs}")
        rr     = tgt["rr_ratio"]
        entry  = f"{tgt['entry_low']}~{tgt['entry_high']}"
        line2  = (f"   進場:{entry} 目標:{tgt['target1']} 停損:{tgt['stop_loss']} "
                  f"RR:{rr:.1f}｜{_brief_comment(r)}")
        stock_lines.append(line1 + "\n" + line2)

    # ─── 組合第一則訊息 ───
    msg1_parts = [
        f"📊 <b>台股選股日報 {date}</b>\n",
        f"🏦 <b>大盤概況</b>",
        f"加權指數：{taiex_str} （{chg_str}）",
        f"成交量：{vol_str} 億",
        f"氛圍：{sentiment_str}\n",
        f"🔥 <b>主題強弱</b>",
        "\n".join(theme_lines),
        f"\n📋 <b>觀察清單（{len(passed)}支）</b>",
        "\n".join(stock_lines) if stock_lines else "（今日無符合條件個股）",
    ]
    msg1 = "\n".join(msg1_parts)

    # ─── 第二則訊息：Top3 亮點 ───
    top3 = passed[:3]
    top3_lines = [f"⭐ <b>今日亮點 Top 3</b>\n"]
    for sid, r in top3:
        name = _get_name(sid)
        sigs = r.get("signals", [])
        rsi  = r.get("rsi14", "N/A")
        f_days = r.get("foreign_buy_days", 0)
        kd   = "KD金叉、" if r.get("kd_cross") else ""
        themes = [t.split("_", 1)[1] for t in STOCK_UNIVERSE.get(sid, [])]
        top3_lines.append(
            f"<b>{sid} {name}</b>\n"
            f"  動能：{'、'.join(sigs)}\n"
            f"  技術：{kd}RSI={rsi}、收盤突破MA20\n"
            f"  主題：{' / '.join(themes)}\n"
            f"  外資連買：{f_days}日\n"
        )

    obs_lines = [
        f"\n👀 <b>明日觀察</b>",
        "1. 留意外資是否持續買超主流股",
        "2. 觀察大盤成交量能否維持穩定",
        "3. KD高檔個股注意追高風險\n",
        "🤖 自動產出｜僅供參考，非投資建議",
    ]
    msg2 = "\n".join(top3_lines + obs_lines)

    messages = [msg1]
    if top3:
        messages.append(msg2)

    return messages


def _brief_comment(r):
    sigs = r.get("signals", [])
    parts = []
    if "M3" in sigs:
        parts.append(f"外資連買{r.get('foreign_buy_days',0)}日")
    if "M5" in sigs:
        parts.append("KD金叉")
    if "M6" in sigs:
        parts.append(f"RSI強勢({r.get('rsi14',''):.0f})" if r.get("rsi14") else "RSI強勢")
    if "M1" in sigs:
        parts.append(f"量能放大{r.get('vol_vs_20d',0):.1f}倍")
    return "、".join(parts[:2]) if parts else "多訊號共振"


def send_to_telegram(messages: list):
    date_str = datetime.today().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"send_{date_str}.log"
    results  = []

    for i, msg in enumerate(messages):
        # 超過 4000 字自動截斷（已在 build_report 拆分）
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            ok = resp.json().get("ok", False)
            status = "SUCCESS" if ok else f"FAIL:{resp.json().get('description')}"
        except Exception as e:
            status = f"ERROR:{e}"

        log_line = f"[{datetime.now()}] 訊息{i+1}: {status}"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
        results.append(status)

        if i < len(messages) - 1:
            time.sleep(1)

    return results


def send_test_message(text="✅ 台股選股系統連線測試成功"):
    return send_to_telegram([text])
