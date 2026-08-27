"""The identity check: does it fail CLOSED, and does it say when it could not look?

A checker whose failure mode is "reports nothing found" is worse than no checker, because it
converts an unperformed check into a clean result. This one guards a rule that has already
been broken twice in a public repository, so every path that could produce a false pass is
tested here rather than reasoned about.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_names", os.path.join(os.path.dirname(__file__), "check_no_identifying_names.py"))
C = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(C)

#: An id of the SHAPE under test, belonging to nobody, ASSEMBLED FROM FRAGMENTS so that no
#: token in this file matches its own pattern.
#:
#: The first version used the real id as a fixture -- in a public repository, in the very file
#: whose job is to keep it out -- and CI caught it, which is the check working. The second used
#: a synthetic one written out in full, and CI caught that too, which is also the check
#: working: it cannot tell whose id an id is. A test needs the shape, never an instance of it.
SYNTHETIC_ID = "Q470" + "B2951"


def _repo(files):
    d = tempfile.mkdtemp(prefix="idcheck_")
    for name, text in files.items():
        full = os.path.join(d, name)
        os.makedirs(os.path.dirname(full) or d, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    subprocess.run(["git", "init", "-q", d], check=False)
    subprocess.run(["git", "-C", d, "add", "-A"], check=False)
    return d


# ---- what it catches ----------------------------------------------------------------------

def test_an_employee_id_is_caught_by_shape():
    d = _repo({"a.py": 'OWNER = "%s"\n' % SYNTHETIC_ID})
    assert [f[1] for f in C.offences(d)] == ["employee-id shape"]


def test_a_home_directory_names_whoever_owns_it():
    # Assembled, for the same reason as SYNTHETIC_ID: a fixture spelled out in full is an
    # instance of exactly what the check exists to find, and this file would then fail it.
    home = "C:/Users" + "/somebody/project"
    d = _repo({"a.py": 'P = "%s"\n' % home})
    assert C.offences(d)


def test_a_placeholder_home_is_not_flagged():
    """全部に火が付く検査は読み飛ばされる。"""
    d = _repo({"a.py": 'P = "C:/Users/Public/x"\nQ = "C:/Users/example/y"\n'})
    assert C.offences(d) == []


def test_a_configured_name_is_caught_in_content_and_in_the_path():
    d = _repo({"acme_notes.md": "nothing here\n", "b.py": "# acme was here\n"})
    got = {f[0] for f in C.offences(d, names=["acme"])}
    assert got == {"acme_notes.md", "b.py"}


def test_a_file_type_outside_any_whitelist_is_still_read():
    """拡張子の許可リストは、そこに無い形式を丸ごと見逃す。"""
    d = _repo({"data.csv": "id,owner\n1,%s\n" % SYNTHETIC_ID,
               "Makefile": "OWNER=%s\n" % SYNTHETIC_ID})
    assert {f[0] for f in C.offences(d)} == {"data.csv", "Makefile"}


# ---- what it must never do -----------------------------------------------------------------

def test_a_failed_git_call_is_not_a_clean_result():
    """git が失敗して空リストを返すと『0件中0件が問題なし』= 合格になっていた。"""
    d = tempfile.mkdtemp(prefix="notarepo_")
    with pytest.raises(C.CheckFailed):
        C.tracked_files(d)


def test_an_empty_repository_is_refused_rather_than_passed():
    d = tempfile.mkdtemp(prefix="emptyrepo_")
    subprocess.run(["git", "init", "-q", d], check=False)
    with pytest.raises(C.CheckFailed):
        C.tracked_files(d)


def test_an_unreadable_file_is_reported_rather_than_skipped():
    """見られなかったファイルは、見て問題が無かったファイルではない。"""
    d = _repo({"a.py": "ok\n"})
    os.remove(os.path.join(d, "a.py"))          # tracked, now unreadable
    got = C.offences(d)
    assert got and "unreadable" in got[0][1]


def test_strict_mode_refuses_to_pass_without_the_names(monkeypatch):
    """秘密が無い = 検査が無い。それを『合格』と印字するなら、絶対と呼んだ規則は
    誰かが変数を設定したときだけ効いている。"""
    monkeypatch.delenv(C.NAMES_ENV, raising=False)
    d = _repo({"a.py": "clean\n"})
    assert C.main([d]) == 0
    assert C.main([d, "--require-names"]) == 2


def test_the_check_reports_its_own_inability_with_a_distinct_code(monkeypatch):
    """『問題なし(0)』と『実行できなかった(2)』は別の答え。"""
    d = tempfile.mkdtemp(prefix="notarepo2_")
    monkeypatch.setenv(C.NAMES_ENV, "acme")
    assert C.main([d]) == 2


def test_a_real_looking_account_name_is_not_on_the_placeholder_list():
    """alice / bob / runner / administrator は誰かの実アカウント名でありうる。
    本物を飲み込む placeholder 一覧は、一覧が無いのと同じ失敗。"""
    for name in ("alice", "bob", "runner", "administrator"):
        assert name not in C.NON_IDENTIFYING_USERS


# ---- the shape itself, which was wrong ------------------------------------------------------

def test_the_shape_matches_an_id_with_letters_among_the_digits():
    r"""最初の形は `[A-Z]\d{6,}` -- 英字1文字のあと数字が6桁以上。
    実IDは英字と数字が交互で、捕まえるために書いた検査が捕まえられなかった。
    リポジトリを通し続け、漏洩を拾ったのは偶然ホームパスの規則の方だった。"""
    assert C.ID_SHAPE.search(SYNTHETIC_ID)
    assert C.ID_SHAPE.search("A12" + "3456")   # the letter-then-digits form


def test_the_shape_does_not_fire_on_ordinary_upper_case_words():
    """行のどこかに数字が5つあるだけで FASTEST が一致していた --
    語の外まで数える lookahead で、リポジトリ全体に33件の誤検出。"""
    line = "the FASTEST run was 2026-01-01 with 5 of 22 passing"
    assert not C.ID_SHAPE.search(line)
    for word in ("CONFLICT", "EXPECTED", "IMPORTANT", "SECURITY"):
        assert not C.ID_SHAPE.search("%s and 12345 here" % word), word


def test_a_guid_fragment_is_not_an_employee_id():
    """GUID の第1グループは8桁16進で、id の条件を全て満たす。"""
    assert not C.ID_SHAPE.search('new Guid("C2F03A33-21F5-47FA-B4BB-156357C4")')


def test_a_git_sha_and_a_standard_name_are_not_ids():
    for token in ("a1872637", "5c76f59", "SHA256", "ISO8601", "M365", "HTTP200"):
        assert not C.ID_SHAPE.search("see %s" % token), token
