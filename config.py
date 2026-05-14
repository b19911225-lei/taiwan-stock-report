# config.py - 環境變數與股票清單

import os

# 從環境變數讀取（GitHub Actions Secrets），本地開發請設定環境變數或建立 .env
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")
FINMIND_TOKEN      = os.environ.get("FINMIND_TOKEN",      "")

# 主題股清單
THEME_STOCKS = {
    # AI 伺服器 / CoWoS 封裝
    "T1_AI伺服器CoWoS": [
        "2330",  # 台積電
        "3711",  # 日月光投控
        "2325",  # 矽品
        "3034",  # 聯詠
        "6274",  # 台燿
        "3008",  # 大立光
        "2308",  # 台達電
        "6669",  # 緯穎
        "2376",  # 技嘉
        "2377",  # 微星
        "3231",  # 緯創
        "2356",  # 英業達
        "3706",  # 神達
        "4966",  # 譜瑞-KY
        "6456",  # GUC
        "2454",  # 聯發科
        "2379",  # 瑞昱
        "8299",  # 群聯
    ],
    # CPO 光通訊
    "T2_CPO光通訊": [
        "6533",  # 晶心科
        "6247",  # 成電
        "6530",  # 創意
        "3105",  # 穩懋
        "4977",  # 眾達電通
        "3490",  # 單驥
        "3491",  # 昇銳
        "4968",  # 立積
        "3703",  # 欣興
        "6288",  # 聯鈞
    ],
    # 矽光子
    "T3_矽光子": [
        "3081",  # 聯亞
        "2455",  # 全訊
        "2338",  # 光罩
        "3508",  # 位速
        "6274",  # 台燿
        "6288",  # 聯鈞
        "2303",  # 聯電
        "6770",  # 力積電
    ],
    # HBM / 先進封裝
    "T4_HBM先進封裝": [
        "2330",  # 台積電
        "8150",  # 南茂
        "6147",  # 頎邦
        "3711",  # 日月光投控
        "2325",  # 矽品
        "3014",  # 台揚
        "6271",  # 同欣電
        "2344",  # 華邦電
        "2408",  # 南亞科
        "5483",  # 中美晶
        "3443",  # 創意電子
        "2049",  # 上銀
    ],
    # AI 散熱 / 電源
    "T5_散熱電源": [
        "3017",  # 奇鋐
        "6230",  # 超眾
        "2308",  # 台達電
        "6415",  # 矽力-KY
        "1514",  # 亞力
        "6669",  # 緯穎
    ],
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
TARGET_STOCK_COUNT  = 10
