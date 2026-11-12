"""Crash/timeout isolation for the EasyOCR child process (2026-11-12).

EasyOCR runs on PyTorch, whose native layer (c10.dll) crashed the gateway's
python.exe with an access violation (0xc0000005) three times on 2026-11-12, each
taking the public endpoint down for 1-4 minutes. A native crash cannot be caught
by a Python try/except -- it takes the whole process with it. The fix runs EasyOCR
in a short-lived CHILD process, so a crash kills only the child and the parent
falls back to Tesseract.

These tests pin that isolation WITHOUT importing easyocr/torch at all: they drive
_run_easyocr_child against tiny stand-in child scripts (one that exits with a
non-zero code to stand in for the 0xc0000005 crash, one that just sleeps to force
the timeout path). The contract under test:

  * a non-zero child exit -> the function returns None, never raises;
  * a child that outruns the timeout -> the child is killed and None is returned;
  * a child that prints the agreed JSON line -> its text is returned;
  * at most one child runs at a time (RAM-constrained host).

Run: pytest -q tools/test_ocr_child_isolation.py
"""
import sys
import threading
import time

import pytest

from tools import ocr_ops


def _write_child(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_nonzero_child_exit_returns_none_without_raising(tmp_path, monkeypatch):
    """A child that dies with a non-zero code (stand-in for the native 0xc0000005
    crash) must surface as None, never as an exception."""
    child = _write_child(tmp_path, "crash_child.py", "import sys\nsys.exit(3)\n")
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out is None


def test_segfault_style_exit_code_returns_none(tmp_path, monkeypatch):
    """An access-violation exit code (the Windows crash shape) is just another
    non-zero code to the parent: still None, still no exception."""
    # 0xC0000005 as a signed int is what a crashed Windows child reports.
    code = -1073741819  # 0xC0000005 interpreted as signed 32-bit
    child = _write_child(tmp_path, "av_child.py",
                         "import sys\nsys.exit(%d)\n" % code)
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out is None


def test_timeout_kills_the_child_and_returns_none(tmp_path, monkeypatch):
    """A child that sleeps past the timeout must be killed (and only the child), and
    the function must return None promptly. No EasyOCR/torch is imported."""
    child = _write_child(tmp_path, "sleep_child.py",
                         "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    start = time.time()
    out = ocr_ops._run_easyocr_child("anything.png", ["en"], timeout=1.0)
    elapsed = time.time() - start

    assert out is None
    # It must return near the timeout, not after the child's full 30s sleep.
    assert elapsed < 20, "timed-out child was not killed promptly (%.1fs)" % elapsed


def test_valid_json_line_is_returned(tmp_path, monkeypatch):
    """The happy path: a child that prints the agreed one-line JSON yields its text."""
    child = _write_child(
        tmp_path, "ok_child.py",
        "import sys\nsys.stdout.write('{\"ok\": true, \"text\": \"hello\"}\\n')\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out == "hello"


def test_ok_false_report_returns_none(tmp_path, monkeypatch):
    """A child reporting ok=false (EasyOCR unavailable/failed) means no usable text."""
    child = _write_child(
        tmp_path, "fail_child.py",
        "import sys\nsys.stdout.write('{\"ok\": false, \"error\": \"nope\"}\\n')\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out is None


def test_non_json_output_returns_none(tmp_path, monkeypatch):
    """Garbage on stdout (not JSON) must not raise; it is treated as no text."""
    child = _write_child(tmp_path, "junk_child.py",
                         "print('not json at all')\n")
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out is None


def test_launch_failure_returns_none(monkeypatch):
    """If the child cannot even be launched, the function returns None, not an error."""
    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(ocr_ops.subprocess, "Popen", _boom)
    out = ocr_ops._run_easyocr_child("anything.png", ["en"])
    assert out is None


def test_only_one_child_runs_at_a_time(tmp_path, monkeypatch):
    """The semaphore must serialise child launches to one at a time (RAM-constrained
    host, same as the old single-worker thread).

    We wrap Popen so that, once a child is launched, the wrapper holds for a moment
    while recording how many launched children are simultaneously inside that window.
    With a correct Semaphore(1) the window is never occupied by more than one caller;
    if the gate were removed, concurrent callers would overlap and max would exceed 1.
    The decrement happens inside the wrapper (before communicate), so it measures the
    launch-serialisation the semaphore is responsible for.
    """
    child = _write_child(
        tmp_path, "ok_child.py",
        "import sys\nsys.stdout.write('{\"ok\": true, \"text\": \"x\"}\\n')\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    live = {"now": 0, "max": 0}
    lock = threading.Lock()
    real_popen = ocr_ops.subprocess.Popen

    def _counting_popen(*a, **k):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        # Hold the launch window open briefly so a second concurrent launch, were the
        # semaphore absent, would be caught overlapping here.
        time.sleep(0.2)
        try:
            return real_popen(*a, **k)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(ocr_ops.subprocess, "Popen", _counting_popen)

    threads = [threading.Thread(target=lambda: ocr_ops._run_easyocr_child("a.png", ["en"]))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert live["max"] == 1, (
        "more than one EasyOCR child launch overlapped (max %d); the semaphore is "
        "not serialising launches" % live["max"]
    )
