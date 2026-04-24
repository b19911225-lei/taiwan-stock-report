import numpy as np
import pandas as pd
import logging

# 嘗試匯入 PyPortfolioOpt；若未安裝則降級
try:
    from pypfopt import EfficientFrontier, risk_models, expected_returns, EfficientRisk
    _PYPFOPT_AVAILABLE = True
except ImportError:
    _PYPFOPT_AVAILABLE = False


class PortfolioManager:
    def __init__(self, config):
        self.config = config
        self.params = config['portfolio_params']
        self.logger = logging.getLogger("PortfolioManager")

        if not _PYPFOPT_AVAILABLE:
            self.logger.warning(
                "PyPortfolioOpt 未安裝，max_sharpe / min_variance / risk_parity "
                "等方法將自動降級為 equal_weight 或 inverse_volatility。"
            )

    # ------------------------------------------------------------------
    # 私有輔助：從 cleaned_data 建立報酬 DataFrame
    # ------------------------------------------------------------------
    def _build_returns(self, tickers: list, cleaned_data: dict) -> pd.DataFrame:
        """從 cleaned_data 取各股收盤價並計算日報酬。"""
        price_frames = {}
        for t in tickers:
            if t not in cleaned_data:
                continue
            df = cleaned_data[t]
            close_col = "Close" if "Close" in df.columns else df.columns[0]
            price_frames[t] = df[close_col].dropna()

        if not price_frames:
            return pd.DataFrame()

        prices = pd.DataFrame(price_frames).dropna(how="all")
        returns = prices.pct_change().dropna(how="all")
        return returns

    # ------------------------------------------------------------------
    # 部位約束 (B-04)
    # ------------------------------------------------------------------
    def _apply_position_constraints(
        self,
        weights: pd.Series,
        sector_map: dict = None,
    ) -> pd.Series:
        """
        截斷個股上下限、產業上限並重新歸一化。

        Parameters
        ----------
        weights    : 未約束的權重 Series
        sector_map : {ticker: sector_name}，可選
        """
        max_pos = self.params.get('max_position_size', 0.15)
        min_pos = self.params.get('min_position_size', 0.02)
        max_sec = self.params.get('max_sector_weight', 0.40)

        # 1. 截斷個股上限
        weights = weights.clip(upper=max_pos)

        # 2. 低於最小部位者歸零
        weights[weights < min_pos] = 0.0

        # 3. 產業約束
        if sector_map is not None:
            sec_series = pd.Series(sector_map)
            for sector, group in sec_series.groupby(sec_series):
                tickers_in_sec = group.index.intersection(weights.index)
                sec_weight = weights[tickers_in_sec].sum()
                if sec_weight > max_sec and sec_weight > 0:
                    scale = max_sec / sec_weight
                    weights[tickers_in_sec] *= scale

        # 4. 重新歸一化
        total = weights.sum()
        if total > 0:
            weights = weights / total

        return weights

    # ------------------------------------------------------------------
    # 主方法：optimize_weights (B-03 + B-04)
    # ------------------------------------------------------------------
    def optimize_weights(
        self,
        factor_scores: pd.DataFrame,
        cleaned_data: dict,
        method: str = None,
        sector_map: dict = None,
    ) -> pd.Series:
        """
        計算投資組合權重。

        Parameters
        ----------
        factor_scores : 已篩選股票的因子分數 DataFrame（index 為 ticker）
        cleaned_data  : {ticker: OHLCV DataFrame}
        method        : 覆寫 config 中的 method；None 則沿用 config 預設
        sector_map    : {ticker: sector_name}，用於產業約束
        """
        if method is None:
            method = self.params.get('method', 'equal_weight')

        tickers = factor_scores.index.tolist()
        if not tickers:
            self.logger.warning("無任何股票符合選股條件，權重分配為空。")
            return pd.Series()

        weights = self._compute_weights(method, tickers, factor_scores, cleaned_data)

        # 套用部位約束
        weights = self._apply_position_constraints(weights, sector_map=sector_map)

        self.logger.info(
            f"權重優化完成 ({method})，持倉數量: {(weights > 0).sum()}，"
            f"分配比例:\n{weights[weights > 0].to_string()}"
        )
        return weights

    def _compute_weights(
        self,
        method: str,
        tickers: list,
        factor_scores: pd.DataFrame,
        cleaned_data: dict,
    ) -> pd.Series:
        """根據方法計算原始（未約束）權重。"""

        # ── 等權重 ──────────────────────────────────────────────────────
        if method == 'equal_weight':
            return pd.Series(1.0 / len(tickers), index=tickers)

        # ── 波動率倒數 ──────────────────────────────────────────────────
        if method == 'inverse_volatility':
            return self._inverse_volatility_weights(tickers, factor_scores, cleaned_data)

        # ── PyPortfolioOpt 系列 ─────────────────────────────────────────
        if method in ('max_sharpe', 'min_variance', 'risk_parity'):
            if not _PYPFOPT_AVAILABLE:
                self.logger.warning(
                    f"pypfopt 不可用，{method} 降級為 "
                    + ("inverse_volatility" if method == 'risk_parity' else "equal_weight")
                )
                if method == 'risk_parity':
                    return self._inverse_volatility_weights(tickers, factor_scores, cleaned_data)
                return pd.Series(1.0 / len(tickers), index=tickers)

            returns = self._build_returns(tickers, cleaned_data)
            valid_tickers = [t for t in tickers if t in returns.columns]

            if len(valid_tickers) < 2:
                self.logger.warning("有效報酬資料不足，改用等權重。")
                return pd.Series(1.0 / len(tickers), index=tickers)

            returns = returns[valid_tickers].dropna()
            mu = expected_returns.mean_historical_return(
                returns, returns_data=True, frequency=252
            )
            S = risk_models.sample_cov(returns, returns_data=True, frequency=252)

            try:
                if method == 'max_sharpe':
                    ef = EfficientFrontier(mu, S)
                    ef.max_sharpe()
                    raw = ef.clean_weights()

                elif method == 'min_variance':
                    ef = EfficientFrontier(mu, S)
                    ef.min_volatility()
                    raw = ef.clean_weights()

                elif method == 'risk_parity':
                    # 使用 EfficientRisk 近似等風險貢獻
                    # 目標波動率設為各股等權重組合波動率
                    eq_w = np.array([1.0 / len(valid_tickers)] * len(valid_tickers))
                    eq_vol = float(np.sqrt(eq_w @ S.values @ eq_w))
                    ef = EfficientRisk(mu, S, target_volatility=eq_vol)
                    ef.efficient_risk(eq_vol)
                    raw = ef.clean_weights()

                weights_series = pd.Series(raw).reindex(tickers).fillna(0.0)
                return weights_series

            except Exception as exc:
                self.logger.warning(f"PyPortfolioOpt 最佳化失敗 ({exc})，改用等權重。")
                return pd.Series(1.0 / len(tickers), index=tickers)

        # ── 未知方法 ────────────────────────────────────────────────────
        self.logger.error(f"不支援的部位控管算法: {method}，預設改用等權重。")
        return pd.Series(1.0 / len(tickers), index=tickers)

    def _inverse_volatility_weights(
        self,
        tickers: list,
        factor_scores: pd.DataFrame,
        cleaned_data: dict,
    ) -> pd.Series:
        """計算波動率倒數權重；優先使用 factor_scores['volatility']，否則從 cleaned_data 計算。"""
        if 'volatility' in factor_scores.columns:
            vols = factor_scores['volatility'].reindex(tickers).fillna(factor_scores['volatility'].mean())
        else:
            self.logger.info("factor_scores 無 volatility 欄位，改從 cleaned_data 計算歷史波動率。")
            returns = self._build_returns(tickers, cleaned_data)
            if returns.empty:
                return pd.Series(1.0 / len(tickers), index=tickers)
            ann_vol = returns.std() * np.sqrt(252)
            vols = ann_vol.reindex(tickers).fillna(ann_vol.mean())

        # 防止除以零
        vols = vols.replace(0, np.nan).fillna(vols[vols > 0].mean() if (vols > 0).any() else 1.0)
        inv_vols = 1.0 / vols
        weights = inv_vols / inv_vols.sum()
        return weights

    # ------------------------------------------------------------------
    # 投資組合波動率輔助
    # ------------------------------------------------------------------
    def calculate_portfolio_vol(
        self,
        weights: pd.Series,
        cleaned_data: dict,
    ) -> float:
        """
        計算投資組合年化波動率。

        Parameters
        ----------
        weights      : 各股權重 Series
        cleaned_data : {ticker: OHLCV DataFrame}

        Returns
        -------
        float: 年化波動率（如 0.15 代表 15%）
        """
        tickers = weights[weights > 0].index.tolist()
        returns = self._build_returns(tickers, cleaned_data)

        if returns.empty or len(returns) < 2:
            self.logger.warning("報酬資料不足，無法計算組合波動率，回傳 NaN。")
            return float('nan')

        valid_tickers = [t for t in tickers if t in returns.columns]
        w = weights.reindex(valid_tickers).fillna(0.0)
        if w.sum() == 0:
            return float('nan')
        w = w / w.sum()

        cov_matrix = returns[valid_tickers].dropna().cov() * 252
        port_var = float(w.values @ cov_matrix.values @ w.values)
        port_vol = np.sqrt(max(port_var, 0.0))

        self.logger.info(f"投資組合年化波動率: {port_vol:.4f}")
        return port_vol
