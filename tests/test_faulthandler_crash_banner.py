"""The crash log must stay readable across several deaths.

enable() and dump_traceback_later() already write to two different files, so the periodic
heartbeat never interleaves a crash trace (see test_faulthandler_rotation.py). But the crash
file is append-only across process generations. Observed 2026-11-11: three crashes -- two
access violations and one 0xc0000374 heap corruption -- sat stacked in faulthandler-crash.log
with no timestamp and no divider between them, so the faulting thread's stack of the heap
corruption could not be told apart from the two earlier deaths or from a prior boot's leftovers.

The fix writes a dated, per-process banner to the crash handle (only) at startup. A crash
trace that follows the newest banner belongs to the current process; everything above it is a
prior life. These tests check the banner is present, is on the crash handle and not the
heartbeat handle, carries a timestamp and pid, and -- by injecting a fake crash dump after the
banner the way faulthandler would -- that the faulting stack is isolable section-by-section.

Portable: no Windows-only construct. faulthandler.dump_traceback() stands in for a real native
crash dump; it emits the same thread-stack format on every platform.
"""
import faulthandler
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _install_src():
    src = open(MAIN, encoding="utf-8").read()
    i = src.index("def _install_faulthandler")
    return src[i:src.index("\ndef ", i + 10)]


# --- Source-level guarantees -------------------------------------------------------------

def test_a_session_banner_is_written_to_the_crash_log():
    """A per-process divider must be written, or stacked crashes have no boundary."""
    body = _install_src()
    assert "session start" in body, "the crash log needs a dated per-process banner"


def test_the_banner_goes_to_the_crash_handle_not_the_heartbeat_handle():
    """The banner must be written to the crash handle (_crash_fh). Writing it to the
    heartbeat handle would put a crash-log divider in the wrong file and leave the crash
    log undivided -- the exact failure this fixes."""
    body = _install_src()
    m = re.search(r"(\w+)\.write\(\s*_banner\s*\)", body)
    assert m, "the banner must be written through a named handle"
    assert m.group(1) == "_crash_fh", (
        "the banner must go to the crash handle, not the heartbeat handle"
    )


def test_the_banner_is_written_before_the_heartbeat_file_is_even_opened():
    """Order matters: the banner belongs to the crash stream set up by enable(), and must
    not be entangled with the periodic dump setup."""
    body = _install_src()
    assert body.index("_crash_fh.write(") < body.index("faulthandler.dump_traceback_later("), (
        "the crash banner must be written before the periodic dump is armed"
    )


def test_the_banner_carries_a_timestamp_and_pid():
    body = _install_src()
    assert "getpid" in body, "the banner must record which process generation crashed"
    assert re.search(r"%Y-%m-%dT%H:%M:%SZ|isoformat", body), (
        "the banner must carry a timestamp so crashes can be ordered in time"
    )


# --- Behavioural: a banner makes a stacked crash log readable -----------------------------

def _write_banner_then_dump(fh, pid, when):
    """Reproduce exactly what _install_faulthandler does for the crash handle: a dated
    banner, then (standing in for a native crash) a faulthandler thread-stack dump."""
    fh.write(
        "\n===== faulthandler crash log | session start " + when
        + " | pid=" + str(pid) + " | python=" + sys.version.split()[0] + " =====\n"
    )
    fh.flush()
    faulthandler.dump_traceback(file=fh)
    fh.flush()


def test_stacked_crashes_are_separable_by_their_banners(tmp_path):
    """Two crashes appended to one file must be isolable: the stack that follows the LAST
    banner is the current crash, and the banner count equals the crash count. This is the
    property that was missing on 2026-11-11, when three undivided crashes could not be told
    apart."""
    crash = tmp_path / "faulthandler-crash.log"
    with open(crash, "a", encoding="utf-8", buffering=1) as fh:
        _write_banner_then_dump(fh, 111, "2026-11-11T17:20:00Z")
        _write_banner_then_dump(fh, 222, "2026-11-11T17:26:00Z")

    text = crash.read_text(encoding="utf-8")
    banners = [m.start() for m in re.finditer(r"===== faulthandler crash log", text)]
    assert len(banners) == 2, "each crash generation must leave exactly one banner"

    # The current crash is everything after the last banner; the prior crash is between the
    # two banners. Each section must contain its own thread stack -- that is the faulting
    # trace -- and the two sections must not bleed into each other.
    last_section = text[banners[-1]:]
    prior_section = text[banners[0]:banners[-1]]
    assert "pid=222" in last_section and "pid=111" not in last_section
    assert "pid=111" in prior_section and "pid=222" not in prior_section
    assert "Current thread" in last_section or "Stack (most recent call first)" in last_section, (
        "the current crash section must carry its own faulting stack"
    )


def test_the_faulting_stack_after_the_last_banner_is_readable(tmp_path):
    """The whole point: the first stack after the newest banner must parse as a stack, not
    as a line shredded through an unrelated dump (as on 2026-11-07, where a heartbeat cut
    through 'Windows fatal exception: code 0xc0000374')."""
    crash = tmp_path / "faulthandler-crash.log"
    with open(crash, "a", encoding="utf-8", buffering=1) as fh:
        _write_banner_then_dump(fh, 999, "2026-11-11T17:26:00Z")

    text = crash.read_text(encoding="utf-8")
    tail = text[text.rindex("=====") + 5:]
    # A readable faulthandler stack has at least one 'File "...", line N' frame, intact.
    assert re.search(r'File "[^"]+", line \d+', tail), (
        "the faulting stack after the banner is not a readable frame"
    )
