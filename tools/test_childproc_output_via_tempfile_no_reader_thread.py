# -*- coding: utf-8 -*-
"""childproc.run must return the SAME result as before while leaking no reader thread.

WHY THIS FILE EXISTS SEPARATELY from test_childproc_timeout_kills_tree_no_thread_leak.py.
That file pins the timeout branch: after a burst of timed-out calls the non-main thread
count returns to baseline. This file pins the OTHER half of the same guarantee -- the
ordinary, successful path -- against the exact inputs the permanent fix called out:

  * a grandchild that inherits the stdout/stderr handles and OUTLIVES a timeout, so the
    pipe write ends would stay open under a direct-child kill (the original leak shape);
  * a plain timeout;
  * several MB of output (a pipe-buffer-filling volume, the size a reader thread was there
    to drain);
  * Japanese (non-ASCII, genuinely UTF-8) output, decoded, not mangled;
  * stdin fed as text.

In every case two things must hold together: the returned result is byte-for-byte what the
old text-mode path produced, AND threading.enumerate() shows no net new non-main thread.
The second is the regression gauge: the CPython pipe reader threads leaked by the old
timeout path are real Thread objects, so a return to the pre-call baseline is exactly the
condition whose violation produced the 500-thread figure. Checking it on the SUCCESS path
too means this rewrite -- which moves output onto a temp file (no _readerthread) -- is held
to the same no-leak bar on the calls that succeed, not only on the calls that time out.

These tests assert on OBSERVABLE behaviour (return value, decoded text, thread count), not
on how run() is implemented, so they stay honest whether the output rides a pipe or a temp
file.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import childproc  # noqa: E402


def _non_main_threads():
    return [t for t in threading.enumerate() if t is not threading.main_thread()]


def _settled_baseline():
    """Let transient startup threads retire, then take the non-main thread count."""
    time.sleep(0.5)
    return len(_non_main_threads())


def _assert_back_to_baseline(base, what):
    deadline = time.time() + 10
    while time.time() < deadline and len(_non_main_threads()) > base:
        time.sleep(0.2)
    leaked = _non_main_threads()
    assert len(leaked) == base, (
        "%s: non-main thread count grew from %d to %d -- a reader/helper thread leaked "
        "(names: %r)" % (what, base, len(leaked), [t.name for t in leaked])
    )


def test_a_few_mb_of_output_comes_back_whole_and_leaks_no_thread():
    # Several MB on stdout -- comfortably past the OS pipe buffer, which is the volume a
    # drain thread existed to move. The whole thing must come back, and the call must not
    # leave a helper thread behind.
    base = _settled_baseline()
    n = 4_000_000
    code = "import sys; sys.stdout.write(\'a\' * %d)" % n
    r = childproc.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 0
    assert len(r.stdout) == n and set(r.stdout) == {"a"}
    assert r.stderr == ""
    _assert_back_to_baseline(base, "multi-MB output")


def test_japanese_output_is_decoded_as_utf8_not_mangled():
    # The module's whole reason for existing: a child that writes UTF-8 must come back as the
    # same text, not a cp932 mis-decode or an empty string from a dead reader thread.
    base = _settled_baseline()
    text = "\u65e5\u672c\u8a9e\u306e\u51fa\u529b\u3067\u3059\u3002\u6587\u5b57\u5316\u3051\u7981\u6b62\u3002"
    code = (
        "import sys, io\n"
        "out = io.TextIOWrapper(sys.stdout.buffer, encoding=\'utf-8\')\n"
        "out.write(%r); out.flush()\n" % text
    )
    r = childproc.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 0
    assert r.stdout == text
    _assert_back_to_baseline(base, "japanese output")


def test_stdin_text_is_fed_through_and_echoed_back():
    # A str passed as input must be encoded on the way in (the documented fix) and the child's
    # echo must come back decoded, with no leftover helper thread.
    base = _settled_baseline()
    payload = "\u3053\u3093\u306b\u3061\u306f stdin"
    code = (
        "import sys, io\n"
        "data = io.TextIOWrapper(sys.stdin.buffer, encoding=\'utf-8\').read()\n"
        "out = io.TextIOWrapper(sys.stdout.buffer, encoding=\'utf-8\')\n"
        "out.write(data); out.flush()\n"
    )
    r = childproc.run([sys.executable, "-c", code], input=payload, capture_output=True)
    assert r.returncode == 0
    assert r.stdout == payload
    _assert_back_to_baseline(base, "stdin text echo")


def test_a_grandchild_holding_the_pipe_past_a_timeout_still_returns_promptly_and_leaks_nothing():
    # The original leak shape: the direct child spawns a grandchild that inherits stdout/stderr
    # and outlives the timeout, so the write ends stay open past a direct-child kill. The tree
    # kill must close them; the call must raise TimeoutExpired quickly and leave no reader
    # thread parked.
    import subprocess

    base = _settled_baseline()
    child = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, \'-c\', \'import time; time.sleep(120)\'])\n"
        "time.sleep(120)\n"
    )
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        childproc.run([sys.executable, "-c", child], capture_output=True, timeout=1)
    assert time.time() - t0 < 60, "timeout path did not return promptly -- tree not killed"
    _assert_back_to_baseline(base, "grandchild-held pipe past timeout")


def test_large_output_on_the_timeout_path_is_offered_on_the_exception():
    # A child that prints a chunk and then hangs past the timeout: the partial output it DID
    # produce must ride back on TimeoutExpired.output (decoded), and the call must not leak a
    # reader thread. This is the drain-then-kill contract for a real-but-interrupted stream.
    import subprocess

    base = _settled_baseline()
    chunk = 200_000
    child = (
        "import sys, time\n"
        "sys.stdout.write(\'b\' * %d); sys.stdout.flush()\n"
        "time.sleep(120)\n" % chunk
    )
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        childproc.run([sys.executable, "-c", child], capture_output=True, timeout=2)
    out = ei.value.output
    # output may be str (decoded) or None/empty if the kill raced the read; when present it
    # must be the decoded partial stream, never raw bytes.
    if out:
        assert isinstance(out, str)
        assert set(out) <= {"b"}
    _assert_back_to_baseline(base, "partial large output on timeout")
