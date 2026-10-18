"""整條每日流程（不連網路）：交易日、休市、去重、AI 檢查、報告核對。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import history
import reporter
import run_daily
from tests.conftest import make_price_df, make_inst_daily

DATE = "2026-10-16"
GOOD_IDS = ["2330", "3034", "6415"]
NO_DATA_IDS = ["3014"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(run_daily, "today_tw", lambda: DATE)
    monkeypatch.setattr(run_daily, "now_tw", lambda: datetime(2026, 10, 16, 18, 0, tzinfo=ZoneInfo("Asia/Taipei")))
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
        if sid in NO_DATA_IDS:
            return pd.DataFrame()
        return make_price_df(end_date=end, drift=-0.004)   # 其他股票下跌趨勢，篩選不會過
    monkeypatch.setattr(f, "get_stock_price", price)
    inst_end = {"value": None}   # None：和查詢日期同一天；設成日期：模擬法人資料還沒更新
    monkeypatch.setattr(f, "get_institutional_investors",
                        lambda sid, end, days=10: (make_inst_daily(end_date=inst_end["value"] or end), []))
    monkeypatch.setattr(f, "get_tw_stock_news",
                        lambda: ([{"id": "N1", "source_id": "1", "title": "台積電法說會", "summary": "", "time": "",
                                   "url": "https://news.cnyes.com/news/id/1"}], None))
    monkeypatch.setattr(f, "get_twse_closes", lambda: ({}, None, "證交所收盤價抓不到（測試）"))
    return {"sent": sent, "taiex_date": taiex_date, "inst_end": inst_end}


def run(*args):
    return run_daily.main([*args, "--no-git"])


def test_full_day_without_ai(env):
    assert run("check-date") == run_daily.EXIT_OK
    assert run("collect") == run_daily.EXIT_OK
    payload = history.load_json(DATE, "picks.json")
    ids = [p["stock_id"] for p in payload["picks"]]
    assert ids and set(ids) <= set(GOOD_IDS)
    assert payload["quality"]["missing_price"] == NO_DATA_IDS
    assert payload["incomplete"] == []
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


def test_performance_tracks_previous_days_picks(env, monkeypatch):
    monkeypatch.setattr(run_daily, "today_tw", lambda: "2026-10-15")
    env["taiex_date"]["value"] = "2026-10-15"
    assert run("all") == run_daily.EXIT_OK
    first = history.load_json("2026-10-15", "picks.json")["picks"]

    monkeypatch.setattr(run_daily, "today_tw", lambda: DATE)
    monkeypatch.setattr(run_daily, "now_tw", lambda: datetime(2026, 10, 16, 18, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    env["taiex_date"]["value"] = DATE
    assert run("all") == run_daily.EXIT_OK
    perf = history.load_json(DATE, "picks.json")["performance"]
    assert perf["total"] == len(first) > 0
    assert "推薦追蹤" in "\n".join(env["sent"][-1]) and "尚無歷史紀錄" not in "\n".join(env["sent"][-1])


def test_not_ready_before_close_does_not_send_closed_notice(env, monkeypatch):
    monkeypatch.setattr(run_daily, "now_tw", lambda: datetime(2026, 10, 16, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    env["taiex_date"]["value"] = "2026-10-15"
    assert run("check-date") == run_daily.EXIT_NOT_READY
    assert env["sent"] == []


def test_past_date_without_data_is_a_holiday(env, monkeypatch):
    # 備援延遲到半夜、用 --date 指定前一天：那天沒資料就是休市
    monkeypatch.setattr(run_daily.fetch, "get_taiex_price",
                        lambda end: pd.DataFrame({"date": pd.to_datetime(["2026-10-13", "2026-10-14"]),
                                                  "close": [47000.0, 47100.0]}))
    assert run("check-date", "--date", "2026-10-15") == run_daily.EXIT_NOT_TRADING
    assert "2026-10-15" in env["sent"][0][0]


def test_malformed_ai_output_still_sends(env):
    run("collect")
    (history.day_dir(DATE) / "ai_output.json").write_text('{"date": "2026-10-16", "stocks": [,]}', encoding="utf-8")
    assert run("validate-ai") == run_daily.EXIT_AI_INVALID
    assert run("finalize") == run_daily.EXIT_OK
    assert "AI 文字格式錯誤" in "\n".join(env["sent"][0])


def test_code_fenced_ai_output_is_accepted(env):
    run("collect")
    body = '{"date": "2026-10-16", "highlights": [], "stocks": []}'
    (history.day_dir(DATE) / "ai_output.json").write_text(f"```json\n{body}\n```", encoding="utf-8")
    out, err = run_daily.load_ai_output(DATE)
    assert err is None and out["date"] == DATE


def test_incomplete_report_is_resent_once_data_is_complete(env):
    env["inst_end"]["value"] = "2026-10-15"          # 17:53 法人資料還沒更新
    assert run("collect") == run_daily.EXIT_OK
    assert history.load_json(DATE, "picks.json")["incomplete"]
    assert run("finalize") == run_daily.EXIT_OK
    assert history.sent_info(DATE)["complete"] is False
    assert not history.is_sent(DATE)

    # 備援再跑一次，資料還是不完整：不重發
    run("collect")
    assert run("finalize") == run_daily.EXIT_ALREADY_SENT
    assert len(env["sent"]) == 1

    # 資料齊了：補發完整版，之後就不再發
    env["inst_end"]["value"] = None
    run("collect")
    assert run("finalize") == run_daily.EXIT_OK
    assert len(env["sent"]) == 2 and "完整版" in env["sent"][1][0]
    assert history.is_sent(DATE)
    assert run("check-date") == run_daily.EXIT_ALREADY_SENT


def test_push_failure_has_its_own_exit_code(env, monkeypatch):
    monkeypatch.setattr(run_daily, "_push", lambda args, msg: False)
    run("collect")
    assert run("finalize") == run_daily.EXIT_PUSH_FAILED
    assert len(env["sent"]) == 1


def test_git_pull_failure_stops_before_sending(env, monkeypatch):
    monkeypatch.setattr(history, "sync_pull", lambda: (False, "連不上 origin"))
    assert run_daily.main(["check-date"]) == run_daily.EXIT_ERROR
    assert env["sent"] == []
