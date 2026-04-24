# 台股量化選股系統 改善實作計畫

> **For Claude Code:** 請依照下方 Task 順序逐一實作，每個 Task 完成後 commit 一次。所有路徑以 `C:\工作\台股量化選股系統` 為根目錄。

**目標：** 補強現有系統缺少的因子驗證（IC/IR）、Walk-Forward 主流程整合、以及 FinLab 籌碼因子擴充。

**架構：** 不破壞現有 modules，以「加法」方式新增功能模組，再整合進 Streamlit 介面。

**Tech Stack：** Python 3.11、Streamlit、pandas、numpy、scipy、yfinance、PyPortfolioOpt（現有）；新增 finlab（選用）

---

## Task 1：新增 IC/IR 計算模組

**目標：** 建立獨立的因子驗證模組，計算每個因子的 IC 時間序列與 IR 統計量。

**Files:**
- 新增：`modules/factor_ic.py`
- 修改：`modules/__init__.py`（加入 import）

**實作內容：**

```python
# modules/factor_ic.py

from __future__ import annotations
import logging
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
from scipy.stats import spearmanr


class FactorICAnalyzer:
    """
    因子 IC（Information Coefficient）分析器。
    
    IC = 因子截面值與下期報酬的 Spearman 相關係數。
    IR = IC 均值 / IC 標準差，衡量因子預測穩定性。
    """

    # IC 品質評級閾值
    GRADE_THRESHOLDS = {
        "強因子":   {"IR": 0.5,  "IC_mean": 0.05, "positive_rate": 0.55},
        "中等因子": {"IR": 0.3,  "IC_mean": 0.03, "positive_rate": 0.50},
        "弱因子":   {"IR": 0.0,  "IC_mean": 0.00, "positive_rate": 0.00},
    }

    def __init__(self) -> None:
        self.logger = logging.getLogger("FactorICAnalyzer")

    def calculate_ic_series(
        self,
        factor_snapshots: Dict[pd.Timestamp, pd.DataFrame],
        returns_snapshots: Dict[pd.Timestamp, pd.Series],
        factor_col: str,
    ) -> pd.Series:
        """
        計算單一因子在每個截面日的 IC。

        Args:
            factor_snapshots: {date: factor_DataFrame}，每個再平衡日的因子截面
            returns_snapshots: {date: 下期報酬 Series}，index 為 ticker
            factor_col: 要計算 IC 的因子欄位名稱

        Returns:
            IC 時間序列 pd.Series，index 為日期
        """
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

    def calculate_icir(self, ic_series: pd.Series) -> dict:
        """
        計算 IC 統計摘要。

        Returns:
            dict with keys: IC_mean, IC_std, IR, IC_positive_rate, n_periods, grade
        """
        if ic_series.empty:
            return {
                "IC_mean": np.nan, "IC_std": np.nan, "IR": np.nan,
                "IC_positive_rate": np.nan, "n_periods": 0, "grade": "資料不足",
            }

        ic_mean = float(ic_series.mean())
        ic_std  = float(ic_series.std())
        ir      = ic_mean / (ic_std + 1e-9)
        positive_rate = float((ic_series > 0).mean())

        # 判斷因子評級
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

    def build_snapshots(
        self,
        cleaned_data: dict,
        fundamental_info: dict,
        config: dict,
        forward_days: int = 21,
        resample_freq: str = "MS",
    ) -> tuple[Dict, Dict]:
        """
        建立因子截面快照與下期報酬快照。

        Args:
            cleaned_data: DataFetcher.preprocess() 的輸出
            fundamental_info: DataFetcher.get_historical_data() 的 fundamental_info
            config: 系統設定
            forward_days: 下期報酬天數（月頻=21）
            resample_freq: 截面頻率（預設每月第一個交易日）

        Returns:
            (factor_snapshots, returns_snapshots)
        """
        from modules.factors import FactorAnalyzer
        analyzer = FactorAnalyzer(config)

        # 取得全部交易日
        base = next(iter(cleaned_data.values()))
        all_dates = base.index

        # 建立月頻截面日期
        monthly_dates = pd.date_range(
            start=all_dates[0], end=all_dates[-max(forward_days + 5, 30)],
            freq=resample_freq
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

            fdf = analyzer.calculate_factors(data_to, fundamental_info)
            factor_snapshots[date] = fdf

            # 下期報酬
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

    def batch_icir(
        self,
        cleaned_data: dict,
        fundamental_info: dict,
        config: dict,
        forward_days: int = 21,
        factors_to_test: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        批量計算所有因子的 IC/IR，回傳排序表。

        Returns:
            pd.DataFrame，columns=[IC_mean, IC_std, IR, IC_positive_rate, n_periods, grade]
            按 |IR| 由大到小排序
        """
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
            self.logger.info(f"{col}: IC={stats['IC_mean']:.4f}, IR={stats['IR']:.4f}, 評級={stats['grade']}")

        result_df = pd.DataFrame(rows).set_index("factor")
        return result_df.reindex(
            result_df["IR"].abs().sort_values(ascending=False).index
        )
```

