# verify.py - 核對程式
#
# 文章的做法：另外寫一支程式，向證交所再抓一次，比對報告上的數字。
# 這裡做三件事：
#   1. verify_closes：觀察清單的收盤價（來自 FinMind）和證交所公布的收盤價是否一致
#      （順便抓出 FinMind 還沒更新、拿到舊資料的情況）
#   2. verify_messages：產生出來的 Telegram 訊息，每檔的收盤／進場／目標／停損／RR 有沒有寫錯
#   3. `python verify.py universe`：股票池的代號和官方名稱是否一致
#
# 用法：
#   python verify.py universe          檢查股票池
#   python verify.py report 2026-10-16 重新核對某一天已存的報告

import re
import sys

from config import STOCK_UNIVERSE, STOCK_NAMES
from reporter import pick_number_strings


def verify_closes(picks, twse_closes):
    """回傳 (核對成功說明, 問題清單)。上櫃股不在證交所清單裡，略過不算錯。"""
    issues, checked, skipped = [], 0, []
    for p in picks:
        sid = p["stock_id"]
        if sid not in twse_closes:
            skipped.append(sid)
            continue
        checked += 1
        if abs(float(twse_closes[sid]) - float(p["close"])) > 0.001:
            issues.append(f"{sid} 收盤價和證交所不一致（報告 {p['close']}、證交所 {twse_closes[sid]}），"
                          f"可能是資料源尚未更新")
    summary = None
    if checked:
        summary = f"觀察清單 {checked} 檔收盤價與證交所一致" if not issues else None
    if skipped:
        summary = (summary + "；" if summary else "") + f"{len(skipped)} 檔上櫃股未核對"
    return summary, issues


def verify_messages(messages, picks):
    """每檔的關鍵數字都要原樣出現在訊息裡。"""
    text = "\n".join(messages)
    issues = []
    for p in picks:
        if not p.get("targets"):
            continue
        block = _find_block(text, p["stock_id"])
        if block is None:
            issues.append(f"{p['stock_id']} 不在報告裡")
            continue
        for label, value in pick_number_strings(p).items():
            if value not in block:
                issues.append(f"{p['stock_id']} 的{label} {value} 沒有正確寫進報告")
    return issues


def _find_block(text, stock_id):
    m = re.search(rf"\d+\. <b>{re.escape(stock_id)} .*?(?=\n\d+\. <b>|\n<i>|\n\n|\Z)", text, re.S)
    return m.group(0) if m else None


def _norm_name(name):
    return re.sub(r"[\s\-*＊]|KY", "", str(name))


def names_match(expected, official):
    a, b = _norm_name(expected), _norm_name(official)
    return bool(a) and bool(b) and (a in b or b in a)


def verify_universe(stock_info):
    """回傳 (官方查無的代號, [(代號, 設定名稱, 官方名稱), ...])。"""
    not_found, mismatched = [], []
    for sid in sorted(STOCK_UNIVERSE):
        official = stock_info.get(sid)
        expected = STOCK_NAMES.get(sid)
        if not official:
            not_found.append(sid)
        elif expected and not names_match(expected, official):
            mismatched.append((sid, expected, official))
    return not_found, mismatched


def _cli_universe():
    from data_fetcher import get_stock_info
    info = get_stock_info()
    if not info:
        print("抓不到 FinMind TaiwanStockInfo，無法核對")
        return 2
    not_found, mismatched = verify_universe(info)
    for sid in not_found:
        print(f"[查無] {sid}（設定名稱：{STOCK_NAMES.get(sid, '未設定')}）")
    for sid, exp, off in mismatched:
        print(f"[不符] {sid}：設定「{exp}」，官方「{off}」")
    if not (not_found or mismatched):
        print(f"股票池 {len(STOCK_UNIVERSE)} 檔，代號和名稱都一致")
    return 1 if (not_found or mismatched) else 0


def _cli_report(date):
    import history
    payload = history.load_json(date, "picks.json")
    report = (history.day_dir(date) / "report.txt")
    if not payload or not report.exists():
        print(f"{date} 沒有 picks.json 或 report.txt")
        return 2
    messages = report.read_text(encoding="utf-8").split("\n=== 訊息")
    issues = verify_messages(messages, payload.get("picks", []))
    for i in issues:
        print(f"[錯誤] {i}")
    print("報告數字核對通過" if not issues else f"共 {len(issues)} 個問題")
    return 1 if issues else 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "universe":
        sys.exit(_cli_universe())
    if len(sys.argv) >= 3 and sys.argv[1] == "report":
        sys.exit(_cli_report(sys.argv[2]))
    print("用法：python verify.py universe | python verify.py report YYYY-MM-DD")
    sys.exit(2)
