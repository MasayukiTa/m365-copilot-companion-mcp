"""共有作業ツリー上での破壊的 git 操作を coding_ops のガードが拒否することの検証。

ここで確かめるのは grep ではなく実挙動:
  * git_add は -A / --all / '.' の一括ステージを拒否し、明示パスは通す。
  * git_checkout は共有(メイン)作業ツリーでのブランチ切替を拒否し、
    create=True(-b)と専用worktreeでの切替は通す。
  * git_commit は main/master への直接コミットを拒否し、機能ブランチ上では通す。
各ガードは contract_gate.check_op("shell_destructive", ...) 経路にも載っており、
契約が INERT(既定)のときは素通りする -- それを実リポジトリで動かして示す。
"""
import os
import subprocess
import pytest

from tools import coding_ops as C


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _no_lock_no_pathcheck(monkeypatch):
    # ロックとallowed-base制約はここでの関心事ではない。実 git 挙動だけを見る。
    monkeypatch.setattr(C, "require_unlocked", lambda: None)
    monkeypatch.setattr(C, "_validate_path", lambda p: __import__("pathlib").Path(p))


@pytest.fixture
def shared_repo(tmp_path):
    """main ブランチを持つ共有(メイン)作業ツリー。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    return repo


# ---- helper 単体 --------------------------------------------------------

def test_shared_worktree_detected_true(shared_repo):
    assert C._is_shared_worktree(shared_repo) is True


def test_linked_worktree_detected_false(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    _git(shared_repo, "worktree", "add", "-b", "feat", str(wt))
    assert C._is_shared_worktree(wt) is False


def test_add_wholesale_flagged():
    assert C._add_is_wholesale(["-A"]) is True
    assert C._add_is_wholesale(["--all"]) is True
    assert C._add_is_wholesale(["."]) is True
    assert C._add_is_wholesale(["tools/x.py", "tests/y.py"]) is False


# ---- git_add ------------------------------------------------------------

def test_git_add_refuses_dash_A(shared_repo):
    (shared_repo / "b.txt").write_text("two\n", encoding="utf-8")
    out = C.git_add(["-A"], repo_path=str(shared_repo))
    assert "refused" in out
    # 何もステージされていないこと
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=str(shared_repo), capture_output=True, text=True).stdout
    assert staged.strip() == ""


def test_git_add_refuses_dot(shared_repo):
    (shared_repo / "b.txt").write_text("two\n", encoding="utf-8")
    out = C.git_add(["."], repo_path=str(shared_repo))
    assert "refused" in out


def test_git_add_allows_explicit_paths(shared_repo):
    (shared_repo / "b.txt").write_text("two\n", encoding="utf-8")
    out = C.git_add(["b.txt"], repo_path=str(shared_repo))
    assert "refused" not in out
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=str(shared_repo), capture_output=True, text=True).stdout
    assert "b.txt" in staged


# ---- git_checkout -------------------------------------------------------

def test_git_checkout_switch_refused_in_shared_tree(shared_repo):
    _git(shared_repo, "branch", "other")
    out = C.git_checkout("other", repo_path=str(shared_repo), create=False)
    assert "refused" in out
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=str(shared_repo), capture_output=True, text=True).stdout.strip()
    assert cur == "main"  # 切り替わっていない


def test_git_checkout_create_allowed_in_shared_tree(shared_repo):
    out = C.git_checkout("fresh", repo_path=str(shared_repo), create=True)
    assert "refused" not in out
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=str(shared_repo), capture_output=True, text=True).stdout.strip()
    assert cur == "fresh"


def test_git_checkout_switch_allowed_in_linked_worktree(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    _git(shared_repo, "worktree", "add", str(wt))  # detached
    _git(shared_repo, "branch", "target")
    out = C.git_checkout("target", repo_path=str(wt), create=False)
    assert "refused" not in out


# ---- git_commit ---------------------------------------------------------

def test_git_commit_refused_on_main(shared_repo):
    (shared_repo / "c.txt").write_text("three\n", encoding="utf-8")
    _git(shared_repo, "add", "c.txt")
    out = C.git_commit("add c", repo_path=str(shared_repo))
    assert "refused" in out


def test_git_commit_allowed_on_feature_branch(shared_repo):
    _git(shared_repo, "checkout", "-b", "feature")
    (shared_repo / "c.txt").write_text("three\n", encoding="utf-8")
    _git(shared_repo, "add", "c.txt")
    out = C.git_commit("add c", repo_path=str(shared_repo))
    assert "refused" not in out
