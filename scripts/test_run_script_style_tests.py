# -*- coding: utf-8 -*-
"""The gate that runs the twenty script-style suites was hanging on the first one that
printed Japanese, and taking the other thirteen with it.

`subprocess.run(..., capture_output=True, text=True)` with no `encoding=` decodes the child
with the LOCALE codec -- cp932 on a Japanese Windows install. The decode raises inside the
reader THREAD, so nothing drains the child's stdout pipe; the child fills the 64 KB pipe
buffer and blocks forever; the parent waits out its 600-second budget and raises
TimeoutExpired, which nothing caught, so the whole gate died on a traceback from inside
subprocess.

Two consequences, and the second is the serious one:

  * the failure named a subprocess internal rather than the slow suite;
  * every suite after it in the list -- thirteen of the twenty -- was never run, and the
    gate had no way to say so.

relay/test_fleet_refute.py, the suite it stopped on, passes 12/12 in 2.6 seconds. It was
never slow; it was the first one to print a Japanese character.

Same root cause as scripts/check_integration_evidence._git, found the same afternoon. Both
places had `text=True` and no encoding, and both turned an unreadable child into something
other than a failure.
"""
import io
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_script_style_tests as R  # noqa: E402

#: Enough to overflow the 64 KB pipe buffer that the dead reader thread stopped draining.
BIG_JAPANESE = (
    "# -*- coding: utf-8 -*-\n"
    "import sys\n"
    "#\n"
    "# UTF-8 BYTES, WRITTEN PAST THE TEXT LAYER, because that is what the real suites emit\n"
    "# and it is the whole reproduction. A child that merely PRINTS Japanese encodes it with\n"
    "# the same locale codec the parent decodes with, so cp932 round-trips and nothing ever\n"
    "# fails. The first version of this fixture did exactly that: it passed with the fix\n"
    "# removed, which is to say it tested nothing.\n"
    "line = '日本語の出力です。これがパイプを埋める。'.encode('utf-8')\n"
    "for i in range(4000):\n"
    "    sys.stdout.buffer.write(line + str(i).encode('ascii') + b'\\n')\n"
    "sys.stdout.buffer.write(b'=== 3/3 fake checks passed ===\\n')\n"
    "sys.stdout.flush()\n"
)

QUIET_ASCII = (
    "print('=== 3/3 fake checks passed ===')\n"
)

HANGS = (
    "import time\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ROOT", tmp_path)
    return tmp_path


def _suite(root, name, source):
    path = root / name
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(source)
    return name


# -- the measured failure -------------------------------------------------------------------

def test_a_suite_that_prints_japanese_is_read_to_completion(fake_root):
    """THE BUG. Under cp932 the reader thread died, the child blocked on a full pipe, and
    this call did not return for 600 seconds."""
    rel = _suite(fake_root, "noisy.py", BIG_JAPANESE)
    ok, message = R.run_one(rel, None, timeout=60)
    assert ok, message
    assert "3/3" in message


def test_the_output_survives_well_enough_to_be_scored(fake_root):
    """errors='replace' must not mangle the ASCII count line the baseline is read from."""
    rel = _suite(fake_root, "quiet.py", QUIET_ASCII)
    ok, message = R.run_one(rel, 3, timeout=60)
    assert ok and "3/3" in message


# -- a timeout is a result, not a crash -------------------------------------------------------

def test_a_genuinely_slow_suite_is_reported_not_raised(fake_root):
    """Before, TimeoutExpired escaped and killed the gate, so the suites after it were
    silently never run."""
    rel = _suite(fake_root, "sleepy.py", HANGS)
    ok, message = R.run_one(rel, None, timeout=2)
    assert not ok
    assert "TIMED OUT" in message and "sleepy.py" in message


def test_the_run_continues_past_a_timeout(fake_root, monkeypatch, capsys):
    """THE POINT OF THE FIX. One slow suite must not stop the other nineteen from running."""
    slow = _suite(fake_root, "a_sleepy.py", HANGS)
    fast = _suite(fake_root, "b_quick.py", QUIET_ASCII)
    monkeypatch.setattr(R, "SUITES", {slow: None, fast: None})
    monkeypatch.setattr(R, "TIMEOUT_S", 2)
    assert R.main() == 1
    printed = capsys.readouterr().out
    assert "TIMED OUT" in printed
    assert "b_quick.py ok" in printed, "the gate stopped at the slow suite again"


def test_a_failing_suite_still_fails(fake_root):
    rel = _suite(fake_root, "broken.py", "import sys\nprint('[FAIL] nope')\nsys.exit(1)\n")
    ok, message = R.run_one(rel, None, timeout=60)
    assert not ok and "FAILED" in message


# -- the real gate ------------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.isfile(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "relay", "test_fleet_refute.py")),
    reason="suite not present in this checkout")
def test_the_suite_it_stopped_on_is_not_actually_slow():
    """It was never slow -- it was the first to print a Japanese character. Kept as a check
    that the explanation still holds; a real regression here would be worth knowing about."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, os.path.join(root, "relay", "test_fleet_refute.py")],
                          cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout[-400:]
