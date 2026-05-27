# debate_analysis.py - 多空辯證分析（雲端版）
#
# 在 GitHub Actions 執行，從環境變數讀取所有 API Key。
# 使用 main.py 的選股結果取得 Top 5 個股，再用 Claude（多方）× Gemini（空方）辯證分析。

import sys
import io
import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import pytz

# 在 Windows 上強制 stdout 使用 UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─── API Key 檢查 ───

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from data_fetcher import get_latest_trading_date
from reporter import send_to_telegram

# ─── 台灣假日表（週末另外判斷） ───

TW_HOLIDAYS = set([
    # 2025
    "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29",
    "2025-01-30", "2025-01-31", "2025-02-28", "2025-04-03",
    "2025-04-04", "2025-05-01", "2025-05-30", "2025-10-10",
    # 2026
    "2026-01-01", "2026-01-26", "2026-01-27", "2026-01-28",
    "2026-01-29", "2026-01-30", "2026-02-28", "2026-04-03",
    "2026-04-05", "2026-05-01", "2026-06-19", "2026-10-10",
])


def is_trading_day_local():
    """本地判斷是否為交易日（週末 + 假日表），不呼叫 API。"""
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in TW_HOLIDAYS:
        return False
    return True


def check_api_keys():
    """檢查 AI API Key 是否已設定，未設定則推播警告並退出。"""
    if not ANTHROPIC_API_KEY:
        msg = "⚠️ ANTHROPIC_API_KEY 未設定，跳過多空辯證"
        logging.warning(msg)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_to_telegram([msg])
        sys.exit(0)


# ─── 取得選股結果 Top 5 ───

def get_top_stocks(today: str, top_n: int = 5) -> list[dict]:
    """
    執行選股流程，回傳 top_n 支通過篩選的股票資料。
    複用 main.py 的選股邏輯但不推播報告。
    """
    from config import STOCK_UNIVERSE, MIN_RR_RATIO
    from data_fetcher import (
        get_stock_price, get_institutional_investors,
        get_taiex_price, get_top_volume_stocks
    )
    from analyzer import apply_filters, calc_price_targets, calc_theme_avg_chg
    from reporter import STOCK_NAMES

    # 建立動態股票池
    dynamic_universe = dict(STOCK_UNIVERSE)
    top_vol = get_top_volume_stocks(today, top_n=10)
    for sid, name, vol in top_vol:
        if sid not in dynamic_universe:
            dynamic_universe[sid] = ["T0_當日熱門"]
        if sid not in STOCK_NAMES:
            STOCK_NAMES[sid] = name

    # 第一輪：抓價量、計算漲跌
    logging.info(f"抓取 {len(dynamic_universe)} 支股票資料...")
    raw_results = {}
    for stock_id in dynamic_universe:
        price_df = get_stock_price(stock_id, days=65)
        if price_df.empty or len(price_df) < 5:
            continue
        latest = price_df.iloc[-1]
        prev = price_df.iloc[-2] if len(price_df) >= 2 else latest
        close = latest["close"]
        prev_c = prev["close"]
        chg = (close - prev_c) / prev_c * 100 if prev_c else 0
        raw_results[stock_id] = {
            "price_df": price_df,
            "today_chg": round(chg, 2),
            "close": close,
        }

    theme_avg_chg = calc_theme_avg_chg({k: v for k, v in raw_results.items()})

    # 第二輪：篩選 + 評分
    passed = []
    for stock_id, raw in raw_results.items():
        price_df = raw["price_df"]
        inst_df = get_institutional_investors(stock_id, days=10)
        result = apply_filters(stock_id, price_df, inst_df, theme_avg_chg,
                               stock_themes=dynamic_universe)
        if not result["passed"]:
            continue

        high_60d = price_df["high"].tail(60).max()
        targets = calc_price_targets(raw["close"], high_60d)
        if not targets or targets["rr_ratio"] < MIN_RR_RATIO:
            continue

        passed.append({
            "ticker": stock_id,
            "name": STOCK_NAMES.get(stock_id, stock_id),
            "close": raw["close"],
            "rsi": result.get("rsi14", 50) or 50,
            "sharpe": 0,  # 雲端版無夏普比率
            "trend_bullish": 1,  # 已通過趨勢篩選
            "volatility": 0,
            "dividend_yield": 0,
            "foreign_buy_days": result.get("foreign_buy_days", 0),
            "revenue_yoy": 0,
            "score": result.get("score", 0) / 100,  # 轉成 0~1
            "today_chg": raw["today_chg"],
            "vol_vs_20d": result.get("vol_vs_20d", 0),
            "signals": result.get("signals", []),
        })

    passed.sort(key=lambda x: x["score"], reverse=True)
    return passed[:top_n]


