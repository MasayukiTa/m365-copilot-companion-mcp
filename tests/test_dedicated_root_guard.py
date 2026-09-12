"""専用リポジトリルート検証述語 (_dedicated_root_ok) とその補助 helper の検証。

ここで確かめるのは grep ではなく実挙動 -- 関数を実際に呼んで戻り値を見る:
  * 専用(linked) worktree のルートでは True を返す。
  * 共有(main)作業ツリーでは False を返す。
  * realpath が解決できない状況では fail-closed (False) になる。
  * 拒否メッセージに代替手段(worktree add)が含まれる。

すべて tmp_path 上の実 git リポジトリで行う。Windows 専用の記述は入れない
(パス比較は tmp_path と realpath の相対的な性質で書く)。作った worktree は必ず消す。
"""
import os

import pytest

from tools import coding_ops as C


def _git(cwd, *args):
    from tools.childproc import run as _run_child
    _run_child(["git", *args], cwd=str(cwd), check=True)


def _head(cwd):
    from tools.childproc import run as _run_child
    return _run_child(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(cwd)).stdout.strip()


@pytest.fixture(autouse=True)
def _no_lock_no_pathcheck(monkeypatch):
    # ロックと allowed-base 制約はここでの関心事ではない。実 git 挙動だけを見る。
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


@pytest.fixture
def dedicated_worktree(shared_repo, tmp_path):
    """shared_repo から切った専用 linked worktree。必ず消す。"""
    wt = tmp_path / "wt-feat"
    _git(shared_repo, "worktree", "add", "-b", "feat", str(wt))
    try:
        yield wt
    finally:
        # クリーンアップ: 登録を外し、ディレクトリも残さない。
        from tools.childproc import run as _run_child
        _run_child(["git", "worktree", "remove", "--force", str(wt)], cwd=str(shared_repo))
        _run_child(["git", "worktree", "prune"], cwd=str(shared_repo))


# ---- _dedicated_root_ok 本体 --------------------------------------------

def test_dedicated_worktree_is_ok(dedicated_worktree):
    ok, reason = C._dedicated_root_ok(str(dedicated_worktree), dedicated_worktree)
    assert ok is True
    assert reason == ""


def test_shared_tree_is_not_ok(shared_repo):
    ok, reason = C._dedicated_root_ok(str(shared_repo), shared_repo)
    assert ok is False
    assert "refused" in reason


def test_non_root_subdir_is_not_ok(dedicated_worktree):
    # ルートでなくサブディレクトリを渡すと拒否される。
    sub = dedicated_worktree / "pkg"
    sub.mkdir()
    ok, reason = C._dedicated_root_ok(str(sub), dedicated_worktree)
    assert ok is False
    assert "checkout root" in reason


def test_realpath_failure_fails_closed(dedicated_worktree, monkeypatch):
    # realpath が解決できないときは fail-closed (False)。
    # 「確認できなかった」を「安全」と読まないことを確かめる。
    def _boom(_p):
        raise OSError("simulated realpath failure")
    monkeypatch.setattr(C.os.path, "realpath", _boom)
    ok, reason = C._dedicated_root_ok(str(dedicated_worktree), dedicated_worktree)
    assert ok is False
    assert "could not be resolved" in reason


def test_refusal_names_the_alternative(shared_repo):
    # 拒否理由には代替手段(git worktree add / create=True)が含まれる。
    ok, reason = C._dedicated_root_ok(str(shared_repo), shared_repo)
    assert ok is False
    assert "git worktree add" in reason
    assert "create=True" in reason


# ---- _realpath_strict --------------------------------------------------

def test_realpath_strict_resolves_existing(tmp_path):
    got = C._realpath_strict(str(tmp_path))
    assert got == os.path.normcase(os.path.realpath(str(tmp_path)))


def test_realpath_strict_raises_on_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(OSError):
        C._realpath_strict(str(missing))


# ---- _count_local_changes ----------------------------------------------

def test_count_local_changes_clean(shared_repo):
    assert C._count_local_changes(shared_repo) == 0


def test_count_local_changes_dirty(shared_repo):
    (shared_repo / "b.txt").write_text("two\n", encoding="utf-8")
    (shared_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    assert C._count_local_changes(shared_repo) == 2


# ---- git_checkout 経由での統合 -------------------------------------------

def test_git_checkout_switch_refused_in_shared_tree(shared_repo):
    # 共有ツリーでの既存ブランチへの切替は拒否される(何も切替らない)。
    _git(shared_repo, "branch", "other")
    out = C.git_checkout("other", repo_path=str(shared_repo))
    assert "refused" in out
    cur = _head(shared_repo)
    assert cur == "main"


def test_git_checkout_create_allowed_in_shared_tree(shared_repo):
    # create=True (-b) は何も破棄しないので共有ツリーでも許可される。
    out = C.git_checkout("brand-new", repo_path=str(shared_repo), create=True)
    assert "refused" not in out
    cur = _head(shared_repo)
    assert cur == "brand-new"


def test_git_checkout_switch_allowed_in_dedicated_worktree(dedicated_worktree):
    # 専用 worktree のルートでは既存ブランチへの切替が許可される。
    # feat に居るので、元の main へは切替できない(main は共有ツリーが占有)ので
    # 別ブランチを作って戻す。
    _git(dedicated_worktree, "branch", "feat2")
    out = C.git_checkout("feat2", repo_path=str(dedicated_worktree))
    assert "refused" not in out
    cur = _head(dedicated_worktree)
    assert cur == "feat2"
