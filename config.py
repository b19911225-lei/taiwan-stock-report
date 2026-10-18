# config.py - 環境變數、股票清單與篩選參數

import os

# 從環境變數讀取（Routine 的雲端環境變數、GitHub Actions Secrets 或本地 .env）
# .strip()：避免複製貼上時帶到換行，造成 InvalidHeader（見 docs/PITFALLS.md）
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()
FINMIND_TOKEN      = os.environ.get("FINMIND_TOKEN",      "").strip()
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "").strip()

# API 備援模式（GitHub Actions 手動執行）使用的模型；Routine 模式由排程 session 自己寫
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5").strip()

# 時區：所有「今天」一律用台北時間判斷，不用主機時間（GitHub Actions 是 UTC）
TIMEZONE = "Asia/Taipei"

# 每日結果（選股 JSON、AI 輸出、已發送標記）存回 GitHub 的分支
HISTORY_BRANCH = os.environ.get("HISTORY_BRANCH", "claude/report-history").strip()

# 主題股清單
# 代號以 FinMind TaiwanStockInfo 的官方名稱為準；報告顯示的名稱也從官方清單取得，
# 這裡的註解只是方便閱讀。執行 `python verify.py universe` 可以檢查代號和名稱是否一致。
THEME_STOCKS = {
    # AI 伺服器 / CoWoS 封裝
    "T1_AI伺服器CoWoS": [
        "2330",  # 台積電
        "3711",  # 日月光投控
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
        "3443",  # 創意（原本誤植為 6456「GUC」，GUC 就是創意 3443）
        "2454",  # 聯發科
        "2379",  # 瑞昱
        "8299",  # 群聯
    ],
    # CPO 光通訊
    "T2_CPO光通訊": [
        "6533",  # 晶心科
        "3443",  # 創意（原本誤植為 6530）
        "3105",  # 穩懋
        "4977",  # 眾達-KY
        "3490",  # 單井
        "3491",  # 昇達科
        "4968",  # 立積
        "3037",  # 欣興（原本誤植為 3703 欣陸）
        "3450",  # 聯鈞（原本誤植為 6288，每天都抓不到資料）
    ],
    # 矽光子
    "T3_矽光子": [
        "3081",  # 聯亞
        "2455",  # 全新
        "2338",  # 光罩
        "3508",  # 位速
        "6274",  # 台燿
        "3450",  # 聯鈞
        "2303",  # 聯電
        "6770",  # 力積電
    ],
    # HBM / 先進封裝
    "T4_HBM先進封裝": [
        "2330",  # 台積電
        "8150",  # 南茂
        "6147",  # 頎邦
        "3711",  # 日月光投控
        "3014",  # 聯陽（註解原寫「台揚」，台揚是 2314；請確認這檔是否是你要的）
        "6271",  # 同欣電
        "2344",  # 華邦電
        "2408",  # 南亞科
        "5483",  # 中美晶
        "3443",  # 創意
        "2049",  # 上銀
    ],
    # AI 散熱 / 電源
    "T5_散熱電源": [
        "3017",  # 奇鋐
        "6230",  # 尼得科超眾
        "2308",  # 台達電
        "6415",  # 矽力*-KY
        "1514",  # 亞力
        "6669",  # 緯穎
    ],
}
# 已移除：2325 矽品（2018 年併入日月光投控後下市）、6247（每天都抓不到資料）

# 預期名稱：官方清單抓不到時拿來顯示，也用來核對代號有沒有填錯（verify.py universe）
STOCK_NAMES = {
    "2330": "台積電",   "3711": "日月光投控", "3034": "聯詠",     "6274": "台燿",
    "3008": "大立光",   "2308": "台達電",     "6669": "緯穎",     "2376": "技嘉",
    "2377": "微星",     "3231": "緯創",       "2356": "英業達",   "3706": "神達",
    "4966": "譜瑞-KY",  "3443": "創意",       "2454": "聯發科",   "2379": "瑞昱",
    "8299": "群聯",     "6533": "晶心科",     "3105": "穩懋",     "4977": "眾達-KY",
    "3490": "單井",     "3491": "昇達科",     "4968": "立積",     "3037": "欣興",
    "3450": "聯鈞",     "3081": "聯亞",       "2455": "全新",     "2338": "光罩",
    "3508": "位速",     "2303": "聯電",       "6770": "力積電",   "8150": "南茂",
    "6147": "頎邦",     "3014": "聯陽",       "6271": "同欣電",   "2344": "華邦電",
    "2408": "南亞科",   "5483": "中美晶",     "2049": "上銀",     "3017": "奇鋐",
    "6230": "超眾",     "6415": "矽力-KY",    "1514": "亞力",
}


# 建立股票 → 所屬主題映射（去重，保留所有主題）
def build_stock_universe():
    stock_themes = {}
    for theme, stocks in THEME_STOCKS.items():
        for s in stocks:
            if s not in stock_themes:
                stock_themes[s] = []
            if theme not in stock_themes[s]:
                stock_themes[s].append(theme)
    return stock_themes

STOCK_UNIVERSE = build_stock_universe()

# 當日成交量排行加入股票池的檔數與主題名稱
TOP_VOLUME_COUNT = 10
HOT_THEME = "T0_當日熱門"

# ─── 篩選參數 ───
VOLUME_RATIO_20D    = 1.5
VOLUME_RATIO_5D     = 1.3
FOREIGN_BUY_DAYS    = 3
RSI_PERIOD          = 14
MIN_VOLUME_LOT      = 500
MIN_PRICE           = 10.0
TARGET_STOCK_COUNT  = 10
DEBATE_STOCK_COUNT  = 5

# ─── 進出場價位（見 docs/SPEC.md「進場、目標、停損」）───
# 停損：MA20 下方 1%，但距離收盤至少 MIN_STOP_PCT、最多 STOP_LOSS_PCT
STOP_LOSS_PCT       = 0.07
MIN_STOP_PCT        = 0.03
STOP_BELOW_MA20     = 0.01
# 目標：60 日高點和「收盤 + ATR_TARGET_MULT 倍 ATR」取較高者
# （入選檔數太少可以調高 ATR_TARGET_MULT 或調低 MIN_RR_RATIO）
ATR_PERIOD          = 14
ATR_TARGET_MULT     = 4.0
MIN_RR_RATIO        = 2.0

# ─── 績效追蹤 ───
PERFORMANCE_LOOKBACK_DAYS = 20   # 追蹤最近幾個交易日的推薦

# ─── 資料等待（FinMind 盤後更新可能晚一點）───
DATA_WAIT_INTERVAL_SEC = 120
