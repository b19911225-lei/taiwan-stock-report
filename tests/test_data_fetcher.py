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


def test_institutional_uses_buysell_dataset(monkeypatch):
    # 原本的 TaiwanStockInstitutionalInvestors 會回 HTTP 422，籌碼分因此一直是 0
    calls = []

    def fake_fetch(dataset, stock_id=None, start_date=None, end_date=None, retries=3):
        calls.append(dataset)
        return pd.DataFrame([{"date": "2026-10-16", "stock_id": "2330", "name": "Foreign_Investor",
                              "buy": 10, "sell": 1}])
    monkeypatch.setattr(data_fetcher, "_fetch", fake_fetch)
    daily, unknown = data_fetcher.get_institutional_investors("2330", "2026-10-16")
    assert calls == ["TaiwanStockInstitutionalInvestorsBuySell"]
    assert daily.iloc[0]["foreign"] == 9 and unknown == []


# 2026-10-22 資料源檢查印出來的實際格式（節錄）
MI_INDEX = {"stat": "OK", "tables": [
    {"title": "115年10月21日 價格指數(臺灣證券交易所)", "fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "46,887.77"]]},
    {"title": "漲跌證券數合計", "fields": ["類型", "整體市場", "股票"],
     "data": [["上漲(漲停)", "10,182(111)", "600(24)"], ["下跌(跌停)", "5,341(61)", "380(1)"], ["持平", "900", "52"]]},
    {"title": "115年10月21日 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
     "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
                "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"],
     "data": [
         ["00400A", "主動國泰動能高息", "22,140,198", "6,089", "330,823,580", "14.95", "15.12", "14.76", "14.84",
          "<p style= color:red>+</p>", "0.23", "14.83", "110", "14.84", "486", "0.00"],
         ["2330", "台積電", "30,000,000", "1", "72,600,000,000", "2400", "2430", "2390", "2,420.00",
          "<p style= color:red>+</p>", "25.00", "", "", "", "", ""],
         ["2409", "友達", "305,682,000", "1", "5,000,000,000", "16", "16.5", "15.8", "16.00",
          "<p style= color:green>-</p>", "0.40", "", "", "", "", ""],
         ["1101", "台泥", "1,000", "1", "30,000", "--", "--", "--", "--", " ", "0.00", "", "", "", "", ""],
         ["2884", "玉山金", "10,000,000", "1", "300,000,000", "30", "30", "29.5", "30.00", "X", "0.00", "", "", "", "", ""],
     ]},
]}


def test_parse_twse_daily_quotes_and_breadth():
    quotes, breadth = data_fetcher.parse_twse_daily(MI_INDEX)
    assert breadth == {"up": 600, "limit_up": 24, "down": 380, "limit_down": 1, "flat": 52}
    assert quotes["2330"]["close"] == 2420.0 and quotes["2330"]["change"] == 25.0
    assert quotes["2330"]["chg_pct"] == round(25 / 2395 * 100, 2) and quotes["2330"]["value_100m"] == 726.0
    assert quotes["2409"]["change"] == -0.4 and quotes["2409"]["volume_lots"] == 305682
    assert "1101" not in quotes                     # 「--」沒有成交
    assert quotes["2884"]["chg_pct"] is None        # 「X」除權息，漲跌幅不能和前一天收盤比


def test_market_scan_picks_liquid_gainers_only():
    import analyzer
    quotes, _ = data_fetcher.parse_twse_daily(MI_INDEX)
    assert analyzer.pick_market_scan(quotes, set(), 10) == [("2330", "台積電")]   # ETF、下跌、除權息都不要
    assert analyzer.pick_market_scan(quotes, {"2330"}, 10) == []
    assert [r["stock_id"] for r in analyzer.top_turnover(quotes, 2)] == ["2330", "2409"]


