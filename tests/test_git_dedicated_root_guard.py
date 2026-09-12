"""「判定できなかった」を「安全」と読まないこと -- 三状態を fail-closed に畳む境界。

`_is_shared_worktree` は三状態を返す: True(共有と証明できた) / False(専用と証明できた) /
None(判定できなかった)。この None が本ファイルの主題である。

None は fail-OPEN の値として設計されている。共有だと証明できないときに操作を止めると、
git リポジトリの外など正常な場面まで塞いでしまうためだ。ところが破壊的な git 操作にとって
その既定は弱すぎる。そこで `_dedicated_root_ok` は逆向きの述語として置かれ、
`_is_shared_worktree` が **厳密に False** のときだけ通す -- 「共有ではないと証明できた」の
であって「共有だと証明できなかった」ではない。

tests/test_dedicated_root_guard.py と tests/test_git_shared_guard.py は、共有ツリーと専用
worktree という**証明できる二つ**を押さえている。どちらも None を通らない。証明できない側の
振る舞いは、実際に None を返させないと再現できず、ここでだけ固定される。

区別が要る理由: 切替(-b なし)は今ある作業ツリーの中身を差し替えるので、確証できなければ
拒否する。ブランチ作成(-b)は HEAD の付け替えでファイルを差し替えないので、確証できなくても
通す。両方を一律に拒否すると、判定不能な場所で新しいブランチすら切れなくなる。
"""
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


# ---- 述語: None は False と同じ側に落ちる --------------------------------

def test_undetermined_is_not_a_dedicated_root(monkeypatch, shared_repo):
    """判定不能を専用ルートと認めないこと。

    `_is_shared_worktree` が None のとき、`not is_shared` は真になる。うっかりその形で
    書くと、判定できなかった木が専用ルートとして通る。必要なのは `is False` である。
    """
    monkeypatch.setattr(C, "_is_shared_worktree", lambda cwd: None)
    ok, reason = C._dedicated_root_ok(str(shared_repo), shared_repo)
    assert ok is False
    assert reason, "拒否しておいて理由を返さないと、呼び出し側は別経路を選べない"


def test_a_proven_dedicated_worktree_is_still_allowed(shared_repo, tmp_path):
    """None を締めた結果、正当な専用 worktree まで塞いでいないこと。

    fail-closed を入れたときに一緒に壊れるのは、いつでも「通ってほしい側」である。
    """
    wt = tmp_path / "wt"
    _git(shared_repo, "worktree", "add", "-b", "feat", str(wt))
    ok, _reason = C._dedicated_root_ok(str(wt), wt)
    assert ok is True


# ---- git_checkout: 判定不能な「切替」は拒否、「作成」は通す ----------------

def test_switch_is_refused_when_the_worktree_cannot_be_determined(monkeypatch, shared_repo):
    """確証できない切替は、実 git を走らせずに拒否すること。

    拒否したと言いながら HEAD が動いていれば、拒否は報告だけのものになる。だから戻り値
    ではなく HEAD を見る。
    """
    _git(shared_repo, "branch", "other")
    monkeypatch.setattr(C, "_is_shared_worktree", lambda cwd: None)
    out = C.git_checkout("other", repo_path=str(shared_repo), create=False)
    assert "refused" in out
    assert _head(shared_repo) == "main", "拒否したのに HEAD が動いている"


def test_creating_a_branch_is_allowed_even_when_undetermined(monkeypatch, shared_repo):
    """-b は通すこと。

    ブランチ作成はファイルを差し替えないので、切替と同じ厳しさで塞ぐ理由がない。ここまで
    拒否すると、判定不能な場所で新しいブランチを切ることすらできなくなる -- ガードが
    仕事を止める側に振れた形であり、それは別の壊れ方である。
    """
    monkeypatch.setattr(C, "_is_shared_worktree", lambda cwd: None)
    out = C.git_checkout("fresh", repo_path=str(shared_repo), create=True)
    assert "refused" not in out
    assert _head(shared_repo) == "fresh"
