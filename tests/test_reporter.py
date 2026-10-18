import copy

import reporter
from reporter import build_messages, split_message, fmt_num
from verify import verify_messages, verify_closes, names_match

PICK = {
    "stock_id": "6415", "name": "矽力*-KY", "themes": ["散熱電源"], "close": 489.0, "today_chg": 4.82,
    "score": 65, "signals": ["M1", "M2", "M5", "M6"], "vol_vs_20d": 2.07, "rsi14": 61.2,
    "kd_cross": True, "foreign_buy_days": 0, "inst_available": False,
    "targets": {"entry_low": 479.22, "entry_high": 498.78, "target1": 540.0, "target_basis": "60日高點",
                "stop_loss": 470.0, "rr_ratio": 2.68},
}
PAYLOAD = {
    "date": "2026-10-16",
    "market": {"close": 47111.26, "chg": -390.0, "chg_pct": -0.82, "money_100m": 5123.0},
    "themes": [{"theme": "T5_散熱電源", "name": "散熱電源", "avg_chg_pct": 1.23}],
    "picks": [PICK],
    "debate_ids": ["6415"],
    "stats": {"universe": 53, "fetched": 50, "prefilter_passed": 9, "scored_passed": 4, "rr_ok": 1},
    "quality": {"missing_price": ["6247"], "missing_inst": ["6415"], "notes": ["新聞抓不到（Timeout）"], "checks": []},
    "performance": {"lookback_days": 20, "total": 0},
    "news": [{"id": "N1", "title": "t", "summary": "", "time": "", "url": "https://news.cnyes.com/news/id/1"}],
}


def test_report_contains_program_numbers_and_quality_section():
    msgs = build_messages(PAYLOAD)
    text = "\n".join(msgs)
    assert verify_messages(msgs, PAYLOAD["picks"]) == []
    assert "今日缺少：價格 1 檔（6247）；法人 1 檔（6415" in text
    assert "新聞抓不到" in text
    assert "47,111.26（▼0.82%）" in text
    assert "尚無歷史紀錄" in text


def test_ai_text_is_escaped_and_shown():
    ai = {"highlights": [{"text": "A<B & C", "news_ids": ["N1"]}],
          "stocks": {"6415": {"bull_points": ["<量增>"], "bear_points": ["RSI 偏高"], "verdict": "謹慎做多",
                              "confidence": 6, "summary": "ok", "news_ids": []}}}
    text = "\n".join(build_messages(PAYLOAD, ai))
    assert "A&lt;B &amp; C" in text and "&lt;量增&gt;" in text
    assert "🟡✅ 謹慎做多" in text
    assert '<a href="https://news.cnyes.com/news/id/1">[N1]</a>' in text


def test_missing_ai_falls_back_to_program_data():
    text = "\n".join(build_messages(PAYLOAD, None, "AI 文字未提供，以下只列程式數據"))
    assert "AI 文字未提供" in text and "外資連買：0日（法人資料缺）" in text


def test_verify_messages_catches_wrong_numbers():
    msgs = build_messages(PAYLOAD)
    broken = [m.replace("540", "54O") for m in msgs]
    issues = verify_messages(broken, PAYLOAD["picks"])
    assert any("目標" in i for i in issues)


def test_verify_closes():
    r = verify_closes([PICK], {"6415": 489.0})
    assert r["issues"] == [] and "1 檔收盤價與證交所一致" in r["ok"]
    r = verify_closes([PICK], {"6415": 480.0})
    assert r["issues"] and r["ok"] is None
    r = verify_closes([PICK], {})
    assert r["issues"] == [] and r["ok"] is None and "上櫃" in r["skipped"]


def test_split_message_keeps_everything():
    text = "\n".join(f"第 {i} 行 " + "字" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


def test_fmt_num():
    assert fmt_num(2420.0) == "2,420"
    assert fmt_num(16.15) == "16.15"
    assert fmt_num(479.2) == "479.2"


def test_names_match():
    assert names_match("矽力-KY", "矽力*-KY")
    assert names_match("超眾", "尼得科超眾")
    assert not names_match("欣興", "欣陸")


def test_send_without_token_reports_error(monkeypatch):
    monkeypatch.setattr(reporter, "TELEGRAM_BOT_TOKEN", "")
    assert reporter.send_to_telegram(["x"])[0].startswith("ERROR")


def test_send_does_not_leak_token_on_exception(monkeypatch, tmp_path):
    # 假 token 刻意寫得不像真的，避免被 secret scanner 當成外洩（見 docs/PITFALLS.md）
    monkeypatch.setattr(reporter, "TELEGRAM_BOT_TOKEN", "fake-bot-token-for-tests-only")
    monkeypatch.setattr(reporter, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(reporter.time, "sleep", lambda s: None)
    monkeypatch.setattr(reporter, "LOG_DIR", tmp_path)

    def boom(url, **kw):
        raise ConnectionError(f"failed to reach {url}")
    monkeypatch.setattr(reporter.requests, "post", boom)
    status = reporter.send_to_telegram(["x"])[0]
    assert "fake-bot-token" not in status
