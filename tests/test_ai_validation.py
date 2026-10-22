import copy

import pytest

from ai_validation import validate, check_text, collect_allowed_numbers

AI_INPUT = {
    "date": "2026-10-16",
    "market": {"close": 47111.26, "chg_pct": -0.82},
    "themes": [{"name": "散熱電源", "avg_chg_pct": 1.23}],
    "candidates": [
        {"stock_id": "6415", "name": "矽力-KY", "close": 489.0, "today_chg_pct": 4.82,
         "vol_vs_20d": 2.07, "rsi14": 61.2,
         "program_levels": {"target1": 540.0, "stop_loss": 470.0, "rr_ratio": 2.3}},
        {"stock_id": "3034", "name": "聯詠", "close": 577.0, "today_chg_pct": 2.12,
         "vol_vs_20d": 1.6, "rsi14": 64.0,
         "program_levels": {"target1": 640.0, "stop_loss": 559.0, "rr_ratio": 3.5}},
    ],
    "news": [{"id": "N1", "title": "散熱族群營收年增 35%", "summary": "", "time": "10/16 14:00"}],
}

GOOD = {
    "date": "2026-10-16",
    "highlights": [{"text": "加權指數 -0.82%，但散熱電源主題 +1.23%；N1 提到散熱族群營收年增 35%。", "news_ids": ["N1"]}],
    "stocks": [
        {"stock_id": "6415", "bull_points": ["今日量是 20 日均量 2.1 倍（M1）", "收盤 489 上漲 4.8%"],
         "bear_points": ["RSI 61.2 偏高"], "verdict": "謹慎做多", "confidence": 6,
         "summary": "量價齊揚，但停損 470 要守。", "news_ids": ["N1"]},
        {"stock_id": "3034", "bull_points": ["連 3 日站上 MA20"], "bear_points": ["RR 3.5 但主題偏弱"],
         "verdict": "中性觀察", "confidence": 5, "summary": "觀察是否續強。", "news_ids": []},
    ],
}


def test_good_output_passes():
    clean, issues = validate(GOOD, AI_INPUT)
    assert issues == []
    assert set(clean["stocks"]) == {"6415", "3034"}
    assert len(clean["highlights"]) == 1


def test_invented_percentage_is_rejected():
    bad = copy.deepcopy(GOOD)
    bad["stocks"][0]["summary"] = "預估還有 12% 上漲空間。"
    clean, issues = validate(bad, AI_INPUT)
    assert "6415" not in clean["stocks"]
    assert "3034" in clean["stocks"]          # 其他合格的段落照常保留
    assert any("12%" in i for i in issues)


def test_small_unitless_counts_are_allowed_but_not_with_units():
    allowed = collect_allowed_numbers(AI_INPUT)
    assert check_text("外資連買 3 日、站上 MA20", allowed, 100) == []
    assert check_text("離停損約 7%", allowed, 100) != []


def test_rounding_to_fewer_decimals_is_allowed():
    allowed = collect_allowed_numbers(AI_INPUT)
    assert check_text("加權指數 47,111 點，跌 0.8%", allowed, 100) == []
    assert check_text("加權指數 47,112 點", allowed, 100) != []


@pytest.mark.parametrize("phrase", ["保證", "穩賺", "一定會漲"])
def test_banned_phrases(phrase):
    allowed = collect_allowed_numbers(AI_INPUT)
    assert check_text(f"這檔{phrase}", allowed, 100)


def test_wrong_date_rejects_everything():
    bad = copy.deepcopy(GOOD)
    bad["date"] = "2026-10-15"
    clean, issues = validate(bad, AI_INPUT)
    assert not clean["highlights"] and not clean["stocks"] and clean["headline"] is None
    assert issues


def test_unknown_news_and_bad_verdict_and_missing_stock():
    bad = copy.deepcopy(GOOD)
    bad["highlights"][0]["news_ids"] = ["N9"]
    bad["stocks"][0]["verdict"] = "大力做多"
    bad["stocks"].pop(1)
    clean, issues = validate(bad, AI_INPUT)
    assert clean["highlights"] == []
    assert clean["stocks"] == {}
    text = "\n".join(issues)
    assert "N9" in text and "大力做多" in text and "3034：AI 沒有寫這檔" in text


def test_confidence_must_be_int_1_to_10():
    bad = copy.deepcopy(GOOD)
    bad["stocks"][0]["confidence"] = 11
    _, issues = validate(bad, AI_INPUT)
    assert any("信心" in i for i in issues)


def test_non_candidate_stock_is_ignored():
    bad = copy.deepcopy(GOOD)
    bad["stocks"].append({**GOOD["stocks"][1], "stock_id": "2330"})
    clean, issues = validate(bad, AI_INPUT)
    assert "2330" not in clean["stocks"]
    assert any("2330" in i for i in issues)


