import streamlit as st
import pandas as pd
import numpy as np
import yaml
import os
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from modules.data_fetcher import DataFetcher
from modules.factors import FactorAnalyzer
from modules.backtest import BacktestEngine, WalkForwardValidator, StressTestEngine
from modules.watchlist import WatchlistManager
from modules.factor_validator import FactorValidator
from modules.factor_ic import FactorICAnalyzer

st.set_page_config(page_title="台股量化分析系統 Pro", layout="wide")

# ── 讀取產業對照表 ────────────────────────────────────────────
@st.cache_data(ttl=86400)
def load_sector_map() -> dict:
    path = "config/sector_mapping.yaml"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("sector_map", {})
    return {}

# ── 快取資料抓取 ─────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="抓取市場資料中…")
def fetch_data(tickers: tuple, _fetcher: DataFetcher):
    raw, fund = _fetcher.get_historical_data(list(tickers))
    cleaned   = _fetcher.preprocess(raw)
    return cleaned, fund

# 初始化 session_state
if 'selected_tickers_a' not in st.session_state:
    st.session_state['selected_tickers_a'] = ["2330.TW", "0050.TW"]
if 'weights_a' not in st.session_state:
    st.session_state['weights_a'] = {}
if 'filtered_df' not in st.session_state:
    st.session_state['filtered_df'] = pd.DataFrame()
if 'cleaned_data' not in st.session_state:
    st.session_state['cleaned_data'] = {}
if 'fundamental_info' not in st.session_state:
    st.session_state['fundamental_info'] = {}

