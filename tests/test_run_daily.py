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
    twse_daily = {"value": ({}, {}, "證交所全部收盤行情抓不到（測試）")}
    monkeypatch.setattr(f, "get_twse_daily", lambda d: twse_daily["value"])
    monkeypatch.setattr(f, "get_stock_news", lambda sid, d, limit=4: [])
    monkeypatch.setattr(f, "get_market_institutional",
                        lambda d: ({"foreign": 22.39, "sitc": 12.8, "dealer": -135.71}, None))
    monkeypatch.setattr(f, "get_us_markets",
                        lambda d: ([{"id": "^SOX", "name": "費城半導體", "date": "2026-10-15",
                                     "close": 11505.56, "chg_pct": 1.2}], None))
    return {"sent": sent, "taiex_date": taiex_date, "inst_end": inst_end, "twse_daily": twse_daily}


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


def test_defer_incomplete_waits_for_a_later_run(env, monkeypatch):
    calls = []
    import ai_writer
    monkeypatch.setattr(ai_writer, "write_with_api", lambda ai_input: calls.append(1) or (None, "skip", None))
    env["inst_end"]["value"] = "2026-10-15"          # 法人資料還沒更新
    assert run("all", "--ai", "api", "--defer-incomplete") == run_daily.EXIT_DEFERRED
    assert env["sent"] == [] and calls == []          # 沒發，也沒有花 API 費用
    assert history.sent_info(DATE) is None

    # 最後一次執行（不加 --defer-incomplete）：照發並標示不完整
    assert run("all") == run_daily.EXIT_OK
    assert len(env["sent"]) == 1 and history.sent_info(DATE)["complete"] is False

    # 之後資料齊了：補發完整版
    env["inst_end"]["value"] = None
    assert run("all", "--defer-incomplete") == run_daily.EXIT_OK
    assert len(env["sent"]) == 2 and "完整版" in env["sent"][1][0]


def test_defer_does_not_block_complete_data(env):
    assert run("all", "--defer-incomplete") == run_daily.EXIT_OK
    assert len(env["sent"]) == 1


def test_select_picks_fills_with_lower_tiers_and_labels_them():
    def r(tier, score, rr):
        return {"tier": tier, "score": score, "targets": {"rr_ratio": rr}}
    results = {"A1": r("A", 60, 3.0), "B1": r("B", 70, 1.8), "B2": r("B", 50, 0.8),
               "C1": r("C", 40, 2.5), "X": {"passed": False}}
    assert run_daily.select_picks(results) == ["A1", "B1", "C1"]   # B2 的 RR 不到 MIN_FILL_RR
    many = {f"A{i}": r("A", i, 3.0) for i in range(8)}
    assert run_daily.select_picks({**many, **results}) == ["A1", "A7", "A6", "A5", "A4"]
    assert run_daily.select_picks({}) == []


def test_watchlist_stock_is_scored_even_when_filtered_out(env):
    # 4577 在測試資料裡是下跌趨勢：不會入選，但要有分數和原因
    assert run("collect") == run_daily.EXIT_OK
    payload = history.load_json(DATE, "picks.json")
    w = payload["watchlist"][0]
    assert w["stock_id"] == "4577" and w["pick_rank"] is None
    assert w["score"] is not None and w["fail_reasons"]
    ai_input = history.load_json(DATE, "ai_input.json")
    assert ai_input["watchlist"][0]["not_selected_reasons"] == w["fail_reasons"]
    assert run("finalize") == run_daily.EXIT_OK
    text = "\n".join(env["sent"][0])
    assert "⭐ <b>4577" in text and "未入選：" in text


def test_market_scan_and_breadth(env):
    quotes = {"1802": {"name": "台玻", "close": 67.4, "change": 1.4, "chg_pct": 2.12, "volume_lots": 140000,
                       "value_100m": 95.0},
              "2330": {"name": "台積電", "close": 2420.0, "change": 25.0, "chg_pct": 1.04, "volume_lots": 30000,
                       "value_100m": 726.0}}
    env["twse_daily"]["value"] = (quotes, {"up": 600, "down": 380}, None)
    assert run("collect") == run_daily.EXIT_OK
    payload = history.load_json(DATE, "picks.json")
    assert payload["stats"]["scan_added"] == 1                     # 2330 已經在主題股裡
    assert payload["market"]["breadth"] == {"up": 600, "down": 380}
    assert payload["market"]["top_turnover"][0]["stock_id"] == "2330"
    assert payload["market"]["institutional_100m"]["foreign"] == 22.39
    assert payload["us_markets"][0]["name"] == "費城半導體"


def test_zero_signal_stock_is_not_a_fill_candidate():
    results = {"A1": {"tier": "A", "score": 60, "targets": {"rr_ratio": 3.0}},
               "Z": {"tier": None, "score": 21, "targets": {"rr_ratio": 5.0}, "momentum_count": 0}}
    assert run_daily.select_picks(results) == ["A1"]
