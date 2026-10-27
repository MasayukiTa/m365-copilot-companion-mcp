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
from typing import Optional

__all__ = ["decode", "run", "headless_creationflags", "tree_popen_kwargs", "kill_tree"]


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



#: Child-process priority classes, lowest-contention first. A fleet worker's children (tests,
#: analysis, builds) run at BELOW_NORMAL by default so they cannot starve the two windows the
#: operator watches (Fleet Cockpit, CopilotChat), which run at Normal or above. Those windows
#: are launched by ui/rebuild_ui.ps1 THROUGH explorer.exe and never pass through this helper,
#: so they keep the ordinary priority of the desktop -- this only lowers the fleet's children.
_PRIORITY_SETTING_KEY = "child_process_priority"
_PRIORITY_ENV = "MCP_CHILD_PROCESS_PRIORITY"
_DEFAULT_PRIORITY = "below_normal"
_PRIORITY_CHOICES = ("idle", "below_normal", "normal")

#: Windows PRIORITY_CLASS creationflags by name. Values are the documented constants; looked up
#: via getattr so a Python build missing one degrades to 0 (no priority flag) rather than crash.
_WIN_PRIORITY_FLAGS = {
    "idle": "IDLE_PRIORITY_CLASS",
    "below_normal": "BELOW_NORMAL_PRIORITY_CLASS",
    "normal": "NORMAL_PRIORITY_CLASS",
}

#: POSIX nice increments by name (higher nice == lower priority). normal == 0 (no change).
_POSIX_NICE = {"idle": 19, "below_normal": 10, "normal": 0}


def _priority_name() -> str:
    """The configured child priority: 'idle' | 'below_normal' | 'normal'. Default below_normal.

    Read live from settings.txt (mtime-cached by tools.settings_path's reader if present),
    overridable by ``MCP_CHILD_PROCESS_PRIORITY`` for tests and emergencies. Never raises; an
    unreadable or unknown value is the default, so a child is never spawned at a WRONG priority
    because the setting could not be read.
    """
    env = (os.environ.get(_PRIORITY_ENV) or "").strip().lower()
    if env in _PRIORITY_CHOICES:
        return env
    try:
        from tools.settings_path import settings_file
        path = settings_file()
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln.startswith(_PRIORITY_SETTING_KEY + "="):
                        v = ln.split("=", 1)[1].strip().lower()
                        return v if v in _PRIORITY_CHOICES else _DEFAULT_PRIORITY
    except Exception:
        pass
    return _DEFAULT_PRIORITY


def priority_creationflags(name: Optional[str] = None) -> int:
    """Windows creationflags carrying the child's PRIORITY_CLASS, else 0.

    0 on POSIX (priority there is applied with ``os.nice`` via ``_priority_preexec``) and 0
    for 'normal' on Windows (no class flag needed). Never raises.
    """
    if os.name != "nt":
        return 0
    nm = (name or _priority_name())
    if nm == "normal":
        return 0
    return getattr(subprocess, _WIN_PRIORITY_FLAGS.get(nm, ""), 0)


def _priority_preexec(name: Optional[str] = None):
    """A POSIX preexec_fn that renices the child, or None when nothing to do.

    Returns None on Windows, for 'normal', or when ``os.nice`` is unavailable, so the caller
    simply omits ``preexec_fn``. The returned function swallows its own errors: a failed renice
    must not stop the child from launching.
    """
    if os.name == "nt":
        return None
    nm = (name or _priority_name())
    inc = _POSIX_NICE.get(nm, 0)
    if not inc or not hasattr(os, "nice"):
        return None

    def _apply():
        try:
            os.nice(inc)
        except Exception:
            pass

    return _apply


def tree_popen_kwargs(headless: bool = False, priority: bool = True) -> dict:
    """Popen kwargs that make the spawned process the root of a killable tree.

    POSIX needs a fresh session so ``killpg(proc.pid, ...)`` cannot target the caller's own
    process group. Windows ``taskkill /PID <root> /T`` already walks descendants while the root
    is live; ``CREATE_NO_WINDOW`` is orthogonal and is added only when the caller is unattended.

    ``priority`` (default True) lowers the child to the configured ``child_process_priority``
    (default below_normal) so a fleet worker's children never starve the operator's windows.
    Pass priority=False for a child that must run at the parent's priority. On Windows the
    priority rides in ``creationflags``; on POSIX it is a ``preexec_fn`` that renices.
    """
    if os.name == "nt":
        flags = headless_creationflags() if headless else 0
        if priority:
            flags |= priority_creationflags()
        return {"creationflags": flags}
    kw = {"start_new_session": True}
    if priority:
        pre = _priority_preexec()
        if pre is not None:
            kw["preexec_fn"] = pre
    return kw


def kill_tree(proc, wait_s: float = 5.0) -> bool:
    """Best-effort hard stop of ``proc`` and everything it started; never raises.

    Every caller using this must have spawned with :func:`tree_popen_kwargs`. On Windows the
    descendant walk must happen while the root PID is still attributable, so taskkill is tried
    before the direct-child fallback. On POSIX the dedicated session makes proc.pid the pgid.
    Returns True when the direct process is no longer running after cleanup.
    """
    if proc is None:
        return True
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=max(1.0, float(wait_s)),
                creationflags=headless_creationflags(),
            )
        else:
            import signal
            try:
                os.killpg(int(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=max(0.1, float(wait_s)))
    except Exception:
        pass
    try:
        return proc.poll() is not None
    except Exception:
        return False
