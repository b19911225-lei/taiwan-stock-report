import pytest

import analyzer
from analyzer import calc_indicators, calc_price_targets, prefilter, score, apply_filters
from config import MIN_STOP_PCT, STOP_LOSS_PCT
from tests.conftest import make_price_df, make_inst_daily


def test_indicators_run_and_have_bool_flags(price_df):
    df = calc_indicators(price_df)
    for col in ["MA5", "MA20", "RSI14", "K", "D", "ATR"]:
        assert col in df
    assert df["KD_cross"].dtype == bool
    assert df["RSI_cross50"].dtype == bool
    assert df["ATR"].iloc[-1] > 0


def test_prefilter_passes_uptrend(price_df):
    pre = prefilter(price_df)
    assert pre["passed"], pre.get("exclude_reason")
    assert pre["close"] > pre["ma20"]


def test_prefilter_excludes_big_drop():
    df = make_price_df(last_chg=-0.05)
    assert prefilter(df)["exclude_reason"] == "X1"


def test_prefilter_excludes_downtrend():
    df = make_price_df(drift=-0.004, last_chg=0.001)
    assert prefilter(df)["exclude_reason"] in ("趨勢不符", "X2")


def test_prefilter_needs_enough_data():
    assert prefilter(make_price_df(n=20))["exclude_reason"] == "資料不足"


def test_score_counts_foreign_buying(price_df):
    pre = prefilter(price_df)
    inst = analyzer.calc_institutional_signals(make_inst_daily(), "2026-10-16")
    res = score("2330", pre, inst)
    assert res["passed"]
    assert "M3" in res["signals"]          # 外資連買 4 日
    assert res["score_breakdown"]["chip"] == 20


def test_score_without_inst_data_still_works(price_df):
    pre = prefilter(price_df)
    inst = analyzer.calc_institutional_signals(None, "2026-10-16")
    res = score("2330", pre, inst)
    assert res["inst_available"] is False
    assert res["score_breakdown"]["chip"] == 0


def test_apply_filters_wrapper(price_df):
    inst = analyzer.calc_institutional_signals(make_inst_daily(), "2026-10-16")
    assert apply_filters("2330", price_df, inst)["passed"]


def test_old_rr_formula_was_constant():
    # 舊公式：停損 -7%、目標 max(60 日高, +15%) → RR 永遠 ≥ 15/7
    for close, high in [(100, 90), (100, 101), (50, 52)]:
        stop = close * 0.93
        target = max(high, close * 1.15)
        assert (target - close) / (close - stop) == pytest.approx(15 / 7)


def test_new_rr_varies_with_volatility_and_distance_to_ma20():
    calm = calc_price_targets(close=100, high_60d=101, ma20=98, atr=1.5)
    wild = calc_price_targets(close=100, high_60d=101, ma20=90, atr=4)
    assert calm["rr_ratio"] != wild["rr_ratio"]
    # 離 MA20 很遠、波動小 → RR 不夠
    stretched = calc_price_targets(close=100, high_60d=101, ma20=85, atr=1)
    assert stretched["rr_ratio"] < 2


@pytest.mark.parametrize("ma20", [99.9, 97, 92, 70])
def test_stop_is_between_3_and_7_percent(ma20):
    t = calc_price_targets(close=100, high_60d=110, ma20=ma20, atr=2)
    assert 100 * (1 - STOP_LOSS_PCT) - 1e-9 <= t["stop_loss"] <= 100 * (1 - MIN_STOP_PCT) + 1e-9


def test_target_uses_higher_of_60d_high_and_atr():
    t = calc_price_targets(close=100, high_60d=130, ma20=96, atr=2)
    assert t["target1"] == 130 and t["target_basis"] == "60日高點"
    t = calc_price_targets(close=100, high_60d=101, ma20=96, atr=2)
    assert t["target1"] == 108 and "ATR" in t["target_basis"]


def test_targets_need_valid_atr():
    assert calc_price_targets(close=100, high_60d=110, ma20=96, atr=None) is None


def test_score_card_matches_score_for_passing_stock(price_df):
    pre = prefilter(price_df)
    inst = analyzer.calc_institutional_signals(make_inst_daily(), "2026-10-16")
    ev, _ = analyzer.evaluate(price_df)
    card = analyzer.score_card("2330", ev, inst)
    res = score("2330", pre, inst)
    assert card["score"] == res["score"] and card["score_breakdown"] == res["score_breakdown"]
    assert card["targets"] == calc_price_targets(res["close"], res["high_60d"], res["ma20"], res["atr"])


def test_score_card_explains_why_a_stock_was_not_picked():
    # 自選股沒通過篩選也要有分數和原因（例如 4577 不在股票池、趨勢不符）
    df = make_price_df(drift=-0.004, last_chg=-0.05, last_vol_mult=1.0)
    ev, _ = analyzer.evaluate(df)
    assert "X1" in ev["exclusions"] and not ev["trend"]["T1"]
    card = analyzer.score_card("4577", ev, analyzer.calc_institutional_signals(None, "2026-10-16"))
    assert card["score"] < 50
    assert 7 * sum(ev["trend"].values()) <= card["score_breakdown"]["tech"] <= 7 * sum(ev["trend"].values()) + 4
    assert "今日跌幅超過 3%" in card["fail_reasons"]
    assert "收盤沒有站上 MA20" in card["fail_reasons"]
    assert any("動能訊號只有" in r for r in card["fail_reasons"])


def test_evaluate_lists_every_exclusion_but_prefilter_reports_the_first():
    df = make_price_df(drift=-0.02, last_chg=-0.05, base_vol=100)
    ev, _ = analyzer.evaluate(df)
    assert ev["exclusions"][:3] == ["X1", "X2", "X3"]
    assert prefilter(df)["exclude_reason"] == "X1"