def test_half_up_rounding_is_accepted():
    allowed = collect_allowed_numbers({"a": 61.25, "b": 64.5, "c": 2.5})
    assert check_text("RSI 61.3、64.5 寫成 65%、量比 3 倍", allowed, 100) == []


def test_direction_must_match_sign():
    allowed = collect_allowed_numbers({"chg_pct": -0.82, "today_chg_pct": 4.82})
    assert check_text("大盤下跌 0.82%", allowed, 100) == []
    assert check_text("大盤 -0.82%", allowed, 100) == []
    assert check_text("大盤上漲 0.82%", allowed, 100) != []
    assert check_text("大盤 +0.82%", allowed, 100) != []
    assert check_text("個股上漲 4.8%", allowed, 100) == []


def test_dates_are_not_negative_numbers():
    allowed = collect_allowed_numbers({"date": "2026-10-16"})
    assert check_text("資料日期 2026-10-16", allowed, 100) == []


def test_cjk_numerals_with_units_are_rejected():
    allowed = collect_allowed_numbers(AI_INPUT)
    assert check_text("上看六百元", allowed, 100) != []
    assert check_text("還有兩成空間", allowed, 100) != []
    assert check_text("成交金額 5,123 億元", collect_allowed_numbers({"m": 5123.0}), 100) == []


def test_wrong_types_do_not_crash():
    bad = copy.deepcopy(GOOD)
    bad["stocks"] = 5
    _, issues = validate(bad, AI_INPUT)
    assert any("stocks 不是陣列" in i for i in issues)
    bad = copy.deepcopy(GOOD)
    bad["highlights"][0]["news_ids"] = [{"id": "N1"}]
    bad["stocks"][0]["news_ids"] = [["N1"]]
    bad["stocks"][1]["bull_points"] = [1, 2]
    clean, issues = validate(bad, AI_INPUT)
    assert clean["highlights"] == [] and clean["stocks"] == {}


BROADCAST_INPUT = {**AI_INPUT, "watchlist": [{"stock_id": "4577", "name": "達航科技", "close": 115.5, "score": 45,
                                               "not_selected_reasons": ["動能訊號只有 1 個（要 2 個）"]}]}
BROADCAST = {
    **GOOD,
    "headline": "台股收 47,111.26 點，散熱電源逆勢 +1.23%",
    "market_commentary": "加權指數下跌 0.82%，散熱電源主題平均 +1.23% 逆勢走強。",
    "watch_tomorrow": ["矽力-KY 能否守住程式停損 470", "散熱電源是否延續強勢"],
    "watchlist": [{"stock_id": "4577", "comment": "達航科技收 115.5、評分 45，動能訊號只有 1 個，暫不入選。",
                   "news_ids": []}],
}


def test_broadcast_fields_pass():
    clean, issues = validate(BROADCAST, BROADCAST_INPUT)
    assert issues == []
    assert clean["headline"].startswith("台股收") and clean["market_commentary"]
    assert len(clean["watch_tomorrow"]) == 2
    assert clean["watchlist"]["4577"]["comment"].startswith("達航科技")


def test_broadcast_fields_with_made_up_numbers_are_dropped():
    bad = copy.deepcopy(BROADCAST)
    bad["headline"] = "台股明天上看 48,000 點"
    bad["market_commentary"] = "保證明天上漲 2%。"
    bad["watch_tomorrow"].append("目標價 600 元")
    bad["watchlist"][0]["comment"] = "達航科技目標 150 元。"
    clean, issues = validate(bad, BROADCAST_INPUT)
    assert clean["headline"] is None and clean["market_commentary"] is None
    assert clean["watch_tomorrow"] == BROADCAST["watch_tomorrow"]
    assert clean["watchlist"] == {}
    assert any("標題" in i for i in issues) and any("自選股 4577" in i for i in issues)


def test_missing_watchlist_comment_is_reported():
    out = copy.deepcopy(BROADCAST)
    out["watchlist"] = []
    _, issues = validate(out, BROADCAST_INPUT)
    assert "自選股 4577：AI 沒有寫評論" in issues


def test_lower_tier_cannot_be_strong_buy():
    ai_input = copy.deepcopy(AI_INPUT)
    ai_input["candidates"][1]["tier"] = "次選"
    out = copy.deepcopy(GOOD)
    out["stocks"][1]["verdict"] = "強力做多"
    clean, issues = validate(out, ai_input)
    assert "3034" not in clean["stocks"] and any("次選不能給「強力做多」" in i for i in issues)


def test_stock_counts_after_up_down_words_are_not_signed():
    # 「下跌 380 家」是 380 家股票下跌，不是 -380（10/21 的盤勢播報因此被誤判拿掉）
    allowed = collect_allowed_numbers({"market": {"breadth": {"up": 600, "down": 380}, "chg": -529.15}})
    assert check_text("上漲 600 家、下跌 380 家", allowed, 100) == []
    assert check_text("下跌 529.15 點", allowed, 100) == []
    assert check_text("上漲 529.15 點", allowed, 100) != []       # 點數還是要檢查方向
