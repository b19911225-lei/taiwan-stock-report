"""factor_ic.py — 因子 IC / IR 分析器

IC  = 因子截面值與下期報酬的 Spearman 相關係數
IR  = IC 均值 / IC 標準差，衡量因子預測穩定性
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


class FactorICAnalyzer:
    """因子 IC（Information Coefficient）分析器。"""

    # IC 品質評級閾值
    GRADE_THRESHOLDS = {
        "強因子":   {"IR": 0.5, "IC_mean": 0.05, "positive_rate": 0.55},
        "中等因子": {"IR": 0.3, "IC_mean": 0.03, "positive_rate": 0.50},
        "弱因子":   {"IR": 0.0, "IC_mean": 0.00, "positive_rate": 0.00},
    }

    def __init__(self) -> None:
        self.logger = logging.getLogger("FactorICAnalyzer")

    # ------------------------------------------------------------------
    # 單一因子 IC 時間序列
    # ------------------------------------------------------------------

    def calculate_ic_series(
        self,
        factor_snapshots: Dict[pd.Timestamp, pd.DataFrame],
        returns_snapshots: Dict[pd.Timestamp, pd.Series],
        factor_col: str,
    ) -> pd.Series:
        """計算單一因子在每個截面日的 IC。"""
        ic_dict: Dict[pd.Timestamp, float] = {}

        for date, fdf in factor_snapshots.items():
            if date not in returns_snapshots:
                continue
            if factor_col not in fdf.columns:
                continue

            ret = returns_snapshots[date]
            common = fdf.index.intersection(ret.index)
            if len(common) < 10:
                continue

            f_vals = fdf.loc[common, factor_col].dropna()
            r_vals = ret.loc[f_vals.index].dropna()
            common2 = f_vals.index.intersection(r_vals.index)
            if len(common2) < 10:
                continue

            ic, _ = spearmanr(f_vals.loc[common2], r_vals.loc[common2])
            if not np.isnan(ic):
                ic_dict[date] = ic

        return pd.Series(ic_dict).sort_index()

    # ------------------------------------------------------------------
    # IC 統計量（IR / 評級）
    # ------------------------------------------------------------------

    def calculate_icir(self, ic_series: pd.Series) -> dict:
        """計算 IC 統計摘要。"""
        if ic_series.empty:
            return {
                "IC_mean": np.nan, "IC_std": np.nan, "IR": np.nan,
                "IC_positive_rate": np.nan, "n_periods": 0, "grade": "資料不足",
            }

        ic_mean = float(ic_series.mean())
        ic_std  = float(ic_series.std())
        ir      = ic_mean / (ic_std + 1e-9)
        positive_rate = float((ic_series > 0).mean())

        grade = "弱因子"
        for g, thresholds in self.GRADE_THRESHOLDS.items():
            if abs(ir) >= thresholds["IR"] and abs(ic_mean) >= thresholds["IC_mean"]:
                grade = g
                break

        return {
            "IC_mean":          round(ic_mean, 4),
            "IC_std":           round(ic_std, 4),
            "IR":               round(ir, 4),
            "IC_positive_rate": round(positive_rate, 4),
            "n_periods":        int(len(ic_series)),
            "grade":            grade,
        }

    # ------------------------------------------------------------------
    # 建立截面快照
    # ------------------------------------------------------------------

    def build_snapshots(
        self,
        cleaned_data: dict,
        fundamental_info: dict,
        config: dict,
        forward_days: int = 21,
        resample_freq: str = "MS",
    ) -> tuple[Dict, Dict]:
        """建立因子截面快照與下期報酬快照。"""
        from modules.factors import FactorAnalyzer
        analyzer = FactorAnalyzer(config)

        base = next(iter(cleaned_data.values()))
        all_dates = base.index

        cutoff_end = all_dates[-max(forward_days + 5, 30)]
        monthly_dates = pd.date_range(
            start=all_dates[0], end=cutoff_end, freq=resample_freq
        )
        monthly_dates = [
            all_dates[all_dates.get_indexer([d], method="nearest")[0]]
            for d in monthly_dates
            if d >= all_dates[0]
        ]

        factor_snapshots: Dict[pd.Timestamp, pd.DataFrame] = {}
        returns_snapshots: Dict[pd.Timestamp, pd.Series] = {}

        for date in monthly_dates:
            # 截至 date 的資料（防止 Lookahead Bias）
            data_to = {k: v.loc[v.index <= date] for k, v in cleaned_data.items()}
            data_to = {k: v for k, v in data_to.items() if len(v) >= 60}

            if not data_to:
                continue

            fdf = analyzer.calculate_factors(data_to, fundamental_info)
            factor_snapshots[date] = fdf

            future_ret: Dict[str, float] = {}
            for ticker, df in cleaned_data.items():
                future = df.loc[df.index > date, "Close"]
                if len(future) >= forward_days:
                    ret = future.iloc[forward_days - 1] / future.iloc[0] - 1
                    future_ret[ticker] = float(ret)
            returns_snapshots[date] = pd.Series(future_ret)

            self.logger.debug(f"截面日 {date.date()}：{len(fdf)} 支股票")

        self.logger.info(f"共建立 {len(factor_snapshots)} 個截面快照")
        return factor_snapshots, returns_snapshots

    # ------------------------------------------------------------------
    # 批量 IC/IR
    # ------------------------------------------------------------------

    def batch_icir(
        self,
        cleaned_data: dict,
        fundamental_info: dict,
        config: dict,
        forward_days: int = 21,
        factors_to_test: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """批量計算所有因子的 IC/IR，依 |IR| 降冪排序。"""
        if factors_to_test is None:
            factors_to_test = [
                "PER", "PBR", "ROE", "RSI", "MACD_Hist", "K",
                "波動率", "殖利率", "個股夏普比率", "個股Alpha",
                "日均成交張數", "週轉率", "個股最大回撤", "個股Beta",
            ]

        factor_snapshots, returns_snapshots = self.build_snapshots(
            cleaned_data, fundamental_info, config, forward_days
        )

        rows = []
        for col in factors_to_test:
            ic_series = self.calculate_ic_series(factor_snapshots, returns_snapshots, col)
            stats = self.calculate_icir(ic_series)
            stats["factor"] = col
            rows.append(stats)
            self.logger.info(
                f"{col}: IC={stats['IC_mean']}, IR={stats['IR']}, 評級={stats['grade']}"
            )

        result_df = pd.DataFrame(rows).set_index("factor")
        if result_df.empty:
            return result_df
        return result_df.reindex(
            result_df["IR"].abs().sort_values(ascending=False).index
        )
