# -*- coding: utf-8 -*-
"""tools/source_text.code_only: 説明文を検査に混ぜない。

この関数は「コードについての検査」が**説明で満たされたり弾かれたりしない**ことだけを
担っている。2026-09-18 に同じクラスの誤検知を1日で4回踏み、4回目は3回目の修正が1つの
テストファイル内の private ヘルパだったために起きた。だから共有モジュールになり、
だから**それ自身にテストが要る** — ガードは機能と同じ根拠を必要とする。
"""
from __future__ import annotations

import io
import os

from tools.source_text import code_only


def _w(tmp_path, text):
    p = tmp_path / "m.py"
    io.open(str(p), "w", encoding="utf-8", newline="\n").write(text)
    return str(p)


def test_a_module_docstring_cannot_answer_a_check_about_code(tmp_path):
    p = _w(tmp_path, '"""We must never send FileBase64 by hand."""\nx = 1\n')
    assert "FileBase64" not in code_only(p)
    assert "x = 1" in code_only(p)


def test_a_function_docstring_is_removed_too(tmp_path):
    p = _w(tmp_path, 'def f():\n    """explains FileBase64"""\n    return 1\n')
    assert "FileBase64" not in code_only(p)
    assert "return 1" in code_only(p)


def test_a_class_docstring_is_removed_too(tmp_path):
    p = _w(tmp_path, 'class C:\n    """about FileBase64"""\n    v = 2\n')
    assert "FileBase64" not in code_only(p)
    assert "v = 2" in code_only(p)


def test_a_multi_line_docstring_is_removed_entirely(tmp_path):
    p = _w(tmp_path, '"""line one\nline two FileBase64\nline three"""\ny = 3\n')
    out = code_only(p)
    assert "FileBase64" not in out and "line three" not in out
    assert "y = 3" in out


def test_comments_go_as_well(tmp_path):
    p = _w(tmp_path, "# never rebuild FileBase64\nz = 4\n")
    assert "FileBase64" not in code_only(p)


def test_a_real_string_literal_in_code_SURVIVES(tmp_path):
    """検査が探しているのはこちら。全部の文字列を消したら、
    `headers["Authorization"] = ...` を固定するテストが誰も守らなくなる。"""
    p = _w(tmp_path, 'headers["Authorization"] = "Bearer " + token\n')
    assert 'headers["Authorization"] = "Bearer " + token' in code_only(p)


def test_line_numbering_is_preserved(tmp_path):
    """docstring を*削る*と行番号がずれ、検査の指す場所が嘘になる。空行に置き換える。"""
    p = _w(tmp_path, '"""a\nb\nc"""\nlast = 1\n')
    assert code_only(p).splitlines()[3] == "last = 1"


def test_a_file_that_does_not_parse_still_loses_its_comments(tmp_path):
    """壊れたファイルで生ソースを返すと、この関数が防いでいる失敗がそのまま戻る。"""
    p = _w(tmp_path, "# FileBase64 in a comment\ndef broken(:\n")
    assert "FileBase64" not in code_only(p)


def test_it_reads_this_repository_s_real_module():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = code_only(os.path.join(here, "relay", "socket_attachment.py"))
    assert "FileBase64" not in out, "本番モジュールの説明文が検査に混ざる"
    assert 'headers["Authorization"] = "Bearer " + token' in out