def run_app():
    with open('config/settings.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    st.sidebar.title("💎 台股量化系統 Pro")
    platform_mode = st.sidebar.radio("切換操作平台", [
        "自定義投資組合建構 (平台A)",
        "策略指標篩選與分配 (平台B)",
        "個股追蹤看板 (平台C)",
        "Walk-Forward 驗證 (平台D)",
        "壓力情境測試 (平台E)",
        "因子 IC/IR 分析 (平台F)",
    ])

    # 快取重置按鈕
    if st.sidebar.button("🔄 強制更新資料快取"):
        st.cache_data.clear()
        st.session_state['cleaned_data'] = {}
        st.sidebar.success("快取已清除，下次執行將重新抓取資料。")

    fetcher = DataFetcher(config)
    analyzer = FactorAnalyzer(config)
    engine = BacktestEngine(config)
    
    # 整合選股池
    config_tickers = config['data_settings']['tickers']
    popular_etfs = list(fetcher.name_map.keys())
    all_available_options = sorted(list(set(config_tickers + popular_etfs)))

    sector_map = load_sector_map()

    if platform_mode == "個股追蹤看板 (平台C)":
        render_platform_c(fetcher, all_available_options)
    elif platform_mode == "Walk-Forward 驗證 (平台D)":
        render_platform_d(fetcher, analyzer, engine, config, all_available_options, sector_map)
    elif platform_mode == "壓力情境測試 (平台E)":
        render_platform_e(fetcher, config, all_available_options)
    elif platform_mode == "因子 IC/IR 分析 (平台F)":
        render_platform_f(fetcher, config, all_available_options)
    elif platform_mode == "自定義投資組合建構 (平台A)":
        st.header("🔍 平台 A：自定義投資組合分析")
        
        # 同步功能區
        if not st.session_state['filtered_df'].empty:
            if st.button("📥 載入平台 B 的篩選結果至此組合"):
                b_tickers = st.session_state['filtered_df'].index.tolist()
                st.session_state['selected_tickers_a'] = sorted(list(set(st.session_state['selected_tickers_a'] + b_tickers)))
                st.success(f"已成功匯入 {len(b_tickers)} 檔標的！")

        with st.container():
            col_in1, col_in2 = st.columns([3, 1])
            selected_tickers = col_in1.multiselect("選擇投資標的", all_available_options, 
                                                  default=st.session_state['selected_tickers_a'],
                                                  key="multiselect_a")
            st.session_state['selected_tickers_a'] = selected_tickers
            benchmark_input = col_in2.text_input("對照基準", value="0050.TW")
            
            if selected_tickers:
                st.subheader("⚖️ 設定投資比例 (%)")
                weight_cols = st.columns(len(selected_tickers))
                user_weights = {}
                
                default_w = 100.0 / len(selected_tickers)
                for i, ticker in enumerate(selected_tickers):
                    with weight_cols[i]:
                        name = fetcher.name_map.get(ticker, ticker)
                        # 從 session_state 讀取或給予預設值
                        saved_w = st.session_state['weights_a'].get(ticker, default_w)
                        user_weights[ticker] = st.number_input(f"{name}", min_value=0.0, max_value=100.0, 
                                                              value=float(saved_w), key=f"w_input_a_{ticker}")
                        st.session_state['weights_a'][ticker] = user_weights[ticker]
                
                total_w = sum(user_weights.values())
                if total_w > 0:
                    final_weights = pd.Series({t: w/total_w for t, w in user_weights.items()})
                    if total_w != 100.0:
                        st.warning(f"總權重 {total_w:.1f}%，已自動依比例調整為 100%。")
                
                if st.button("🚀 執行組合分析與回測", use_container_width=True):
                    with st.spinner("計算中..."):
                        cleaned, fundamental = fetch_data(tuple(sorted(set(selected_tickers + [benchmark_input]))), fetcher)
                        st.session_state['cleaned_data'].update(cleaned)
                        results = engine.run(
                            final_weights,
                            st.session_state['cleaned_data'],
                            benchmark_ticker=benchmark_input,
                            execution_delay=config.get('backtest', {}).get('execution_delay', 1),
                        )
                        
                        if results:
                            st.divider()
                            m = results['metrics']
                            c1, c2, c3, c4, c5 = st.columns(5)
                            c1.metric("組合年化報酬", f"{m['annualized_return']:.2%}")
                            c2.metric("最大回撤 (MDD)", f"{m['max_drawdown']:.2%}")
                            c3.metric("夏普比率", f"{m['sharpe_ratio']:.2f}")
                            c4.metric("Alpha (α)", f"{m['alpha']:.2%}")
                            c5.metric("風報比 (Calmar)", f"{m['calmar_ratio']:.2f}")

                            fig = go.Figure()
                            fig.add_trace(go.Scatter(x=results['equity_curve'].index, y=results['equity_curve'], name='組合淨值', line=dict(width=3, color='royalblue')))
                            if benchmark_input in st.session_state['cleaned_data']:
                                b_df = st.session_state['cleaned_data'][benchmark_input]
                                b_col = 'Close' if 'Close' in b_df.columns else b_df.columns[0]
                                b_curve = (1 + b_df[b_col].pct_change().dropna()).cumprod()
                                fig.add_trace(go.Scatter(x=b_curve.index, y=b_curve, name=f'基準 ({benchmark_input})', line=dict(dash='dash', color='gray')))
                            fig.update_layout(title="累積淨值對照圖", height=550)
                            st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("請先挑選標的。")

    elif platform_mode == "策略指標篩選與分配 (平台B)":
        st.header("🎯 平台 B：策略指標篩選與分配")
        
        with st.sidebar:
            st.markdown("### 🛠 策略因子自定義")
            with st.expander("💰 基礎量能", expanded=True):
                amt_min    = st.number_input("金額下限 (億)", value=1.0)
                price_range = st.slider("股價範圍", 0, 1500, (10, 1000))
                min_lots   = st.number_input("日均成交張數下限", value=500, step=100,
                                             help="NEW-05：過濾低流動性高價股")
            with st.expander("🏛 基本面", expanded=False):
                per_max   = st.number_input("PER 上限", value=30.0)
                yield_min = st.slider("殖利率下限 (%)", 0, 10, 2) / 100
            with st.expander("🏆 歷史績效篩選", expanded=False):
                min_ann_ret = st.number_input("最小年化報酬 (%)", value=-50.0) / 100
                min_sharpe  = st.number_input("最小夏普", value=-1.0)
                min_alpha   = st.number_input("最小 Alpha (%)", value=-50.0) / 100
                beta_range  = st.slider("Beta 範圍", 0.0, 3.0, (0.0, 2.0))
                min_calmar  = st.number_input("最小風報比", value=0.0)

        if st.sidebar.button("🚀 執行海量篩選", use_container_width=True):
            with st.spinner("計算指標中..."):
                cleaned, fundamental = fetch_data(tuple(sorted(all_available_options)), fetcher)
                st.session_state['cleaned_data'].update(cleaned)
                factor_df = analyzer.calculate_factors(
                    st.session_state['cleaned_data'], fundamental, sector_map=sector_map or None
                )
                criteria = {
                    '成交金額(億)':  lambda x: x >= amt_min,
                    '成交價':        lambda x: (x >= price_range[0]) & (x <= price_range[1]),
                    '日均成交張數':  lambda x: x >= min_lots,
                    'PER':           lambda x: x <= per_max,
                    '殖利率':        lambda x: x >= yield_min,
                    '個股年化報酬':  lambda x: x >= min_ann_ret,
                    '個股夏普比率':  lambda x: x >= min_sharpe,
                    '個股Alpha':     lambda x: x >= min_alpha,
                    '個股Beta':      lambda x: (x >= beta_range[0]) & (x <= beta_range[1]),
                    '個股風報比':    lambda x: x >= min_calmar,
                }
                f_df = analyzer.screen_stocks(factor_df, criteria)
                if "0050.TW" in f_df.index:
                    f_df = f_df.drop("0050.TW")
                st.session_state['filtered_df'] = f_df

        if not st.session_state['filtered_df'].empty:
            f_df = st.session_state['filtered_df']
            st.subheader(f"✅ 策略篩選結果 (共 {len(f_df)} 檔)")
            show_cols = ["名稱", "成交價", "每張成本(元)", "日均成交張數",
                         "個股年化報酬", "個股夏普比率", "個股Alpha", "個股風報比"]
            show_cols = [c for c in show_cols if c in f_df.columns]
            fmt = {"成交價": "{:.2f}", "每張成本(元)": "{:,.0f}", "日均成交張數": "{:,.0f}",
                   "個股年化報酬": "{:.2%}", "個股夏普比率": "{:.2f}",
                   "個股Alpha": "{:.2%}", "個股風報比": "{:.2f}"}
            st.dataframe(f_df[show_cols].style.format(fmt), use_container_width=True)

            # 產業分布圖
            if sector_map:
                sectors = f_df.index.map(lambda t: sector_map.get(t, "其他"))
                sec_counts = sectors.value_counts()
                fig_sec = go.Figure(go.Pie(labels=sec_counts.index, values=sec_counts.values,
                                           hole=0.4, textinfo="label+percent"))
                fig_sec.update_layout(title="篩選結果產業分布", height=320, showlegend=False)
                st.plotly_chart(fig_sec, use_container_width=True)
            
            st.divider()
            st.subheader("⚖️ 動態權重分配")
            weight_data = pd.DataFrame({
                '股票': f_df.index,
                '名稱': f_df['名稱'].values,
                '權重(%)': [100.0 / len(f_df)] * len(f_df)
            })
            edited_weights = st.data_editor(weight_data, hide_index=True, use_container_width=True, key="editor_b")
            
            if st.button("📊 更新模擬績效"):
                final_w_vals = edited_weights['權重(%)'].values
                total_w = sum(final_w_vals)
                if total_w > 0:
                    weights_series = pd.Series(final_w_vals / total_w, index=edited_weights['股票'])
                    results = engine.run(weights_series, st.session_state['cleaned_data'], benchmark_ticker="0050.TW")
                    
                    if results:
                        m = results['metrics']
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("模擬年化報酬", f"{m['annualized_return']:.2%}")
                        c2.metric("最大回撤", f"{m['max_drawdown']:.2%}")
                        c3.metric("夏普比率", f"{m['sharpe_ratio']:.2f}")
                        c4.metric("Alpha (α)", f"{m['alpha']:.2%}")
                        
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=results['equity_curve'].index, y=results['equity_curve'], name='分配組合', line=dict(color='firebrick', width=3)))
                        b_df = st.session_state['cleaned_data']["0050.TW"]
                        b_col = 'Close' if 'Close' in b_df.columns else b_df.columns[0]
                        b_curve = (1 + b_df[b_col].pct_change().dropna()).cumprod()
                        fig.add_trace(go.Scatter(x=b_curve.index, y=b_curve, name='0050 基準', line=dict(dash='dash', color='gray')))
                        st.plotly_chart(fig, use_container_width=True)

