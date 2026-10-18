# ai_validation.py - 檢查 AI 寫的文字（規則見 docs/AI_WRITING_RULES.md）
#
# 文章的設計：數字由程式算好，AI 只照抄和寫文字。這裡用程式把關：
#   - AI 文字裡出現的每個數字，都必須在 ai_input.json 裡找得到（允許四捨五入到較少位數）
#   - 不能有保證獲利之類的用語
#   - 判決只能是四選一，信心分數 1~10，引用的新聞編號必須存在
# 不合格的段落直接拿掉，不會送出；其他合格的段落照常使用。

import re

VERDICTS = ["強力做多", "謹慎做多", "中性觀察", "規避"]
BANNED_PHRASES = ["保證", "穩賺", "穩贏", "必漲", "必跌", "一定會漲", "一定會跌", "明牌", "內線", "無風險", "零風險"]

# 計數、指標週期這類不帶單位的小數字不需要出現在資料裡（例如「連買 3 日」「MA20」）；
# 帶單位（%、倍、元、張、億、萬、成）的數字一律要能在資料裡找到，避免 AI 自己算出「約 4%」
FREE_NUMBERS = set(range(0, 21)) | {60}

MAX_POINTS = 3
MAX_POINT_LEN = 80
MAX_SUMMARY_LEN = 150
MAX_HIGHLIGHTS = 3
MAX_HIGHLIGHT_LEN = 150

UNITS = "%|％|倍|元|張|億|萬|成"
# 正負號只在前面不是數字或英文字母時才算（避免把「2026-10-16」的「-10」當成負數）
NUMBER_RE = re.compile(rf"(?:(?<![0-9A-Za-z])([+\-−＋－▲▼]))?(\d[\d,]*(?:\.\d+)?)\s*({UNITS})?")
NEGATIVE_SIGNS = set("-−－▼")
# 數字前面緊接著「漲／跌」時，方向要和資料的正負號一致（例如資料是 -0.82，不能寫「上漲 0.82%」）
DIRECTION_RE = re.compile(r"(漲|升|跌|降)\s*$")
# 中文數字 + 單位（例如「六百元」「兩成」）沒辦法核對，一律不允許
# （開頭一定要是數字字，「5,123 億元」的「億元」不算）
CJK_NUMBER_RE = re.compile(rf"[零〇一二兩三四五六七八九十][零〇一二兩三四五六七八九十百千萬億]*\s*(?:{UNITS})")

# 給 Claude API 結構化輸出（output_config.format）用的 schema；長度、範圍由下面的程式檢查
AI_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "news_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "news_ids"],
                "additionalProperties": False,
            },
        },
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "stock_id": {"type": "string"},
                    "bull_points": {"type": "array", "items": {"type": "string"}},
                    "bear_points": {"type": "array", "items": {"type": "string"}},
                    "verdict": {"type": "string", "enum": VERDICTS},
                    "confidence": {"type": "integer"},
                    "summary": {"type": "string"},
                    "news_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["stock_id", "bull_points", "bear_points", "verdict",
                             "confidence", "summary", "news_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["date", "highlights", "stocks"],
    "additionalProperties": False,
}


def _numbers_in_text(text):
    """
    [{"value": 數值（含正負號）, "decimals": 小數位數, "token": 原字串, "unit": 是否帶單位,
      "signed": 是否有明確方向（正負號或緊接在漲／跌後面）}, ...]
    """
    text = str(text)
    out = []
    for m in NUMBER_RE.finditer(text):
        sign, raw, unit = m.group(1), m.group(2), m.group(3) or ""
        tok = raw.replace(",", "")
        try:
            value = float(tok)
        except ValueError:
            continue
        signed = False
        if sign:
            signed = True
            if sign in NEGATIVE_SIGNS:
                value = -value
        else:
            d = DIRECTION_RE.search(text[:m.start()])
            if d:
                signed = True
                if d.group(1) in ("跌", "降"):
                    value = -value
        out.append({"value": value, "decimals": len(tok.split(".")[1]) if "." in tok else 0,
                    "token": (sign or "") + raw + unit, "unit": bool(unit), "signed": signed})
    return out


class AllowedNumbers:
    """ai_input 裡出現過的數字：絕對值集合（一般比對）和帶正負號的集合（有方向時比對）。"""

    def __init__(self):
        self.abs = set()
        self.signed = set()

    def add(self, v):
        self.signed.add(v)
        self.abs.add(abs(v))


def collect_allowed_numbers(obj, acc=None):
    """ai_input 裡所有數字（數值本身，以及字串裡出現的數字）。"""
    if acc is None:
        acc = AllowedNumbers()
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, str):
        for n in _numbers_in_text(obj):
            acc.add(n["value"] if n["signed"] else abs(n["value"]))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            collect_allowed_numbers(k, acc)
            collect_allowed_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_allowed_numbers(v, acc)
    return acc


def _close_enough(x, value, decimals):
    """四捨五入到 decimals 位（一般的四捨五入，不是 Python round 的銀行家捨入）。"""
    return abs(x - value) <= 0.5 * 10 ** -decimals + 1e-9


