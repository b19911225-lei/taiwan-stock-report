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
    assert clean == {"highlights": [], "stocks": {}}
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
