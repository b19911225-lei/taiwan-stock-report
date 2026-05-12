# analyzer.py - 選股邏輯與評分

import warnings
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)
warnings.filterwarnings("ignore", category=FutureWarning)
import numpy as np
from config import (
    VOLUME_RATIO_20D, VOLUME_RATIO_5D, FOREIGN_BUY_DAYS,
    MIN_VOLUME_LOT, MIN_PRICE, MIN_RR_RATIO, THEME_STOCKS, STOCK_UNIVERSE
)


def calc_indicators(df):
    df = df.copy()
    df["MA5"]  = df["close"].rolling(5).mean()
    df["MA20"] = df["close"].rolling(20).mean()

    # RSI14
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD := 14).mean()
    loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["RSI14"] = 100 - (100 / (1 + rs))

    # KD (隨機指標，9日)
    low9  = df["low"].rolling(9).min()
    high9 = df["high"].rolling(9).max()
    df["RSV"] = (df["close"] - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["K"] = df["RSV"].ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()

    # KD 近3日金叉（K上穿D）
    k_above = df["K"] > df["D"]
    k_cross = k_above & (~k_above.shift(1).fillna(False).infer_objects(copy=False))
    df["KD_cross"] = k_cross.rolling(3).max().fillna(False).infer_objects(copy=False).astype(bool)

    # RSI 從 <50 上穿 50
    rsi_above50 = df["RSI14"] >= 50
    df["RSI_cross50"] = (rsi_above50 & (~rsi_above50.shift(1).fillna(False).infer_objects(copy=False))).fillna(False).infer_objects(copy=False)

    return df


def calc_volume_signals(df):
    if len(df) < 20:
        return 0.0, 0.0
    today_vol = df.iloc[-1]["volume"]
    avg20 = df["volume"].iloc[-21:-1].mean()
    avg5  = df["volume"].iloc[-6:-1].mean()
    vol_vs_20d = today_vol / avg20 if avg20 > 0 else 0
    vol_vs_5d  = today_vol / avg5  if avg5  > 0 else 0
    return vol_vs_20d, vol_vs_5d


def calc_institutional_signals(inst_df):
    result = {
        "foreign_buy_days": 0,
        "sitc_net_5d": 0,
        "dealer_net_5d": 0,
        "main_force_increasing": False,
    }
    if inst_df.empty or "name" not in inst_df.columns:
        return result

    # 外資連續買超天數
    foreign_df = inst_df[inst_df["name"] == "外資"].sort_values("date").tail(10)
    if not foreign_df.empty and "net" in foreign_df.columns:
        days = 0
        for net in reversed(foreign_df["net"].tolist()):
            if net > 0:
                days += 1
            else:
                break
        result["foreign_buy_days"] = days

    # 投信近5日淨買超
    sitc_df = inst_df[inst_df["name"] == "投信"].sort_values("date").tail(5)
    if not sitc_df.empty and "net" in sitc_df.columns:
        result["sitc_net_5d"] = sitc_df["net"].sum()

    # 自營近5日淨買超
    dealer_df = inst_df[inst_df["name"] == "自營商"].sort_values("date").tail(5)
    if not dealer_df.empty and "net" in dealer_df.columns:
        result["dealer_net_5d"] = dealer_df["net"].sum()

    # 主力（投信+自營）是否逐日增加
    main = pd.DataFrame()
    if not sitc_df.empty and not dealer_df.empty:
        try:
            s = sitc_df.set_index("date")["net"]
            d = dealer_df.set_index("date")["net"]
            combined = (s + d).dropna().tail(3)
            if len(combined) >= 2:
                result["main_force_increasing"] = all(
                    combined.iloc[i] > combined.iloc[i-1]
                    for i in range(1, len(combined))
                )
        except Exception:
            pass

    return result


def apply_filters(stock_id, price_df, inst_df, theme_avg_chg=None):
    if price_df.empty or len(price_df) < 25:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "資料不足", "momentum_count": 0}

    price_df = calc_indicators(price_df)
    latest   = price_df.iloc[-1]
    prev     = price_df.iloc[-2] if len(price_df) >= 2 else latest

    close     = latest["close"]
    prev_close = prev["close"]
    ma5       = latest["MA5"]
    ma20      = latest["MA20"]
    rsi14     = latest["RSI14"]
    kd_cross  = latest["KD_cross"]
    rsi_cross = latest["RSI_cross50"]
    today_vol = latest["volume"]

    if pd.isna(ma20) or pd.isna(ma5):
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "指標計算不足", "momentum_count": 0}

    # ─── 排除條件 ───
    today_chg = (close - prev_close) / prev_close if prev_close else 0
    if today_chg < -0.03:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "X1", "momentum_count": 0}

    close_5d_ago = price_df.iloc[-6]["close"] if len(price_df) >= 6 else close
    chg_5d = (close - close_5d_ago) / close_5d_ago if close_5d_ago else 0
    if chg_5d < -0.08:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "X2", "momentum_count": 0}

    if today_vol < MIN_VOLUME_LOT:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "X3", "momentum_count": 0}

    if close < MIN_PRICE:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "X4", "momentum_count": 0}

    # X5：近5日內爆量長黑
    recent5 = price_df.tail(6).iloc[:-1]
    avg_vol  = price_df["volume"].iloc[-21:-1].mean() if len(price_df) >= 21 else today_vol
    x5_triggered = False
    for _, row in recent5.iterrows():
        day_chg = (row["close"] - row["open"]) / row["open"] if row["open"] else 0
        if row["volume"] >= avg_vol * 1.5 and day_chg <= -0.03:
            x5_triggered = True
            break
    if x5_triggered:
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "X5", "momentum_count": 0}

    # ─── 趨勢條件（全部須滿足）───
    t1 = close > ma20
    t2 = ma5 > ma20
    t3 = close > prev_close
    if not (t1 and t2 and t3):
        return {"passed": False, "score": 0, "signals": [], "exclude_reason": "趨勢不符", "momentum_count": 0}

    # ─── 動能條件 ───
    vol_vs_20d, vol_vs_5d = calc_volume_signals(price_df)
    inst_signals = calc_institutional_signals(inst_df)

    signals = []
    if vol_vs_20d >= VOLUME_RATIO_20D:           signals.append("M1")
    if vol_vs_5d  >= VOLUME_RATIO_5D:            signals.append("M2")
    if inst_signals["foreign_buy_days"] >= FOREIGN_BUY_DAYS: signals.append("M3")
    if inst_signals["main_force_increasing"]:    signals.append("M4")
    if kd_cross:                                 signals.append("M5")
    if rsi_cross or (not pd.isna(rsi14) and rsi14 > 60): signals.append("M6")

    momentum_count = len(signals)
    if momentum_count < 2:
        return {"passed": False, "score": 0, "signals": signals, "exclude_reason": "動能不足", "momentum_count": momentum_count}

    # ─── 評分 ───
    # 量能分數（30分，M1~M6 每項5分）
    momentum_score = momentum_count * 5

    # 籌碼分數（25分）
    foreign_days = min(inst_signals["foreign_buy_days"], 5)
    chip_score   = foreign_days * 5

    # 技術分數（25分）
    tech_score = 0
    if t1: tech_score += 7
    if t2: tech_score += 7
    if t3: tech_score += 7
    if kd_cross: tech_score += 2
    if rsi_cross or (not pd.isna(rsi14) and rsi14 > 60): tech_score += 2

    # 主題熱度（20分）
    theme_score = 0
    if theme_avg_chg is not None:
        themes = STOCK_UNIVERSE.get(stock_id, [])
        max_chg = max((theme_avg_chg.get(t, 0) for t in themes), default=0)
        if max_chg > 0.01:   theme_score = 20
        elif max_chg > 0:    theme_score = 10

    score = min(momentum_score + chip_score + tech_score + theme_score, 100)

    return {
        "passed": True,
        "score": score,
        "signals": signals,
        "exclude_reason": None,
        "momentum_count": momentum_count,
        "vol_vs_20d": round(vol_vs_20d, 2),
        "vol_vs_5d":  round(vol_vs_5d, 2),
        "foreign_buy_days": inst_signals["foreign_buy_days"],
        "sitc_net_5d": inst_signals["sitc_net_5d"],
        "rsi14": round(rsi14, 1) if not pd.isna(rsi14) else None,
        "kd_cross": bool(kd_cross),
        "today_chg": round(today_chg * 100, 2),
        "close": close,
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
    }


def calc_price_targets(close, high_60d):
    entry_low   = round(close * 0.98, 2)
    entry_high  = round(close * 1.02, 2)
    stop_loss   = round(close * 0.93, 2)
    risk        = close - stop_loss

    # 以60日高點為初始目標，若空間不足則以「最低2倍RR」的合理延伸為目標
    raw_target = max(high_60d, close * 1.15)
    target1    = round(raw_target, 2)
    potential  = target1 - close
    rr_ratio   = round(potential / risk, 2) if risk > 0 else 0

    if rr_ratio < MIN_RR_RATIO:
        return None
    return {
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "target1":    target1,
        "stop_loss":  stop_loss,
        "rr_ratio":   rr_ratio,
    }


def calc_theme_avg_chg(all_results):
    """計算各主題平均漲跌幅"""
    theme_chgs = {t: [] for t in THEME_STOCKS}
    for stock_id, res in all_results.items():
        if "today_chg" not in res:
            continue
        for theme in STOCK_UNIVERSE.get(stock_id, []):
            theme_chgs[theme].append(res["today_chg"] / 100)
    return {t: np.mean(v) if v else 0 for t, v in theme_chgs.items()}
