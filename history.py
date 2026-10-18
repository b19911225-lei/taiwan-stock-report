# history.py - 每日結果存回 GitHub（獨立分支，不和程式碼混在一起）
#
# history/ 是 HISTORY_BRANCH 分支的 git worktree：
#   history/YYYY-MM-DD/picks.json      程式算好的選股結果（所有數字的來源）
#   history/YYYY-MM-DD/ai_input.json   給 AI 的資料
#   history/YYYY-MM-DD/ai_output.json  AI 寫的文字
#   history/YYYY-MM-DD/report.txt      實際送出的 Telegram 訊息
#   history/YYYY-MM-DD/sent.json       已發送標記（同一個資料日期不重複發）
#   history/closed/YYYY-MM-DD.json     休市通知已發送標記
# 存回 GitHub 的好處：去重、績效追蹤都靠它；Routine 每次都是全新的主機，不存就沒了。

import json
import subprocess
import time
from pathlib import Path

from config import HISTORY_BRANCH

REPO_DIR = Path(__file__).parent
HISTORY_DIR = REPO_DIR / "history"

GIT_IDENTITY = ["-c", "user.name=taiwan-stock-report", "-c", "user.email=taiwan-stock-report@users.noreply.github.com"]


def day_dir(date):
    return HISTORY_DIR / date


def save_json(date, name, obj):
    path = day_dir(date) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_json(date, name):
    path = day_dir(date) / name
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_text(date, name, text):
    path = day_dir(date) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sent_info(date):
    """已發送標記；沒發過回 None。"""
    return load_json(date, "sent.json")


def is_sent(date):
    """發過「完整」的報告才算。資料不完整時發的報告，之後資料齊了還會再補發一次。"""
    info = sent_info(date)
    return info is not None and info.get("complete", True)


def closed_marker(date):
    return HISTORY_DIR / "closed" / f"{date}.json"


def past_picks(before_date, limit):
    """[(日期, picks), ...]，由新到舊，只算 before_date 之前且真的有發送的日子。"""
    if not HISTORY_DIR.exists():
        return []
    out = []
    for d in sorted((p.name for p in HISTORY_DIR.iterdir() if p.is_dir()), reverse=True):
        if len(d) != 10 or d >= before_date or not (HISTORY_DIR / d / "sent.json").exists():
            continue
        payload = load_json(d, "picks.json") or {}
        picks = [
            {"stock_id": p["stock_id"], "close": p["close"],
             "target1": p["targets"]["target1"], "stop_loss": p["targets"]["stop_loss"]}
            for p in payload.get("picks", []) if p.get("targets")
        ]
        out.append((d, picks))
        if len(out) >= limit:
            break
    return out


# ─── git 同步 ───

def _git(*args, cwd=None, check=True):
    # cwd 在呼叫時才決定（不能寫成預設參數 cwd=REPO_DIR，否則測試替換 REPO_DIR 也沒用）
    return subprocess.run(["git", *args], cwd=cwd or REPO_DIR, capture_output=True, text=True, check=check)


def _remote_branch_state():
    """'exists'、'missing'，或連不上時回錯誤訊息。"""
    for wait in (2, 4, None):
        r = _git("ls-remote", "--exit-code", "origin", f"refs/heads/{HISTORY_BRANCH}", check=False)
        if r.returncode == 0:
            return "exists"
        if r.returncode == 2:
            return "missing"
        if wait:
            time.sleep(wait)
    return f"連不上 origin：{(r.stderr or '').strip()[:200]}"


def _fetch_branch():
    return _git("fetch", "origin", f"+refs/heads/{HISTORY_BRANCH}:refs/remotes/origin/{HISTORY_BRANCH}",
                check=False).returncode == 0


def sync_pull():
    """
    把 HISTORY_BRANCH 準備到 history/。回傳 (成功與否, 說明)。
    抓不到遠端時一定回 False：不能當成「還沒有歷史」，否則會重複發送。
    """
    if not (REPO_DIR / ".git").exists():
        return False, "不是 git repo（本機測試請加 --no-git）"
    state = _remote_branch_state()
    if state not in ("exists", "missing"):
        return False, state
    if state == "exists" and not _fetch_branch():
        return False, f"抓取 {HISTORY_BRANCH} 失敗"
    try:
        if (HISTORY_DIR / ".git").exists():
            if state == "exists":
                r = _git("merge", "--ff-only", f"origin/{HISTORY_BRANCH}", cwd=HISTORY_DIR, check=False)
                if r.returncode != 0:
                    return False, f"history/ 無法快轉到遠端：{(r.stderr or '').strip()[:200]}"
            return True, "已更新"
        if HISTORY_DIR.exists() and any(HISTORY_DIR.iterdir()):
            return False, "history/ 已存在但不是 worktree（本機測試請加 --no-git）"
        if HISTORY_DIR.exists():
            HISTORY_DIR.rmdir()
        _git("worktree", "prune", check=False)
        if state == "exists":
            _git("worktree", "add", "-B", HISTORY_BRANCH, str(HISTORY_DIR), f"origin/{HISTORY_BRANCH}")
            return True, "已取得歷史分支"
        _git("worktree", "add", "--orphan", "-b", HISTORY_BRANCH, str(HISTORY_DIR))
        (HISTORY_DIR / "README.md").write_text(
            "# 每日選股紀錄\n\n由 run_daily.py 自動產生，請勿手動修改。說明見 main 分支的 docs/SPEC.md。\n",
            encoding="utf-8")
        return True, "遠端還沒有歷史分支，已建立新的"
    except subprocess.CalledProcessError as e:
        return False, f"git worktree 失敗：{(e.stderr or '').strip()[:200]}"


def sync_push(message):
    """commit 並推送 history/，推完確認遠端真的是這個 commit。回傳 (成功與否, 說明)。"""
    if not (HISTORY_DIR / ".git").exists():
        return False, "history/ 不是 git worktree，沒有推送"
    _git("add", "-A", cwd=HISTORY_DIR)
    if _git("diff", "--cached", "--quiet", cwd=HISTORY_DIR, check=False).returncode == 0:
        return True, "沒有變更"
    _git(*GIT_IDENTITY, "commit", "-m", message, cwd=HISTORY_DIR)
    last = ""
    for wait in (2, 4, 8, 16, None):
        r = _git("push", "origin", f"HEAD:refs/heads/{HISTORY_BRANCH}", cwd=HISTORY_DIR, check=False)
        if r.returncode == 0:
            head = _git("rev-parse", "HEAD", cwd=HISTORY_DIR).stdout.strip()
            remote = _git("ls-remote", "origin", f"refs/heads/{HISTORY_BRANCH}", check=False).stdout.split()
            if remote and remote[0] == head:
                return True, f"已推送到 {HISTORY_BRANCH}"
            last = f"推送後遠端不是本機的 commit（遠端 {remote[:1]}，本機 {head[:7]}）"
            return False, last
        last = (r.stderr or "").strip()
        if "rejected" in last or "non-fast-forward" in last or "fetch first" in last:
            # 別的執行先推了：rebase 到遠端最新再推；衝突（例如同一天都寫了 sent.json）就放棄
            _fetch_branch()
            rb = _git(*GIT_IDENTITY, "rebase", f"origin/{HISTORY_BRANCH}", cwd=HISTORY_DIR, check=False)
            if rb.returncode != 0:
                _git("rebase", "--abort", cwd=HISTORY_DIR, check=False)
                return False, f"和遠端的紀錄衝突，沒有推送：{(rb.stdout + rb.stderr).strip()[:200]}"
        if wait:
            time.sleep(wait)
    return False, f"推送失敗：{last[:200]}"
