"""Detect a running MCP server whose in-memory code is older than the checkout.

WHY THIS EXISTS
---------------
``git pull`` (or the guided rewritten-upstream reset) lands new ``relay/*.py`` /
``tools/*.py`` / ``main.py`` on disk, but a server process that is already running
keeps executing the code it imported at startup. Nobody notices: ``/health`` still
answers 200 and doctor is green, so the checkout and the live process silently
disagree. ``ui/*.cs`` had a rebuild path in ``Invoke-PostUpdateTail``; the Python
side had nothing symmetrical, and doctor had no check that would ever go red on it.

This module is the PURE decision core for both halves of the fix:

* doctor calls :func:`classify_staleness` (via the CLI ``main`` below) to turn
  "server-start HEAD" vs "current HEAD" into one word doctor maps to green / red /
  indeterminate -- the same shape ``check_unlock_usable.py`` already uses.
* ``Invoke-PostUpdateTail`` in ``start_all.ps1`` calls the same reasoning through
  :func:`python_side_changed` + :func:`decide_post_update_action` to decide whether
  the freshly-pulled Python needs the server swapped, and -- crucially -- whether a
  fleet/review run is live, in which case it must only REPORT rather than disturb it.

Every function here is pure (no I/O, no side effects) so the decisions can be
exercised directly from pytest on any OS. The only I/O lives in the small CLI at the
bottom, which reads two files and prints one verdict word; it is deliberately thin so
that what is tested is what runs.

Stdlib only. No Windows-specific calls: this runs in the Linux CI test job.
"""

from __future__ import annotations

import sys
import os
import json
from pathlib import PurePosixPath

# _run_appears_live may import relay.fleet_reaper for its exact pid-liveness rule.
# scripts/ is a sibling of relay/, so put the repo root (this file's parent's parent)
# on sys.path; the import is optional and guarded, so this never hard-fails the module.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


#: Top-level paths whose change means the RUNNING server is now executing stale code.
#: main.py is the entrypoint; relay/ and tools/ are imported into the live process. A
#: change under any of these is a change the running server cannot see until it is
#: replaced. Kept deliberately narrow: docs, ui/, scripts/ and tests do NOT ride in
#: the server process, so touching them must not trigger a server swap (that would
#: break the "re-running is a no-op" contract for the common docs-only update).
SERVER_CODE_PREFIXES: tuple[str, ...] = ("relay/", "tools/")
SERVER_CODE_FILES: frozenset[str] = frozenset({"main.py"})