**修改 `modules/__init__.py`：**

```python
# 在現有 import 下方加入
from modules.factor_ic import FactorICAnalyzer
```

**驗證方式：**

```bash
cd C:\工作\台股量化選股系統
python -c "from modules.factor_ic import FactorICAnalyzer; print('OK')"
```

**Commit：**
```bash
git add modules/factor_ic.py modules/__init__.py
git commit -m "feat: 新增 FactorICAnalyzer 模組（IC/IR 計算）"
```

---

## Task 2：新增 IC/IR 的單元測試

**目標：** 確保 IC 計算邏輯正確，防止未來修改時引入 bug。

**Files:**
- 新增：`tests/test_factor_ic.py`

**實作內容：**

```python
# tests/test_factor_ic.py

import pytest
import numpy as np
import pandas as pd
from modules.factor_ic import FactorICAnalyzer


@pytest.fixture
def analyzer():
    return FactorICAnalyzer()


def make_snapshots(n_dates=12, n_stocks=30, seed=42):
    """製造測試用的因子快照與報酬快照。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_dates, freq="MS")
    tickers = [f"TICKER_{i:04d}" for i in range(n_stocks)]

    factor_snapshots = {}
    returns_snapshots = {}

    for date in dates:
        factor_vals = rng.standard_normal(n_stocks)
        # 讓因子與報酬有正相關（IC 應為正）
        returns = factor_vals * 0.5 + rng.standard_normal(n_stocks) * 0.5

        fdf = pd.DataFrame({"test_factor": factor_vals}, index=tickers)
        factor_snapshots[date] = fdf
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
        # 因子與報酬正相關，IC 均值應為正
        assert ic.mean() > 0

    def test_missing_factor_col_returns_empty(self, analyzer):
        snaps, rets = make_snapshots()
        ic = analyzer.calculate_ic_series(snaps, rets, "nonexistent_factor")
        assert ic.empty

    def test_insufficient_stocks_skipped(self, analyzer):
        snaps, rets = make_snapshots(n_stocks=5)  # 少於 10 支，應跳過
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
        # 高 IC、高穩定 → 強因子
        ic = pd.Series([0.08] * 30)
        result = analyzer.calculate_icir(ic)
        assert result["grade"] == "強因子"

    def test_grade_weak_factor(self, analyzer):
        # 接近 0 → 弱因子
        ic = pd.Series([0.001, -0.001, 0.002, -0.002] * 5)
        result = analyzer.calculate_icir(ic)
        assert result["grade"] == "弱因子"
```

**執行測試：**

```bash
cd C:\工作\台股量化選股系統
pytest tests/test_factor_ic.py -v
```

**預期結果：** 所有測試 PASS

**Commit：**
```bash
git add tests/test_factor_ic.py
git commit -m "test: 新增 FactorICAnalyzer 單元測試"
```

---

## Task 3：將 IC/IR 整合進 Streamlit 介面

**目標：** 在 `app.py` 加入「因子 IC/IR 分析」頁籤，讓使用者可以視覺化查看哪些因子最有效。

**Files:**
- 修改：`app.py`

**在現有的 Streamlit 頁籤區塊中，加入新頁籤：**

找到 `app.py` 中 `st.tabs` 的地方（或主要 UI 進入點），加入以下邏輯：

