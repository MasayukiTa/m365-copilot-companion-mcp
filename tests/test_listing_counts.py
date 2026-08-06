"""一覧系ツールが件数を自分で返すことを固定する。

件数を返していなかった頃、呼び出し側は一覧を目で数えていて、同じ問い
(「Desktop 直下の .md は何個か」)に 14/15/16/17/18 と答えが揺れた。正解は16。
数えるのはツールの仕事なので、先頭行の件数はツールの契約として扱う。
"""
import os

from tools.file_ops import list_directory
from tools.search_ops import glob


def _make(tmp_path, n_md=3, n_other=2, n_dir=1):
    for i in range(n_md):
        (tmp_path / f"a{i}.md").write_text("x", encoding="utf-8")
    for i in range(n_other):
        (tmp_path / f"b{i}.txt").write_text("x", encoding="utf-8")
    for i in range(n_dir):
        (tmp_path / f"d{i}").mkdir()
    return tmp_path


def test_glob_reports_count_first(tmp_path):
    _make(tmp_path, n_md=7)
    out = glob("*.md", str(tmp_path))
    assert out.splitlines()[0] == f"7 matches under {tmp_path}"
    # 件数と実際に並ぶ行数が食い違うと、読み手はどちらを信じるか迷う
    assert len(out.splitlines()) == 8


def test_glob_zero_is_a_number_not_prose(tmp_path):
    # "(no matches)" だと「何個？」に数字で答えられない
    assert glob("*.md", str(tmp_path)) == f"0 matches under {tmp_path}"


def test_glob_truncation_says_more_exist(tmp_path):
    _make(tmp_path, n_md=10, n_other=0, n_dir=0)
    head = glob("*.md", str(tmp_path), max_results=4).splitlines()[0]
    assert head.startswith("4 matches")
    # 上限で切ったことを言わないと、4個しか無いと読まれる
    assert "more exist" in head


def test_list_directory_separates_files_and_dirs(tmp_path):
    _make(tmp_path, n_md=3, n_other=2, n_dir=4)
    assert list_directory(str(tmp_path)).splitlines()[0] == f"5 files, 4 directories in {tmp_path}"


def test_list_directory_pattern_counts_only_matches(tmp_path):
    """絞り込んだ件数が機械から出ること。

    絞り込めなかった頃、glob の正しい件数を得た後にそれを疑い、混ざった一覧から
    .md 行を拾い直して 16 を 15 と答えた回があった。
    """
    _make(tmp_path, n_md=6, n_other=4, n_dir=3)
    out = list_directory(str(tmp_path), pattern="*.md")
    assert out.splitlines()[0] == f"6 files matching *.md in {tmp_path}"
    # ディレクトリが混ざると、拾い直す作業がまた発生する
    assert "[DIR]" not in out


def test_list_directory_pattern_agrees_with_glob(tmp_path):
    _make(tmp_path, n_md=9, n_other=5, n_dir=2)
    n_glob = glob("*.md", str(tmp_path)).splitlines()[0].split()[0]
    n_list = list_directory(str(tmp_path), pattern="*.md").splitlines()[0].split()[0]
    assert n_glob == n_list == "9"


def test_list_directory_pattern_no_match_is_zero(tmp_path):
    _make(tmp_path, n_md=2)
    assert list_directory(str(tmp_path), pattern="*.zzz").startswith("0 files matching")


def test_list_directory_empty_still_gives_numbers(tmp_path):
    assert list_directory(str(tmp_path)).startswith("0 files, 0 directories")


def test_counts_are_reproducible(tmp_path):
    """同じ入力で数が揺れないこと。揺れていたのが元の不具合。"""
    _make(tmp_path, n_md=6)
    heads = {glob("*.md", str(tmp_path)).splitlines()[0] for _ in range(5)}
    assert heads == {f"6 matches under {tmp_path}"}


def test_pattern_may_carry_the_whole_path(tmp_path):
    """glob("<dir>/*.md") が 0 を返さないこと。

    返していた頃、呼び出し側はこれを最初に試し、0 を見て「~ が展開されない」と
    判断し、list_directory に切り替えて目視で数え、答えを外していた。
    """
    _make(tmp_path, n_md=5)
    assert glob(f"{tmp_path}/*.md").splitlines()[0] == f"5 matches under {tmp_path}"
    assert glob(f"{tmp_path}\\*.md").splitlines()[0] == f"5 matches under {tmp_path}"
    # 根つきパターンは場所を言い切っているので、別の path を渡されても勝つ
    assert glob(f"{tmp_path}/*.md", str(tmp_path.parent)).splitlines()[0] == f"5 matches under {tmp_path}"


def test_head_says_where_it_looked(tmp_path):
    """件数だけでなく、どこの件数かを言うこと。

    既定の "." はサーバの作業ディレクトリで、呼び出し側の意図とまず一致しない。
    場所を書いていなかった頃、そこの結果を見て混乱し、やり直しの過程で手計算に
    落ちて 16 を 15 と答えた回があった。
    """
    _make(tmp_path, n_md=2)
    assert str(tmp_path) in glob("*.md", str(tmp_path)).splitlines()[0]
    assert str(tmp_path) in list_directory(str(tmp_path)).splitlines()[0]
    # 既定でも、どこを見たかは黙らない
    assert " under " in glob("*.nothing_matches_this").splitlines()[0]


def test_home_relative_pattern_expands(tmp_path, monkeypatch):
    # ホームを差し替えて確かめる。実際のホームに何かある前提で書くと、空の環境
    # （CI がそう）で落ちる。見たいのは ~ が展開されることだけ。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _make(tmp_path, n_md=2, n_other=1, n_dir=0)
    head = glob("~/*").splitlines()[0]
    assert head.split()[1] == "matches"
    assert head.split()[0] == "3"
    assert str(tmp_path) in head


def test_missing_directory_is_not_zero_matches(tmp_path):
    """綴り違いが「該当なし」に化けないこと。"""
    out = glob(f"{tmp_path}/nosuchdir/*.md")
    assert "not found" in out
    assert "0 matches" not in out


def test_real_desktop_count_matches_filesystem():
    """本番と同じ経路。ツールの件数が os.listdir と一致すること。"""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desktop):
        return
    truth = len([f for f in os.listdir(desktop)
                 if f.lower().endswith(".md")
                 and os.path.isfile(os.path.join(desktop, f))])
    assert glob("*.md", desktop).splitlines()[0] == f"{truth} matches under {desktop}"