def _normalize(path: str) -> str:
    """A git --name-only path as forward-slash posix, stripped of a leading ./.

    git already emits forward slashes on every platform, but a caller (or a test)
    might hand us a backslash path or a leading ``./``; normalize both so the prefix
    test below cannot be fooled by cosmetics. Never touches the filesystem.
    """
    p = path.strip().replace("\\\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return PurePosixPath(p).as_posix() if p else p


def is_server_code_path(path: str) -> bool:
    """True when this one changed path is code the running server imported.

    A directory prefix match for relay/ and tools/, plus an exact match for the
    top-level entrypoints (main.py). Anything else -- ui/, scripts/, docs/, a
    tools-named file OUTSIDE the tools/ dir -- is not server code.
    """
    norm = _normalize(path)
    if not norm:
        return False
    if norm in SERVER_CODE_FILES:
        return True
    return any(norm.startswith(prefix) for prefix in SERVER_CODE_PREFIXES)


def python_side_changed(changed_paths) -> bool:
    """Did this update touch any code the running server executes?

    ``changed_paths`` is an iterable of ``git diff --name-only`` lines. Returns True
    iff at least one is server code (see :func:`is_server_code_path`). Empty / all-UI
    / all-docs updates return False, which is what keeps a docs-only pull a no-op.
    """
    return any(is_server_code_path(p) for p in changed_paths if p and p.strip())


def classify_staleness(started_head, current_head, server_running: bool) -> str:
    """One word describing the running server vs the checkout.

    * ``"no_server"``  -- nothing is running, so there is nothing to be stale.
    * ``"unknown"``    -- a server is up but we could not read the SHA it started at
                          (no marker file yet, or an unreadable one). NOT a pass and
                          NOT a failure: doctor treats it as indeterminate.
    * ``"current"``    -- running server started at the SHA the checkout is on now.
    * ``"stale"``      -- running server started at a DIFFERENT SHA than HEAD: the
                          live process is executing code the checkout has moved past.

    Pure: the caller supplies both SHAs and the liveness flag. ``current_head`` being
    missing is itself ``unknown`` -- without a HEAD to compare to, nothing can be
    concluded.
    """
    if not server_running:
        return "no_server"
    sh = (started_head or "").strip()
    cur = (current_head or "").strip()
    if not sh or not cur:
        return "unknown"
    return "current" if sh == cur else "stale"


def decide_post_update_action(python_changed: bool, fleet_running: bool) -> str:
    """What Invoke-PostUpdateTail should do after a Python-side update landed.

    * ``"noop"``        -- no server code changed; leave the running server alone.
                           This preserves start_all's "re-running is a no-op" design
                           for docs-only / ui-only updates.
    * ``"report-only"`` -- server code changed BUT a fleet/review run is live. A swap
                           would drop that run, so we must not touch the server; the
                           caller surfaces "a restart is needed" instead.
    * ``"swap-needed"`` -- server code changed and no run is live: safe to let the
                           supervisor bring the server back on its fresh code.

    Pure. Note the ORDER: an unchanged Python side is a no-op even if a fleet is
    running -- there is simply nothing to swap.
    """
    if not python_changed:
        return "noop"
    if fleet_running:
        return "report-only"
    return "swap-needed"


def fleet_is_running(marker_states) -> bool:
    """True if any supplied run-marker is present AND its pid is still alive.

    ``marker_states`` is an iterable of (present: bool, pid_alive: bool) pairs -- one
    per marker the caller checked (the fleet run marker and the review run marker).
    Mirrors the supervisor's own rule: a marker with a DEAD pid is a crashed run, not
    a live one, so it does not count. Pure: the caller does the Test-Path / pid probe
    and hands in booleans.
    """
    return any(present and pid_alive for present, pid_alive in marker_states)


# --------------------------------------------------------------------------- CLI
# doctor invokes this file through Invoke-BoundedPythonFile (see check_unlock_usable.py
# for the same pattern) with two arguments: the path to the server-start HEAD marker,
# and the current HEAD sha. It prints exactly one verdict word on stdout. Any failure
# to read the marker is reported as its own word rather than crashing, so doctor's
# tri-state check can tell "indeterminate" apart from "stale".


def _read_marker(marker_path: str):
    """Return the SHA recorded in the marker file, or None if it cannot be read.

    Missing file, empty file, or an I/O error all collapse to None -- the caller maps
    that to ``unknown``. Only the first whitespace-delimited token is taken, so a
    trailing newline or an accidental extra field cannot corrupt the compare.
    """
    try:
        with open(marker_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    return raw.split()[0]


def _run_appears_live(fleet_dir: str) -> bool:
    """Best-effort: does <fleet_dir> show a LIVE fleet/review run?

    Mirrors relay/fleet_reaper.py's authoritative signal WITHOUT its side effects
    (we never finalize/reap here -- start_all only needs a read). Order:
      1. fleet_run_active.json present with an int pid -> live iff that pid is alive.
      2. No usable marker -> status.json with running==True is treated as live.
      3. Anything unreadable/ambiguous -> True (conservative: withhold the swap).

    The pid-liveness check reuses relay.fleet_reaper's own helper when importable, so
    this cannot drift from what the reaper considers alive; if that import fails we
    fall back to os.kill(pid, 0)/psutil and, failing even that, to the safe default.
    """
    active_path = os.path.join(fleet_dir, "fleet_run_active.json")
    status_path = os.path.join(fleet_dir, "status.json")

    def _load(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    marker = _load(active_path)
    if isinstance(marker, dict):
        raw_pid = marker.get("pid")
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None
        if pid is not None:
            return _pid_alive(pid)
        # marker present but no usable pid -> cannot confirm dead -> conservative live.
        return True

    status = _load(status_path)
    if isinstance(status, dict) and status.get("running") is True:
        return True
    return False


def _pid_alive(pid: int) -> bool:
    """True if <pid> is a currently-running process; conservative True on uncertainty."""
    try:
        from relay.fleet_reaper import _pid_alive_psutil  # type: ignore
        return bool(_pid_alive_psutil(pid))
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if hasattr(os, "kill"):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours
        except OSError:
            return True
    return True


def main(argv=None) -> int:
    """CLI with two shapes.

    Default (doctor):
        stale_server_check.py <marker_path> <current_head> [<server_running>]
      prints one of no_server / unknown / current / stale.

    Sub-command (start_all's Invoke-PostUpdateTail):
        stale_server_check.py --pyside <changed_path> [<changed_path> ...]
      prints "yes" if any changed path is code the running server imports, else "no".
      The change list may also be supplied one-per-line on stdin when no paths follow
      the flag, which is how a long `git diff --name-only` is passed without hitting a
      command-line length limit.

    ``server_running`` defaults to "1" (doctor only runs this check once /health has
    already answered, so a server is up by construction); pass "0" to model "nothing
    listening". The marker file may be absent -- that is ``unknown``, not an error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--pyside":
        paths = args[1:]
        if not paths:
            paths = sys.stdin.read().splitlines()
        print("yes" if python_side_changed(paths) else "no")
        return 0
    if args and args[0] == "--runlive":
        # Is a fleet/review run LIVE right now? The authoritative signal lives under
        # <fleet_dir> and is defined by relay/fleet_reaper.py: an active-run marker
        # (fleet_run_active.json) whose recorded pid is still alive means live; if the
        # marker is absent/corrupt we fall back to status.json running==True. We only
        # READ here (never finalize) -- start_all must not be the thing that reaps a run.
        # SAFETY DEFAULT: any ambiguity prints "yes" (treat as live) so a server swap is
        # withheld rather than risked against a running review.
        fleet_dir = args[1] if len(args) >= 2 else ".fleet"
        print("yes" if _run_appears_live(fleet_dir) else "no")
        return 0
    if len(args) < 2:
        print("error:usage")
        return 2
    marker_path, current_head = args[0], args[1]
    server_running = True
    if len(args) >= 3:
        server_running = args[2].strip() not in ("0", "", "false", "False")
    started_head = _read_marker(marker_path)
    verdict = classify_staleness(started_head, current_head, server_running)
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