```python
# 在 app.py 的 import 區塊加入
from modules.factor_ic import FactorICAnalyzer

# 在頁籤區塊加入「因子分析」頁籤
# 找到類似 tab1, tab2, tab3 = st.tabs([...]) 的地方，加入新頁籤

# ── 因子 IC/IR 分析頁籤內容 ──
with tab_factor_ic:   # 將 tab_factor_ic 加入你現有的 tabs 宣告
    st.header("📊 因子 IC / IR 分析")
    st.caption("驗證各因子對下期報酬的預測力。IR > 0.5 為強因子，0.3~0.5 為中等，< 0.3 為弱因子。")

    forward_days = st.selectbox(
        "下期報酬天數",
        options=[5, 10, 21, 63],
        index=2,
        format_func=lambda x: {5: "1週", 10: "2週", 21: "1個月", 63: "3個月"}[x],
    )

    if st.button("🔍 開始計算 IC/IR（需要幾分鐘）", type="primary"):
        with st.spinner("計算因子截面快照與 IC 序列中..."):
            try:
                ic_analyzer = FactorICAnalyzer()
                icir_df = ic_analyzer.batch_icir(
                    cleaned_data=st.session_state.get("cleaned_data", {}),
                    fundamental_info=st.session_state.get("fundamental_info", {}),
                    config=config,
                    forward_days=forward_days,
                )

                st.success(f"完成！共分析 {len(icir_df)} 個因子。")

                # 顏色標示評級
                def highlight_grade(row):
                    if row["grade"] == "強因子":
                        return ["background-color: #d4edda"] * len(row)
                    elif row["grade"] == "中等因子":
                        return ["background-color: #fff3cd"] * len(row)
                    else:
                        return ["background-color: #f8d7da"] * len(row)

                st.dataframe(
                    icir_df.style.apply(highlight_grade, axis=1).format({
                        "IC_mean": "{:.4f}",
                        "IC_std":  "{:.4f}",
                        "IR":      "{:.4f}",
                        "IC_positive_rate": "{:.1%}",
                    }),
                    use_container_width=True,
                    height=450,
                )

                # 長條圖：IR 視覺化
                import plotly.express as px
                fig = px.bar(
                    icir_df.reset_index(),
                    x="factor",
                    y="IR",
                    color="grade",
                    color_discrete_map={"強因子": "#28a745", "中等因子": "#ffc107", "弱因子": "#dc3545"},
                    title="各因子 IR 排序（絕對值越大預測力越強）",
                    labels={"factor": "因子", "IR": "IR 值"},
                )
                fig.add_hline(y=0.5, line_dash="dash", line_color="green", annotation_text="強因子門檻")
                fig.add_hline(y=0.3, line_dash="dash", line_color="orange", annotation_text="中等門檻")
                fig.add_hline(y=-0.5, line_dash="dash", line_color="green")
                fig.add_hline(y=-0.3, line_dash="dash", line_color="orange")
                st.plotly_chart(fig, use_container_width=True)

                st.session_state["icir_result"] = icir_df

            except Exception as e:
                st.error(f"計算失敗：{e}")

    elif "icir_result" in st.session_state:
        st.info("顯示上次計算結果（重新計算請按上方按鈕）")
        st.dataframe(st.session_state["icir_result"], use_container_width=True)
```

**注意：**
1. 確認 `cleaned_data` 和 `fundamental_info` 有存入 `st.session_state`
2. 若原本沒有，在資料載入完成後加入：
   ```python
   st.session_state["cleaned_data"] = cleaned_data
   st.session_state["fundamental_info"] = fundamental_info
   ```

**驗證方式：**
```bash
cd C:\工作\台股量化選股系統
streamlit run app.py
```
啟動後，點擊「因子分析」頁籤，按下「開始計算」按鈕，應出現 IC/IR 表格與長條圖。

**Commit：**
```bash
git add app.py
git commit -m "feat: Streamlit 新增因子 IC/IR 分析頁籤"
```

---

## Task 4：Walk-Forward 整合進主流程

**目標：** 在 Streamlit 加入「Walk-Forward 驗證」按鈕，讓使用者能一鍵執行樣本外驗證。

**Files:**
- 修改：`app.py`

**在 Streamlit 中加入 Walk-Forward 頁籤：**

