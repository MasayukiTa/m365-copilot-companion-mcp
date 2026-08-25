"""grep のフォールバックが、サーバのメモリを1回の呼び出しで持っていかないこと。

2026-08-25 実測。`rg` が PATH に無いのでフォールバックが常用経路になっており、そこが
`read_text().splitlines()` -- ファイル全文の str と、その上に全行の list -- を作っていた。
リポジトリには 48MB の faulthandler.log があり、フリートは複数の AnyIO ワーカースレッドから
同時にこのツールを呼ぶ。MCP サーバは 222MB から5分で 2.4GB、やがて 5GB を超え、空きRAMが
フリート自身のリサイクル閾値を割り、走行が共有ブラウザを hard-reset して兄弟走行を巻き込んだ。
生きたプロセスに py-spy を当てて、この行が名指しされた。
"""
import pathlib

import pytest

from tools import coding_ops


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
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 3


def test_a_binary_file_does_not_take_the_search_down(monkeypatch, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x01needle")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    out = coding_ops.grep("needle", str(tmp_path))
    assert "a.txt" in out
    assert "error" not in out.lower()


def test_the_cap_is_configurable_without_editing_source():
    import inspect

    src = inspect.getsource(coding_ops)
    assert "MCP_GREP_MAX_FILE_MB" in src
