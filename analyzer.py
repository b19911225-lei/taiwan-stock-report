# analyzer.py - 選股邏輯與評分（規則說明見 docs/SPEC.md）
#
# 流程拆成兩段，減少對 FinMind 的請求：
#   prefilter()：只用價量判斷排除條件 X1~X5 和趨勢條件 T1~T3
#   score()：通過 prefilter 的股票才去抓法人資料，再算動能訊號和評分

import numpy as np
import pandas as pd

from config import (
    VOLUME_RATIO_20D, VOLUME_RATIO_5D, FOREIGN_BUY_DAYS, RSI_PERIOD,
    MIN_VOLUME_LOT, MIN_PRICE, THEME_STOCKS, STOCK_UNIVERSE,
    STOP_LOSS_PCT, MIN_STOP_PCT, STOP_BELOW_MA20, ATR_PERIOD, ATR_TARGET_MULT,
)


def _f(x, nd=2):
    """轉成可寫進 JSON 的 float；NaN 回 None。"""
    if x is None or (isinstance(x, float) and np.isnan(x)) or pd.isna(x):
        return None
    return round(float(x), nd)


def calc_indicators(df):
    df = df.copy()
    close = df["close"]
    df["MA5"] = close.rolling(5).mean()
    df["MA20"] = close.rolling(20).mean()

    # RSI14（簡單平均版）
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    df["RSI14"] = rsi.mask((loss == 0) & (gain > 0), 100.0)  # 14 日都沒跌時 RSI = 100

    # KD（9 日）
    low9 = df["low"].rolling(9).min()
    high9 = df["high"].rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["K"] = rsv.ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()

    # KD 近 3 日金叉（K 上穿 D）
    k_above = (df["K"] > df["D"]).astype(bool)
    k_cross = k_above & ~k_above.shift(1, fill_value=False)
    df["KD_cross"] = k_cross.astype(int).rolling(3, min_periods=1).max().astype(bool)

    # RSI 從 <50 上穿 50
    rsi_above50 = (df["RSI14"] >= 50).astype(bool)
    df["RSI_cross50"] = rsi_above50 & ~rsi_above50.shift(1, fill_value=False)

    # ATR（真實波幅平均），用來算目標價
    prev_close = close.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(ATR_PERIOD).mean()
    return df


def calc_volume_signals(df):
    if len(df) < 21:
        return 0.0, 0.0
    today_vol = df.iloc[-1]["volume"]
    avg20 = df["volume"].iloc[-21:-1].mean()
    avg5 = df["volume"].iloc[-6:-1].mean()
    vol_vs_20d = today_vol / avg20 if avg20 > 0 else 0.0
    vol_vs_5d = today_vol / avg5 if avg5 > 0 else 0.0
    return float(vol_vs_20d), float(vol_vs_5d)


def calc_institutional_signals(inst_daily, data_date=None):
    """
    inst_daily：data_fetcher.normalize_institutional_df() 的結果（每日一列，單位是股）。
    最新一筆不是 data_date（法人資料還沒更新）時視為缺資料，不拿舊資料算訊號。
    """
    result = {
        "available": False,
        "latest_date": None,
        "foreign_buy_days": 0,
        "foreign_net_5d": 0,
        "sitc_net_5d": 0,
        "dealer_net_5d": 0,
        "main_force_increasing": False,
    }
    if inst_daily is None or inst_daily.empty:
        return result
    df = inst_daily.sort_values("date").reset_index(drop=True)
    latest = pd.to_datetime(df["date"].iloc[-1]).strftime("%Y-%m-%d")
    result["latest_date"] = latest
    if data_date and latest != data_date:
        return result
    result["available"] = True

    days = 0
    for net in reversed(df["foreign"].tolist()):
        if net > 0:
            days += 1
        else:
            break
    result["foreign_buy_days"] = days

    tail5 = df.tail(5)
    result["foreign_net_5d"] = int(tail5["foreign"].sum())
    result["sitc_net_5d"] = int(tail5["sitc"].sum())
    result["dealer_net_5d"] = int(tail5["dealer"].sum())

    # 主力（投信 + 自營）近 3 日是否逐日增加
    main = (df["sitc"] + df["dealer"]).tail(3).tolist()
    if len(main) >= 2:
        result["main_force_increasing"] = all(main[i] > main[i - 1] for i in range(1, len(main)))
    return result


