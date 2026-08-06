"""子プロセスの出力が、符号化の違いで消えないことを固定する。

text=True はその場のコードページ（日本語Windowsなら cp932）で復号する。UTF-8 で
出力するスクリプトを走らせると例外になり、出力が丸ごと消えた。実際、returncode 0
なのに「cp932 で復号できない」で落ち、呼び出し側はファイルへ迂回した。
出力の一部が化けるより、出力ごと消えるほうが困る。
"""
import subprocess
import sys

from tools.code_exec import _decode, _format_output


def _run(code, encoding):
    src = ("import sys;sys.stdout.reconfigure(encoding=%r);print(%r)" % (encoding, code))
    return subprocess.run([sys.executable, "-c", src], capture_output=True)


def test_utf8_child_is_readable():
    assert "銅箔検査 148 件" in _format_output(_run("銅箔検査 148 件", "utf-8"), "t")


def test_codepage_child_is_readable():
    assert "日本語です" in _format_output(_run("日本語です", "cp932"), "t")


def test_undecodable_bytes_do_not_lose_the_output():
    # 読めない並びが混ざっても、残りは読めること
    raw = b"OK-\xff\xfe-END"
    got = _decode(raw)
    assert "OK-" in got and "-END" in got


def test_empty_output_is_empty_string():
    assert _decode(b"") == ""


def test_returncode_is_reported():
    r = subprocess.run([sys.executable, "-c", "raise SystemExit(3)"], capture_output=True)
    assert "[returncode: 3]" in _format_output(r, "t")


def test_no_output_says_so():
    r = subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    assert _format_output(r, "t") == "(no output)"


def test_stderr_is_kept_separately():
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.stderr.reconfigure(encoding='utf-8');sys.stderr.write('警告')"],
        capture_output=True)
    out = _format_output(r, "t")
    assert "[stderr]" in out and "警告" in out
