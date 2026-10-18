# performance.py - 追蹤過去推薦的結果
#
# 每一檔推薦從推薦日的收盤價進場，之後逐日檢查：
#   先碰到停損 → 停損（跳空開低時用開盤價出場）
#   先碰到目標 → 達標
#   都沒碰到   → 持有中，用最新收盤價計算報酬
# 同一天同時碰到停損和目標時，保守算停損。

import pandas as pd


def evaluate_pick(pick, price_df):
    entry = float(pick["close"])
    target = float(pick["target1"])
    stop = float(pick["stop_loss"])
    out = {"stock_id": pick["stock_id"], "date": pick["date"], "entry": entry,
           "target1": target, "stop_loss": stop}
    if price_df is None or price_df.empty or entry <= 0:
        return {**out, "status": "無資料", "return_pct": None}

    after = price_df[pd.to_datetime(price_df["date"]) > pd.Timestamp(pick["date"])]
    for _, bar in after.iterrows():
        if bar["low"] <= stop:
            exit_price = min(stop, float(bar["open"])) if bar["open"] > 0 else stop
            return {**out, "status": "停損", "exit": exit_price,
                    "exit_date": pd.Timestamp(bar["date"]).strftime("%Y-%m-%d"),
                    "return_pct": round((exit_price - entry) / entry * 100, 2)}
        if bar["high"] >= target:
            return {**out, "status": "達標", "exit": target,
                    "exit_date": pd.Timestamp(bar["date"]).strftime("%Y-%m-%d"),
                    "return_pct": round((target - entry) / entry * 100, 2)}
    last = float(after["close"].iloc[-1]) if not after.empty else entry
    return {**out, "status": "持有中", "exit": None,
            "return_pct": round((last - entry) / entry * 100, 2)}


def summarize(past_picks, price_lookup, lookback_days):
    """
    past_picks: [(日期, [pick, ...]), ...]，日期由新到舊，不含今天
    price_lookup: {代號: 價格 DataFrame}
    """
    dates = [d for d, _ in past_picks][:lookback_days]
    items = []
    for d, picks in past_picks[:lookback_days]:
        for p in picks:
            items.append(evaluate_pick({**p, "date": d}, price_lookup.get(p["stock_id"])))

    counted = [i for i in items if i["status"] != "無資料"]
    hit = sum(1 for i in counted if i["status"] == "達標")
    stopped = sum(1 for i in counted if i["status"] == "停損")
    holding = sum(1 for i in counted if i["status"] == "持有中")
    returns = [i["return_pct"] for i in counted if i["return_pct"] is not None]
    return {
        "lookback_days": lookback_days,
        "days_with_history": len(dates),
        "total": len(counted),
        "hit": hit,
        "stopped": stopped,
        "holding": holding,
        "no_data": len(items) - len(counted),
        "win_rate_pct": round(hit / (hit + stopped) * 100, 1) if (hit + stopped) else None,
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "items": items,
    }
