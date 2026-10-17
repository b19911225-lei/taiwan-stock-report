import pandas as pd
import pytest

import data_fetcher
from data_fetcher import (TwseBlocked, _twse_get_json, parse_top_volume, parse_cnyes_news, normalize_price_df,
                          parse_twse_date)


class FakeResp:
    def __init__(self, status=200, text="", json_data=None, headers=None):
        self.status_code = status
        self.text = text
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(data_fetcher.time, "sleep", lambda s: None)


def test_block_page_with_http_200_is_detected(monkeypatch):
    # 文章坑 1：證交所擋頁回 HTTP 200，只看狀態碼會以為成功
    page = "<html>因為安全性考量，您所執行的頁面無法呈現</html>"
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: FakeResp(200, page))
    with pytest.raises(TwseBlocked):
        _twse_get_json("https://openapi.twse.com.tw/x")


def test_redirect_without_location_is_detected(monkeypatch):
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: FakeResp(307, ""))
    with pytest.raises(TwseBlocked):
        _twse_get_json("https://www.twse.com.tw/x")


def test_non_json_is_detected(monkeypatch):
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: FakeResp(200, "<html>hi</html>"))
    with pytest.raises(TwseBlocked):
        _twse_get_json("https://www.twse.com.tw/x")


def test_twse_request_does_not_pin_certifi(monkeypatch):
    # Routine 主機走代理，CA 由 REQUESTS_CA_BUNDLE 指定；寫死 verify=certifi.where() 會 TLS 失敗
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        return FakeResp(200, "{}", {"stat": "OK"})
    monkeypatch.setattr(data_fetcher.requests, "get", fake_get)
    _twse_get_json("https://www.twse.com.tw/x")
    assert "verify" not in seen


def test_top_volume_get_returns_error_instead_of_silent_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: FakeResp(200, "安全性考量"))
    monkeypatch.setattr(data_fetcher, "LOG_DIR", tmp_path)
    rows, err = data_fetcher.get_top_volume_stocks("2026-10-16")
    assert rows == [] and "擋頁" in err


def test_parse_top_volume_skips_etf_and_warrants():
    data = {"fields": ["排名", "證券代號", "證券名稱", "成交股數"],
            "data": [["1", "0050", "元大台灣50", "99,000,000"],
                     ["2", "2409", "友達", "341,960,123"],
                     ["3", "03055P", "權證", "500,000,000"],
                     ["4", "2303", "聯電", "92,105,000"]]}
    assert parse_top_volume(data, 10) == [("2409", "友達", 341960), ("2303", "聯電", 92105)]


def test_parse_cnyes_news():
    data = {"items": {"data": [
        {"newsId": 111, "title": "<b>台積電</b> 法說", "summary": "重點&amp;摘要", "publishAt": 1_000_000},
        {"newsId": 222, "title": "太舊", "summary": "", "publishAt": 1},
    ]}}
    news = parse_cnyes_news(data, hours=24, now_ts=1_000_100)
    assert len(news) == 1
    assert news[0]["id"] == "N1" and news[0]["title"] == "台積電 法說" and news[0]["summary"] == "重點&摘要"
    assert news[0]["url"].endswith("/111")
    # 1_000_000 = 1970-01-12 13:46 UTC → 台北 21:46（不受主機時區影響）
    assert news[0]["time"] == "01/12 21:46"


def test_normalize_price_df_drops_zero_price_rows():
    raw = pd.DataFrame({
        "date": ["2026-10-15", "2026-10-16"], "open": [10, 0], "max": [11, 0], "min": [9, 0],
        "close": [10.5, 0], "Trading_Volume": [1_000_000, 0],
    })
    df = normalize_price_df(raw)
    assert len(df) == 1 and df.iloc[0]["volume"] == 1000


def test_parse_twse_date():
    assert parse_twse_date("1151016") == "2026-10-16"
    assert parse_twse_date("115/10/16") == "2026-10-16"
    assert parse_twse_date("20261016") == "2026-10-16"
    assert parse_twse_date("2026-10-16") == "2026-10-16"
    assert parse_twse_date("") is None


def test_twse_closes_returns_date(monkeypatch):
    rows = [{"Date": "1151016", "Code": "2330", "ClosingPrice": "2,420.00"}]
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: FakeResp(200, "[]", rows))
    closes, d, err = data_fetcher.get_twse_closes()
    assert err is None and d == "2026-10-16" and closes == {"2330": 2420.0}
