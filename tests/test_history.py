"""歷史分支的 git 同步（用本機 bare repo 當 origin）。"""

import shutil
import subprocess

import pytest

import history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    monkeypatch.setattr(history, "REPO_DIR", work)
    monkeypatch.setattr(history, "HISTORY_DIR", work / "history")
    monkeypatch.setattr(history, "HISTORY_BRANCH", "claude/report-history")
    monkeypatch.setattr(history.time, "sleep", lambda s: None)
    # 保險：git 指令必須跑在暫存 repo，絕不能碰到真正的 repo 和 GitHub
    top = history._git("rev-parse", "--show-toplevel").stdout.strip()
    assert top == str(work.resolve()), top
    assert history._git("remote", "get-url", "origin").stdout.strip() == str(origin)
    return {"origin": origin, "work": work, "tmp": tmp_path}


def test_creates_orphan_branch_then_pushes_and_reuses(repo):
    ok, msg = history.sync_pull()
    assert ok, msg
    history.save_json("2026-10-16", "sent.json", {"ok": True})
    ok, msg = history.sync_push("report 2026-10-16")
    assert ok, msg
    files = git("ls-tree", "-r", "--name-only", "claude/report-history", cwd=repo["origin"])
    assert "2026-10-16/sent.json" in files and "README.md" in files
    # 歷史分支是孤立分支：只有紀錄的 commit，不含 main 的程式碼歷史
    assert git("log", "--oneline", "claude/report-history", cwd=repo["origin"]).count("\n") == 1

    # 模擬下一次 Routine：全新的 clone
    fresh = repo["tmp"] / "fresh"
    git("clone", "-q", str(repo["origin"]), str(fresh), cwd=repo["tmp"])
    history.REPO_DIR = fresh
    history.HISTORY_DIR = fresh / "history"
    ok, msg = history.sync_pull()
    assert ok, msg
    assert history.is_sent("2026-10-16")


def test_push_without_changes_is_noop(repo):
    history.sync_pull()
    history.save_json("2026-10-16", "a.json", {})
    history.sync_push("first")
    ok, msg = history.sync_push("again")
    assert ok and msg == "沒有變更"


def test_past_picks_only_counts_sent_days(repo):
    history.sync_pull()
    pick = {"stock_id": "2330", "close": 100, "targets": {"target1": 110, "stop_loss": 95}}
    history.save_json("2026-10-14", "picks.json", {"picks": [pick]})
    history.save_json("2026-10-14", "sent.json", {})
    history.save_json("2026-10-15", "picks.json", {"picks": [pick]})   # 沒有 sent.json：沒發成功
    past = history.past_picks("2026-10-16", 20)
    assert [d for d, _ in past] == ["2026-10-14"]
    assert past[0][1][0] == {"stock_id": "2330", "close": 100, "target1": 110, "stop_loss": 95}


def test_unreachable_origin_is_an_error_not_an_empty_history(repo):
    # 抓不到遠端時不能當成「還沒有歷史」，否則會重發
    git("remote", "set-url", "origin", str(repo["tmp"] / "does-not-exist.git"), cwd=repo["work"])
    ok, msg = history.sync_pull()
    assert not ok
    assert not (repo["work"] / "history").exists()


def test_conflicting_push_is_reported_as_failure(repo):
    history.sync_pull()
    history.save_json("2026-10-16", "sent.json", {"by": "routine"})
    assert history.sync_push("routine")[0]

    # 另一個 clone（例如備援）同時也寫了同一天的 sent.json
    other = repo["tmp"] / "other"
    git("clone", "-q", "-b", "claude/report-history", str(repo["origin"]), str(other), cwd=repo["tmp"])
    (other / "2026-10-16" / "sent.json").write_text('{"by": "fallback-first"}', encoding="utf-8")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fallback", cwd=other)
    git("push", "-q", "origin", "claude/report-history", cwd=other)

    history.save_json("2026-10-16", "sent.json", {"by": "routine-again"})
    ok, msg = history.sync_push("again")
    assert not ok and "衝突" in msg
    # rebase 已經放棄，worktree 不會卡在一半
    status = history._git("status", cwd=history.HISTORY_DIR).stdout
    assert "rebase" not in status
