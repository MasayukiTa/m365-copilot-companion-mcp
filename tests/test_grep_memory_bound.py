"""検索系ツールが、1回の呼び出しでサーバのメモリを持っていかないこと（grep / find_files）。

2026-08-25 実測。`rg` が PATH に無いのでフォールバックが常用経路になっており、そこが
`read_text().splitlines()` -- ファイル全文の str と、その上に全行の list -- を作っていた。
リポジトリには 48MB の faulthandler.log があり、フリートは複数の AnyIO ワーカースレッドから
同時にこのツールを呼ぶ。MCP サーバは 222MB から5分で 2.4GB、やがて 5GB を超え、空きRAMが
フリート自身のリサイクル閾値を割り、走行が共有ブラウザを hard-reset して兄弟走行を巻き込んだ。
生きたプロセスに py-spy を当てて、この行が名指しされた。
"""
import pathlib

import pytest

from tools import coding_ops, search_ops


@pytest.fixture(autouse=True)
def _allow_tmp(monkeypatch, tmp_path):
    """_validate_path は利用者ディレクトリ配下しか許さない。ここで見たいのは別のこと。"""
    monkeypatch.setattr(coding_ops, "_validate_path", lambda p: pathlib.Path(p))
    monkeypatch.setattr(coding_ops.shutil, "which", lambda name: None)  # 常にフォールバック


def _no_whole_file_reads(monkeypatch):
    def boom(self, *a, **kw):
        raise AssertionError("read_text: ファイルを丸ごとメモリに載せている")

    monkeypatch.setattr(pathlib.Path, "read_text", boom)