def render_platform_c(fetcher, all_available_options):
    st.header("📌 平台 C：個股追蹤看板")
    wm = WatchlistManager()

    # ── 新增追蹤個股 ──────────────────────────────────────
    with st.expander("➕ 新增追蹤個股", expanded=False):
        col1, col2, col3 = st.columns(3)
        new_ticker = col1.selectbox("選擇標的", all_available_options, key="wl_new_ticker")
        new_price  = col2.number_input("買入成本 (0 = 僅觀察)", min_value=0.0, value=0.0, key="wl_entry")
        new_shares = col3.number_input("持有股數", min_value=0, value=0, step=1000, key="wl_shares")
        col4, col5, col6 = st.columns(3)
        alert_high = col4.number_input("漲幅警示上限 (0=不設)", min_value=0.0, value=0.0, key="wl_ah")
        alert_low  = col5.number_input("下跌警示下限 (0=不設)", min_value=0.0, value=0.0, key="wl_al")
        notes      = col6.text_input("備註", key="wl_notes")
        if st.button("✅ 加入追蹤清單", key="wl_add"):
            name = fetcher.name_map.get(new_ticker, new_ticker)
            ok = wm.add(new_ticker, name, new_price, new_shares, alert_high, alert_low, notes)
            if ok:
                st.success(f"已加入 {name} ({new_ticker})")
                st.rerun()
            else:
                st.warning(f"{new_ticker} 已在追蹤清單中")

    # ── 載入資料並顯示 ────────────────────────────────────
    items = wm.get_all()
    if not items:
        st.info("追蹤清單為空，請從上方新增個股。")
        return

    tickers_to_fetch = [d["ticker"] for d in items]
    with st.spinner("抓取最新報價..."):
        raw, fund = fetcher.get_historical_data(tickers_to_fetch)
        cleaned = fetcher.preprocess(raw)

    enriched_df = wm.enrich_with_price(cleaned)
    if enriched_df.empty:
        st.warning("無法取得報價資料。")
        return

    # ── 警示摘要 ──────────────────────────────────────────
    alerts = enriched_df[enriched_df.get("價格警示", pd.Series("")) != ""]
    if "價格警示" in enriched_df.columns:
        alert_rows = enriched_df[enriched_df["價格警示"] != ""]
        if not alert_rows.empty:
            st.error("⚠️ **價格警示觸發：** " + "　".join(
                f"{r['name']}({r['ticker']}) {r['價格警示']}"
                for _, r in alert_rows.iterrows()
            ))

    # ── 主表格 ────────────────────────────────────────────
    display_cols = ["ticker", "name", "現價", "日漲跌(%)", "RSI",
                    "entry_price", "shares", "未實現損益", "損益(%)",
                    "技術訊號", "價格警示", "notes"]
    display_cols = [c for c in display_cols if c in enriched_df.columns]

    col_rename = {
        "ticker": "代號", "name": "名稱",
        "entry_price": "成本", "shares": "股數", "notes": "備註"
    }
    show_df = enriched_df[display_cols].rename(columns=col_rename)

    def highlight_row(row):
        style = [""] * len(row)
        # 日漲跌 染色
        if "日漲跌(%)" in row.index:
            v = row["日漲跌(%)"]
            try:
                v = float(v)
                if v > 0:
                    style[list(row.index).index("日漲跌(%)")] = "color: red"
                elif v < 0:
                    style[list(row.index).index("日漲跌(%)")] = "color: green"
            except Exception:
                pass
        # 損益 染色
        if "損益(%)" in row.index:
            v = row["損益(%)"]
            try:
                v = float(v)
                idx = list(row.index).index("損益(%)")
                style[idx] = "color: red" if v > 0 else "color: green"
            except Exception:
                pass
        return style

    fmt = {}
    for c in ["現價", "成本"]:
        if c in show_df.columns:
            fmt[c] = "{:.2f}"
    for c in ["日漲跌(%)", "損益(%)"]:
        if c in show_df.columns:
            fmt[c] = "{:+.2f}%"
    if "未實現損益" in show_df.columns:
        fmt["未實現損益"] = lambda x: f"${x:,.0f}" if isinstance(x, (int, float)) and x != "" else x

    st.dataframe(
        show_df.style.apply(highlight_row, axis=1).format(fmt, na_rep="—"),
        use_container_width=True, height=420
    )

    # ── 刪除 ──────────────────────────────────────────────
    st.divider()
    col_del1, col_del2 = st.columns([2, 1])
    del_ticker = col_del1.selectbox(
        "移除追蹤個股",
        options=[d["ticker"] for d in items],
        format_func=lambda t: f"{fetcher.name_map.get(t, t)} ({t})",
        key="wl_del"
    )
    if col_del2.button("🗑 移除", key="wl_del_btn"):
        wm.remove(del_ticker)
        st.success(f"已移除 {del_ticker}")
        st.rerun()

    # ── 一鍵加入平台A ─────────────────────────────────────
    st.divider()
    if st.button("📥 將追蹤清單全部加入平台 A 投資組合", use_container_width=True):
        existing = st.session_state.get("selected_tickers_a", [])
        new_list = sorted(list(set(existing + [d["ticker"] for d in items])))
        st.session_state["selected_tickers_a"] = new_list
        st.success(f"已加入 {len(items)} 檔至平台A，請切換至平台A執行回測。")

    # ── 走勢圖 ────────────────────────────────────────────
    st.divider()
    st.subheader("📈 個股走勢對照")
    chart_tickers = st.multiselect(
        "選擇顯示標的",
        options=tickers_to_fetch,
        default=tickers_to_fetch[:min(4, len(tickers_to_fetch))],
        format_func=lambda t: f"{fetcher.name_map.get(t, t)} ({t})",
        key="wl_chart"
    )
    if chart_tickers:
        fig = go.Figure()
        for t in chart_tickers:
            if t in cleaned:
                df = cleaned[t]
                col = "Close" if "Close" in df.columns else df.columns[0]
                norm = df[col] / df[col].iloc[0] * 100
                fig.add_trace(go.Scatter(
                    x=norm.index, y=norm,
                    name=fetcher.name_map.get(t, t),
                    mode="lines"
                ))
        fig.update_layout(
            title="標準化走勢 (起始=100)",
            yaxis_title="相對淨值",
            height=420,
            hovermode="x unified"
        )
        st.plotly_chart(fig, use_container_width=True)


