"""整條每日流程（不連網路）：交易日、休市、去重、AI 檢查、報告核對。"""

import json

import pandas as pd
import pytest

import history
import reporter
import run_daily
from config import STOCK_UNIVERSE
from tests.conftest import make_price_df, make_inst_daily

DATE = "2026-10-16"
GOOD_IDS = ["2330", "3034", "6415"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(run_daily, "today_tw", lambda: DATE)
    monkeypatch.setattr(reporter, "send_to_telegram", lambda msgs: sent.append(msgs) or ["SUCCESS"] * len(msgs))

    f = run_daily.fetch
    taiex_date = {"value": DATE}

    def taiex(end):
        return pd.DataFrame({"date": pd.to_datetime(["2026-10-15", taiex_date["value"]]),
                             "close": [47501.0, 47111.26], "Trading_money": [5.1e11, 5.123e11]})
    monkeypatch.setattr(f, "get_taiex_price", taiex)
    monkeypatch.setattr(f, "get_stock_info", lambda: {"2330": "台積電", "3034": "聯詠", "6415": "矽力*-KY"})
    monkeypatch.setattr(f, "get_top_volume_stocks", lambda d, n: ([("2409", "友達", 341960)], None))

    def price(sid, end, days=65):
        if sid in GOOD_IDS:
            return make_price_df(end_date=end, start=100 + GOOD_IDS.index(sid) * 50)
        if sid == "2409":
            return make_price_df(end_date=end, drift=-0.004)
        return pd.DataFrame()
    monkeypatch.setattr(f, "get_stock_price", price)
    monkeypatch.setattr(f, "get_institutional_investors", lambda sid, end, days=10: (make_inst_daily(end_date=end), []))
    monkeypatch.setattr(f, "get_tw_stock_news",
                        lambda: ([{"id": "N1", "source_id": "1", "title": "台積電法說會", "summary": "", "time": "",
                                   "url": "https://news.cnyes.com/news/id/1"}], None))
    monkeypatch.setattr(f, "get_twse_closes", lambda: ({}, "證交所收盤價抓不到（測試）"))
    return {"sent": sent, "taiex_date": taiex_date}


def run(*args):
    return run_daily.main([*args, "--no-git"])


def test_full_day_without_ai(env):
    assert run("check-date") == run_daily.EXIT_OK
    assert run("collect") == run_daily.EXIT_OK
    payload = history.load_json(DATE, "picks.json")
    ids = [p["stock_id"] for p in payload["picks"]]
    assert ids and set(ids) <= set(GOOD_IDS)
    assert len(payload["quality"]["missing_price"]) == len(STOCK_UNIVERSE) - len(GOOD_IDS)
    ai_input = history.load_json(DATE, "ai_input.json")
    assert {c["stock_id"] for c in ai_input["candidates"]} == set(ids)

    assert run("finalize") == run_daily.EXIT_OK
    assert len(env["sent"]) == 1
    text = "\n".join(env["sent"][0])
    assert "AI 文字未提供" in text and "證交所收盤價抓不到" in text
    assert history.is_sent(DATE)

    # 同一天再跑：不重發（Actions 延遲 + 手動觸發造成的重複報告）
    assert run("check-date") == run_daily.EXIT_ALREADY_SENT
    assert run("finalize") == run_daily.EXIT_ALREADY_SENT
    assert len(env["sent"]) == 1


def test_ai_output_is_validated_and_bad_parts_dropped(env):
    run("collect")
    ai_input = history.load_json(DATE, "ai_input.json")
    stocks = [{"stock_id": c["stock_id"], "bull_points": [f"量比 {c['vol_vs_20d']} 倍"],
               "bear_points": ["RSI 偏高"], "verdict": "謹慎做多", "confidence": 6,
               "summary": f"停損 {c['program_levels']['stop_loss']}。", "news_ids": []}
              for c in ai_input["candidates"]]
    stocks[0]["summary"] = "目標上看 999 元。"   # 自己編的價位
    history.save_json(DATE, "ai_output.json", {"date": DATE, "highlights": [], "stocks": stocks})
    assert run("validate-ai") == run_daily.EXIT_AI_INVALID

    assert run("finalize") == run_daily.EXIT_OK
    text = "\n".join(env["sent"][0])
    assert "999" not in text
    assert "未通過程式檢查" in text
    assert text.count("判決：") == len(stocks) - 1


def test_holiday_sends_one_line_once(env):
    env["taiex_date"]["value"] = "2026-10-15"   # 加權指數最新是前一天 → 今天沒有交易資料
    assert run("check-date") == run_daily.EXIT_NOT_TRADING
    assert len(env["sent"]) == 1 and "不發選股報告" in env["sent"][0][0]
    assert run("check-date") == run_daily.EXIT_NOT_TRADING
    assert len(env["sent"]) == 1                 # 不會重發，也不會把前一天的報告再發一次
    assert run("collect") == run_daily.EXIT_NOT_TRADING


def test_weekend_is_silent(env, monkeypatch):
    monkeypatch.setattr(run_daily, "today_tw", lambda: "2026-10-17")
    env["taiex_date"]["value"] = DATE
    assert run("check-date") == run_daily.EXIT_NOT_TRADING
    assert env["sent"] == []


def test_send_failure_does_not_mark_sent(env, monkeypatch):
    monkeypatch.setattr(reporter, "send_to_telegram", lambda msgs: ["FAIL:bad"] * len(msgs))
    run("collect")
    assert run("finalize") == run_daily.EXIT_SEND_FAILED
    assert not history.is_sent(DATE)


def test_dry_run_does_not_send(env):
    run("collect")
    assert run("finalize", "--dry-run") == run_daily.EXIT_OK
    assert env["sent"] == []
    assert (history.day_dir(DATE) / "report.txt").exists()


def test_all_command(env):
    assert run("all", "--ai", "none") == run_daily.EXIT_OK
    assert len(env["sent"]) == 1
