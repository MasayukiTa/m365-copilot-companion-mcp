# -*- coding: utf-8 -*-
"""A patch is arbitrary bytes, and one of them threw away 60 solved instances.

MEASURED. The n=100 A/B died at 20:27:56 on 2026-09-11, three chunks in, having captured 60:

    Exception in thread Thread-183 (_readerthread):
      UnicodeDecodeError: 'cp932' codec can't decode byte 0x9c in position 370
    Traceback (most recent call last):
      File "bench\\swe_solve_decoupled.py", line 259, in main
        ne = capture(ch)
      File "bench\\swe_solve_decoupled.py", line 152, in capture
        if diff.strip():
    AttributeError: 'NoneType' object has no attribute 'strip'
    [20:27:56] ON solve did not reach its done marker (rc=1); aborting

THE CHAIN, and why the existing try/except could not help. `capture()` ran `git diff` with
`text=True` and no encoding, so Python decoded the patch with `locale.getpreferredencoding()` --
cp932 on this machine. One byte that is not valid cp932 killed subprocess's reader THREAD.
`run()` returned normally, with `.stdout` set to None. The parent saw no exception at all, so
the `except Exception` around it never fired; `diff.strip()` raised instead, and the orchestrator
exited rc=1, which made the loop abort the whole arm.

The failure mode is the worst available: not a mangled character, but the loss of every instance
in the run.

tests/test_child_output_decoding.py already exists for this class and states the rule --
"出力の一部が化けるより、出力ごと消えるほうが困る" -- and tools/code_exec._decode already
implements it. This caller was simply never swept.

A REPO-WIDE SWEEP FOUND 68 MORE. `subprocess.run(..., text=True)` with no `encoding=` and no
`errors=` appears 68 times across bench/, relay/, tools/ and scripts/. Most read ASCII (git SHAs,
version strings) and are harmless in practice; this one read a patch. The rest are listed in the
session record and are deliberately NOT changed here -- a mechanical 68-site edit does not belong
in the same commit as the fix for the one that actually failed.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: The exact byte from the incident. 0x9c is a valid lead byte in cp932 but not a valid
#: standalone character, and it is ordinary in a UTF-8 patch or in binary content.
KILLER = b"diff --git a/x b/x\n+\x9c not valid cp932\n"


def _decode_child():
    from bench import swe_solve_decoupled as S
    return S._decode_child


# ── the decoder ───────────────────────────────────────────────────────────────────────────

def test_the_incident_byte_no_longer_raises():
    assert "diff --git" in _decode_child()(KILLER)


def test_a_utf8_patch_survives_intact():
    """UTF-8 is tried FIRST, so a patch with real Japanese in it is not mangled into the local
    codepage on the way through."""
    text = "diff --git a/x b/x\n+銅箔検査 148 件\n"
    assert "銅箔検査 148 件" in _decode_child()(text.encode("utf-8"))


def test_empty_and_none_are_empty_strings():
    assert _decode_child()(b"") == ""
    assert _decode_child()(None) == ""


def test_it_never_raises_on_any_byte():
    """The whole point. Every byte 0-255, in a sequence no codec accepts as a whole."""
    blob = bytes(range(256)) * 3
    out = _decode_child()(blob)
    assert isinstance(out, str)


# ── the capture path ──────────────────────────────────────────────────────────────────────

def test_capture_reads_the_patch_as_bytes_not_platform_text():
    """The defect was `text=True` with no encoding on the git diff call. Comments are stripped
    first: the fix's own comment quotes `text=True` to explain what it replaced, and matching
    that would make this test pass for the wrong reason."""
    src = open(os.path.join(REPO, "bench", "swe_solve_decoupled.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    m = re.search(r'subprocess\.run\(\["git", "-C", wt, "diff"\][^)]*\)', body, re.S)
    assert m, "the git diff capture call is gone; this test no longer guards anything"
    assert "text=True" not in m.group(0), (
        "the patch is being decoded with the platform codepage again")


def test_a_none_stdout_cannot_reach_strip():
    """A SEPARATE failure from the decoding. When the reader thread dies the parent sees no
    exception and .stdout is None, so the guard has to exist independently of the decoder."""
    src = open(os.path.join(REPO, "bench", "swe_solve_decoupled.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "(diff or \"\").strip()" in body, (
        "diff.strip() is unguarded again; a None from any path would abort the whole arm")


def test_the_real_subprocess_path_survives_the_killer_byte(tmp_path):
    """End to end through a real child process, because the failure lived in subprocess's
    reader thread rather than in any function this repository wrote."""
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(%r)\n" % KILLER, encoding="utf-8")
    raw = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=60).stdout
    assert raw is not None, "the child produced nothing; this test is not exercising the path"
    out = _decode_child()(raw)
    assert "diff --git" in out


def test_the_old_way_really_did_lose_everything(tmp_path):
    """Guards against the reproduction being theatre: with text=True and the cp932 codepage,
    the same bytes lose the ENTIRE output rather than one character.

    Skipped where cp932 is not available (CI runs on Linux), because the point is about a
    codepage that cannot represent the byte, not about Windows.
    """
    try:
        KILLER.decode("cp932")
        pytest.skip("this byte is decodable here, so there is nothing to demonstrate")
    except (UnicodeDecodeError, LookupError) as exc:
        if isinstance(exc, LookupError):
            pytest.skip("cp932 is not available in this environment")
    script = tmp_path / "emit.py"
    script.write_text("import sys\nsys.stdout.buffer.write(%r)\n" % KILLER, encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, encoding="cp932", timeout=60)
    assert r.stdout is None or "diff --git" not in (r.stdout or ""), (
        "the old path did not lose the output, so the incident is not reproduced here")


# ── the same defect in the grade path ─────────────────────────────────────────────────────
#
# Fixing only the solve caller would have left the identical line where the run goes next.
# bench/swe_check_remote.py:147 was the SAME call on the SAME data, and _ssh_ps (line 92) is
# worse in a different way: it already guarded with `(r.stdout or "")`, so a decode failure
# did not crash -- it returned "", retried, gave up, and the caller reported ZERO REAL
# VERDICTS, which loop.py logs as "the eval host unreachable". A host answering perfectly
# would be recorded as down and an entire arm thrown away.

def _remote_decode():
    from bench import swe_check_remote as R
    return R._decode_child


def test_the_grade_path_has_the_same_decoder():
    assert "diff --git" in _remote_decode()(KILLER)
    assert _remote_decode()(b"") == "" and _remote_decode()(None) == ""


def test_the_grade_path_keeps_utf8_intact():
    """The eval host's output carries test names and tracebacks. Mangling them into the local
    codepage would corrupt the verdicts this run is trying to produce."""
    assert _remote_decode()("検査 148 件".encode("utf-8")) == "検査 148 件"


@pytest.mark.parametrize("needle", ["_ssh_ps", "diff"])
def test_the_grade_path_no_longer_decodes_with_the_platform_codepage(needle):
    """Comments stripped first -- the fix's own comments name text=True to explain what they
    replaced, and matching those would pass for the wrong reason."""
    src = open(os.path.join(REPO, "bench", "swe_check_remote.py"), encoding="utf-8").read()
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    if needle == "_ssh_ps":
        m = re.search(r"r = subprocess\.run\(_SSH_BASE.*?\)\n", body, re.S)
    else:
        m = re.search(r'subprocess\.run\(\["git", "-C", wt, "diff"\].*?\)', body, re.S)
    assert m, "the %s call is gone; this test no longer guards anything" % needle
    assert "text=True" not in m.group(0), (
        "%s decodes with the platform codepage again" % needle)


def test_a_silent_empty_is_the_failure_mode_worth_naming():
    """Pins WHY _ssh_ps mattered more than the crash: its guard turns a decode failure into an
    empty string, and an empty string there is indistinguishable from an unreachable host."""
    src = open(os.path.join(REPO, "bench", "swe_check_remote.py"), encoding="utf-8").read()
    assert '_decode_child(r.stdout)' in src, (
        "_ssh_ps no longer decodes tolerantly; a bad byte would read as an unreachable host")
