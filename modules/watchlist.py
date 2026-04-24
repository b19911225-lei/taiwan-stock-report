"""watchlist.py — 個股追蹤清單管理器（SQLite 版）"""
from __future__ import annotations

import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yaml

DB_PATH = "data/watchlist.db"


class WatchlistManager:
    """以 SQLite 持久化存儲個股追蹤清單，支援損益計算與技術訊號警示。"""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self.logger = logging.getLogger("WatchlistManager")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # 資料庫初始化
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """建立 WAL 模式的 SQLite 連線。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """建立 watchlist 資料表（若不存在）。"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker      TEXT PRIMARY KEY,
                    name        TEXT DEFAULT '',
                    entry_price REAL DEFAULT 0.0,
                    shares      INTEGER DEFAULT 0,
                    alert_high  REAL DEFAULT 0.0,
                    alert_low   REAL DEFAULT 0.0,
                    notes       TEXT DEFAULT '',
                    date_added  TEXT
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        ticker: str,
        name: str,
        entry_price: float = 0.0,
        shares: int = 0,
        alert_high: float = 0.0,
        alert_low: float = 0.0,
        notes: str = "",
    ) -> bool:
        """新增追蹤個股；已存在則回傳 False。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist VALUES (?,?,?,?,?,?,?,?)",
                (ticker, name, float(entry_price), int(shares),
                 float(alert_high), float(alert_low), notes,
                 datetime.now().strftime("%Y-%m-%d")),
            )
            conn.commit()
            return cur.rowcount > 0

    def remove(self, ticker: str) -> bool:
        """移除指定個股；不存在則回傳 False。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker,))
            conn.commit()
            return cur.rowcount > 0

    def update(self, ticker: str, **kwargs) -> bool:
        """更新指定欄位；若 ticker 不存在則回傳 False。"""
        if not kwargs:
            return False
        allowed = {"name", "entry_price", "shares", "alert_high", "alert_low", "notes"}
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return False
        set_clause = ", ".join(f"{k}=?" for k in cols)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE watchlist SET {set_clause} WHERE ticker=?",
                (*cols.values(), ticker),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_all(self) -> list[dict]:
        """回傳所有追蹤個股的 list[dict]。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM watchlist ORDER BY date_added").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 即時價格與訊號豐富化
    # ------------------------------------------------------------------

    def enrich_with_price(self, data_dict: dict) -> pd.DataFrame:
        """合併最新報價、損益及技術訊號至追蹤清單。"""
        items = self.get_all()
        if not items:
            return pd.DataFrame()

        rows = []
        for item in items:
            ticker = item["ticker"]
            row = dict(item)
            if ticker not in data_dict:
                rows.append(row)
                continue

            df = data_dict[ticker]
            close_col = "Close" if "Close" in df.columns else df.columns[0]
            prices = df[close_col].dropna()
            if prices.empty:
                rows.append(row)
                continue

            last_price = prices.iloc[-1]
            prev_price = prices.iloc[-2] if len(prices) > 1 else last_price
            day_chg_pct = (last_price - prev_price) / (prev_price + 1e-9)

            # 損益
            cost   = item.get("entry_price", 0) or 0
            shares = item.get("shares", 0) or 0
            pnl     = (last_price - cost) * shares if cost > 0 and shares > 0 else np.nan
            pnl_pct = (last_price - cost) / cost   if cost > 0 else np.nan

            # RSI
            delta = prices.diff()
            gain  = delta.where(delta > 0, 0).rolling(14).mean()
            loss  = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi   = (100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1]

            # 技術訊號
            signals = []
            if rsi >= 70:
                signals.append("🔴 RSI超買")
            elif rsi <= 30:
                signals.append("🟢 RSI超賣")

            ema12 = prices.ewm(span=12).mean()
            ema26 = prices.ewm(span=26).mean()
            macd_h = ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()
            if len(macd_h) >= 2:
                if macd_h.iloc[-1] > 0 >= macd_h.iloc[-2]:
                    signals.append("🟢 MACD金叉")
                elif macd_h.iloc[-1] < 0 <= macd_h.iloc[-2]:
                    signals.append("🔴 MACD死叉")

            if len(prices) >= 60:
                sma20 = prices.rolling(20).mean().iloc[-1]
                sma60 = prices.rolling(60).mean().iloc[-1]
                if last_price > sma20 > sma60:
                    signals.append("📈 多頭排列")
                elif last_price < sma20 < sma60:
                    signals.append("📉 空頭排列")

            # 價格警示
            ah = item.get("alert_high", 0) or 0
            al = item.get("alert_low", 0) or 0
            price_alert = ""
            if ah > 0 and last_price >= ah:
                price_alert = f"⚠️ 觸及上限 {ah}"
            elif al > 0 and last_price <= al:
                price_alert = f"⚠️ 觸及下限 {al}"

            row.update({
                "現價":       round(last_price, 2),
                "日漲跌(%)":  round(day_chg_pct * 100, 2),
                "RSI":        round(rsi, 1),
                "未實現損益": round(pnl, 0)   if not np.isnan(pnl)     else "",
                "損益(%)":    round(pnl_pct * 100, 2) if not np.isnan(pnl_pct) else "",
                "技術訊號":   " | ".join(signals) if signals else "—",
                "價格警示":   price_alert,
            })
            rows.append(row)

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 遷移工具
    # ------------------------------------------------------------------

    @classmethod
    def migrate_from_yaml(cls, yaml_path: str, db_path: str = DB_PATH) -> int:
        """從舊版 YAML 檔案一次性遷移至 SQLite，回傳遷移筆數。"""
        if not os.path.exists(yaml_path):
            return 0
        with open(yaml_path, "r", encoding="utf-8") as f:
            items = yaml.safe_load(f) or []

        mgr = cls(db_path)
        count = 0
        for item in items:
            ok = mgr.add(
                ticker=item.get("ticker", ""),
                name=item.get("name", ""),
                entry_price=item.get("entry_price", 0.0),
                shares=item.get("shares", 0),
                alert_high=item.get("alert_high", 0.0),
                alert_low=item.get("alert_low", 0.0),
                notes=item.get("notes", ""),
            )
            if ok:
                count += 1
        return count