```python
# ── Walk-Forward 驗證頁籤 ──
with tab_walkforward:   # 加入新頁籤
    st.header("🔄 Walk-Forward 驗證")
    st.caption("防止策略過擬合的黃金標準。樣本外夏普 > 0.5 代表策略真實有效。")

    col1, col2, col3 = st.columns(3)
    with col1:
        in_sample_months = st.number_input("訓練期（月）", min_value=6, max_value=36, value=12)
    with col2:
        out_sample_months = st.number_input("測試期（月）", min_value=1, max_value=12, value=3)
    with col3:
        step_months = st.number_input("滾動步長（月）", min_value=1, max_value=6, value=3)

    if st.button("🚀 執行 Walk-Forward 驗證", type="primary"):
        with st.spinner("Walk-Forward 驗證中，請稍候..."):
            try:
                from modules.backtest import WalkForwardValidator
                from modules.factors import FactorAnalyzer

                validator = WalkForwardValidator(
                    config=config,
                    in_sample_months=in_sample_months,
                    out_sample_months=out_sample_months,
                    step_months=step_months,
                )

                analyzer = FactorAnalyzer(config)
                fetcher_ref = st.session_state.get("fetcher")

                def strategy_fn(in_sample_data, cutoff_date):
                    """訓練期策略：選出夏普比率前 15 支"""
                    _, fi = fetcher_ref.get_historical_data(list(in_sample_data.keys()))
                    fdf = analyzer.calculate_factors(in_sample_data, fi)
                    screened = analyzer.screen_stocks(fdf, {
                        "RSI":           lambda x: (x > 30) & (x < 70),
                        "日均成交張數":   lambda x: x > 500,
                        "個股夏普比率":   lambda x: x > 0,
                    })
                    top = screened.nlargest(15, "個股夏普比率")
                    import pandas as pd
                    return pd.Series(1/len(top), index=top.index) if not top.empty else pd.Series()

                cleaned = st.session_state.get("cleaned_data", {})
                result = validator.run(strategy_fn, cleaned)

                oos = result.get("oos_metrics", {})
                windows = result.get("windows", [])

                # 關鍵指標顯示
                col_a, col_b, col_c = st.columns(3)
                col_a.metric(
                    "樣本外總報酬",
                    f"{oos.get('total_return', 0):.2%}",
                    help="所有樣本外視窗的累積報酬"
                )
                col_b.metric(
                    "樣本外夏普比率",
                    f"{oos.get('sharpe_ratio', 0):.2f}",
                    delta="✅ 通過" if oos.get("sharpe_ratio", 0) > 0.5 else "⚠️ 偏低",
                    help="> 0.5 代表策略有效"
                )
                col_c.metric(
                    "樣本外最大回撤",
                    f"{oos.get('max_drawdown', 0):.2%}",
                    help="樣本外期間最大跌幅"
                )

                # 視窗明細表
                if windows:
                    import pandas as pd
                    win_df = pd.DataFrame(windows)[
                        ["window_id", "in_start", "oos_start", "oos_end",
                         "oos_total_return", "oos_sharpe", "oos_max_drawdown"]
                    ]
                    win_df.columns = ["視窗", "訓練開始", "測試開始", "測試結束",
                                       "測試報酬", "測試夏普", "測試回撤"]
                    st.dataframe(
                        win_df.style.format({
                            "測試報酬": "{:.2%}",
                            "測試夏普": "{:.2f}",
                            "測試回撤": "{:.2%}",
                        }).background_gradient(subset=["測試報酬", "測試夏普"], cmap="RdYlGn"),
                        use_container_width=True,
                    )

                    # 樣本外權益曲線
                    oos_equity = result.get("oos_equity", None)
                    if oos_equity is not None and not oos_equity.empty:
                        import plotly.express as px
                        fig = px.line(
                            oos_equity.reset_index(),
                            x="index", y=0,
                            title="Walk-Forward 樣本外累積報酬曲線",
                            labels={"index": "日期", 0: "累積報酬"},
                        )
                        fig.add_hline(y=1.0, line_dash="dash", line_color="gray")
                        st.plotly_chart(fig, use_container_width=True)

            except Exception as e:
                st.error(f"Walk-Forward 執行失敗：{e}")
                import traceback
                st.code(traceback.format_exc())
```

**確認在資料載入時加入 fetcher 到 session_state：**

```python
st.session_state["fetcher"] = fetcher
```

**Commit：**
```bash
git add app.py
git commit -m "feat: Streamlit 新增 Walk-Forward 驗證頁籤"
```

---

## Task 5：修正 `requirements.txt` 並加入 scipy

**目標：** `factor_ic.py` 使用了 `scipy.stats.spearmanr`，確保相依套件齊全。

**Files:**
- 修改：`requirements.txt`

**現有 requirements.txt 加入：**

```
scipy
```

（已確認現有 requirements.txt 中已有 `scipy`，若已有則此 Task 跳過。）