def render_platform_d(fetcher, analyzer, engine, config, all_available_options, sector_map):
    """平台D：Walk-Forward 滾動前進驗證。"""
    st.header("🔬 平台 D：Walk-Forward 滾動前進驗證")
    st.info("Walk-Forward 驗證使用「樣本內訓練 → 樣本外測試」滾動方式，避免過擬合，是策略上線前的最低門檻。")

    col1, col2, col3 = st.columns(3)
    in_months  = col1.number_input("樣本內月數", value=12, min_value=3, max_value=36)
    oos_months = col2.number_input("樣本外月數", value=3,  min_value=1, max_value=12)
    step_months = col3.number_input("滾動步長月數", value=3, min_value=1, max_value=6)

    wf_tickers = st.multiselect(
        "選擇標的池（建議 10–30 支）",
        all_available_options,
        default=["2330.TW", "2317.TW", "2454.TW", "0050.TW", "2882.TW", "2412.TW"],
        key="wf_tickers"
    )

    if st.button("🚀 執行 Walk-Forward 驗證", use_container_width=True):
        if len(wf_tickers) < 3:
            st.error("請至少選擇 3 支標的。")
            return

        with st.spinner("滾動驗證計算中（視標的數與視窗數可能需要數分鐘）…"):
            cleaned, fund = fetch_data(tuple(sorted(wf_tickers)), fetcher)

            def strategy_fn(data_slice: dict, as_of_date) -> pd.Series:
                factor_df = analyzer.calculate_factors(data_slice, fund, sector_map=sector_map or None)
                if factor_df.empty:
                    return pd.Series(dtype=float)
                top_n = max(3, len(factor_df) // 4)
                if '個股夏普比率' in factor_df.columns:
                    selected = factor_df.nlargest(top_n, '個股夏普比率')
                else:
                    selected = factor_df.head(top_n)
                return pd.Series(1.0 / len(selected), index=selected.index)

            wfv = WalkForwardValidator(config, in_sample_months=int(in_months),
                                       out_sample_months=int(oos_months),
                                       step_months=int(step_months))
            result = wfv.run(strategy_fn, cleaned)

        if not result or result.get('oos_equity') is None or result['oos_equity'].empty:
            st.warning("資料不足以建立完整的滾動視窗，請增加標的或調整參數。")
            return

        oos_m = result.get('oos_metrics', {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("OOS 年化報酬", f"{oos_m.get('annualized_return', 0):.2%}")
        c2.metric("OOS 夏普比率", f"{oos_m.get('sharpe_ratio', 0):.2f}")
        c3.metric("OOS 最大回撤", f"{oos_m.get('max_drawdown', 0):.2%}")
        c4.metric("視窗數",       str(len(result.get('windows', []))))

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=result['oos_equity'].index, y=result['oos_equity'],
            name="OOS 累積淨值", line=dict(color="firebrick", width=2)
        ))
        if "0050.TW" in cleaned:
            b = cleaned["0050.TW"]
            b_col = "Close" if "Close" in b.columns else b.columns[0]
            b_curve = (1 + b[b_col].pct_change().dropna()).cumprod()
            common = result['oos_equity'].index
            b_slice = b_curve.reindex(common).dropna()
            fig.add_trace(go.Scatter(x=b_slice.index, y=b_slice / b_slice.iloc[0],
                                     name="0050 基準", line=dict(dash="dash", color="gray")))
        fig.update_layout(title="Walk-Forward 樣本外累積淨值", height=480, hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)

        if result.get('windows'):
            win_df = pd.DataFrame(result['windows'])
            st.subheader("各視窗績效摘要")
            st.dataframe(win_df, use_container_width=True)

        sharpe_oos = oos_m.get('sharpe_ratio', 0)
        if sharpe_oos < 0.3:
            st.error(f"⚠️ OOS 夏普比率 {sharpe_oos:.2f} < 0.3，策略在樣本外表現不佳，建議重新審視因子設計。")
        else:
            st.success(f"✅ OOS 夏普比率 {sharpe_oos:.2f} ≥ 0.3，策略通過基本樣本外驗證。")


def render_platform_e(fetcher, config, all_available_options):
    """平台E：壓力情境測試。"""
    st.header("💥 平台 E：壓力情境測試")
    st.info("以台股三大歷史危機情境，測試投資組合在極端市況下的最大回撤與相對損失。")

    stress_tickers = st.multiselect(
        "選擇測試標的",
        all_available_options,
        default=["2330.TW", "2317.TW", "0050.TW"],
        key="stress_tickers"
    )
    col1, col2 = st.columns(2)
    with col1:
        weights_input = {}
        if stress_tickers:
            st.subheader("設定權重（%）")
            for t in stress_tickers:
                weights_input[t] = st.number_input(t, 0.0, 100.0,
                    value=round(100.0 / len(stress_tickers), 1), key=f"stress_w_{t}")

    if st.button("🚀 執行壓力測試", use_container_width=True) and stress_tickers:
        with st.spinner("抓取歷史資料並計算情境績效…"):
            cleaned, _ = fetch_data(tuple(sorted(set(stress_tickers + ["0050.TW"]))), fetcher)
            total_w = sum(weights_input.values())
            weights = pd.Series({t: w / total_w for t, w in weights_input.items()})
            ste = StressTestEngine(config)
            scenario_results = ste.run(weights, cleaned)

        if not scenario_results:
            st.warning("無法計算壓力情境（資料不足）。")
            return

        rows = []
        for name, m in scenario_results.items():
            if m:
                rows.append({
                    "情境":        name,
                    "期間報酬":    f"{m.get('total_return', 0):.2%}",
                    "最大回撤":    f"{m.get('max_drawdown', 0):.2%}",
                    "夏普比率":    f"{m.get('sharpe_ratio', 0):.2f}",
                    "相對 0050 Alpha": f"{m.get('alpha', 0):.2%}" if m.get('alpha') else "—",
                })
        if rows:
            st.subheader("情境測試結果")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        fig = go.Figure()
        for name, m in scenario_results.items():
            if m and 'equity_curve' in m:
                ec = m['equity_curve']
                fig.add_trace(go.Scatter(x=ec.index, y=ec, name=name, mode="lines"))
        fig.update_layout(title="各情境淨值走勢", height=420, hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)


def render_platform_f(fetcher, config, all_available_options):
    """平台F：因子 IC/IR 分析。"""
    st.header("📊 因子 IC / IR 分析")
    st.caption("驗證各因子對下期報酬的預測力。IR > 0.5 為強因子，0.3~0.5 為中等，< 0.3 為弱因子。")

    col1, col2 = st.columns(2)
    with col1:
        selected_tickers = st.multiselect(
            "選擇分析標的池（建議 ≥ 30 支）",
            all_available_options,
            default=all_available_options[: min(50, len(all_available_options))],
            key="icir_tickers",
        )
    with col2:
        forward_days = st.selectbox(
            "下期報酬天數",
            options=[5, 10, 21, 63],
            index=2,
            format_func=lambda x: {5: "1週", 10: "2週", 21: "1個月", 63: "3個月"}[x],
        )

    if st.button("🔍 開始計算 IC/IR（需要幾分鐘）", type="primary") and selected_tickers:
        with st.spinner("計算因子截面快照與 IC 序列中…"):
            try:
                cleaned, fundamental = fetch_data(tuple(sorted(set(selected_tickers))), fetcher)
                st.session_state['cleaned_data'].update(cleaned)
                st.session_state['fundamental_info'].update(fundamental)

                ic_analyzer = FactorICAnalyzer()
                icir_df = ic_analyzer.batch_icir(
                    cleaned_data=cleaned,
                    fundamental_info=fundamental,
                    config=config,
                    forward_days=forward_days,
                )

                if icir_df.empty:
                    st.warning("資料不足，無法計算 IC/IR。請增加標的數或拉長資料期間。")
                    return

                st.success(f"完成！共分析 {len(icir_df)} 個因子。")

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

                import plotly.express as px
                fig = px.bar(
                    icir_df.reset_index(),
                    x="factor", y="IR", color="grade",
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
                import traceback
                st.code(traceback.format_exc())

    elif "icir_result" in st.session_state:
        st.info("顯示上次計算結果（重新計算請按上方按鈕）")
        st.dataframe(st.session_state["icir_result"], use_container_width=True)


if __name__ == "__main__":
    run_app()
