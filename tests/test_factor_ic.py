"""tests/test_factor_ic.py — FactorICAnalyzer 單元測試"""
import numpy as np
import pandas as pd
import pytest

from modules.factor_ic import FactorICAnalyzer


@pytest.fixture
def analyzer():
    return FactorICAnalyzer()


def make_snapshots(n_dates=12, n_stocks=30, seed=42):
    """製造測試用的因子快照與報酬快照（因子與報酬正相關）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_dates, freq="MS")
    tickers = [f"TICKER_{i:04d}" for i in range(n_stocks)]

    factor_snapshots = {}
    returns_snapshots = {}

    for date in dates:
        factor_vals = rng.standard_normal(n_stocks)
        returns = factor_vals * 0.5 + rng.standard_normal(n_stocks) * 0.5

        factor_snapshots[date] = pd.DataFrame(
            {"test_factor": factor_vals}, index=tickers
        )
        returns_snapshots[date] = pd.Series(returns, index=tickers)

    return factor_snapshots, returns_snapshots


class TestCalculateICSeries:
    def test_returns_series(self, analyzer):
        snaps, rets = make_snapshots()
        ic = analyzer.calculate_ic_series(snaps, rets, "test_factor")
        assert isinstance(ic, pd.Series)
        assert len(ic) == 12

    def test_ic_positive_for_correlated_factor(self, analyzer):
        snaps, rets = make_snapshots()
        ic = analyzer.calculate_ic_series(snaps, rets, "test_factor")
        assert ic.mean() > 0

    def test_missing_factor_col_returns_empty(self, analyzer):
        snaps, rets = make_snapshots()
        ic = analyzer.calculate_ic_series(snaps, rets, "nonexistent_factor")
        assert ic.empty

    def test_insufficient_stocks_skipped(self, analyzer):
        snaps, rets = make_snapshots(n_stocks=5)
        ic = analyzer.calculate_ic_series(snaps, rets, "test_factor")
        assert ic.empty


class TestCalculateICIR:
    def test_returns_all_keys(self, analyzer):
        ic = pd.Series([0.05, 0.03, 0.07, 0.04, 0.06])
        result = analyzer.calculate_icir(ic)
        for key in ["IC_mean", "IC_std", "IR", "IC_positive_rate", "n_periods", "grade"]:
            assert key in result

    def test_positive_ic_positive_ir(self, analyzer):
        ic = pd.Series([0.05] * 20)
        result = analyzer.calculate_icir(ic)
        assert result["IR"] > 0

    def test_empty_series_returns_nan(self, analyzer):
        result = analyzer.calculate_icir(pd.Series(dtype=float))
        assert np.isnan(result["IC_mean"])
        assert result["n_periods"] == 0

    def test_grade_strong_factor(self, analyzer):
        ic = pd.Series([0.08] * 30)
        result = analyzer.calculate_icir(ic)
        assert result["grade"] == "強因子"

    def test_grade_weak_factor(self, analyzer):
        ic = pd.Series([0.001, -0.001, 0.002, -0.002] * 5)
        result = analyzer.calculate_icir(ic)
        assert result["grade"] == "弱因子"
