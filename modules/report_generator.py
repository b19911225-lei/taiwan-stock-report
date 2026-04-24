import os
import pandas as pd
import matplotlib.pyplot as plt
import logging
from datetime import datetime

class ReportGenerator:
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("ReportGenerator")

    def create_output_directory(self):
        """建立以 YYYY-MM-DD_執行結果 為名的子資料夾"""
        base_path = self.config['system']['output_path']
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        folder_name = f"{timestamp}_執行結果"
        output_folder = os.path.join(base_path, folder_name)
        
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
        
        return output_folder

    def generate_excel(self, backtest_results, weights, output_folder):
        """將最終選股結果與回測數據輸出為格式化的 Excel (使用 openpyxl)"""
        excel_path = os.path.join(output_folder, "策略績效報告.xlsx")
        
        # 1. 績效彙總數據 - 映射為繁體中文名稱
        metrics = backtest_results['metrics']
        metric_mapping = {
            'total_return': '總報酬率',
            'annualized_return': '年化報酬率',
            'annualized_vol': '年化波動率',
            'sharpe_ratio': '夏普比率',
            'max_drawdown': '最大回撤 (MDD)'
        }
        
        translated_metrics = {metric_mapping.get(k, k): v for k, v in metrics.items()}
        metrics_df = pd.DataFrame.from_dict(translated_metrics, orient='index', columns=['數值'])
        metrics_df.index.name = "績效指標"
        
        # 2. 選股權重分配
        weights_df = weights.to_frame(name="配置權重 (%)")
        weights_df.index.name = "股票代號"
        weights_df["配置權重 (%)"] = (weights_df["配置權重 (%)"] * 100).round(2)
        
        # 3. 寫入多個分頁
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            metrics_df.to_excel(writer, sheet_name='績效彙總')
            weights_df.to_excel(writer, sheet_name='選股配置清單')
            
        self.logger.info(f"Excel 報表已生成於: {excel_path}")

    def plot_equity_curve(self, backtest_results, output_folder):
        """利用 matplotlib 繪製精美的繁體中文資金淨值曲線圖"""
        equity_curve = backtest_results['equity_curve']
        
        if equity_curve.empty:
            return

        # 設定中文字體 (Windows 預設微軟正黑體)
        plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei']
        plt.rcParams['axes.unicode_minus'] = False # 解決負號顯示問題

        plt.figure(figsize=(12, 6))
        plt.plot(equity_curve, label='投資組合淨值 (Equity)', color='#1a237e', linewidth=2)
        plt.axhline(1.0, linestyle='--', color='gray', alpha=0.5)
        
        # 設定圖表樣式
        plt.title('量化選股策略 - 歷史資金淨值曲線', fontsize=16)
        plt.xlabel('日期', fontsize=12)
        plt.ylabel('淨值 (歸一化)', fontsize=12)
        plt.legend(loc='upper left')
        plt.grid(True, linestyle=':', alpha=0.6)
        
        # 存入輸出資料夾
        plot_path = os.path.join(output_folder, "資金淨值曲線圖.png")
        plt.savefig(plot_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"淨值曲線圖已保存於: {plot_path}")
