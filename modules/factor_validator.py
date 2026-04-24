"""
factor_validator.py — 因子有效性驗證模組

提供 IC（資訊係數）與 ICIR 計算，協助識別無效因子並建立動態加權合成。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from scipy.stats import spearmanr
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


class FactorValidator:
    """因子有效性驗證器：IC/ICIR 計算、因子排名、IC 加權動態合成。"""

    IC_THRESHOLDS = {
        "強":   0.05,
        "中":   0.03,
        "弱":   0.01,
        "無效": 0.00,
    }

    def __init__(self) -> None:
        self.logger = logging.getLogger("FactorValidator")

    # ------------------------------------------------------------------
    # 輔助
    # ------------------------------------------------------------------

    @staticmethod
    def _spearman(x: pd.Series, y: pd.Series) -> float:
        """計算兩個 Series 的 Spearman 等級相關係數。"""
        common = x.dropna().index.intersection(y.dropna().index)
        if len(common) < 5:
            return np.nan
        if _SCIPY_AVAILABLE:
            corr, _ = spearmanr(x.loc[common].values, y.loc[common].values)
            return float(corr)
        return float(x.loc[common].corr(y.loc[common], method="spearman"))

    @staticmethod
    def _build_forward_returns(
        price_data: dict,
        period: int,
    ) -> pd.DataFrame:
        """建立各標的在 period 日後的前瞻報酬 DataFrame（index=date, columns=ticker）。"""
        frames: Dict[str, pd.Series] = {}
        for ticker, df in price_data.items():
            col = "Close" if "Close" in df.columns else df.columns[0]
            close = df[col].dropna()
            fwd = close.shift(-period) / close - 1
            frames[ticker] = fwd
        return pd.DataFrame(frames)

    # ------------------------------------------------------------------
    # 公開方法
    # ------------------------------------------------------------------

    def calculate_ic_series(
        self,
        factor_panel: pd.DataFrame,
        price_data: dict,
        periods: List[int] = None,
    ) -> pd.DataFrame:
        """計算各持有期間的 IC 時序（Spearman rank correlation）。

        Args:
            factor_panel: index=date, columns=ticker，每個日期每支股票的單一因子值。
            price_data:   {ticker: OHLCV DataFrame}。
            periods:      持有期間天數列表，預設 [5, 10, 20, 60]。

        Returns:
            DataFrame，index=date，columns=[f"IC_{p}d" for p in periods]。
        """
        if periods is None:
            periods = [5, 10, 20, 60]

        results: Dict[str, pd.Series] = {}
        for p in periods:
            fwd_df = self._build_forward_returns(price_data, p)
            ic_vals: Dict = {}
            for date in factor_panel.index:
                if date not in fwd_df.index:
                    continue
                factor_row = factor_panel.loc[date].dropna()
                fwd_row = fwd_df.loc[date].dropna()
                ic = self._spearman(factor_row, fwd_row)
                ic_vals[date] = ic
            results[f"IC_{p}d"] = pd.Series(ic_vals)

        return pd.DataFrame(results)

    def calculate_icir(
        self,
        ic_series: pd.Series,
        rolling_window: int = 12,
    ) -> pd.Series:
        """計算滾動 ICIR = rolling_mean(IC) / rolling_std(IC)。

        Args:
            ic_series:      IC 時序（pd.Series）。
            rolling_window: 滾動視窗長度，預設 12。

        Returns:
            pd.Series，與 ic_series 同 index 的 ICIR。
        """
        roll_mean = ic_series.rolling(rolling_window, min_periods=3).mean()
        roll_std  = ic_series.rolling(rolling_window, min_periods=3).std()
        return roll_mean / (roll_std + 1e-9)

    def rank_factors(
        self,
        factor_snapshot_df: pd.DataFrame,
        price_data: dict,
        forward_period: int = 20,
    ) -> pd.DataFrame:
        """對 factor_snapshot_df 的每個因子計算單期 IC 並評級。

        Args:
            factor_snapshot_df: index=ticker, columns=factor_names（截面快照）。
            price_data:         {ticker: OHLCV DataFrame}。
            forward_period:     前瞻報酬計算天數，預設 20。

        Returns:
            DataFrame，含 factor_name / IC / |IC| / 評級欄位。
        """
        fwd_df = self._build_forward_returns(price_data, forward_period)
        # 取最新可用日期的前瞻報酬
        latest_date = fwd_df.dropna(how="all").index.max()
        if pd.isna(latest_date):
            self.logger.warning("無法取得前瞻報酬資料。")
            return pd.DataFrame()

        fwd_row = fwd_df.loc[latest_date].dropna()

        records = []
        numeric_cols = factor_snapshot_df.select_dtypes(include="number").columns
        for col in numeric_cols:
            factor_row = factor_snapshot_df[col].dropna()
            ic = self._spearman(factor_row, fwd_row)
            abs_ic = abs(ic) if not np.isnan(ic) else 0.0

            if abs_ic >= self.IC_THRESHOLDS["強"]:
                rating = "強"
            elif abs_ic >= self.IC_THRESHOLDS["中"]:
                rating = "中"
            elif abs_ic >= self.IC_THRESHOLDS["弱"]:
                rating = "弱"
            else:
                rating = "無效"

            records.append({
                "因子名稱": col,
                "IC":   round(ic, 4) if not np.isnan(ic) else np.nan,
                "|IC|": round(abs_ic, 4),
                "評級": rating,
            })

        result = pd.DataFrame(records).sort_values("|IC|", ascending=False)
        return result.reset_index(drop=True)

    def ic_weighted_combine(
        self,
        factor_df: pd.DataFrame,
        ic_scores: pd.Series,
        min_ic_threshold: float = 0.01,
    ) -> pd.Series:
        """IC 加權動態合成因子分數。

        規則：
        - 負 IC 因子自動歸零（不做空因子）
        - |IC| < min_ic_threshold 的因子歸零
        - 其餘因子以 |IC| 為權重加權平均

        Args:
            factor_df:         index=ticker, columns=factor_names（已標準化分數）。
            ic_scores:         index=factor_name, values=IC 均值。
            min_ic_threshold:  最低 |IC| 門檻，預設 0.01。

        Returns:
            pd.Series，index=ticker，為合成分數。
        """
        # 只保留因子 df 中有 IC 分數的欄位
        common_factors = [c for c in factor_df.columns if c in ic_scores.index]
        if not common_factors:
            self.logger.warning("factor_df 與 ic_scores 無共同因子，回傳等權合成。")
            return factor_df.select_dtypes(include="number").mean(axis=1)

        weights: Dict[str, float] = {}
        for f in common_factors:
            ic = ic_scores[f]
            if np.isnan(ic) or ic < 0 or abs(ic) < min_ic_threshold:
                weights[f] = 0.0
            else:
                weights[f] = abs(ic)

        total_w = sum(weights.values())
        if total_w == 0:
            self.logger.warning("所有因子 IC 不足門檻，降級為等權重合成。")
            return factor_df[common_factors].mean(axis=1)

        combined = pd.Series(0.0, index=factor_df.index)
        for f, w in weights.items():
            if w > 0:
                combined += factor_df[f].fillna(0) * (w / total_w)

        return combined