**驗證：**
```bash
pip install -r requirements.txt
python -c "from scipy.stats import spearmanr; print('scipy OK')"
```

**Commit：**
```bash
git add requirements.txt
git commit -m "chore: 確認 scipy 相依套件"
```

---

## Task 6：（選做）FinLab 籌碼因子擴充

> **前提：需要 FinLab API Token（免費帳號有 500MB/日限制）。若無 Token 可跳過此 Task。**

**目標：** 在 `FactorAnalyzer.calculate_factors()` 中新增籌碼面因子（外資、投信買賣超）。

**Files:**
- 修改：`modules/factors.py`
- 修改：`config/settings.yaml`

**在 `settings.yaml` 加入：**

```yaml
# 資料源設定（新增）
data_source_settings:
  use_finlab: false          # 設為 true 啟用 FinLab 籌碼因子
  finlab_token: ""           # 填入 FinLab token（或用 finlab.login()）
```

**在 `modules/factors.py` 的 `calculate_factors()` 末尾加入：**

```python
def _get_finlab_chip_factors(self, tickers: list) -> pd.DataFrame:
    """
    取得 FinLab 籌碼面因子。
    需要 finlab 套件且 API Token 有效。

    Returns:
        DataFrame，index=ticker，含外資/投信買賣超欄位
    """
    try:
        from finlab import data as fl_data

        # 外資近 5 日淨買超（買 > 0）
        foreign = fl_data.get(
            "institutional_investors_trading_summary:"
            "外陸資買賣超股數(不含外資自營商)"
        )
        # 投信近 5 日淨買超
        trust = fl_data.get("institutional_investors_trading_summary:投信買賣超股數")

        result = {}
        for ticker in tickers:
            code = ticker.replace(".TW", "").replace(".TWO", "")
            row = {}

            if code in foreign.columns:
                recent = foreign[code].dropna().tail(5)
                row["外資近5日買超"] = float(recent.sum())
                row["外資連買天數"] = int((recent > 0).sum())

            if code in trust.columns:
                recent_t = trust[code].dropna().tail(5)
                row["投信近5日買超"] = float(recent_t.sum())

            result[ticker] = row

        return pd.DataFrame(result).T

    except ImportError:
        self.logger.warning("FinLab 未安裝，跳過籌碼因子")
        return pd.DataFrame()
    except Exception as e:
        self.logger.warning(f"FinLab 籌碼因子取得失敗: {e}")
        return pd.DataFrame()
```

**在 `calculate_factors()` 結尾合併籌碼資料：**

```python
# 在 factor_df = pd.DataFrame(all_factors).set_index('ticker') 之後加入
if self.config.get("data_source_settings", {}).get("use_finlab", False):
    chip_df = self._get_finlab_chip_factors(list(data_dict.keys()))
    if not chip_df.empty:
        factor_df = factor_df.join(chip_df, how="left")
        self.logger.info(f"已合併 FinLab 籌碼因子，新增欄位：{list(chip_df.columns)}")
```

**Commit：**
```bash
git add modules/factors.py config/settings.yaml
git commit -m "feat: 新增 FinLab 籌碼因子（選用）"
```

---

## 實作完成後的驗證清單

```bash
# 1. 所有單元測試通過
pytest tests/ -v

# 2. 系統正常啟動
streamlit run app.py

# 3. 確認新功能可見
# - Streamlit 有「因子 IC/IR 分析」頁籤
# - Streamlit 有「Walk-Forward 驗證」頁籤

# 4. IC/IR 計算結果合理
# - 至少有 2~3 個因子 IR 絕對值 > 0.3
# - 結果表格顯示「強/中等/弱因子」評級

# 5. Walk-Forward 可正常執行
# - 能顯示每個視窗的樣本外指標
# - 能顯示樣本外權益曲線
```

---

## 注意事項

1. **不要修改 `config/settings.yaml` 中現有的 tickers 清單**
2. **IC/IR 計算較耗時**（200 支股票 × 24 個截面 ≈ 2-5 分鐘），建議加入 `st.spinner`
3. **Walk-Forward 首次執行需要完整資料**，確認 `cleaned_data` 已載入才能執行
4. **FinLab Task 6 為選做**，沒有 Token 跳過即可，不影響其他功能
5. **每個 Task commit 一次**，方便回滾

---

*本計畫由 Hermes Agent 依現有程式碼分析產出。2026-04-25*
