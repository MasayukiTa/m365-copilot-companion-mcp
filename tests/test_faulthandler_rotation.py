"""The forensic dump has to have an end.

_install_faulthandler writes every thread's stack every five minutes and appended for ever.
The file was found at 51.8 MB with nothing to stop it, on a machine whose disk floor is
single-digit gigabytes and which spent the same day deferring fleet admission because the
disk was tight. A diagnostic with no bound eventually costs the thing it diagnoses.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _install_src():
    src = open(MAIN, encoding="utf-8").read()
    i = src.index("def _install_faulthandler")
    return src[i:src.index("\ndef ", i + 10)]


def test_the_dump_is_rotated_not_appended_for_ever():
    body = _install_src()
    assert "rename" in body, "nothing moves the old file aside"
    assert "MCP_FAULTHANDLER_MAX_BYTES" in body, "the cap must be adjustable without an edit"


def test_only_one_generation_is_kept():
    """Two files at the cap is the bound; a growing family of them is not."""
    body = _install_src()
    assert "unlink" in body, "the previous generation must be dropped, not accumulated"


def test_rotation_never_stops_the_server_starting():
    """A failure to tidy a log is not a reason to refuse to boot."""
    body = _install_src()
    rotate = body[body.index("ROTATE"):body.index("Keep a handle open")]
    assert "except Exception" in rotate


def test_the_cap_is_a_real_number_and_not_absurd():
    body = _install_src()
    m = re.search(r'"MCP_FAULTHANDLER_MAX_BYTES",\s*str\(([^)]+)\)', body)
    assert m, "the default cap must be visible in the source"
    cap = eval(m.group(1), {"__builtins__": {}})
    assert 1024 * 1024 <= cap <= 64 * 1024 * 1024, cap
