"""
backtest.py — 回測引擎模組

包含三個類別：
  - BacktestEngine        : 單次回測（支援 T+1 執行、交易稅、再平衡）
  - WalkForwardValidator  : 滾動前進驗證（樣本內 / 樣本外切割）
  - StressTestEngine      : 壓力情境測試
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """歷史回測引擎，支援成本模型、T+1 執行延遲與定期再平衡。"""

    def __init__(self, config: dict) -> None:
        """
        初始化回測引擎。

        Parameters
        ----------
        config : dict
            完整設定字典，需包含 'backtest' 子字典。
        """
        self.config = config
        self.backtest_params: dict = config['backtest']
        self.logger = logging.getLogger("BacktestEngine")

        # 成本參數
        self.slippage: float = self.backtest_params.get('slippage', 0.001)
        self.commission: float = self.backtest_params.get('commission', 0.001425)

        # NEW-01：台股交易稅
        self.transaction_tax_stock: float = self.backtest_params.get(
            'transaction_tax_stock', 0.003
        )
        self.transaction_tax_etf: float = self.backtest_params.get(
            'transaction_tax_etf', 0.001
        )
        # ETF 清單（從 config 讀取，若無則空集合）
        etf_list = self.backtest_params.get('etf_tickers', []) or []
        self.etf_tickers: set = set(etf_list)

    # ------------------------------------------------------------------
    # helper
    # ------------------------------------------------------------------

    def get_tax_rate(self, ticker: str) -> float:
        """
        依標的類型回傳賣出交易稅率。

        Parameters
        ----------
        ticker : str
            股票代號。

        Returns
        -------
        float
            ETF 稅率或股票稅率。
        """
        return (
            self.transaction_tax_etf
            if ticker in self.etf_tickers
            else self.transaction_tax_stock
        )

    def _build_execution_price(
        self,
        df: pd.DataFrame,
        execution_delay: int,
    ) -> pd.Series:
        """
        建立執行價格序列（T+execution_delay 的 Open，或 fallback 至 Close）。

        Parameters
        ----------
        df : pd.DataFrame
            單一標的 OHLCV DataFrame。
        execution_delay : int
            執行延遲天數（0 表示當日收盤，1 表示次日開盤）。

        Returns
        -------
        pd.Series
            執行價格序列。
        """
        if execution_delay > 0 and 'Open' in df.columns:
            # shift(-execution_delay) 讓今日的執行價 = 未來第 execution_delay 天的 Open
            return df['Open'].shift(-execution_delay)
        col = 'Close' if 'Close' in df.columns else df.columns[0]
        return df[col]

    def _cost_per_rebalance(self, weights_old: pd.Series, weights_new: pd.Series) -> float:
        """
        計算一次再平衡的摩擦成本（佔投資組合淨值比例）。

        買入成本係數  : 1 + commission + slippage
        賣出成本係數  : 1 - commission - slippage - tax_rate
        成本          : turnover × (2×commission + 2×slippage + avg_tax)

        Parameters
        ----------
        weights_old : pd.Series
            再平衡前的權重。
        weights_new : pd.Series
            再平衡後的權重。

        Returns
        -------
        float
            成本佔淨值比例（0~1 之間的正值）。
        """
        all_tickers = weights_old.index.union(weights_new.index)
        w_old = weights_old.reindex(all_tickers, fill_value=0.0)
        w_new = weights_new.reindex(all_tickers, fill_value=0.0)

        delta = w_new - w_old
        turnover = delta.abs().sum() / 2.0  # 單邊換手率

        # 以各標的的平均稅率計算加權平均稅
        avg_tax = np.mean([self.get_tax_rate(t) for t in all_tickers])
        friction = 2 * self.commission + 2 * self.slippage + avg_tax

        return turnover * friction

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(
        self,
        weights: pd.Series,
        cleaned_data: dict,
        benchmark_ticker: str = '0050.TW',
        execution_delay: int = 1,
        rebalance_fn: Optional[Callable] = None,
        rebalance_frequency: Optional[str] = None,
    ) -> dict:
        """
        執行歷史回測，回傳績效字典。

        Parameters
        ----------
        weights : pd.Series
            初始股票權重（index = ticker）。
        cleaned_data : dict
            {ticker: pd.DataFrame} 格式的歷史資料字典。
        benchmark_ticker : str
            基準指數代號，預設 '0050.TW'。
        execution_delay : int
            執行延遲天數（NEW-02），預設 1（T+1 次日開盤執行）。
        rebalance_fn : callable, optional
            再平衡函數，簽名為 fn(data_slice, as_of_date) -> pd.Series。
            若為 None 則維持原權重。
        rebalance_frequency : str, optional
            再平衡頻率（B-01），接受 'M'、'W'、'Q'，None 表示不再平衡。

        Returns
        -------
        dict
            含以下鍵：
            - daily_returns  : pd.Series
            - equity_curve   : pd.Series
            - metrics        : dict
            - rebalance_log  : list[dict]（僅再平衡模式）
        """
        tickers = weights.index.tolist()
        if not tickers:
            self.logger.warning("回測池為空，無法執行。")
            return {}

        # ---- 建立執行價格 DataFrame ----
        exec_price_df = pd.DataFrame()
        for ticker in tickers:
            if ticker in cleaned_data:
                df = cleaned_data[ticker]
                exec_price_df[ticker] = self._build_execution_price(df, execution_delay)

        exec_price_df = exec_price_df.dropna(how='all')
        if exec_price_df.empty:
            return {}

        # 日報酬率（基於執行價）
        returns_df = exec_price_df.pct_change().dropna()
        if returns_df.empty:
            return {}

        # ---- 取得基準報酬 ----
        benchmark_returns: Optional[pd.Series] = None
        if benchmark_ticker in cleaned_data:
            b_df = cleaned_data[benchmark_ticker]
            b_col = 'Close' if 'Close' in b_df.columns else b_df.columns[0]
            benchmark_returns = b_df[b_col].pct_change().dropna()

        # ---- 再平衡模式 ----
        if rebalance_frequency is not None:
            return self._run_with_rebalance(
                returns_df=returns_df,
                cleaned_data=cleaned_data,
                initial_weights=weights,
                rebalance_fn=rebalance_fn,
                rebalance_frequency=rebalance_frequency,
                benchmark_returns=benchmark_returns,
            )

        # ---- 單一權重回測 ----
        # 只保留有資料的 tickers
        available = [t for t in tickers if t in returns_df.columns]
        if not available:
            return {}
        w = weights.reindex(available)
        w = w / w.sum()  # 正規化

        portfolio_returns = returns_df[available].dot(w)
        equity_curve = (1 + portfolio_returns).cumprod()

        metrics = self.calculate_metrics(portfolio_returns, equity_curve, benchmark_returns)
        if 'total_return' in metrics:
            self.logger.info(
                f"回測完成：總報酬率 {metrics['total_return']*100:.2f}%，"
                f"MDD {metrics['max_drawdown']*100:.2f}%"
            )

        return {
            'daily_returns': portfolio_returns,
            'equity_curve': equity_curve,
            'metrics': metrics,
            'rebalance_log': [],
        }

    # ------------------------------------------------------------------
    # 再平衡內部實作
    # ------------------------------------------------------------------

    def _run_with_rebalance(
        self,
        returns_df: pd.DataFrame,
        cleaned_data: dict,
        initial_weights: pd.Series,
        rebalance_fn: Optional[Callable],
        rebalance_frequency: str,
        benchmark_returns: Optional[pd.Series],
    ) -> dict:
        """
        以逐段方式執行再平衡回測。

        Parameters
        ----------
        returns_df : pd.DataFrame
            所有標的的每日報酬率 DataFrame。
        cleaned_data : dict
            原始歷史資料字典（傳給 rebalance_fn 用）。
        initial_weights : pd.Series
            初始權重。
        rebalance_fn : callable or None
            再平衡函數；None 表示維持權重不變。
        rebalance_frequency : str
            pandas offset alias（'M'、'W'、'Q'）。
        benchmark_returns : pd.Series or None
            基準每日報酬。

        Returns
        -------
        dict
            與 run() 相同格式的結果字典。
        """
        all_dates = returns_df.index
        start_date = all_dates[0]
        end_date = all_dates[-1]

        # 產生再平衡日期（月底、週底、季底）
        rebalance_dates = pd.date_range(
            start=start_date, end=end_date, freq=rebalance_frequency
        )
        # 確保日期在 returns_df 的交易日內（取最近的交易日）
        rebalance_dates = [
            all_dates[all_dates.get_loc(d, method='nearest')]
            for d in rebalance_dates
            if d >= start_date
        ]
        # 去重並排序
        rebalance_dates = sorted(set(rebalance_dates))

        current_weights = initial_weights.copy()
        rebalance_log: List[dict] = []
        segment_returns: List[pd.Series] = []
        equity_level = 1.0  # 持續追蹤累積淨值

        segment_start = start_date
        processed_dates = set()

        for rb_date in rebalance_dates:
            if rb_date in processed_dates:
                continue
            processed_dates.add(rb_date)

            # 取出本段資料
            segment_mask = (all_dates >= segment_start) & (all_dates < rb_date)
            seg_returns = returns_df.loc[segment_mask]

            if not seg_returns.empty:
                available = [
                    t for t in current_weights.index if t in seg_returns.columns
                ]
                if available:
                    w = current_weights.reindex(available).fillna(0.0)
                    if w.sum() > 0:
                        w = w / w.sum()
                    seg_port = seg_returns[available].dot(w)
                    seg_port.iloc[0] = seg_port.iloc[0]  # no-op，保持索引
                    # 調整至當前淨值水平
                    seg_equity = (1 + seg_port).cumprod() * equity_level
                    equity_level = seg_equity.iloc[-1]
                    segment_returns.append(seg_port)

            # 再平衡：取得新權重
            data_up_to_date = {
                k: v.loc[v.index <= rb_date]
                for k, v in cleaned_data.items()
                if not v.loc[v.index <= rb_date].empty
            }

            if rebalance_fn is not None:
                try:
                    new_weights = rebalance_fn(data_up_to_date, rb_date)
                except Exception as e:
                    self.logger.warning(f"再平衡函數在 {rb_date} 執行失敗：{e}，維持原權重")
                    new_weights = current_weights.copy()
            else:
                new_weights = current_weights.copy()

            # 確保 weights 正規化
            if new_weights.sum() > 0:
                new_weights = new_weights / new_weights.sum()

            # 計算換股成本並扣除
            cost = self._cost_per_rebalance(current_weights, new_weights)
            equity_level *= (1.0 - cost)

            # 記錄換股日誌
            old_set = set(current_weights[current_weights > 0].index)
            new_set = set(new_weights[new_weights > 0].index)
            turnover = (current_weights.reindex(new_weights.index, fill_value=0.0) - new_weights).abs().sum() / 2.0

            rebalance_log.append({
                'date': rb_date,
                'turnover': float(turnover),
                'cost': float(cost),
                'tickers_in': sorted(new_set - old_set),
                'tickers_out': sorted(old_set - new_set),
            })

            current_weights = new_weights
            segment_start = rb_date

        # 最後一段（最後再平衡日至結束）
        final_mask = all_dates >= segment_start
        seg_returns = returns_df.loc[final_mask]
        if not seg_returns.empty:
            available = [t for t in current_weights.index if t in seg_returns.columns]
            if available:
                w = current_weights.reindex(available).fillna(0.0)
                if w.sum() > 0:
                    w = w / w.sum()
                seg_port = seg_returns[available].dot(w)
                segment_returns.append(seg_port)

        if not segment_returns:
            return {}

        portfolio_returns = pd.concat(segment_returns).sort_index()
        # 去除可能的重複索引
        portfolio_returns = portfolio_returns[~portfolio_returns.index.duplicated(keep='first')]

        equity_curve = (1 + portfolio_returns).cumprod()
        # 套用換股成本調整（equity_level 已隨過程累積扣除，此處重建為一致曲線）
        # 注意：segment_returns 已反映成本扣除後的 equity_level，但 cumprod 重算會忽略跳點
        # 因此我們採用逐段重建方式
        equity_curve = self._rebuild_equity_curve(segment_returns, rebalance_log, portfolio_returns.index)

        metrics = self.calculate_metrics(portfolio_returns, equity_curve, benchmark_returns)
        if 'total_return' in metrics:
            self.logger.info(
                f"再平衡回測完成（頻率={rebalance_frequency}）："
                f"總報酬率 {metrics['total_return']*100:.2f}%，"
                f"MDD {metrics['max_drawdown']*100:.2f}%，"
                f"再平衡次數 {len(rebalance_log)}"
            )

        return {
            'daily_returns': portfolio_returns,
            'equity_curve': equity_curve,
            'metrics': metrics,
            'rebalance_log': rebalance_log,
        }

    def _rebuild_equity_curve(
        self,
        segment_returns: List[pd.Series],
        rebalance_log: List[dict],
        full_index: pd.Index,
    ) -> pd.Series:
        """
        逐段重建考量換股成本跳點的淨值曲線。

        Parameters
        ----------
        segment_returns : list[pd.Series]
            各段的每日報酬率序列。
        rebalance_log : list[dict]
            每次再平衡的成本記錄。
        full_index : pd.Index
            完整交易日索引。

        Returns
        -------
        pd.Series
            對齊 full_index 的累積淨值曲線。
        """
        equity_map: Dict[pd.Timestamp, float] = {}
        level = 1.0
        cost_by_date = {log['date']: log['cost'] for log in rebalance_log}

        for seg in segment_returns:
            for date, ret in seg.items():
                # 若該日為再平衡日（段首），先扣成本
                if date in cost_by_date:
                    level *= (1.0 - cost_by_date[date])
                level *= (1.0 + ret)
                equity_map[date] = level

        equity_curve = pd.Series(equity_map).reindex(full_index)
        # 對缺失值做前向填充
        equity_curve = equity_curve.ffill()
        return equity_curve

    # ------------------------------------------------------------------
    # calculate_metrics
    # ------------------------------------------------------------------

    def calculate_metrics(
        self,
        daily_returns: pd.Series,
        equity_curve: pd.Series,
        benchmark_returns: Optional[pd.Series] = None,
    ) -> dict:
        """
        計算關鍵績效指標。

        Parameters
        ----------
        daily_returns : pd.Series
            每日投資組合報酬率。
        equity_curve : pd.Series
            累積淨值曲線（起始值 ≈ 1）。
        benchmark_returns : pd.Series, optional
            基準每日報酬率（用於 Alpha/Beta 計算）。

        Returns
        -------
        dict
            績效指標字典，含 total_return、annualized_return、annualized_vol、
            sharpe_ratio、max_drawdown、sortino_ratio、calmar_ratio、alpha、beta。
        """
        try:
            # (1) 總報酬率
            total_return = float(equity_curve.iloc[-1] - 1)

            # (2) 年化報酬率
            n_days = len(daily_returns)
            if n_days < 2:
                return {}
            annualized_return = float((1 + total_return) ** (252 / n_days) - 1)

            # (3) 年化波動率
            annualized_vol = float(daily_returns.std() * np.sqrt(252))

            # (4) 夏普比率
            rf: float = self.backtest_params.get('risk_free_rate', 0.02)
            sharpe_ratio = float((annualized_return - rf) / (annualized_vol + 1e-9))

            # (5) 最大回撤
            rolling_max = equity_curve.cummax()
            drawdown = (equity_curve - rolling_max) / rolling_max
            max_drawdown = float(drawdown.min())

            # (6) 索提諾比率
            downside_returns = daily_returns[daily_returns < 0]
            downside_vol = float(downside_returns.std() * np.sqrt(252)) if len(downside_returns) > 1 else 0.0
            sortino_ratio = float((annualized_return - rf) / (downside_vol + 1e-9))

            # (7) 卡馬比率
            calmar_ratio = float(annualized_return / (abs(max_drawdown) + 1e-9))

            # (8) Alpha & Beta
            alpha: float = float('nan')
            beta: float = float('nan')
            if benchmark_returns is not None:
                common_idx = daily_returns.index.intersection(benchmark_returns.index)
                if len(common_idx) > 30:
                    s_ret = daily_returns.loc[common_idx].values
                    b_ret = benchmark_returns.loc[common_idx].values
                    matrix = np.vstack([b_ret, np.ones(len(b_ret))]).T
                    coeffs = np.linalg.lstsq(matrix, s_ret, rcond=None)[0]
                    beta = float(coeffs[0])
                    alpha = float(coeffs[1] * 252)

            return {
                'total_return': total_return,
                'annualized_return': annualized_return,
                'annualized_vol': annualized_vol,
                'sharpe_ratio': sharpe_ratio,
                'max_drawdown': max_drawdown,
                'sortino_ratio': sortino_ratio,
                'calmar_ratio': calmar_ratio,
                'alpha': alpha,
                'beta': beta,
            }
        except Exception as e:
            self.logger.error(f"指標計算錯誤: {e}")
            return {}


# ---------------------------------------------------------------------------
# WalkForwardValidator
# ---------------------------------------------------------------------------

class WalkForwardValidator:
    """
    滾動前進（Walk-Forward）驗證器。

    將歷史資料依照樣本內 / 樣本外視窗切割，逐視窗訓練並在樣本外期間
    以 BacktestEngine 評估策略績效，最終彙整所有 OOS 報酬與指標。
    """

    def __init__(
        self,
        config: dict,
        in_sample_months: int = 12,
        out_sample_months: int = 3,
        step_months: int = 3,
    ) -> None:
        """
        Parameters
        ----------
        config : dict
            完整設定字典，傳入 BacktestEngine 使用。
        in_sample_months : int
            樣本內訓練期長度（月），預設 12。
        out_sample_months : int
            樣本外測試期長度（月），預設 3。
        step_months : int
            視窗滾動步長（月），預設 3。
        """
        self.config = config
        self.in_sample_months = in_sample_months
        self.out_sample_months = out_sample_months
        self.step_months = step_months
        self.logger = logging.getLogger("WalkForwardValidator")
        self._engine = BacktestEngine(config)

    def run(
        self,
        strategy_fn: Callable[[dict, datetime], pd.Series],
        cleaned_data: dict,
        benchmark_ticker: str = '0050.TW',
    ) -> dict:
        """
        執行滾動前進驗證。

        Parameters
        ----------
        strategy_fn : callable
            選股策略函數，簽名：fn(data_slice: dict, as_of_date) -> pd.Series（weights）。
        cleaned_data : dict
            {ticker: pd.DataFrame} 格式的完整歷史資料字典。
        benchmark_ticker : str
            基準指數代號，預設 '0050.TW'。

        Returns
        -------
        dict
            含以下鍵：
            - oos_returns  : pd.Series  拼接的樣本外每日報酬
            - oos_equity   : pd.Series  樣本外累積淨值
            - windows      : list[dict] 每個視窗摘要
            - oos_metrics  : dict       整體樣本外績效指標
        """
        # 取得所有標的的共同日期範圍
        all_indices = [df.index for df in cleaned_data.values() if not df.empty]
        if not all_indices:
            self.logger.error("cleaned_data 為空，無法執行驗證。")
            return {}

        global_start = min(idx.min() for idx in all_indices)
        global_end = max(idx.max() for idx in all_indices)

        windows: List[dict] = []
        oos_returns_list: List[pd.Series] = []

        cursor = global_start
        window_id = 0

        while True:
            in_start = cursor
            in_end = in_start + pd.DateOffset(months=self.in_sample_months)
            oos_start = in_end
            oos_end = oos_start + pd.DateOffset(months=self.out_sample_months)

            if oos_end > global_end:
                break

            window_id += 1

            # 切割樣本內資料
            in_sample_data = {
                ticker: df.loc[(df.index >= in_start) & (df.index < in_end)]
                for ticker, df in cleaned_data.items()
            }
            in_sample_data = {k: v for k, v in in_sample_data.items() if not v.empty}

            # 切割樣本外資料
            oos_data = {
                ticker: df.loc[(df.index >= oos_start) & (df.index < oos_end)]
                for ticker, df in cleaned_data.items()
            }
            oos_data = {k: v for k, v in oos_data.items() if not v.empty}

            # 呼叫策略函數取得 weights
            try:
                weights = strategy_fn(in_sample_data, in_end)
            except Exception as e:
                self.logger.warning(f"視窗 {window_id}：strategy_fn 執行失敗 ({e})，跳過。")
                cursor += pd.DateOffset(months=self.step_months)
                continue

            if weights is None or weights.empty:
                self.logger.warning(f"視窗 {window_id}：strategy_fn 回傳空權重，跳過。")
                cursor += pd.DateOffset(months=self.step_months)
                continue

            # 在樣本外資料上執行回測
            result = self._engine.run(
                weights=weights,
                cleaned_data=oos_data,
                benchmark_ticker=benchmark_ticker,
            )

            oos_ret = result.get('daily_returns', pd.Series(dtype=float))
            oos_metrics = result.get('metrics', {})

            windows.append({
                'window_id': window_id,
                'in_start': str(in_start.date()),
                'in_end': str(in_end.date()),
                'oos_start': str(oos_start.date()),
                'oos_end': str(oos_end.date()),
                'weights': weights.to_dict(),
                'oos_total_return': oos_metrics.get('total_return', np.nan),
                'oos_sharpe': oos_metrics.get('sharpe_ratio', np.nan),
                'oos_max_drawdown': oos_metrics.get('max_drawdown', np.nan),
            })

            if not oos_ret.empty:
                oos_returns_list.append(oos_ret)

            cursor += pd.DateOffset(months=self.step_months)

        if not oos_returns_list:
            self.logger.warning("無有效樣本外報酬，請確認資料範圍與策略函數。")
            return {
                'oos_returns': pd.Series(dtype=float),
                'oos_equity': pd.Series(dtype=float),
                'windows': windows,
                'oos_metrics': {},
            }

        # 拼接所有樣本外報酬
        oos_returns = pd.concat(oos_returns_list).sort_index()
        oos_returns = oos_returns[~oos_returns.index.duplicated(keep='first')]
        oos_equity = (1 + oos_returns).cumprod()

        # 計算整體 OOS 績效
        oos_metrics = self._engine.calculate_metrics(oos_returns, oos_equity)

        self.logger.info(
            f"WalkForward 完成：{len(windows)} 個視窗，"
            f"OOS 總報酬 {oos_metrics.get('total_return', float('nan'))*100:.2f}%"
        )

        return {
            'oos_returns': oos_returns,
            'oos_equity': oos_equity,
            'windows': windows,
            'oos_metrics': oos_metrics,
        }


# ---------------------------------------------------------------------------
# StressTestEngine
# ---------------------------------------------------------------------------

class StressTestEngine:
    """
    壓力情境測試引擎。

    針對預設的歷史壓力情境，計算策略在該期間的績效指標，
    以評估策略的抗壓性與尾部風險。
    """

    SCENARIOS: Dict[str, Tuple[str, str]] = {
        'COVID崩盤_2020':  ('2020-02-01', '2020-03-23'),
        '升息熊市_2022':   ('2022-01-01', '2022-12-31'),
        '日圓閃崩_2024':   ('2024-07-29', '2024-08-12'),
    }

    def __init__(self, config: dict) -> None:
        """
        Parameters
        ----------
        config : dict
            完整設定字典，傳入 BacktestEngine 使用。
        """
        self.config = config
        self.logger = logging.getLogger("StressTestEngine")
        self._engine = BacktestEngine(config)

    def run(
        self,
        weights: pd.Series,
        cleaned_data: dict,
        benchmark_ticker: str = '0050.TW',
    ) -> Dict[str, dict]:
        """
        對每個預設情境執行回測並回傳績效指標。

        Parameters
        ----------
        weights : pd.Series
            股票權重（index = ticker）。
        cleaned_data : dict
            {ticker: pd.DataFrame} 格式的歷史資料字典（需涵蓋情境日期範圍）。
        benchmark_ticker : str
            基準指數代號，預設 '0050.TW'。

        Returns
        -------
        dict
            {scenario_name: metrics_dict}，metrics_dict 與 BacktestEngine.calculate_metrics
            回傳格式相同；若該情境無資料則為 {'error': '訊息'}。
        """
        results: Dict[str, dict] = {}

        for scenario_name, (start_str, end_str) in self.SCENARIOS.items():
            start_dt = pd.Timestamp(start_str)
            end_dt = pd.Timestamp(end_str)

            # 切割情境期間資料
            scenario_data = {
                ticker: df.loc[(df.index >= start_dt) & (df.index <= end_dt)]
                for ticker, df in cleaned_data.items()
            }
            scenario_data = {k: v for k, v in scenario_data.items() if not v.empty}

            if not scenario_data:
                self.logger.warning(f"情境 '{scenario_name}' 無可用資料（{start_str} ~ {end_str}）。")
                results[scenario_name] = {'error': f'無資料：{start_str} ~ {end_str}'}
                continue

            result = self._engine.run(
                weights=weights,
                cleaned_data=scenario_data,
                benchmark_ticker=benchmark_ticker,
                execution_delay=0,  # 壓力測試使用當日收盤，不加延遲
            )

            metrics = result.get('metrics', {})
            if not metrics:
                results[scenario_name] = {'error': '指標計算失敗'}
            else:
                results[scenario_name] = metrics
                self.logger.info(
                    f"情境 '{scenario_name}'：總報酬 {metrics.get('total_return', float('nan'))*100:.2f}%，"
                    f"MDD {metrics.get('max_drawdown', float('nan'))*100:.2f}%"
                )

        return results
