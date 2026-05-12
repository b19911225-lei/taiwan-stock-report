# config.py - 環境變數與股票清單

import os

# 優先從環境變數讀取（GitHub Actions Secrets），否則使用預設值
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8697694217:AAH0TbcFMK6Gychm9Ra726Ut9Tc3cdGIFTU")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "5961357097")
FINMIND_TOKEN      = os.environ.get("FINMIND_TOKEN",      "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiYjE5OTExMjI1IiwiZW1haWwiOiJiMTk5MTEyMjVAZ21haWwuY29tIiwidG9rZW5fdmVyc2lvbiI6MH0.eKAkhs7WRuDiq41vfHr-O6uZA9FB2t2PvH6VRDE0P7s")

# 主題股清單（含重複去除後保留所有主題標籤）
THEME_STOCKS = {
    "T1_AI伺服器CoWoS": ["2330", "3711", "2325", "3034", "6274", "3008", "2308"],
    "T2_CPO光通訊":     ["6533", "6247", "6530", "3105", "4977", "3490"],
    "T3_矽光子":        ["3081", "2455", "2338", "3508", "6274"],
    "T4_HBM先進封裝":   ["2330", "8150", "6147", "3711", "2325", "3014"],
}

# 建立股票 → 所屬主題映射（去重，保留所有主題）
def build_stock_universe():
    stock_themes = {}
    for theme, stocks in THEME_STOCKS.items():
        for s in stocks:
            if s not in stock_themes:
                stock_themes[s] = []
            stock_themes[s].append(theme)
    return stock_themes

STOCK_UNIVERSE = build_stock_universe()

# 篩選參數
VOLUME_RATIO_20D    = 1.5
VOLUME_RATIO_5D     = 1.3
FOREIGN_BUY_DAYS    = 3
RSI_PERIOD          = 14
STOP_LOSS_PCT       = 0.07
MIN_RR_RATIO        = 2.0
MIN_VOLUME_LOT      = 500
MIN_PRICE           = 10.0
TARGET_STOCK_COUNT  = 15