# ─── 多方分析（Claude）───

BULL_SYSTEM_PROMPT = """你是一位積極進取的台股多方分析師。
你的任務是：針對系統提供的量化數據，找出最有力的做多依據。
你必須：
1. 列出 3 個具體做多理由（每個理由必須對應到一個量化指標）
2. 給出明確進場條件（價格區間或突破點）
3. 設定停利目標（%）
4. 評估信心分數 1-10

輸出格式（JSON）：
{
  "ticker": "股票代號",
  "bull_reasons": ["理由1", "理由2", "理由3"],
  "entry_price_hint": "進場價位建議",
  "target_gain_pct": 數字,
  "confidence": 數字1-10,
  "one_liner": "一句話看多理由"
}"""


def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
    """直接用 requests 呼叫 Claude API（避免 httpx 連線問題）"""
    import requests as req

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_prompt:
        payload["system"] = system_prompt

    for attempt in range(3):
        try:
            resp = req.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"].strip()
        except Exception as e:
            logging.warning(f"Claude API attempt {attempt+1}/3 失敗：{type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError("Claude API 3 次呼叫全部失敗")


def analyze_bull(stock: dict) -> dict:
    user_prompt = f"""請分析以下台股個股，提供多方論點：

股票：{stock['name']}（{stock['ticker']}）
收盤價：{stock['close']}
RSI：{stock['rsi']:.1f}
今日漲跌：{stock.get('today_chg', 0):+.2f}%
量比20日：{stock.get('vol_vs_20d', 0):.1f}倍
外資連買天數：{stock.get('foreign_buy_days', 'N/A')}
動能訊號：{', '.join(stock.get('signals', []))}
量化綜合評分：{stock['score']:.2f}（滿分1.0）

請以 JSON 格式輸出多方論點。"""

    try:
        text = _call_claude(BULL_SYSTEM_PROMPT, user_prompt)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logging.warning(f"Claude 多方分析失敗：{type(e).__name__}: {e}")
        return {"ticker": stock["ticker"], "error": str(e),
                "bull_reasons": [], "confidence": 0, "one_liner": "分析失敗"}


# ─── 空方分析（Gemini）───

BEAR_SYSTEM_PROMPT = """你是一位謹慎保守的台股空方風險分析師。
你的任務是：針對量化系統推薦的個股，找出潛在風險與看空依據。
你必須：
1. 列出 3 個具體風險點（每個必須對應量化指標或市場結構）
2. 給出明確停損條件
3. 評估最大下跌風險（%）
4. 評估信心分數 1-10（10=極度看空）

輸出格式（JSON）：
{
  "ticker": "股票代號",
  "bear_risks": ["風險1", "風險2", "風險3"],
  "stop_loss_hint": "停損條件建議",
  "max_drawdown_est_pct": 數字,
  "confidence": 數字1-10,
  "one_liner": "一句話看空理由"
}"""


def analyze_bear(stock: dict) -> dict:
    """空方分析（Claude）— 使用不同 system prompt 確保立場對立"""
    user_prompt = f"""請分析以下台股個股，提供空方風險論點：

股票：{stock['name']}（{stock['ticker']}）
收盤價：{stock['close']}
RSI：{stock['rsi']:.1f}
今日漲跌：{stock.get('today_chg', 0):+.2f}%
量比20日：{stock.get('vol_vs_20d', 0):.1f}倍
外資連買天數：{stock.get('foreign_buy_days', 'N/A')}
動能訊號：{', '.join(stock.get('signals', []))}
量化綜合評分：{stock['score']:.2f}（滿分1.0）

請以 JSON 格式輸出空方風險論點。"""

    try:
        text = _call_claude(BEAR_SYSTEM_PROMPT, user_prompt)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logging.warning(f"Claude 空方分析失敗：{e}")
        return {"ticker": stock["ticker"], "error": str(e),
                "bear_risks": [], "confidence": 0, "one_liner": "空方分析失敗"}


# ─── 整合判斷（Claude）───

INTEGRATOR_SYSTEM_PROMPT = """你是一位客觀的台股投資研究主管。
你會收到同一檔股票的「多方分析」和「空方分析」，任務是：
1. 找出雙方論點的盲點
2. 評估多空論點的說服力強弱
3. 給出最終建議：「強力做多」/「謹慎做多」/「中性觀察」/「規避」四選一
4. 給出綜合信心分數 1-10

輸出格式（JSON）：
{
  "ticker": "股票代號",
  "bull_blind_spots": ["多方沒考慮到的1"],
  "bear_blind_spots": ["空方沒考慮到的1"],
  "stronger_side": "bull 或 bear",
  "verdict": "強力做多 | 謹慎做多 | 中性觀察 | 規避",
  "final_confidence": 數字1-10,
  "summary": "2-3句綜合摘要"
}"""


def integrate(bull: dict, bear: dict) -> dict:
    user_prompt = f"""請整合以下多空分析：

【多方分析結果】
{json.dumps(bull, ensure_ascii=False, indent=2)}

【空方分析結果】
{json.dumps(bear, ensure_ascii=False, indent=2)}

請以 JSON 格式輸出整合判斷。"""

    try:
        text = _call_claude(INTEGRATOR_SYSTEM_PROMPT, user_prompt)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logging.warning(f"Claude 整合分析失敗：{type(e).__name__}: {e}")
        return {"ticker": bull.get("ticker"), "error": str(e),
                "verdict": "分析失敗", "final_confidence": 0, "summary": "整合失敗"}


# ─── 格式化 Telegram 訊息 ───

def format_debate_message(results: list, today_str: str) -> str:
    verdict_emoji = {
        "強力做多": "🟢🔥",
        "謹慎做多": "🟡✅",
        "中性觀察": "⚪👀",
        "規避": "🔴⚠️",
    }

    lines = [
        f"⚔️ <b>多空辯證報告</b> {today_str}",
        "",
        "TOP5 個股多空角力分析（Claude 多方 × Claude 空方）",
        "",
    ]

    for i, res in enumerate(results, 1):
        ticker = res["ticker"]
        name = res.get("name", ticker)
        verdict = res.get("verdict", "分析失敗")
        emoji = verdict_emoji.get(verdict, "❓")
        confidence = res.get("final_confidence", 0)
        summary = res.get("summary", "")
        bull_one = res.get("bull_one_liner", "")
        bear_one = res.get("bear_one_liner", "")

        lines.append(
            f"<b>{i}. {name}（{ticker}）</b>\n"
            f"  判決：{emoji} {verdict}  信心：{confidence}/10\n"
            f"  🐂 多方：{bull_one}\n"
            f"  🐻 空方：{bear_one}\n"
            f"  📝 {summary}"
        )
        lines.append("")

    lines += [
        "⚠️ <b>本報告為 AI 輔助分析，不構成投資建議。</b>",
        "🤖 Claude（多方）× Claude（空方）辯證生成",
    ]
    return "\n".join(lines)


# ─── 主程式 ───

def main():
    today = datetime.today().strftime("%Y-%m-%d")
    logging.info(f"多空辯證分析 {today}")

    # 非交易日跳過（使用本地假日表，不呼叫 API）
    if not is_trading_day_local():
        latest = get_latest_trading_date()
        logging.info(f"{today} 非交易日，最近交易日為 {latest}")
        print(f"非交易日，跳過（最近交易日：{latest}）")
        sys.exit(0)

    # 檢查 AI API Key
    check_api_keys()

    # 取得 Top 5
    logging.info("取得選股 Top 5...")
    stocks = get_top_stocks(today, top_n=5)

    if not stocks:
        msg = "⚔️ 多空辯證：今日無符合條件個股，跳過分析"
        logging.info(msg)
        send_to_telegram([msg])
        sys.exit(0)

    logging.info(f"開始辯證分析 {len(stocks)} 支股票...")

    # 逐支辯證
    debate_results = []
    for stock in stocks:
        logging.info(f"分析中：{stock['name']}（{stock['ticker']}）")

        bull = analyze_bull(stock)
        time.sleep(1)
        bear = analyze_bear(stock)
        time.sleep(1)
        integration = integrate(bull, bear)

        result = {
            **integration,
            "ticker": stock["ticker"],
            "name": stock["name"],
            "bull_one_liner": bull.get("one_liner", ""),
            "bear_one_liner": bear.get("one_liner", ""),
        }
        debate_results.append(result)
        verdict = integration.get("verdict", "N/A")
        conf = integration.get("final_confidence", 0)
        logging.info(f"  -> {verdict}  信心 {conf}/10")

    # 產生報告
    msg = format_debate_message(debate_results, today)
    logging.info("推播多空辯證報告...")
    send_to_telegram([msg])

    # 存檔
    report_dir = Path(__file__).parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"debate_{today}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(msg)

    detail_file = report_dir / f"debate_{today}_detail.json"
    with open(detail_file, "w", encoding="utf-8") as f:
        json.dump(debate_results, f, ensure_ascii=False, indent=2)

    logging.info(f"報告已儲存：{report_file}")
    logging.info("多空辯證分析完成")


if __name__ == "__main__":
    main()
