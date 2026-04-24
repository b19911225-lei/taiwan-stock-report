import os
import yaml
import logging
import sys
from datetime import datetime

# 將當前目錄加入 Python 路徑以便模組互相引用
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.data_fetcher import DataFetcher
from modules.factors import FactorAnalyzer
from modules.portfolio import PortfolioManager
from modules.backtest import BacktestEngine
from modules.report_generator import ReportGenerator

def setup_logger(config):
    """配置系統日誌"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"system_{timestamp}.log")
    
    # 清理舊的 root handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
        
    logging.basicConfig(
        level=config['system']['log_level'],
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger("Main")

def main():
    # 0. 路徑修正
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # 1. 載入配置
    config_path = "config/settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    logger = setup_logger(config)
    logger.info("==========================================")
    logger.info("  台股自主化量化交易系統 (Quant Platform)  ")
    logger.info("==========================================")

    try:
        # 2. 數據獲取 (Self-Healing ETL)
        fetcher = DataFetcher(config)
        raw_data, fundamental_info = fetcher.get_historical_data()
        cleaned_data = fetcher.preprocess(raw_data)

        # 3. 因子計算 (預設權重: 動能 0.5, 低波動 0.5)
        analyzer = FactorAnalyzer(config)
        default_weights = {'momentum': 0.5, 'low_vol': 0.5}
        factor_scores = analyzer.calculate_factors(cleaned_data, fundamental_info, default_weights)

        # 4. 部位控管 (Equal Weight / Inverse Vol)
        pm = PortfolioManager(config)
        weights = pm.optimize_weights(factor_scores, cleaned_data)

        if weights.empty:
            logger.error("沒有選出任何股票，回測終止。")
            return

        # 5. 歷史回測
        bt = BacktestEngine(config)
        backtest_results = bt.run(weights, cleaned_data)

        # 6. 報表產出 (Excel & Equity Curve)
        reporter = ReportGenerator(config)
        output_folder = reporter.create_output_directory()
        reporter.generate_excel(backtest_results, weights, output_folder)
        reporter.plot_equity_curve(backtest_results, output_folder)

        logger.info(f"系統執行成功！結果已存儲至: {output_folder}")
        logger.info("==========================================")

    except Exception as e:
        logger.error(f"系統運行發生嚴重錯誤: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
