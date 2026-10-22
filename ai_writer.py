# ai_writer.py - API 備援模式：用 Claude API 寫 ai_output.json
#
# 平常由 Routine 排程的 Claude session 自己讀 ai_input.json、照 docs/AI_WRITING_RULES.md 寫；
# 在 GitHub Actions 手動執行（備援）時才用這支呼叫 API。兩種寫法產生同一種 JSON，
# 都會經過 ai_validation.py 檢查。
#
# 直接用 requests 呼叫（之前改用 SDK 時遇過連線問題，見 docs/PITFALLS.md）。

import json
import logging
import time
from pathlib import Path

import requests

from ai_validation import AI_OUTPUT_SCHEMA
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

API_URL = "https://api.anthropic.com/v1/messages"
RULES_PATH = Path(__file__).parent / "docs" / "AI_WRITING_RULES.md"


def build_prompt(ai_input):
    rules = RULES_PATH.read_text(encoding="utf-8")
    system = (
        "你是財經台盤後節目的主播兼撰稿人，負責台股盤後選股日報的文字。"
        "所有數字都已經由程式算好，你只負責把它們和新聞串成好讀的播報，"
        "必須嚴格遵守下面的撰寫規則。\n\n" + rules
    )
    user = (
        "以下是今天的 ai_input.json。請依規則輸出 ai_output.json 的內容。\n\n"
        + json.dumps(ai_input, ensure_ascii=False, indent=1)
    )
    return system, user


def write_with_api(ai_input, retries=3):
    """回傳 (ai_output 或 None, 錯誤訊息或 None, usage)。"""
    if not ANTHROPIC_API_KEY:
        return None, "未設定 ANTHROPIC_API_KEY", None
    system, user = build_prompt(ai_input)
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "server-side-fallback-2026-07-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": AI_OUTPUT_SCHEMA},
        },
        # 安全分類器拒答時，由伺服器自動改用合適的模型重跑
        "fallbacks": "default",
    }

    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=300)
            if resp.status_code in (429, 500, 502, 503, 529):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                # 400/401/404 重試也不會好，直接回報
                return None, f"HTTP {resp.status_code}: {resp.text[:300]}", None
            data = resp.json()
            usage = data.get("usage")
            if data.get("stop_reason") == "refusal":
                return None, f"模型拒絕回答：{data.get('stop_details')}", usage
            if data.get("stop_reason") == "max_tokens":
                return None, "輸出超過 max_tokens 被截斷", usage
            texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            if not texts:
                return None, "回應沒有文字內容", usage
            return json.loads(texts[-1]), None, usage
        except Exception as e:
            last_error = e
            logging.warning(f"Claude API 第 {attempt + 1}/{retries} 次失敗：{type(e).__name__}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    return None, f"Claude API 呼叫失敗：{type(last_error).__name__}: {last_error}", None
