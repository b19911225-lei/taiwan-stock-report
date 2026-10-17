import pandas as pd

from analyzer import calc_institutional_signals
from data_fetcher import normalize_institutional_df
from tests.conftest import make_inst_daily


def _raw(rows):
    return pd.DataFrame(rows, columns=["date", "stock_id", "name", "buy", "sell"])


def test_finmind_english_names_are_recognized():
    # FinMind 的 name 欄是英文代號；舊程式用「外資」比對，永遠比對不到，籌碼分一直是 0
    raw = _raw([
        ("2026-10-15", "2330", "Foreign_Investor", 5000, 1000),
        ("2026-10-15", "2330", "Foreign_Dealer_Self", 900, 0),
        ("2026-10-15", "2330", "Investment_Trust", 300, 100),
        ("2026-10-15", "2330", "Dealer_self", 50, 10),
        ("2026-10-15", "2330", "Dealer_Hedging", 20, 40),
        ("2026-10-16", "2330", "Foreign_Investor", 3000, 1000),
        ("2026-10-16", "2330", "Investment_Trust", 0, 200),
        ("2026-10-16", "2330", "Dealer_self", 10, 0),
    ])
    daily, unknown = normalize_institutional_df(raw)
    assert unknown == []
    row = daily[daily["date"] == "2026-10-15"].iloc[0]
    assert row["foreign"] == 4000          # 外資自營商不另外加（已含在自營商裡）
    assert row["sitc"] == 200
    assert row["dealer"] == 40 + (-20)     # 自行買賣 + 避險
    sig = calc_institutional_signals(daily, "2026-10-16")
    assert sig["available"] is True
    assert sig["foreign_buy_days"] == 2


def test_chinese_names_still_work():
    raw = _raw([
        ("2026-10-16", "2330", "外資", 10, 1),
        ("2026-10-16", "2330", "投信", 5, 1),
        ("2026-10-16", "2330", "自營商", 1, 2),
    ])
    daily, unknown = normalize_institutional_df(raw)
    assert unknown == []
    assert daily.iloc[0][["foreign", "sitc", "dealer"]].tolist() == [9, 4, -1]


def test_unknown_names_are_reported_not_silently_dropped():
    raw = _raw([("2026-10-16", "2330", "Mystery_Fund", 10, 1)])
    daily, unknown = normalize_institutional_df(raw)
    assert unknown == ["Mystery_Fund"]
    assert daily.empty


def test_stale_institutional_data_is_marked_unavailable():
    daily = make_inst_daily(end_date="2026-10-15")
    sig = calc_institutional_signals(daily, "2026-10-16")
    assert sig["available"] is False
    assert sig["foreign_buy_days"] == 0


def test_signals_from_daily_data():
    daily = make_inst_daily(foreign=(-1, 1, 1, 1, 1), sitc=(1, 2, 3, 4, 5))
    sig = calc_institutional_signals(daily, "2026-10-16")
    assert sig["foreign_buy_days"] == 4
    assert sig["main_force_increasing"] is True
    assert sig["foreign_net_5d"] == 3_000_000
