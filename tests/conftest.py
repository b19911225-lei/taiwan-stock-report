import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_price_df(end_date="2026-10-16", n=65, start=100.0, drift=0.004,
                  last_chg=0.02, last_vol_mult=2.0, base_vol=1000.0, spread=0.02):
    """穩定上漲、最後一天放量上漲的合成 K 線（volume 單位：張；spread 控制日內高低差）。"""
    dates = pd.bdate_range(end=end_date, periods=n)
    closes = [start * (1 + drift) ** i * (1 + 0.004 * math.sin(i)) for i in range(n - 1)]
    closes.append(closes[-1] * (1 + last_chg))
    rows = []
    for i, (d, c) in enumerate(zip(dates, closes)):
        o = closes[i - 1] if i else c
        rows.append({
            "date": d, "open": o, "high": max(o, c) * (1 + spread), "low": min(o, c) * (1 - spread), "close": c,
            "volume": base_vol * (last_vol_mult if i == n - 1 else 1.0),
        })
    return pd.DataFrame(rows)


def make_inst_daily(end_date="2026-10-16", foreign=(-1, 1, 1, 1, 1), sitc=(1, 2, 3, 4, 5), dealer=(0, 0, 0, 0, 0)):
    """normalize_institutional_df() 輸出格式的合成法人資料（單位：股）。"""
    n = len(foreign)
    dates = pd.bdate_range(end=end_date, periods=n)
    return pd.DataFrame({
        "date": dates,
        "foreign": [x * 1_000_000 for x in foreign],
        "sitc": [x * 100_000 for x in sitc],
        "dealer": [x * 100_000 for x in dealer],
    })


@pytest.fixture
def price_df():
    return make_price_df()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """測試不能連網路：沒有被測試換掉的 HTTP 呼叫一律直接失敗（不然會卡在重試和等待）。"""
    import requests

    def blocked(*args, **kwargs):
        raise RuntimeError("測試不能連網路，請在測試裡換掉這個呼叫")
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
