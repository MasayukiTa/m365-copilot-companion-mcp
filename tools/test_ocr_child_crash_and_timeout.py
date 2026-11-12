# -*- coding: utf-8 -*-
"""Parent-side crash and timeout isolation for the EasyOCR child process (2026-11-12).

WHAT THIS GUARDS. EasyOCR runs on PyTorch, whose native layer (c10.dll) crashed the
gateway's python.exe with an access violation (0xc0000005) three times on 2026-11-12,
each taking the public endpoint down for 1-4 minutes. The fix moves EasyOCR into a
short-lived CHILD process (tools/_easyocr_child.py); the parent must survive whatever the
child does. These tests pin the two parent-side promises that keep the gateway up:

  1. A child that exits with an ABNORMAL code -- including the large unsigned code a
     Windows access violation surfaces as -- must NOT raise in the parent. The parent
     logs the code and returns None, so the caller falls back to Tesseract.
  2. A child that HANGS must be killed (only the child) once the timeout elapses, and the
     function must return None rather than block the gateway forever.

WHY A STUB CHILD, NOT REAL TORCH. Loading torch under this host's test launcher fails
(WinError 1114) and a real Reader needs a model download. The stub children here only
exit with a chosen code, or sleep -- they never import easyocr/torch. That is exactly the
parent-side behaviour under test: spawn, observe the child's fate, stay alive, return None.
"""
from __future__ import annotations

import os
import sys
import time

from tools import ocr_ops


class _FakeImage:
    """Stands in for a PIL image: convert() returns self, save() writes a token PNG."""

    def convert(self, mode):
        return self

    def save(self, path, format=None):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")


def _write_child(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_abnormal_exit_code_returns_none_without_raising(tmp_path, monkeypatch):
    """A child that dies with a non-zero code (a crash is just one such code) must leave the
    parent unharmed: no exception, just None so the caller falls back to Tesseract."""
    # Exit 3221225477 == 0xC0000005, the code a Windows access violation produces. Using the
    # literal value exercises the exact path the real crash takes, without importing torch.
    child = _write_child(
        tmp_path, "crash_child.py",
        "import sys\n"
        "sys.stderr.write('simulated native crash')\n"
        "sys.exit(3221225477)\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    # Must not raise, and must report failure as None (not an empty string, not garbage).
    result = ocr_ops._easyocr_image_text(_FakeImage(), "eng")
    assert result is None


def test_no_output_but_clean_exit_also_returns_none(tmp_path, monkeypatch):
    """A child that exits 0 yet prints no parseable JSON line is still a failed read: None."""
    child = _write_child(
        tmp_path, "silent_child.py",
        "import sys\n"
        "sys.exit(0)\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)

    assert ocr_ops._easyocr_image_text(_FakeImage(), "eng") is None


def test_timeout_kills_the_child_and_returns_none(tmp_path, monkeypatch):
    """A hanging child must be killed once the timeout elapses, and the call must return None
    rather than block the gateway. The child only sleeps -- it never imports easyocr/torch.

    The timeout is injected tiny so the test is fast; the child would otherwise sleep far
    longer, so if the kill did not happen the call could not return within the deadline.
    """
    child = _write_child(
        tmp_path, "sleep_child.py",
        "import time\n"
        "time.sleep(60)\n",
    )
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD", child)
    # Drive the timeout straight through the helper so we do not depend on the module default.
    monkeypatch.setattr(ocr_ops, "_OCR_CHILD_TIMEOUT_S", 1.0)

    start = time.time()
    result = ocr_ops._run_easyocr_child(str(tmp_path / "img.png"), ["en"], timeout=1.0)
    elapsed = time.time() - start

    assert result is None
    # The sleeper would run 60s; returning well under that proves the child was killed, not
    # waited out. Generous upper bound to stay stable on a loaded CI runner.
    assert elapsed < 30, "timeout did not kill the hanging child promptly (%.1fs)" % elapsed


def test_child_that_cannot_start_returns_none(tmp_path, monkeypatch):
    """If the child script path does not exist, launching fails; the parent returns None
    rather than propagating the OSError."""
    monkeypatch.setattr(ocr_ops, "_EASYOCR_CHILD",
                        str(tmp_path / "does_not_exist_child.py"))
    # A missing interpreter target raises inside Popen on some platforms and yields a
    # non-zero exit on others; both must collapse to None without escaping.
    assert ocr_ops._run_easyocr_child(str(tmp_path / "img.png"), ["en"]) is None
