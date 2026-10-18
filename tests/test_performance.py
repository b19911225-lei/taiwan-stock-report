import pandas as pd

from performance import evaluate_pick, summarize


def _bars(rows):
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"]).assign(
        date=lambda d: pd.to_datetime(d["date"]))


PICK = {"stock_id": "2330", "date": "2026-10-01", "close": 100, "target1": 110, "stop_loss": 95}


def test_hit_target():
    bars = _bars([("2026-10-01", 99, 101, 98, 100), ("2026-10-02", 101, 111, 100, 109)])
    r = evaluate_pick(PICK, bars)
    assert r["status"] == "達標" and r["return_pct"] == 10.0


def test_hit_stop_and_gap_down_exits_at_open():
    bars = _bars([("2026-10-02", 101, 103, 99, 102), ("2026-10-05", 90, 92, 88, 91)])
    r = evaluate_pick(PICK, bars)
    assert r["status"] == "停損" and r["exit"] == 90 and r["return_pct"] == -10.0


def test_same_bar_both_counts_as_stop():
    bars = _bars([("2026-10-02", 100, 112, 94, 105)])
    assert evaluate_pick(PICK, bars)["status"] == "停損"


def test_holding_uses_latest_close():
    bars = _bars([("2026-10-02", 100, 105, 97, 104)])
    r = evaluate_pick(PICK, bars)
    assert r["status"] == "持有中" and r["return_pct"] == 4.0


def test_summary_counts():
    bars = _bars([("2026-10-02", 101, 111, 100, 109)])
    past = [("2026-10-01", [PICK, {**PICK, "stock_id": "9999"}])]
    s = summarize(past, {"2330": bars}, 20)
    assert s["total"] == 1 and s["hit"] == 1 and s["no_data"] == 1
    assert s["win_rate_pct"] == 100.0


def test_summary_without_history():
    s = summarize([], {}, 20)
    assert s["total"] == 0 and s["win_rate_pct"] is None