def test_it_never_reads_a_whole_file_into_memory(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    _no_whole_file_reads(monkeypatch)
    out = coding_ops.grep("beta", str(tmp_path))
    assert "beta" in out


def test_a_file_over_the_cap_is_not_opened(monkeypatch, tmp_path):
    big = tmp_path / "huge.log"
    big.write_text("needle\n" * 2000, encoding="utf-8")
    monkeypatch.setattr(coding_ops, "_GREP_MAX_FILE_BYTES", 100)

    import builtins

    real_open = builtins.open

    def guard(path, *a, **kw):
        assert str(path) != str(big), "上限を超えたファイルを開いている"
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", guard)
    coding_ops.grep("needle", str(tmp_path))


def test_skipping_a_big_file_is_reported_not_silent(monkeypatch, tmp_path):
    """黙って飛ばすと『(no matches)』になる。grep が絶対に偽ってはいけない答え。"""
    (tmp_path / "huge.log").write_text("needle\n" * 2000, encoding="utf-8")
    monkeypatch.setattr(coding_ops, "_GREP_MAX_FILE_BYTES", 100)
    out = coding_ops.grep("needle", str(tmp_path))
    assert "not searched" in out, "飛ばしたことがどこにも出ていない"
    assert "1 file(s)" in out


def test_a_small_file_is_still_searched_alongside_a_skipped_one(monkeypatch, tmp_path):
    (tmp_path / "huge.log").write_text("needle\n" * 2000, encoding="utf-8")
    (tmp_path / "small.txt").write_text("a needle here\n", encoding="utf-8")
    monkeypatch.setattr(coding_ops, "_GREP_MAX_FILE_BYTES", 100)
    out = coding_ops.grep("needle", str(tmp_path))
    assert "small.txt" in out and "not searched" in out


def test_max_matches_still_stops_early(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("hit\n" * 50, encoding="utf-8")
    out = coding_ops.grep("hit", str(tmp_path), max_matches=3)
    # 一致行だけ数える。全非空行を数えていて、開示の1行が足された途端に落ちた
    # -- 検査していたのは上限であって、出力の行数ではない。
    assert len([ln for ln in out.splitlines() if ln.startswith(str(tmp_path))]) == 3


def test_a_binary_file_does_not_take_the_search_down(monkeypatch, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x01needle")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    out = coding_ops.grep("needle", str(tmp_path))
    assert "a.txt" in out
    assert not out.startswith("[grep error:"), out
    # 途中で読めなくなったファイルがあったことは**述べる**。以前この検査は
    # "error" が本文に出ないことを見ていて、開示そのものを禁じてしまっていた。
    assert "decoding error" in out


def test_the_cap_is_configurable_without_editing_source():
    import inspect

    src = inspect.getsource(coding_ops)
    assert "MCP_GREP_MAX_FILE_MB" in src


# ---- find_files も同じ欠陥を持っていた -------------------------------------------------------
#
# grep を直した直後の py-spy ダンプで、3スレッドが find_files の内包表記の中にいた。
# 木全体のマッチを一旦すべてリストに積み、stat() でソートしてから max_results に切っていたので、
# 呼び出し側の上限はピークを何も縛っていなかった。最初のダンプにも写っていたのに拾わなかった。


def test_find_files_holds_only_the_answer_not_the_whole_tree(monkeypatch, tmp_path):
    import pathlib as _pl

    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    for i in range(50):
        (tmp_path / ("hit_%02d.txt" % i)).write_text("x", encoding="utf-8")

    held = []
    real_push = search_ops.heapq.heappush

    def spy(heap, item):
        real_push(heap, item)
        held.append(len(heap))

    monkeypatch.setattr(search_ops.heapq, "heappush", spy)
    out = search_ops.find_files("hit_", str(tmp_path), max_results=5)
    assert max(held) <= 5, "上限を超えて保持している: %d" % max(held)
    assert "truncated at 5" in out


def test_find_files_still_returns_the_newest_across_the_whole_tree(monkeypatch, tmp_path):
    """上位N件だけ保持しても、答えは木全体の最新N件でなければならない。"""
    import os
    import pathlib as _pl

    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    for i in range(10):
        f = tmp_path / ("hit_%02d.txt" % i)
        f.write_text("x", encoding="utf-8")
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    out = search_ops.find_files("hit_", str(tmp_path), max_results=3)
    lines = [ln for ln in out.splitlines() if ln.startswith(str(tmp_path))]
    assert [_pl.Path(ln).name for ln in lines] == ["hit_09.txt", "hit_08.txt", "hit_07.txt"]


def test_find_files_says_nothing_when_nothing_matches(monkeypatch, tmp_path):
    import pathlib as _pl

    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")
    assert search_ops.find_files("hit_", str(tmp_path)) == "(no matches)"


def test_find_files_skips_a_directory_named_like_the_needle(monkeypatch, tmp_path):
    """S_ISREG に置き換えたので、ディレクトリを取りこぼさず弾けていること。"""
    import pathlib as _pl

    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    (tmp_path / "hit_dir").mkdir()
    (tmp_path / "hit_file.txt").write_text("x", encoding="utf-8")
    out = search_ops.find_files("hit_", str(tmp_path))
    assert "hit_file.txt" in out and "hit_dir" not in out


def test_the_skip_note_survives_the_early_return(monkeypatch, tmp_path):
    """max_matches に達した時の早期 return がお知らせを落としていた。
    そこは「他にも一致がある」場面そのもので、読み手が最も結果を信じたい瞬間。"""
    import pathlib as _pl

    monkeypatch.setattr(coding_ops, "_validate_path", lambda p: _pl.Path(p))
    monkeypatch.setattr(coding_ops.shutil, "which", lambda name: None)
    monkeypatch.setattr(coding_ops, "_GREP_MAX_FILE_BYTES", 100)
    (tmp_path / "huge.log").write_text("needle\n" * 2000, encoding="utf-8")
    # 70 バイト -- 上限(100)の下。ここを 350 バイトにしていて両方飛ばされ、
    # 「お知らせは出たが一致が0件」という、検査したいのと別の状態を見ていた。
    (tmp_path / "small.txt").write_text("needle\n" * 10, encoding="utf-8")
    out = coding_ops.grep("needle", str(tmp_path), max_matches=3)
    assert "not searched" in out, "早期 return でお知らせが消えている"
    assert len([l for l in out.splitlines() if l.startswith(str(tmp_path))]) == 3


def test_find_files_keeps_the_first_of_equal_timestamps(monkeypatch, tmp_path):
    """同一秒のファイルだらけのディレクトリ（展開したアーカイブ、生成物）で効く。
    ヒープは最大N件を残すので、素の連番だと『最後に見た方が勝つ』になり、
    以前の安定ソートと逆になっていた。"""
    import os
    import pathlib as _pl

    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    names = ["hit_a.txt", "hit_b.txt", "hit_c.txt"]
    for n in names:
        f = tmp_path / n
        f.write_text("x", encoding="utf-8")
        os.utime(f, (1_700_000_000, 1_700_000_000))       # 全部同じ mtime
    out = search_ops.find_files("hit_", str(tmp_path), max_results=2)
    kept = [_pl.Path(l).name for l in out.splitlines() if l.startswith(str(tmp_path))]
    walked = [p.name for p in sorted(tmp_path.rglob("*")) if p.name.startswith("hit_")]
    assert set(kept) == set(walked[:2]), "同時刻のタイで後勝ちになっている: %s" % kept


# ---- 歩く範囲そのものを削る --------------------------------------------------------------------
#
# 2026-08-26 実測。保持量を縛る修正を2つ入れた後でも +260 MB/分 -- 元の 263 MB/分と同じだった。
# 1回の検索が 76,129 ファイルを歩いており、うち 61,757 が .venv、2,651 が .git。
# **何を保持するか**を縛っても、**何に触るか**は何も変わっていなかった。
# 剪定後: 9,946 ファイル、11.33秒 → 0.20秒。

from tools import walk as walk_mod


def test_the_vendored_world_is_not_walked(tmp_path):
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "hit.txt").write_text("needle", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hit.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hit.txt").write_text("needle", encoding="utf-8")
    found = {p.name + "|" + p.parent.name for p in walk_mod.iter_files(tmp_path)}
    assert "hit.txt|src" in found
    assert not any(f.endswith("|lib") or f.endswith("|.git") for f in found), found


def test_the_caller_can_ask_for_everything(tmp_path, monkeypatch):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hit.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("MCP_SEARCH_INCLUDE_ALL", "1")
    assert any(p.parent.name == ".venv" for p in walk_mod.iter_files(tmp_path))
    assert walk_mod.pruned_note() == ""


def test_pruning_is_disclosed_because_it_changes_what_can_be_found(monkeypatch, tmp_path):
    """『(no matches)』を信じる読み手には、どこを見ていないかが要る。"""
    monkeypatch.delenv("MCP_SEARCH_INCLUDE_ALL", raising=False)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hit.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "a.txt").write_text("nothing here", encoding="utf-8")
    out = coding_ops.grep("needle", str(tmp_path))
    assert ".venv" in out and "MCP_SEARCH_INCLUDE_ALL" in out


def test_nothing_is_claimed_about_a_tree_with_nothing_to_prune(monkeypatch, tmp_path):
    """設定だけを見て毎回同じ注記を出していた -- 単一ファイルの検索にも、
    剪定対象が1つも無い木にも。常に出る開示は読まれなくなる。"""
    monkeypatch.delenv("MCP_SEARCH_INCLUDE_ALL", raising=False)
    (tmp_path / "a.txt").write_text("needle", encoding="utf-8")
    out = coding_ops.grep("needle", str(tmp_path))
    assert "not searched" not in out, out


def test_a_single_file_search_claims_nothing_either(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_SEARCH_INCLUDE_ALL", raising=False)
    f = tmp_path / "a.txt"
    f.write_text("needle", encoding="utf-8")
    out = coding_ops.grep("needle", str(f))
    assert "not searched" not in out, out


def test_find_files_discloses_it_too(monkeypatch, tmp_path):
    import pathlib as _pl

    monkeypatch.delenv("MCP_SEARCH_INCLUDE_ALL", raising=False)
    monkeypatch.setattr(search_ops, "_validate_path", lambda p: _pl.Path(p))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "hit_x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "hit_a.txt").write_text("x", encoding="utf-8")
    out = search_ops.find_files("hit_", str(tmp_path))
    assert "node_modules" in out and "MCP_SEARCH_INCLUDE_ALL" in out


def test_a_walk_is_a_generator_not_a_list():
    """一覧を作った瞬間に、削ったはずの山が戻ってくる。"""
    import inspect

    assert inspect.isgeneratorfunction(walk_mod.iter_files)
