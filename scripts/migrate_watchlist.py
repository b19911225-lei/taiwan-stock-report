"""
migrate_watchlist.py — 一次性將 config/watchlist.yaml 遷移至 data/watchlist.db
使用方式：python scripts/migrate_watchlist.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.watchlist import WatchlistManager

YAML_PATH = "config/watchlist.yaml"
DB_PATH   = "data/watchlist.db"

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    count = WatchlistManager.migrate_from_yaml(YAML_PATH, DB_PATH)
    print(f"遷移完成，共匯入 {count} 筆追蹤記錄至 {DB_PATH}")
    if count > 0:
        print(f"原始 YAML 檔案保留於 {YAML_PATH}，確認無誤後可手動刪除。")
