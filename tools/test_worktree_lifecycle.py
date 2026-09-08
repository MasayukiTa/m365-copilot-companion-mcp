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


# ---- survey_worktrees: 読み取り専用の棚卸し -------------------------------
#
# survey_worktrees は「消して良いか」を報告するだけで、何も削除しない。
# ここでは実 git で worktree を作り、判定と「非破壊」を実挙動で確かめる。

def _row_for(rows, path):
    target = str(pathlib.Path(path).resolve())
    for r in rows:
        if str(pathlib.Path(r["path"]).resolve()) == target:
            return r
    return None


def test_survey_reports_and_deletes_nothing(shared_repo, tmp_path):
    # 共有ツリー + リンク worktree を1つ用意する。
    wt = tmp_path / "wt"
    C.worktree_add(str(wt), "feat/survey1", "HEAD", repo_path=str(shared_repo))
    before = set(_worktrees(shared_repo))

    rows = C.survey_worktrees(repo_path=str(shared_repo))
    assert isinstance(rows, list)
    assert not any("error" in r for r in rows), rows

    # 共有(メイン)ツリーは is_main=True かつ削除候補ではない。
    main_row = _row_for(rows, shared_repo)
    assert main_row is not None
    assert main_row["is_main"] is True
    assert main_row["safe_to_remove"] is False

    # リンク worktree も報告に含まれる。
    wt_row = _row_for(rows, wt)
    assert wt_row is not None
    assert wt_row["is_main"] is False
    assert wt_row["branch"] == "feat/survey1"

    # 決定的に読み取り専用: 呼んだあとも worktree 集合は不変。
    after = set(_worktrees(shared_repo))
    assert after == before
    assert wt.is_dir()
    assert shared_repo.is_dir() and (shared_repo / "a.txt").exists()


def test_survey_flags_dirty_worktree_not_safe(shared_repo, tmp_path):
    # HEAD が base(main) に含まれていても、未コミット変更があれば safe にしない。
    wt = tmp_path / "wt"
    C.worktree_add(str(wt), "feat/survey2", "HEAD", repo_path=str(shared_repo))
    (wt / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    rows = C.survey_worktrees(repo_path=str(shared_repo))
    wt_row = _row_for(rows, wt)
    assert wt_row is not None
    assert wt_row["dirty"] is True
    assert wt_row["safe_to_remove"] is False
    # 何も消していない。
    assert wt.is_dir() and (wt / "scratch.txt").exists()


def test_survey_flags_merged_clean_worktree_safe(shared_repo, tmp_path):
    # 新規ブランチを main と同じ committed HEAD から生やし、変更しない。
    # HEAD は main に含まれ(merged)、dirty でなく、locked/main でもない -> safe_to_remove=True。
    wt = tmp_path / "wt"
    C.worktree_add(str(wt), "feat/survey3", "HEAD", repo_path=str(shared_repo))

    rows = C.survey_worktrees(repo_path=str(shared_repo), base="main")
    wt_row = _row_for(rows, wt)
    assert wt_row is not None
    assert wt_row["dirty"] is False
    assert wt_row["merged_into_base"] is True
    assert wt_row["safe_to_remove"] is True
    # 報告のみ: worktree は残っている。
    assert wt.is_dir()


def test_survey_returns_error_list_on_bad_repo(tmp_path):
    # git リポジトリでない場所を渡しても、常に list を返し、例外を漏らさない。
    rows = C.survey_worktrees(repo_path=str(tmp_path))
    assert isinstance(rows, list) and rows
    assert "error" in rows[0]


# ---- survey_worktrees: 読み取り専用、削除はしない -----------------------------

def _find(rows, path):
    key = str(pathlib.Path(path).resolve())
    for r in rows:
        if str(pathlib.Path(r["path"]).resolve()) == key:
            return r
    return None


def test_survey_lists_main_and_marks_it_unsafe(shared_repo):
    rows = C.survey_worktrees(repo_path=str(shared_repo))
    assert isinstance(rows, list) and rows
    main = _find(rows, shared_repo)
    assert main is not None
    assert main["is_main"] is True
    # 共有(メイン)ツリーは決して削除候補にしない。
    assert main["safe_to_remove"] is False


def test_survey_merged_clean_linked_is_safe(shared_repo, tmp_path):
    # base(HEAD)から枝分かれしたままのクリーンな worktree → merged・not dirty → safe。
    wt = tmp_path / "wtm"
    C.worktree_add(str(wt), "feat/merged", "HEAD", repo_path=str(shared_repo))
    rows = C.survey_worktrees(repo_path=str(shared_repo))
    row = _find(rows, wt)
    assert row is not None
    assert row["is_main"] is False
    assert row["dirty"] is False
    assert row["merged_into_base"] is True
    assert row["safe_to_remove"] is True


def test_survey_dirty_linked_is_unsafe(shared_repo, tmp_path):
    # 未コミットの変更があれば、たとえ merged でも削除は安全でない。
    wt = tmp_path / "wtd"
    C.worktree_add(str(wt), "feat/dirty", "HEAD", repo_path=str(shared_repo))
    (wt / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
    rows = C.survey_worktrees(repo_path=str(shared_repo))
    row = _find(rows, wt)
    assert row is not None
    assert row["dirty"] is True
    assert row["safe_to_remove"] is False


def test_survey_unmerged_clean_linked_is_unsafe(shared_repo, tmp_path):
    # base に取り込まれていないコミットが乗っている→削除で未マージの作業を失う→unsafe。
    wt = tmp_path / "wtu"
    C.worktree_add(str(wt), "feat/unmerged", "HEAD", repo_path=str(shared_repo))
    (wt / "n.txt").write_text("new\n", encoding="utf-8")
    _git(wt, "add", "n.txt")
    _git(wt, "commit", "-m", "ahead of base")
    rows = C.survey_worktrees(repo_path=str(shared_repo))
    row = _find(rows, wt)
    assert row is not None
    assert row["dirty"] is False
    assert row["merged_into_base"] is False
    assert row["safe_to_remove"] is False


def test_survey_is_read_only_no_worktree_removed(shared_repo, tmp_path):
    # survey の前後で worktree 本数が変わらない(何も削除しない)ことを実 git で確かめる。
    wt = tmp_path / "wtro"
    C.worktree_add(str(wt), "feat/ro", "HEAD", repo_path=str(shared_repo))
    before = set(_worktrees(shared_repo))
    C.survey_worktrees(repo_path=str(shared_repo))
    after = set(_worktrees(shared_repo))
    assert before == after
    assert wt.is_dir()   # survey は対象を消さない


def test_survey_never_returns_safe_for_locked(shared_repo, tmp_path):
    wt = tmp_path / "wtl"
    C.worktree_add(str(wt), "feat/locked", "HEAD", repo_path=str(shared_repo))
    _git(shared_repo, "worktree", "lock", str(wt))
    try:
        rows = C.survey_worktrees(repo_path=str(shared_repo))
        row = _find(rows, wt)
        assert row is not None
        assert row["locked"] is True
        assert row["safe_to_remove"] is False
    finally:
        _git(shared_repo, "worktree", "unlock", str(wt))
