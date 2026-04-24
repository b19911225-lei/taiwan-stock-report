import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from typing import Optional


class FactorAnalyzer:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.logger = logging.getLogger("FactorAnalyzer")

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算技術指標，回傳含所有指標欄位的新 DataFrame（不修改原始輸入）。

        Args:
            df: 包含 Open/High/Low/Close/Volume/Amount 欄位的 OHLCV DataFrame。

        Returns:
            新的 DataFrame，包含原始欄位及所有技術指標欄位。
        """
        df = df.copy()
        try:
            # 1. 均線 (SMA, EMA)
            df['SMA20'] = df['Close'].rolling(window=20).mean()
            df['SMA60'] = df['Close'].rolling(window=60).mean()
            df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
            df['EMA60'] = df['Close'].ewm(span=60, adjust=False).mean()

            # 2. RSI (相對強弱指標)
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / (loss + 1e-9)
            df['RSI'] = 100 - (100 / (1 + rs))

            # 3. MACD
            ema12 = df['Close'].ewm(span=12, adjust=False).mean()
            ema26 = df['Close'].ewm(span=26, adjust=False).mean()
            df['MACD_DIF'] = ema12 - ema26
            df['MACD_DEA'] = df['MACD_DIF'].ewm(span=9, adjust=False).mean()
            df['MACD_Hist'] = df['MACD_DIF'] - df['MACD_DEA']

            # 4. Bollinger Bands (布林通道)
            ma20 = df['Close'].rolling(window=20).mean()
            std20 = df['Close'].rolling(window=20).std()
            df['BB_Upper'] = ma20 + (std20 * 2)
            df['BB_Lower'] = ma20 - (std20 * 2)
            df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / ma20

            # 5. KD 指標 (Stochastic Oscillator)
            low_min = df['Low'].rolling(window=9).min()
            high_max = df['High'].rolling(window=9).max()
            rsv = 100 * (df['Close'] - low_min) / (high_max - low_min + 1e-9)
            df['K'] = rsv.ewm(com=2, adjust=False).mean()
            df['D'] = df['K'].ewm(com=2, adjust=False).mean()

            # 6. ADX (平均趨向指標)
            plus_dm = df['High'].diff()
            minus_dm = df['Low'].diff()
            plus_dm[plus_dm < 0] = 0
            minus_dm[minus_dm > 0] = 0
            minus_dm = -minus_dm

            tr1 = df['High'] - df['Low']
            tr2 = abs(df['High'] - df['Close'].shift(1))
            tr3 = abs(df['Low'] - df['Close'].shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean()

            plus_di = 100 * (plus_dm.rolling(window=14).mean() / (atr + 1e-9))
            minus_di = 100 * (minus_dm.rolling(window=14).mean() / (atr + 1e-9))
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
            df['ADX'] = dx.rolling(window=14).mean()

            # 7. OBV (能量潮)
            df['OBV'] = (np.sign(df['Close'].diff()) * df['Volume']).fillna(0).cumsum()

            # 8. 波動率 (20日年化波動率)
            df['Volatility'] = df['Close'].pct_change().rolling(window=20).std() * np.sqrt(252)

        except Exception as e:
            self.logger.error(f"指標計算失敗: {e}")

        return df

    def calculate_factors(
        self,
        data_dict: dict,
        fundamental_info: dict,
        sector_map: Optional[dict] = None,
    ) -> pd.DataFrame:
        """計算所有股票的因子截面資料。

        Args:
            data_dict: {ticker: OHLCV DataFrame}
            fundamental_info: {ticker: 基本面資訊 dict}
            sector_map: {ticker: 產業名稱}，若提供則自動附加各因子的產業中性化
                Z-Score 欄位（欄位名加 _Z 後綴）。預設 None 表示不做中性化。

        Returns:
            以 ticker 為 index 的因子 DataFrame。
        """
        all_factors = []
        now = datetime.now()
        one_year_ago = now - timedelta(days=365)

        # 預先計算基準 (0050) 的報酬率以計算 Alpha/Beta
        benchmark_ticker = "0050.TW"
        b_ret = None
        if benchmark_ticker in data_dict:
            b_df = data_dict[benchmark_ticker]
            b_ret = b_df['Close'].pct_change().dropna()

        rf = self.config['backtest']['risk_free_rate']

        for ticker, df in data_dict.items():
            try:
                # 1. 計算技術指標（一次性，後續直接讀取欄位）
                df_ind = self.calculate_indicators(df)

                last_idx = -1
                last_price = df_ind['Close'].iloc[last_idx]
                last_vol = df_ind['Volume'].iloc[last_idx]
                last_amt = df_ind['Amount'].iloc[last_idx]

                # 2. 歷史績效指標計算
                daily_ret = df_ind['Close'].pct_change().dropna()
                n_days = len(daily_ret)

                if n_days > 20:
                    cum_ret = (1 + daily_ret).prod() - 1
                    ann_ret = (1 + cum_ret) ** (252 / n_days) - 1
                    ann_vol = daily_ret.std() * np.sqrt(252)
                    sharpe = (ann_ret - rf) / (ann_vol + 1e-9)
                    equity_curve = (1 + daily_ret).cumprod()
                    rolling_max = equity_curve.cummax()
                    mdd = ((equity_curve - rolling_max) / rolling_max).min()
                    calmar = ann_ret / (abs(mdd) + 1e-9)

                    alpha, beta = np.nan, np.nan
                    if b_ret is not None:
                        common_idx = daily_ret.index.intersection(b_ret.index)
                        if len(common_idx) > 30:
                            s_slice = daily_ret.loc[common_idx]
                            b_slice = b_ret.loc[common_idx]
                            matrix = np.vstack([b_slice, np.ones(len(b_slice))]).T
                            beta, alpha_daily = np.linalg.lstsq(matrix, s_slice, rcond=None)[0]
                            alpha = alpha_daily * 252
                else:
                    ann_ret, sharpe, mdd, calmar, alpha, beta = 0, 0, 0, 0, 0, 0

                # 3. 殖利率與基本面
                info = fundamental_info.get(ticker, {})
                div_hist = info.get('dividends_history', pd.Series())
                if not div_hist.empty:
                    last_year_divs = div_hist[div_hist.index >= one_year_ago.strftime("%Y-%m-%d")]
                    div_yield = last_year_divs.sum() / last_price if last_price > 0 else 0
                else:
                    div_yield = 0.0

                pe_ratio = info.get('pe_ratio', np.nan)
                pb_ratio = info.get('pb_ratio', np.nan)
                roe = info.get('roe', np.nan)

                # 4. 技術指標快照（直接從已計算的 df_ind 讀取，不重複計算）
                rsi = df_ind['RSI'].iloc[last_idx]
                macd_hist = df_ind['MACD_Hist'].iloc[last_idx]
                k = df_ind['K'].iloc[last_idx]
                volatility = df_ind['Volatility'].iloc[last_idx]
                trend = 1 if (
                    last_price > df_ind['SMA20'].iloc[last_idx] > df_ind['SMA60'].iloc[last_idx]
                ) else 0

                shares = info.get('shares_outstanding', 0)
                turnover = (last_vol / shares) if shares > 0 else 0

                # 5. 每張成本流動性指標 (NEW-05)
                daily_vol_lots = last_vol / 1000          # 日均成交張數
                cost_per_lot = last_price * 1000          # 每張成本(元)

                all_factors.append({
                    'ticker': ticker,
                    '名稱': info.get('name', ticker),
                    '成交價': last_price,
                    '成交金額(億)': last_amt / 100000000,
                    'PER': pe_ratio,
                    'PBR': pb_ratio,
                    'ROE': roe,
                    'RSI': rsi,
                    'MACD_Hist': macd_hist,
                    'K': k,
                    '波動率': volatility,
                    '趨勢看多': trend,
                    '週轉率': turnover,
                    '殖利率': div_yield,
                    '日均成交張數': daily_vol_lots,
                    '每張成本(元)': cost_per_lot,
                    '個股年化報酬': ann_ret,
                    '個股夏普比率': sharpe,
                    '個股Alpha': alpha,
                    '個股Beta': beta,
                    '個股風報比': calmar,
                    '個股最大回撤': mdd,
                })
            except Exception as e:
                self.logger.warning(f"計算 {ticker} 因子錯誤: {e}")

        factor_df = pd.DataFrame(all_factors).set_index('ticker')

        # 6. 產業中性化 Z-Score（若有提供 sector_map）
        if sector_map is not None and not factor_df.empty:
            factor_df = self.sector_neutralize(factor_df, sector_map)

        # 7. FinLab 籌碼面因子（選用，需 use_finlab=True）
        if self.config.get("data_source_settings", {}).get("use_finlab", False) and not factor_df.empty:
            chip_df = self._get_finlab_chip_factors(list(data_dict.keys()))
            if not chip_df.empty:
                factor_df = factor_df.join(chip_df, how="left")
                self.logger.info(f"已合併 FinLab 籌碼因子，新增欄位：{list(chip_df.columns)}")

        return factor_df

    def _get_finlab_chip_factors(self, tickers: list) -> pd.DataFrame:
        """取得 FinLab 籌碼面因子（外資/投信近 5 日買賣超）。

        需要 ``finlab`` 套件及已登入 API Token。失敗時回傳空 DataFrame，
        不影響主流程其他因子。
        """
        try:
            from finlab import data as fl_data

            foreign = fl_data.get(
                "institutional_investors_trading_summary:"
                "外陸資買賣超股數(不含外資自營商)"
            )
            trust = fl_data.get(
                "institutional_investors_trading_summary:投信買賣超股數"
            )

            result: dict = {}
            for ticker in tickers:
                code = ticker.replace(".TW", "").replace(".TWO", "")
                row: dict = {}
                if code in foreign.columns:
                    recent = foreign[code].dropna().tail(5)
                    row["外資近5日買超"] = float(recent.sum())
                    row["外資連買天數"] = int((recent > 0).sum())
                if code in trust.columns:
                    recent_t = trust[code].dropna().tail(5)
                    row["投信近5日買超"] = float(recent_t.sum())
                if row:
                    result[ticker] = row

            return pd.DataFrame(result).T

        except ImportError:
            self.logger.warning("FinLab 未安裝，跳過籌碼因子")
            return pd.DataFrame()
        except Exception as e:
            self.logger.warning(f"FinLab 籌碼因子取得失敗: {e}")
            return pd.DataFrame()

    def sector_neutralize(
        self,
        factor_df: pd.DataFrame,
        sector_map: dict,
    ) -> pd.DataFrame:
        """對因子 DataFrame 進行產業截面 Z-Score 中性化。

        對每個數值型因子欄位，依 sector_map 指定的產業分組計算截面 Z-Score：
            z_i = (factor_i - mean_sector) / (std_sector + 1e-9)
        若某產業僅含 1 支股票，該股票的 Z-Score 設為 0。
        原始欄位保留，新增欄位以 ``_Z`` 為後綴（例如 ``RSI_Z``）。

        Args:
            factor_df: index=ticker 的因子 DataFrame（由 calculate_factors 產生）。
            sector_map: {ticker: 產業名稱} 的 dict，例如
                {"2330.TW": "半導體", "2317.TW": "電子製造"}。

        Returns:
            原始欄位 + _Z 後綴欄位的 DataFrame，結構與輸入相同。
        """
        df = factor_df.copy()

        # 只對數值欄位做中性化，排除純文字欄位
        non_numeric_cols = {'名稱'}
        numeric_cols = [
            col for col in df.columns
            if col not in non_numeric_cols and pd.api.types.is_numeric_dtype(df[col])
        ]

        # 建立 ticker → sector Series（只含在 factor_df 中的 ticker）
        sector_series = pd.Series(sector_map).reindex(df.index)

        for col in numeric_cols:
            z_col = col + '_Z'
            z_values = pd.Series(0.0, index=df.index)

            for sector, group_idx in sector_series.groupby(sector_series).groups.items():
                # group_idx 為該產業的所有 ticker
                valid_idx = [t for t in group_idx if t in df.index]
                if len(valid_idx) <= 1:
                    # 單支股票產業：Z-Score 設為 0（已初始化為 0）
                    continue
                group_vals = df.loc[valid_idx, col]
                mean_val = group_vals.mean()
                std_val = group_vals.std()
                z_values.loc[valid_idx] = (group_vals - mean_val) / (std_val + 1e-9)

            # 不在 sector_map 中的 ticker 維持 0
            df[z_col] = z_values

        return df

    def screen_stocks(
        self,
        df: pd.DataFrame,
        criteria: dict,
    ) -> pd.DataFrame:
        """根據 criteria 條件字典篩選股票。

        Args:
            df: 因子 DataFrame（index=ticker）。
            criteria: {欄位名稱: callable}，callable 接受 pd.Series 回傳布林 Series。
                例如 {"RSI": lambda x: x < 70, "日均成交張數": lambda x: x > 500}。

        Returns:
            符合所有條件的股票子集 DataFrame。
        """
        filtered_df = df.copy()
        for factor, cond in criteria.items():
            if cond and factor in filtered_df.columns:
                filtered_df = filtered_df[cond(filtered_df[factor])]
        return filtered_df
