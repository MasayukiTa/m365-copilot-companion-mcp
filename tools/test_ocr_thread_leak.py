"""OCR crash-isolation regression (2026-11-12), formerly the thread-leak regression.

HISTORY. The original fix (2026-10-31) funnelled EasyOCR/torch onto one dedicated worker
thread to stop a NATIVE thread-pool leak. That stopped the leak but could not stop a
NATIVE CRASH: on 2026-11-12 torch's c10.dll raised an access violation (0xc0000005) three
times and took the whole gateway process down with it, because a native crash cannot be
caught by a Python try/except on the thread it happens on.

THE FIX UNDER TEST. EasyOCR now runs in a short-lived CHILD PROCESS (tools/_easyocr_child.py),
launched by ocr_ops._run_easyocr_child. If the child crashes or exits non-zero, the parent
reads the exit code, logs one line, returns None, and the caller falls back to Tesseract --
the gateway stays up. These tests assert that contract WITHOUT importing easyocr/torch:
they point ocr_ops at a tiny stand-in child script.

The structural property that made the old leak impossible (torch never runs on a gateway
thread) is now guaranteed more strongly: torch never runs in the gateway PROCESS at all.
"""
import os
import sys
import threading
import time

import pytest

from tools import ocr_ops


class _FakeImage:
    """Minimal stand-in for a PIL image: convert() returns an object that can be saved."""
    def convert(self, mode):
        return self

    def save(self, path, format=None):
        # Write a byte so the temp file exists; the stub child does not read it.
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")


def _write_child(tmp_path, body: str) -> str:
    p = tmp_path / "stub_child.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_easyocr_work_runs_in_a_child_process_not_this_one(tmp_path, monkeypatch):
    """The recognition must happen in a separate process. The stub child writes its own
    PID; the test asserts it differs from this process's PID -- i.e. torch would run OUT
    OF PROCESS, which is the whole point of the crash isolation."""
    child = _write_child(tmp_path, (
        "import json, os, sys\n"
        "sys.stdout.write(json.dumps({'ok': True, 'text': 'pid=%d' % os.getpid()}))\n"
        "sys.stdout.write('\\n')\n"
    ))
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    out = ocr_ops._easyocr_image_text(_FakeImage(), "eng")
    assert out is not None
    assert out.startswith("pid=")
    assert out != "pid=%d" % os.getpid(), "OCR ran in THIS process, not a child"


def test_a_crashing_child_returns_none_and_does_not_raise(tmp_path, monkeypatch):
    """A child that dies with a non-zero/abnormal exit (standing in for the 0xc0000005
    torch crash) must leave the parent intact: no exception, result None, fall back."""
    child = _write_child(tmp_path, (
        "import os\n"
        "os._exit(3221225477)\n"  # 0xC0000005, the measured access-violation code
    ))
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    # Must not raise, and must report 'no text' so the caller falls back.
    assert ocr_ops._easyocr_image_text(_FakeImage(), "eng") is None


def test_a_slow_child_is_killed_on_timeout_and_returns_none(tmp_path, monkeypatch):
    """A child that sleeps past the timeout must be killed (child only) and yield None.
    The stub only sleeps -- it never imports easyocr/torch."""
    child = _write_child(tmp_path, (
        "import time\n"
        "time.sleep(30)\n"
    ))
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    start = time.time()
    out = ocr_ops._run_easyocr_child(str(tmp_path / "x.png"), ["en"], timeout=1.0)
    elapsed = time.time() - start
    assert out is None
    assert elapsed < 15, "timeout did not kill the child promptly (%.1fs)" % elapsed


def test_only_one_child_runs_at_a_time(tmp_path, monkeypatch):
    """The host is RAM-constrained: at most one torch child at a time. The stub child
    records concurrency by touching a shared counter file; the max seen must be 1."""
    counter = tmp_path / "live.txt"
    peak = tmp_path / "peak.txt"
    counter.write_text("0", encoding="utf-8")
    peak.write_text("0", encoding="utf-8")
    child = _write_child(tmp_path, (
        "import json, sys, time, os\n"
        "c = r'%s'\n" % str(counter).replace("\\", "\\\\") +
        "p = r'%s'\n" % str(peak).replace("\\", "\\\\") +
        "n = int(open(c).read() or '0') + 1\n"
        "open(c, 'w').write(str(n))\n"
        "mx = max(n, int(open(p).read() or '0'))\n"
        "open(p, 'w').write(str(mx))\n"
        "time.sleep(0.4)\n"
        "n = int(open(c).read() or '0') - 1\n"
        "open(c, 'w').write(str(n))\n"
        "sys.stdout.write(json.dumps({'ok': True, 'text': 'x'}))\n"
        "sys.stdout.write('\\n')\n"
    ))
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    def call():
        ocr_ops._run_easyocr_child(str(tmp_path / "x.png"), ["en"], timeout=30)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert int(peak.read_text(encoding="utf-8")) == 1, \
        "more than one OCR child ran concurrently"


def test_a_child_reporting_ok_false_yields_none(tmp_path, monkeypatch):
    """When the child runs cleanly but reports ok=false (easyocr unavailable, etc.), the
    parent treats it as 'no text' and falls back."""
    child = _write_child(tmp_path, (
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'ok': False, 'error': 'easyocr unavailable'}))\n"
        "sys.stdout.write('\\n')\n"
    ))
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    assert ocr_ops._easyocr_image_text(_FakeImage(), "eng") is None