def _number_ok(n, allowed):
    if n["decimals"] == 0 and not n["unit"] and abs(n["value"]) in FREE_NUMBERS:
        return True
    if n["signed"]:
        return any(_close_enough(x, n["value"], n["decimals"]) for x in allowed.signed)
    return any(_close_enough(x, n["value"], n["decimals"]) for x in allowed.abs)


def check_text(text, allowed, max_len):
    """回傳問題清單（空清單代表合格）。"""
    problems = []
    if not isinstance(text, str) or not text.strip():
        return ["空白"]
    if len(text) > max_len:
        problems.append(f"超過 {max_len} 字")
    for phrase in BANNED_PHRASES:
        if phrase in text:
            problems.append(f"含禁用語「{phrase}」")
    bad = [n["token"] for n in _numbers_in_text(text) if not _number_ok(n, allowed)]
    if bad:
        problems.append(f"數字不在資料裡（或正負方向不符）：{', '.join(bad[:5])}")
    cjk = CJK_NUMBER_RE.findall(text)
    if cjk:
        problems.append(f"用中文數字寫數值（無法核對）：{', '.join(cjk[:3])}")
    return problems


def _str_list(x):
    """是字串陣列就原樣回傳，否則回 None。"""
    if isinstance(x, list) and all(isinstance(i, str) for i in x):
        return x
    return None


def validate(ai_output, ai_input):
    """
    回傳 (合格內容, 問題清單)。
    合格內容格式：{"highlights": [...], "stocks": {代號: {...}}}；不合格的段落被拿掉。
    """
    clean = {"highlights": [], "stocks": {}}
    issues = []
    if not isinstance(ai_output, dict):
        return clean, ["AI 輸出不是 JSON 物件"]
    if ai_output.get("date") != ai_input.get("date"):
        return clean, [f"AI 輸出日期 {ai_output.get('date')} 和資料日期 {ai_input.get('date')} 不符"]

    allowed = collect_allowed_numbers(ai_input)
    news_ids = {n["id"] for n in ai_input.get("news", [])}
    candidates = {c["stock_id"] for c in ai_input.get("candidates", [])}

    highlights = ai_output.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
        issues.append("highlights 不是陣列")
    for i, h in enumerate(highlights[:MAX_HIGHLIGHTS], 1):
        if not isinstance(h, dict):
            issues.append(f"今日重點 {i}：格式錯誤")
            continue
        problems = check_text(h.get("text"), allowed, MAX_HIGHLIGHT_LEN)
        refs = _str_list(h.get("news_ids") or [])
        if refs is None:
            problems.append("news_ids 要是字串陣列")
            refs = []
        unknown = [r for r in refs if r not in news_ids]
        if unknown:
            problems.append(f"引用不存在的新聞 {', '.join(unknown)}")
        if problems:
            issues.append(f"今日重點 {i}：{'；'.join(problems)}")
        else:
            clean["highlights"].append({"text": h["text"].strip(), "news_ids": refs})
    if len(highlights) > MAX_HIGHLIGHTS:
        issues.append(f"今日重點超過 {MAX_HIGHLIGHTS} 則，只取前 {MAX_HIGHLIGHTS} 則")

    seen = set()
    stocks = ai_output.get("stocks") or []
    if not isinstance(stocks, list):
        issues.append("stocks 不是陣列")
        stocks = []
    for s in stocks:
        if not isinstance(s, dict):
            issues.append("stocks 裡有格式錯誤的項目")
            continue
        sid = str(s.get("stock_id", ""))
        if sid not in candidates:
            issues.append(f"{sid}：不在辯證名單裡，忽略")
            continue
        if sid in seen:
            issues.append(f"{sid}：重複，只取第一筆")
            continue
        seen.add(sid)
        problems = []
        for key, label in (("bull_points", "多方"), ("bear_points", "空方")):
            pts = _str_list(s.get(key))
            if pts is None or not (1 <= len(pts) <= MAX_POINTS):
                problems.append(f"{label}要 1~{MAX_POINTS} 點")
                continue
            for j, p in enumerate(pts, 1):
                for prob in check_text(p, allowed, MAX_POINT_LEN):
                    problems.append(f"{label}第 {j} 點{prob}")
        if s.get("verdict") not in VERDICTS:
            problems.append(f"判決「{s.get('verdict')}」不在四個選項裡")
        conf = s.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, int) or not (1 <= conf <= 10):
            problems.append("信心分數要是 1~10 的整數")
        for prob in check_text(s.get("summary"), allowed, MAX_SUMMARY_LEN):
            problems.append(f"摘要{prob}")
        refs = _str_list(s.get("news_ids") or [])
        if refs is None:
            problems.append("news_ids 要是字串陣列")
            refs = []
        unknown = [r for r in refs if r not in news_ids]
        if unknown:
            problems.append(f"引用不存在的新聞 {', '.join(unknown)}")
        if problems:
            issues.append(f"{sid}：{'；'.join(problems)}")
        else:
            clean["stocks"][sid] = {
                "bull_points": [p.strip() for p in s["bull_points"]],
                "bear_points": [p.strip() for p in s["bear_points"]],
                "verdict": s["verdict"],
                "confidence": conf,
                "summary": s["summary"].strip(),
                "news_ids": refs,
            }
    for sid in sorted(candidates - seen):
        issues.append(f"{sid}：AI 沒有寫這檔")
    return clean, issues
