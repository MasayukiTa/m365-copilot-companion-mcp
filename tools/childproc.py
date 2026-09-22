# -*- coding: utf-8 -*-
"""One decode policy for child-process output, and one way to ask for it.

`text=True` decodes with `locale.getpreferredencoding(False)`, which on this machine is
**cp932**. A child that writes UTF-8 -- git, ssh, pip, any script this repository runs -- then
dies on its first non-cp932 byte, and the caller does not get a short string: it gets nothing,
or an exception from inside the reader thread with `returncode == 0`.

THIS HAS COST REAL WORK TWICE, both measured, both in the same week:

  * `bench/swe_solve_decoupled.py` -- `git diff` of a patch containing one non-cp932 byte
    killed the reader thread, `.stdout` came back None, and the arm aborted after 60 instances
    had already been solved. Fixed in 4ef0d31.
  * `bench/swe_check_remote.py` -- the same defect in the GRADE path, where `(r.stdout or "")`
    turned the failure into a silent empty string and the run was misdiagnosed as "eval host
    unreachable". Fixed in 1c83939.

Two call sites of one failure class were fixed one at a time. This module exists so the class
is fixed instead: `decode` is the policy, `run` is the way to get it, and
`tools/test_child_output_is_not_decoded_by_luck.py` refuses to let a new unguarded `text=True`
into the tree without a stated reason.

WHY UTF-8 FIRST AND REPLACE LAST. UTF-8 is what the children here actually emit, and a
mis-decode is detectable (UnicodeDecodeError) where a wrong-but-valid cp932 read is not. The
local code page is the honest second guess for genuine Windows tools. `errors="replace"` is
the floor: **a garbled character beats losing the whole output**, which is exactly what both
incidents lost.
"""
from __future__ import annotations

import locale
import os
import subprocess

__all__ = ["decode", "run", "headless_creationflags"]


def decode(raw) -> str:
    """Bytes from a child process, as text, without ever raising.

    UTF-8, then the local code page, then replacement characters. Already-decoded input is
    passed through so a caller can hand this either half of a `CompletedProcess` without
    checking which mode it ran in, and `None` -- which is what a dead reader thread leaves --
    becomes "" rather than an AttributeError one frame later.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        pass
    fallback = locale.getpreferredencoding(False) or "utf-8"
    return bytes(raw).decode(fallback, errors="replace")


def run(cmd, **kw):
    """`subprocess.run` in binary mode, with stdout/stderr decoded by `decode`.

    Returns the real `CompletedProcess`, with `.stdout` and `.stderr` replaced by strings, so
    every existing caller reads unchanged. `capture_output=True` is the default because a
    caller reaching for this wants the output; pass `capture_output=False` when it should go to
    the console.

    Refuses `text`, `universal_newlines` and `encoding`: each of them re-introduces the locale
    decode this exists to avoid, and accepting one silently would make the guarantee a lie.
    """
    for bad in ("text", "universal_newlines", "encoding", "errors"):
        if bad in kw:
            raise TypeError(
                "childproc.run decodes for you; %r would put the locale codec back in the "
                "path this exists to keep it out of" % bad)
    kw.setdefault("capture_output", True)
    # BINARY IN, BINARY OUT -- BUT NOT AT THE CALLER'S EXPENSE. Running without text=True is
    # what keeps the locale codec out of the OUTPUT path, and it also makes stdin binary, so a
    # caller that passed a str to subprocess.run and switched to this got
    # "TypeError: a bytes-like object is required, not 'str'" from inside Popen._stdin_write.
    # Measured 2026-09-12 while converting scripts/test_stale_server_check.py. Encoding it here
    # in UTF-8 -- the same direction `decode` takes on the way back -- is the whole fix.
    # Making every call site remember instead is the version of this that keeps biting.
    if isinstance(kw.get("input"), str):
        kw["input"] = kw["input"].encode("utf-8")
    proc = subprocess.run(cmd, **kw)
    try:
        proc.stdout = decode(proc.stdout)
        proc.stderr = decode(proc.stderr)
    except AttributeError:          # capture_output=False -> both are None already
        pass
    return proc


def headless_creationflags() -> int:
    """`creationflags` for a child that must never put a window on the desktop.

    A VALUE, NOT A DEFAULT, and that is the whole design. `CREATE_NO_WINDOW` does not mean
    "the parent's console, hidden" -- it means the child gets NO console attachment, so
    anything that reads through CONIN$, prompts for an ssh passphrase or a git credential, or
    expects a terminal, stops working. Making it the default for `run` would change the I/O
    contract of every caller in this repository to fix a window-allocation defect in a few of
    them. Callers that are unattended by construction ask for it by name; nothing else is
    touched.

    WHY THIS IS NEEDED AT ALL: measured 2026-09-19, a console program started by a parent with
    NO CONSOLE allocates a brand new one, which Windows Terminal then shows -- a black window
    on an unattended desktop (PID 18152, class CASCADIA_HOSTING_WINDOW_CLASS). Any launch
    whose parent is `wscript.exe`, a WPF process, or a scheduled task is in that position.
    `capture_output=True` does not help: it redirects the pipes and never touches console
    allocation.

    `CREATE_NEW_PROCESS_GROUP` IS DELIBERATELY NOT HERE, although the one call site that
    predates this function pairs the two. It creates a group with Ctrl+C handling initially
    disabled, and `GenerateConsoleCtrlEvent` requires the recipient to share the sender's
    console -- which a windowless child does not have. So it cannot be a graceful-stop channel
    here, and it is not being used as one: `CTRL_BREAK_EVENT`, `CTRL_C_EVENT`, `send_signal`
    and `GenerateConsoleCtrlEvent` appear in ZERO tracked files (measured 2026-09-22).
    Shutdown is `taskkill /T` and file flags. A flag that buys nothing and disables a signal
    path is not a default.
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
