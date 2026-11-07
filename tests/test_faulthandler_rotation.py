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


# --- The crash dump must not be shredded by the periodic heartbeat ---------------
#
# faulthandler.enable() fires from whatever thread takes the fatal signal; the
# periodic dump_traceback_later() fires from its own watchdog thread every 5 min.
# faulthandler holds no lock between them, so when the two share one file handle and
# a crash lands mid-heartbeat, the dumps interleave byte-for-byte and the crash trace
# -- the only forensic record of a native death -- becomes unreadable. Observed
# 2026-11-07: a 0xc0000374 (heap corruption) crash dump came out as lines like
#   File "C:\\Users\\...\\ProgramsWindows fatal exception: code 0xc0000374
# The fix is to give enable() and dump_traceback_later() DISTINCT file objects; two
# separate OS handles never interleave each other's writes.


def test_crash_dump_and_periodic_dump_use_distinct_files():
    body = _install_src()
    assert "faulthandler-crash.log" in body, (
        "the crash dump (enable) must have its own file, separate from the periodic log"
    )


def test_enable_and_dump_later_do_not_share_a_handle():
    """enable() backs the crash dump; dump_traceback_later() backs the heartbeat.
    They must be handed two different file objects or their output can interleave."""
    body = _install_src()
    m_enable = re.search(r"faulthandler\.enable\(file=(\w+)\)", body)
    m_later = re.search(r"dump_traceback_later\([^)]*file=(\w+)", body)
    assert m_enable, "enable(file=...) must name its handle explicitly"
    assert m_later, "dump_traceback_later(file=...) must name its handle explicitly"
    assert m_enable.group(1) != m_later.group(1), (
        "enable() and dump_traceback_later() share a handle -- a crash landing during "
        "a periodic dump will interleave and shred the crash trace"
    )


def test_both_dump_files_are_rotated():
    """The crash file is bounded too: a crash loop must not fill the disk either."""
    body = _install_src()
    assert body.count("_rotate(") >= 2, (
        "both the periodic log and the crash log must be rotated under the cap"
    )
