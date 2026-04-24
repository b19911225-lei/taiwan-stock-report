import yfinance as yf
import pandas as pd
import numpy as np
import logging
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


class DataFetcher:
    """從 yfinance 抓取台股歷史資料，支援 SQLite 本地快取與除權息資料驗證。"""

    def __init__(self, config: dict) -> None:
        """初始化 DataFetcher，建立快取目錄與資料庫。"""
        self.config = config
        self.data_settings = config['data_settings']
        self.logger = logging.getLogger("DataFetcher")

        # 市場狀態過濾
        self.excluded_market_status: List[str] = self.data_settings.get(
            'excluded_market_status', ['delisted', 'full_delivery']
        )
        self.excluded_tickers: set = set(
            self.data_settings.get('excluded_tickers', [])
        )

        # 除權息驗證警示紀錄
        self.validation_warnings: Dict[str, List[str]] = {}

        # SQLite 快取設定
        self.cache_db_path: str = "cache/price_cache.db"
        os.makedirs("cache", exist_ok=True)
        self._init_cache_db()

        # 內建台股前 200 大與熱門 ETF 名稱表
        self.name_map: Dict[str, str] = {
            "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科", "2308.TW": "台達電",
            "2881.TW": "富邦金", "2882.TW": "國泰金", "2412.TW": "中華電", "2603.TW": "長榮",
            "2884.TW": "玉山金", "2886.TW": "兆豐金", "2382.TW": "廣達", "2357.TW": "華碩",
            "3711.TW": "日月光投控", "2408.TW": "南亞科", "2301.TW": "光寶科", "2891.TW": "中信金",
            "2892.TW": "第一金", "5880.TW": "合庫金", "2885.TW": "元大金", "2002.TW": "中鋼",
            "1101.TW": "台泥", "1102.TW": "亞泥", "1216.TW": "統一", "2105.TW": "正新",
            "2609.TW": "陽明", "2610.TW": "華航", "2618.TW": "長榮航", "2883.TW": "開發金",
            "2887.TW": "台新金", "2890.TW": "永豐金", "5871.TW": "中租-KY", "5876.TW": "上海商銀",
            # 熱門與常見台股 ETF (大幅擴充版)
            "0050.TW": "元大台灣50", "0051.TW": "元大中型100", "0052.TW": "富邦科技",
            "0053.TW": "元大電子", "0055.TW": "元大MSCI金融", "0056.TW": "元大高股息",
            "0057.TW": "富邦摩台", "006203.TW": "元大MSCI台灣", "006204.TW": "永豐臺灣加權",
            "006208.TW": "富邦台50", "00690.TW": "兆豐台灣ESG永續", "00692.TW": "富邦公司治理",
            "00701.TW": "國泰股利精選30", "00713.TW": "元大台灣高息低波", "00728.TW": "第一金工業30",
            "00730.TW": "富邦臺灣優質高息", "00731.TW": "復華富時高息低波", "00733.TW": "富邦臺灣中小",
            "00850.TW": "元大臺灣ESG永續", "00878.TW": "國泰永續高股息", "00881.TW": "國泰台灣5G+",
            "00888.TW": "富邦台灣半導體", "00891.TW": "中信關鍵半導體", "00892.TW": "富邦台灣核心半導體",
            "00894.TW": "中信小資高價30", "00896.TW": "中信綠能及電動車", "00900.TW": "富邦特選高股息30",
            "00904.TW": "新光臺灣半導體30", "00905.TW": "FT臺灣Smart", "00907.TW": "永豐優息存股",
            "00912.TW": "中信臺灣智慧50", "00915.TW": "凱基優選高股息30", "00918.TW": "大華優利高填息30",
            "00919.TW": "群益台灣精選高息", "00921.TW": "兆豐龍頭等權重", "00922.TW": "復華台灣科技優息",
            "00923.TW": "群益台ESG低碳50", "00927.TW": "群益半導體收益", "00929.TW": "復華台灣科技優息",
            "00930.TW": "永豐ESG低碳高息", "00932.TW": "兆豐永續高息等權", "00934.TW": "中信成長高股息",
            "00935.TW": "野村趨勢動能高息", "00936.TW": "台新永續高息中小", "00939.TW": "統一台灣高息動能",
            "00940.TW": "元大台灣價值高息", "00941.TW": "中信上游半導體", "00943.TW": "兆豐電子高息等權",
            "00944.TW": "野村臺灣新科技50", "00946.TW": "群益科技高息成長",
            "00981A.TW": "統一台股增長主動式ETF",
            # 海外型 ETF
            "00646.TW": "元大S&P500", "00662.TW": "富邦NASDAQ", "00668.TW": "國泰美國道瓊",
            "00757.TW": "第一金太空衛星", "00770.TW": "國泰北美科技", "00830.TW": "國泰費城半導體",
            "00858.TW": "永豐美國500大", "00886.TW": "永豐美國科技", "00893.TW": "國泰全球自動駕駛",
            "00895.TW": "富邦未來車", "00897.TW": "富邦元宇宙", "00898.TW": "國泰數位支付服務",
            "00902.TW": "中信電池及儲能", "00903.TW": "富邦特選大蘋果", "00908.TW": "富邦基因免疫生技",
            "00909.TW": "國泰全球基因免疫", "00911.TW": "兆豐洲際半導體", "00916.TW": "國泰全球品牌50",
            # 債券型 ETF
            "00679B.TW": "元大美債20年", "00680B.TW": "元大美債7-10", "00687B.TW": "國泰20年美債",
            "00696B.TW": "富邦美債20年", "00719B.TW": "元大美債1-3", "00720B.TW": "元大投資級公司債",
            "00725B.TW": "國泰投資級公司債", "00740B.TW": "富邦全球投等債", "00746B.TW": "富邦A級公司債",
            "00751B.TW": "元大AAA至A公司債", "00758B.TW": "富邦金融投等債", "00761B.TW": "國泰A級公司債",
            "00772B.TW": "亞太投等債券", "00773B.TW": "中信優先金融債", "00782B.TW": "國泰A級金融債",
            "00789B.TW": "復華20年美債", "00795B.TW": "中信美國公債20年", "00836B.TW": "中信ESG投資級債",
        }

    # ------------------------------------------------------------------
    # SQLite 快取內部方法
    # ------------------------------------------------------------------

    def _init_cache_db(self) -> None:
        """建立 SQLite 快取資料庫與 price_cache 資料表（若不存在）。"""
        conn = sqlite3.connect(self.cache_db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_cache (
                    ticker TEXT,
                    date   TEXT,
                    open   REAL,
                    high   REAL,
                    low    REAL,
                    close  REAL,
                    volume REAL,
                    amount REAL,
                    PRIMARY KEY (ticker, date)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _load_from_cache(
        self, ticker: str, start: str, end: str
    ) -> Optional[pd.DataFrame]:
        """從 SQLite 快取讀取指定 ticker 與日期區間的資料，若無資料回傳 None。"""
        conn = sqlite3.connect(self.cache_db_path)
        try:
            query = (
                "SELECT date, open, high, low, close, volume, amount "
                "FROM price_cache "
                "WHERE ticker = ? AND date >= ? AND date <= ? "
                "ORDER BY date"
            )
            df = pd.read_sql_query(query, conn, params=(ticker, start, end))
        finally:
            conn.close()

        if df.empty:
            return None

        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df.index.name = 'Date'
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Amount']
        return df

    def _save_to_cache(self, ticker: str, df: pd.DataFrame) -> None:
        """將 DataFrame 以 upsert 方式存入 SQLite 快取。"""
        if df is None or df.empty:
            return

        rows = []
        for idx, row in df.iterrows():
            date_str = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)[:10]
            open_val  = float(row['Open'])   if 'Open'   in row.index else np.nan
            high_val  = float(row['High'])   if 'High'   in row.index else np.nan
            low_val   = float(row['Low'])    if 'Low'    in row.index else np.nan
            close_val = float(row['Close'])  if 'Close'  in row.index else np.nan
            vol_val   = float(row['Volume']) if 'Volume' in row.index else np.nan
            amt_val   = float(row['Amount']) if 'Amount' in row.index else (close_val * vol_val if not np.isnan(close_val) else np.nan)
            rows.append((ticker, date_str, open_val, high_val, low_val, close_val, vol_val, amt_val))

        conn = sqlite3.connect(self.cache_db_path)
        try:
            conn.executemany(
                """
                INSERT OR REPLACE INTO price_cache
                    (ticker, date, open, high, low, close, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _get_cache_date_range(self, ticker: str) -> Tuple[Optional[str], Optional[str]]:
        """回傳指定 ticker 在快取中已有的最早與最晚日期（字串 YYYY-MM-DD），若無資料則回傳 (None, None)。"""
        conn = sqlite3.connect(self.cache_db_path)
        try:
            cur = conn.execute(
                "SELECT MIN(date), MAX(date) FROM price_cache WHERE ticker = ?",
                (ticker,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None or row[0] is None:
            return None, None
        return row[0], row[1]

    def clear_cache(self, ticker: Optional[str] = None) -> None:
        """清除快取；ticker=None 時清除全部，否則清除指定 ticker 的資料。"""
        conn = sqlite3.connect(self.cache_db_path)
        try:
            if ticker is None:
                conn.execute("DELETE FROM price_cache")
                self.logger.info("已清除全部快取資料")
            else:
                conn.execute("DELETE FROM price_cache WHERE ticker = ?", (ticker,))
                self.logger.info(f"已清除 {ticker} 的快取資料")
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 除權息資料驗證
    # ------------------------------------------------------------------

    def validate_adjusted_prices(
        self,
        ticker: str,
        df: pd.DataFrame,
        benchmark_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """驗證調整後股價是否存在疑似除權息誤差，將可疑日期記錄至 validation_warnings。"""
        if df is None or df.empty or 'Close' not in df.columns:
            return

        ret = df['Close'].pct_change()
        suspect_dates = ret[ret < -0.15].index

        if len(suspect_dates) == 0:
            return

        warnings_for_ticker: List[str] = []
        for dt in suspect_dates:
            # 若有基準（0050）資料，交叉驗證：基準當日跌幅 < 5% 才標記
            if benchmark_df is not None and not benchmark_df.empty and 'Close' in benchmark_df.columns:
                bench_ret = benchmark_df['Close'].pct_change()
                if dt in bench_ret.index:
                    if bench_ret.loc[dt] >= -0.05:
                        date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
                        warnings_for_ticker.append(date_str)
                        self.logger.warning(
                            f"[除權息驗證] {ticker} 在 {date_str} 單日跌幅 "
                            f"{ret.loc[dt]:.2%}，疑似調整誤差"
                        )
                    # 若基準也大跌，屬市場系統性下跌，不標記
                else:
                    # 基準日期不存在，保守地記錄
                    date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
                    warnings_for_ticker.append(date_str)
                    self.logger.warning(
                        f"[除權息驗證] {ticker} 在 {date_str} 單日跌幅 "
                        f"{ret.loc[dt]:.2%}，基準無對應日期，疑似調整誤差"
                    )
            else:
                # 無基準資料，直接標記超過 -15% 的日期
                date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
                warnings_for_ticker.append(date_str)
                self.logger.warning(
                    f"[除權息驗證] {ticker} 在 {date_str} 單日跌幅 "
                    f"{ret.loc[dt]:.2%}，疑似調整誤差（無基準）"
                )

        if warnings_for_ticker:
            if ticker not in self.validation_warnings:
                self.validation_warnings[ticker] = []
            self.validation_warnings[ticker].extend(warnings_for_ticker)

    def get_validation_warnings(self) -> Dict[str, List[str]]:
        """回傳所有除權息驗證警示，格式為 {ticker: [date_str, ...]}。"""
        return self.validation_warnings

    # ------------------------------------------------------------------
    # 主要公開方法
    # ------------------------------------------------------------------

    def get_historical_data(
        self, ticker_list: Optional[List[str]] = None
    ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, dict]]:
        """抓取歷史股價，優先使用本地快取，僅對缺漏區間呼叫 yfinance 增量更新。"""
        tickers = ticker_list if ticker_list is not None else self.data_settings['tickers']
        start_date = "2021-01-01"
        end_date = datetime.now().strftime("%Y-%m-%d")

        all_data: Dict[str, pd.DataFrame] = {}
        fundamental_info: Dict[str, dict] = {}

        self.logger.info(f"正在抓取 {len(tickers)} 檔標的的量化資料...")

        for ticker in tickers:
            # 市場狀態過濾：跳過被排除的標的
            if ticker in self.excluded_tickers:
                self.logger.info(f"跳過被排除標的：{ticker}")
                continue

            try:
                # 1. 查詢快取，取得已快取的日期範圍
                cache_min, cache_max = self._get_cache_date_range(ticker)

                new_df: Optional[pd.DataFrame] = None

                if cache_max is None:
                    # 快取完全沒有此 ticker，全量抓取
                    fetch_start = start_date
                    fetch_end = end_date
                    self.logger.debug(f"{ticker}: 快取無資料，全量下載 {fetch_start} ~ {fetch_end}")
                    new_df = self._fetch_from_yfinance(ticker, fetch_start, fetch_end)
                else:
                    # 增量更新：只抓快取最晚日期之後的資料
                    next_day = (
                        datetime.strptime(cache_max, '%Y-%m-%d') + timedelta(days=1)
                    ).strftime('%Y-%m-%d')
                    if next_day <= end_date:
                        self.logger.debug(f"{ticker}: 快取至 {cache_max}，增量下載 {next_day} ~ {end_date}")
                        new_df = self._fetch_from_yfinance(ticker, next_day, end_date)
                    else:
                        self.logger.debug(f"{ticker}: 快取已是最新，無需下載")

                # 2. 將新資料存入快取
                if new_df is not None and not new_df.empty:
                    self._save_to_cache(ticker, new_df)

                # 3. 從快取讀取完整資料（含剛存入的新資料）
                cached_df = self._load_from_cache(ticker, start_date, end_date)

                if cached_df is None or cached_df.empty:
                    self.logger.warning(f"跳過 {ticker}: 快取與網路均無資料")
                    continue

                # 4. 抓取基本面資料（dividends, info）
                dividends = pd.Series(dtype=float)
                try:
                    tk = yf.Ticker(ticker)
                    dividends = tk.dividends
                    info = tk.info
                    name = self.name_map.get(ticker) or info.get('shortName') or info.get('longName') or ticker
                    fundamental_info[ticker] = {
                        'name': name,
                        'pe_ratio': info.get('trailingPE', np.nan),
                        'pb_ratio': info.get('priceToBook', np.nan),
                        'roe': info.get('returnOnEquity', np.nan),
                        'shares_outstanding': info.get('sharesOutstanding', 0),
                        'dividends_history': dividends,
                    }
                except Exception:
                    fundamental_info[ticker] = {
                        'name': self.name_map.get(ticker, ticker),
                        'dividends_history': dividends,
                    }

                all_data[ticker] = cached_df

            except Exception as e:
                self.logger.warning(f"跳過 {ticker}: {e}")

        return all_data, fundamental_info

    def preprocess(
        self,
        raw_data: Dict[str, pd.DataFrame],
        benchmark_df: Optional[pd.DataFrame] = None,
    ) -> Dict[str, pd.DataFrame]:
        """清理資料、計算 Amount 欄位，並執行除權息驗證；保留 Open 欄位。"""
        cleaned: Dict[str, pd.DataFrame] = {}
        for t, df in raw_data.items():
            df = df.copy()
            df['Amount'] = df['Close'] * df['Volume']
            # 確保 Open 欄位存在時不被丟棄
            df = df.ffill().dropna(subset=['Close', 'Volume'])
            # 除權息資料驗證
            self.validate_adjusted_prices(t, df, benchmark_df=benchmark_df)
            cleaned[t] = df
        return cleaned

    # ------------------------------------------------------------------
    # 內部輔助方法
    # ------------------------------------------------------------------

    def _fetch_from_yfinance(
        self, ticker: str, start: str, end: str
    ) -> Optional[pd.DataFrame]:
        """呼叫 yfinance 下載指定 ticker 與日期區間的歷史股價，失敗時回傳 None。"""
        try:
            tk = yf.Ticker(ticker)
            df = tk.history(start=start, end=end, auto_adjust=True)
            if df.empty:
                return None
            # 標準化 index 名稱
            df.index.name = 'Date'
            # 移除 timezone 使 index 為純日期
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            # 計算 Amount（方便存快取）
            if 'Amount' not in df.columns:
                df['Amount'] = df['Close'] * df['Volume']
            return df
        except Exception as e:
            self.logger.warning(f"yfinance 下載 {ticker} 失敗（{start}~{end}）: {e}")
            return None