def _fail(reason, signals=None):
    return {"passed": False, "score": 0, "signals": signals or [], "exclude_reason": reason, "momentum_count": 0}


def prefilter(price_df):
    """排除條件與趨勢條件（只用價量）。通過時回傳 passed=True 和計算好的指標。"""
    if price_df is None or price_df.empty or len(price_df) < 25:
        return _fail("資料不足")

    df = calc_indicators(price_df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close, prev_close = float(latest["close"]), float(prev["close"])
    ma5, ma20 = latest["MA5"], latest["MA20"]
    if pd.isna(ma20) or pd.isna(ma5):
        return _fail("指標計算不足")

    # ─── 排除條件 ───
    today_chg = (close - prev_close) / prev_close if prev_close else 0.0
    if today_chg < -0.03:
        return _fail("X1")

    close_5d_ago = float(df.iloc[-6]["close"])
    chg_5d = (close - close_5d_ago) / close_5d_ago if close_5d_ago else 0.0
    if chg_5d < -0.08:
        return _fail("X2")

    if latest["volume"] < MIN_VOLUME_LOT:
        return _fail("X3")

    if close < MIN_PRICE:
        return _fail("X4")

    # X5：近 5 日內爆量長黑
    avg_vol = df["volume"].iloc[-21:-1].mean() if len(df) >= 21 else latest["volume"]
    for _, row in df.tail(6).iloc[:-1].iterrows():
        day_chg = (row["close"] - row["open"]) / row["open"] if row["open"] else 0.0
        if row["volume"] >= avg_vol * 1.5 and day_chg <= -0.03:
            return _fail("X5")

    # ─── 趨勢條件（全部須滿足）───
    t1 = close > ma20
    t2 = ma5 > ma20
    t3 = close > prev_close
    if not (t1 and t2 and t3):
        return _fail("趨勢不符")

    return {
        "passed": True,
        "df": df,
        "close": close,
        "today_chg": today_chg,
        "chg_5d": chg_5d,
        "ma5": float(ma5),
        "ma20": float(ma20),
        "rsi14": latest["RSI14"],
        "kd_cross": bool(latest["KD_cross"]),
        "rsi_cross": bool(latest["RSI_cross50"]),
        "atr": latest["ATR"],
        "high_60d": float(df["high"].tail(60).max()),
    }


def calc_theme_score(stock_id, theme_avg_chg, stock_themes):
    if not theme_avg_chg:
        return 0
    themes = stock_themes.get(stock_id, [])
    max_chg = max((theme_avg_chg.get(t) or 0 for t in themes), default=0)
    if max_chg > 0.01:
        return 20
    if max_chg > 0:
        return 10
    return 0


def score(stock_id, pre, inst_signals, theme_avg_chg=None, stock_themes=None):
    """prefilter 通過後，計算動能訊號 M1~M6 與評分（滿分 100）。"""
    if stock_themes is None:
        stock_themes = STOCK_UNIVERSE
    df = pre["df"]
    vol_vs_20d, vol_vs_5d = calc_volume_signals(df)
    rsi14 = pre["rsi14"]
    rsi_strong = pre["rsi_cross"] or (not pd.isna(rsi14) and rsi14 > 60)

    signals = []
    if vol_vs_20d >= VOLUME_RATIO_20D:                         signals.append("M1")
    if vol_vs_5d >= VOLUME_RATIO_5D:                           signals.append("M2")
    if inst_signals["foreign_buy_days"] >= FOREIGN_BUY_DAYS:   signals.append("M3")
    if inst_signals["main_force_increasing"]:                  signals.append("M4")
    if pre["kd_cross"]:                                        signals.append("M5")
    if rsi_strong:                                             signals.append("M6")

    momentum_count = len(signals)
    base = {
        "signals": signals,
        "momentum_count": momentum_count,
        "vol_vs_20d": round(vol_vs_20d, 2),
        "vol_vs_5d": round(vol_vs_5d, 2),
        "inst_available": inst_signals["available"],
        "foreign_buy_days": inst_signals["foreign_buy_days"],
        "foreign_net_5d_lots": int(inst_signals["foreign_net_5d"] / 1000),
        "sitc_net_5d_lots": int(inst_signals["sitc_net_5d"] / 1000),
        "dealer_net_5d_lots": int(inst_signals["dealer_net_5d"] / 1000),
        "rsi14": _f(rsi14, 1),
        "kd_cross": pre["kd_cross"],
        "today_chg": round(pre["today_chg"] * 100, 2),
        "chg_5d": round(pre["chg_5d"] * 100, 2),
        "close": _f(pre["close"]),
        "ma5": _f(pre["ma5"]),
        "ma20": _f(pre["ma20"]),
        "atr": _f(pre["atr"]),
        "high_60d": _f(pre["high_60d"]),
    }
    if momentum_count < 2:
        return {**base, "passed": False, "score": 0, "exclude_reason": "動能不足"}

    momentum_score = momentum_count * 5                                   # 30 分
    chip_score = min(inst_signals["foreign_buy_days"], 5) * 5             # 25 分
    tech_score = 21 + (2 if pre["kd_cross"] else 0) + (2 if rsi_strong else 0)  # 25 分（T1~T3 各 7 分）
    theme_score = calc_theme_score(stock_id, theme_avg_chg, stock_themes)  # 20 分
    total = min(momentum_score + chip_score + tech_score + theme_score, 100)

    return {
        **base,
        "passed": True,
        "score": total,
        "score_breakdown": {"momentum": momentum_score, "chip": chip_score,
                            "tech": tech_score, "theme": theme_score},
        "exclude_reason": None,
    }


def apply_filters(stock_id, price_df, inst_signals, theme_avg_chg=None, stock_themes=None):
    """prefilter + score 一次做完（測試和單檔分析用）。"""
    pre = prefilter(price_df)
    if not pre["passed"]:
        return pre
    return score(stock_id, pre, inst_signals, theme_avg_chg, stock_themes)


def calc_price_targets(close, high_60d, ma20, atr):
    """
    停損：MA20 下方 1%，但距離收盤至少 3%、最多 7%。
    目標：60 日高點和「收盤 + 4 倍 ATR」取較高者。
    原本的目標是 max(60 日高點, 收盤 × 1.15)、停損固定 -7%，RR 最低就是 15/7 ≈ 2.14，
    「RR ≥ 2」這道篩選永遠會過；現在 RR 會隨波動度和離 MA20 的距離變化。
    """
    if not close or close <= 0 or atr is None or pd.isna(atr) or atr <= 0:
        return None
    stop = ma20 * (1 - STOP_BELOW_MA20) if ma20 else close * (1 - STOP_LOSS_PCT)
    stop = max(stop, close * (1 - STOP_LOSS_PCT))
    stop = min(stop, close * (1 - MIN_STOP_PCT))

    atr_target = close + ATR_TARGET_MULT * atr
    if high_60d and high_60d > atr_target:
        target, basis = high_60d, "60日高點"
    else:
        target, basis = atr_target, f"{ATR_TARGET_MULT:g}倍ATR"

    risk = close - stop
    rr = (target - close) / risk if risk > 0 else 0.0
    return {
        "entry_low": round(close * 0.98, 2),
        "entry_high": round(close * 1.02, 2),
        "target1": round(target, 2),
        "target_basis": basis,
        "stop_loss": round(stop, 2),
        "rr_ratio": round(rr, 2),
    }


def calc_theme_avg_chg(raw_results):
    """各主題平均漲跌幅（小數）；整個主題都沒有資料時是 None。raw_results: {代號: {"today_chg": 百分比}}。"""
    out = {}
    for theme, stocks in THEME_STOCKS.items():
        chgs = [raw_results[s]["today_chg"] / 100 for s in stocks
                if s in raw_results and "today_chg" in raw_results[s]]
        out[theme] = float(np.mean(chgs)) if chgs else None
    return out
