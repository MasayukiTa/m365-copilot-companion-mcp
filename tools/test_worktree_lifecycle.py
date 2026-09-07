"""スコープ付き worktree ライフサイクル (coding_ops) の実挙動検証。

ここで確かめるのは grep ではなく実 git 挙動:
  * worktree_add は新規ブランチ・専用リンク worktree を base(既定は committed ref)から作る。
  * worktree_scope は work のあとで必ず後始末する。yield 中で例外が出ても husk を残さない。
  * worktree_remove は共有(メイン)作業ツリーの削除を拒否する -- ほかのワーカーと
    オーナーの未コミット変更を巻き込まないため。
  * remove 失敗時の rmtree フォールバックは、対象が確実にリンク worktree のときだけ動き、
    共有ツリーに解決し得るときは動かない(husk 化・共有ツリー消去の防止)。
各ガードは contract_gate.check_op("shell_destructive", ...) 経路にも載っており、
契約が INERT(既定)のときは素通りする -- それを実リポジトリで動かして示す。
"""
import pathlib
import subprocess

import pytest

from tools import coding_ops as C


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _worktrees(repo):
    out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(repo),
                         check=True, capture_output=True, text=True).stdout
    # git は porcelain で '/' 区切りを返す。比較のため resolve して揃える。
    return [str(pathlib.Path(line[len("worktree "):]).resolve())
            for line in out.splitlines() if line.startswith("worktree ")]


@pytest.fixture(autouse=True)
def _no_lock_no_pathcheck(monkeypatch):
    # ロックと allowed-base 制約はここでの関心事ではない。実 git 挙動だけを見る。
    monkeypatch.setattr(C, "require_unlocked", lambda: None)
    monkeypatch.setattr(C, "_validate_path", lambda p: pathlib.Path(p))


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


# ---- worktree_add -------------------------------------------------------

def test_add_creates_linked_worktree_on_new_branch(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    out = C.worktree_add(str(wt), "feat/x", "HEAD", repo_path=str(shared_repo))
    assert "[returncode:" not in out, out
    assert wt.is_dir()
    # 専用リンク worktree であり、共有ツリーではない。
    assert C._is_shared_worktree(wt) is False
    assert str(wt.resolve()) in "\n".join(_worktrees(shared_repo))


def test_add_off_committed_base_omits_shared_uncommitted_changes(shared_repo, tmp_path):
    # 共有ツリーに未コミットの新規ファイルを置く。committed base から生やせば持ち込まれない。
    (shared_repo / "dirty.txt").write_text("scratch\n", encoding="utf-8")
    wt = tmp_path / "wt"
    C.worktree_add(str(wt), "feat/y", "HEAD", repo_path=str(shared_repo))
    assert not (wt / "dirty.txt").exists()


# ---- worktree_remove: 共有ツリー保護 ------------------------------------

def test_remove_refuses_shared_main_tree(shared_repo):
    # 共有ツリー自身を消そうとする指示は拒否され、ツリーは残る。
    out = C.worktree_remove(str(shared_repo), repo_path=str(shared_repo))
    assert "refused" in out
    assert shared_repo.is_dir()
    assert (shared_repo / "a.txt").exists()


def test_remove_takes_down_linked_worktree(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    C.worktree_add(str(wt), "feat/z", "HEAD", repo_path=str(shared_repo))
    assert wt.is_dir()
    out = C.worktree_remove(str(wt), repo_path=str(shared_repo))
    assert "git worktree remove: ok" in out
    assert not wt.exists()
    # 後始末後、worktree 一覧から消えており、共有ツリーは無傷。
    assert str(wt.resolve()) not in "\n".join(_worktrees(shared_repo))
    assert shared_repo.is_dir() and (shared_repo / "a.txt").exists()


def test_remove_absent_path_is_safe_noop(shared_repo, tmp_path):
    out = C.worktree_remove(str(tmp_path / "never-existed"), repo_path=str(shared_repo))
    # 拒否ではなく、何も壊さずに完了する(prune まで通る)。
    assert "refused" not in out
    assert shared_repo.is_dir()


def test_remove_fallback_never_deletes_shared_tree(shared_repo, monkeypatch):
    """git remove が失敗しても、共有ツリーに解決するパスは rmtree で消さない。

    これがまさに husk 事故の核心: `.git` がメインリポジトリに解決するディレクトリを
    path 指定で rmtree すると、共有チェックアウトを消しかねない。
    """
    import shutil
    called = {"rmtree": False}
    monkeypatch.setattr(shutil, "rmtree",
                        lambda *a, **k: called.__setitem__("rmtree", True))
    # git remove を強制的に失敗させる。
    monkeypatch.setattr(C, "_run", lambda *a, **k: "[returncode:1] boom")
    out = C.worktree_remove(str(shared_repo), repo_path=str(shared_repo))
    assert "refused" in out           # そもそも共有ツリーは (A) で弾かれる
    assert called["rmtree"] is False  # rmtree には到達しない
    assert shared_repo.is_dir()


# ---- worktree_scope: 例外があっても後始末 -------------------------------

def test_scope_tears_down_on_success(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    with C.worktree_scope(str(wt), "feat/s1", "HEAD", repo_path=str(shared_repo)) as p:
        assert p == str(wt)
        assert wt.is_dir()
        (wt / "work.txt").write_text("edit\n", encoding="utf-8")
    # スコープを抜けたら worktree は消えている。
    assert not wt.exists()
    assert str(wt.resolve()) not in "\n".join(_worktrees(shared_repo))


def test_scope_tears_down_on_exception(shared_repo, tmp_path):
    wt = tmp_path / "wt"
    with pytest.raises(RuntimeError):
        with C.worktree_scope(str(wt), "feat/s2", "HEAD", repo_path=str(shared_repo)):
            assert wt.is_dir()
            raise RuntimeError("work blew up")
    # 例外が出ても husk を残さない。
    assert not wt.exists()
    assert str(wt.resolve()) not in "\n".join(_worktrees(shared_repo))
    # 共有ツリーは無傷。
    assert shared_repo.is_dir() and (shared_repo / "a.txt").exists()


def test_scope_add_failure_yields_none_and_noops(shared_repo, tmp_path, monkeypatch):
    # add を失敗させると scope は None を yield し、後始末は安全な no-op。
    monkeypatch.setattr(C, "worktree_add", lambda *a, **k: "[returncode:1] cannot add")
    entered = {"val": "unset"}
    with C.worktree_scope(str(tmp_path / "wt"), "feat/s3", "HEAD",
                          repo_path=str(shared_repo)) as p:
        entered["val"] = p
    assert entered["val"] is None
    assert shared_repo.is_dir()