def test_market_institutional(monkeypatch):
    rows = [{"date": "2026-10-21", "name": n, "buy": b, "sell": s} for n, b, s in [
        ("Foreign_Investor", 347069351600, 344830441784), ("Foreign_Dealer_Self", 0, 0),
        ("Dealer_self", 8827306626, 15014776691), ("Dealer_Hedging", 31790923805, 39175157607),
        ("Investment_Trust", 30398904149, 17600366911), ("total", 418086486180, 416620742993)]]
    rows.append({"date": "2026-10-20", "name": "Foreign_Investor", "buy": 1, "sell": 999})
    monkeypatch.setattr(data_fetcher, "_fetch", lambda *a, **k: pd.DataFrame(rows))
    out, err = data_fetcher.get_market_institutional("2026-10-21")
    assert err is None and out == {"foreign": 22.39, "sitc": 127.99, "dealer": -135.72}
    out, err = data_fetcher.get_market_institutional("2026-10-22")
    assert out is None and "還沒有 2026-10-22" in err


def test_us_markets_use_the_session_before_the_report_date(monkeypatch):
    # 台股 10/21 收盤時，美股 10/21 還沒開盤：要用 10/20 的收盤
    def fake(dataset, stock_id=None, start_date=None, end_date=None, retries=3):
        return pd.DataFrame({"date": ["2026-10-19", "2026-10-20", "2026-10-21"], "Close": [100.0, 102.0, 200.0]})
    monkeypatch.setattr(data_fetcher, "_fetch", fake)
    out, err = data_fetcher.get_us_markets("2026-10-21")
    assert err is None and len(out) == len(data_fetcher.US_MARKETS)
    assert out[0]["date"] == "2026-10-20" and out[0]["chg_pct"] == 2.0


def test_stock_news_strips_source_and_dedupes(monkeypatch):
    df = pd.DataFrame([
        {"date": "2026-10-21 01:02:00", "title": "台積電漲25元 - ETtoday財經雲", "source": "ETtoday財經雲", "link": "https://a"},
        {"date": "2026-10-21 01:03:00", "title": "台積電漲25元 - ETtoday財經雲", "source": "ETtoday財經雲", "link": "https://b"},
        {"date": "2026-10-23 01:00:00", "title": "之後的新聞", "source": "UDN", "link": "https://c"},
    ])
    monkeypatch.setattr(data_fetcher, "_fetch", lambda *a, **k: df)
    news = data_fetcher.get_stock_news("2330", "2026-10-21")
    assert [n["title"] for n in news] == ["台積電漲25元"] and news[0]["source"] == "ETtoday財經雲"


def test_cnyes_items_merge_categories_and_skip_analyst_estimates():
    items = [
        ("台股", {"newsId": 1, "title": "台積電(2330)法說", "publishAt": 1_000_000, "stock": ["2330"]}),
        ("盤勢", {"newsId": 1, "title": "台積電(2330)法說", "publishAt": 1_000_000}),
        ("台股", {"newsId": 2, "title": "鉅亨速報 - Factset 最新調查：台積電目標價", "publishAt": 1_000_050}),
        ("國際", {"newsId": 3, "title": "費半大漲", "publishAt": 1_000_090}),
    ]
    news = data_fetcher.parse_cnyes_items(items, hours=24, now_ts=1_000_100)
    assert [n["title"] for n in news] == ["費半大漲", "台積電(2330)法說"]
    assert news[0]["id"] == "N1" and news[0]["category"] == "國際" and news[1]["stocks"] == ["2330"]


def test_related_news_ids():
    news = [{"id": "N1", "title": "矽力營收創新高", "stocks": []},
            {"id": "N2", "title": "盤後焦點", "stocks": ["6415"]},
            {"id": "N3", "title": "全新推出新品", "stocks": []},
            {"id": "N4", "title": "全新(2455)接單", "stocks": []},
            {"id": "N5", "title": "加權指數 64155 點", "stocks": []}]
    assert data_fetcher.related_news_ids(news, "6415", "矽力*-KY") == ["N1", "N2"]
    assert data_fetcher.related_news_ids(news, "2455", "全新") == ["N4"]    # 名稱是常見詞，只認代號
