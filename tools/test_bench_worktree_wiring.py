"""bench の teardown 2 サイトが scoped-worktree ヘルパに配線されていることの実挙動検証。

レビュー指摘への回帰ガード: 要件(1)後半「隔離が要る呼び出し側へ新ヘルパを通す」。
bench/pro_capture.py と bench/pro_cycle.py は `python bench/<script>.py` として
HTTP/unlock コンテキストなしで走る。だから gated worktree_remove ではなく
gate-free の _worktree_teardown を共有する必要がある -- それを実 git で示す。
"""
import importlib
import inspect
import os
import pathlib

import pytest

from tools import coding_ops as C


def _git(cwd, *args):
    from tools.childproc import run as _run_child
    _run_child(["git", *args], cwd=str(cwd), check=True)


def _wt_list(repo):
    from tools.childproc import run as _run_child
    out = _run_child(["git", "worktree", "list", "--porcelain"], cwd=str(repo),
                      check=True).stdout
    return [str(pathlib.Path(l[len("worktree "):]).resolve())
            for l in out.splitlines() if l.startswith("worktree ")]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-m", "init")
    return r


# ---- gate-free teardown は CLI(locked)でも動く ------------------------------
# require_unlocked() は HTTP コンテキストなしで locked を返す。gated worktree_remove は
# そこで拒否して husk を残す。_worktree_teardown はゲートを通さないので実際に消す。

def test_teardown_removes_real_worktree_without_unlock(repo, tmp_path, monkeypatch):
    # 実 CLI 相当: require_unlocked をパッチしない。ここでは locked のはず。
    from tools.security import require_unlocked
    assert require_unlocked() is not None  # 前提: in-process は locked
    wt = tmp_path / "wt1"
    _git(repo, "worktree", "add", "-b", "feat/x", str(wt), "HEAD")
    assert wt.is_dir()
    shared = C._resolves_into_common_dir(wt, pathlib.Path(str(repo)))
    assert shared is False
    rep = C._worktree_teardown(wt, pathlib.Path(str(repo)), shared)
    assert "git worktree remove: ok" in rep
    assert not wt.exists()                       # 実際に消えた
    assert str(wt.resolve()) not in _wt_list(repo)  # 登録も消えた(prune)


def test_teardown_refuses_shared_via_guard(repo):
    # 共有ツリー自身を渡したら shared is True。_worktree_teardown はゲート前提の
    # ヘルパなので、呼び出し側は shared is True で弾く責務がある -- ここでは判定器が
    # True を返すことを確認する(呼び出し側の分岐はソース回帰テストで担保)。
    shared = C._resolves_into_common_dir(pathlib.Path(str(repo)), pathlib.Path(str(repo)))
    assert shared is True


# ---- 呼び出し側が実際にヘルパへ配線されている(回帰ガード) ----------------------

def test_pro_capture_wired_to_helper():
    m = importlib.import_module("bench.pro_capture")
    src = inspect.getsource(m)
    assert "_worktree_teardown" in src
    assert "_resolves_into_common_dir" in src
    # 直接 rmtree / 直接 git worktree remove に戻っていないこと
    assert 'subprocess.run(["git", "-C", REPO, "worktree", "remove"' not in src


def test_pro_cycle_wired_to_helper():
    m = importlib.import_module("bench.pro_cycle")
    src = inspect.getsource(m)
    assert "_worktree_teardown" in src
    assert "_resolves_into_common_dir" in src


# ---- pro_cycle._discard を実 git で走らせ husk を残さない --------------------
# 実 CLI 相当(require_unlocked パッチなし)。_discard は SW/work/* を掃く。

def test_pro_cycle_discard_leaves_no_husk(tmp_path, monkeypatch):
    import bench.pro_cycle as PC
    # 隔離した REPO/SW を用意
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    sw = tmp_path / ".fleet" / "swe"
    work = sw / "work"
    work.mkdir(parents=True)
    wt = work / "inst1"
    _git(repo, "worktree", "add", "-b", "feat/inst1", str(wt), "HEAD")
    assert wt.is_dir()
    monkeypatch.setattr(PC, "REPO", str(repo))
    monkeypatch.setattr(PC, "SW", str(sw))
    logs = []
    monkeypatch.setattr(PC, "log", lambda *a: logs.append(" ".join(str(x) for x in a)))
    PC._discard()
    assert not wt.exists()                          # worktree ディレクトリが消えた
    assert str(wt.resolve()) not in _wt_list(repo)  # 登録も消えた


def test_swe_run_until_done_wired_to_helper():
    # このスクリプトは import 時に argparse を実行するため、import せずソースを直読する。
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "bench" / "swe_run_until_done.py").read_text(encoding="utf-8")
    assert "_worktree_teardown" in src
    assert "_resolves_into_common_dir" in src
