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
* ``start_all.ps1`` asks ONE question through ``--server-action`` (see ``_cli_server_action``)
  from BOTH places that can stop a running server: ``Invoke-PostUpdateTail`` after its own
  pull (changed paths from ``git diff``), and the daily "server is older than its code"
  check (a start time, compared against every file the server imports). Both get
  :func:`decide_post_update_action` over :func:`fleet_is_running`, so neither can swap the
  server while a fleet run, a review run or a bridge turn is live.

  IT USED TO BE TWO RULES. The post-update tail called ``--pyside`` and ``--runlive`` and
  re-implemented decide_post_update_action by hand in PowerShell (equivalent, measured over
  all 36 output combinations of the two calls before this was wired); the daily check had no
  live-run test at all and killed the server on a top-level mtime, so ``git pull`` plus a
  double-click dropped a live run, and a change in a subpackage was never seen (new-PC
  analysis D12/D30).

The decision functions are pure (no I/O, no side effects) so they can be exercised
directly from pytest on any OS. The I/O -- the run markers, the bridge's /status, file
times -- lives in the underscore readers and the CLI at the bottom, which prints one
verdict word; it is deliberately thin so that what is tested is what runs.

Stdlib only (plus this repository's own tools/deploy_freshness and
bench/ui_build_check, imported lazily by the sub-commands that need them). No
Windows-specific calls: this runs in the Linux CI test job.
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
    # ONE backslash, not two. This read replace("\\\\", "/") -- a DOUBLE backslash -- so a
    # Windows path with single separators ("tools\x.py") was never normalised and was
    # classified as not-server-code; only the doubled form a test happened to use passed
    # (new-PC analysis D29). A doubled separator still classifies after this: "a//b" keeps
    # its prefix.
    p = path.strip().replace("\\", "/")
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


def classify_staleness(started_head, current_head, server_running: bool,
                       watched_changed=None) -> str:
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

    ``watched_changed`` SEPARATES "THE COMMIT MOVED" FROM "THIS SERVER'S CODE MOVED".
    A different SHA was the whole rule until 2026-09-17, so a commit touching only docs, only
    the cockpit, or only tests turned the server dot amber and told the operator that fixes
    were not live -- when nothing the server loads had changed at all. Measured that day: the
    dot went amber three times in an afternoon, every time for a commit, and each time a person
    had to notice and clear it. A dot that is amber for reasons the reader knows are irrelevant
    is a dot that stops being read, which is the failure it exists to prevent.

    The distinction was already in this repository and unused here: tools/deploy_freshness
    knows which paths the server imports (WATCHED, pinned against main.py's real imports), and
    decide_post_update_action twenty lines below already answers "noop -- no server code
    changed" for docs-only updates.

      * ``None``  -- the caller could not tell. The SHA rule stands, which is the conservative
                     side: reporting stale when it might be is better than the reverse.
      * ``False`` -- nothing the server imports changed. A different SHA is then not staleness.
      * ``True``  -- something it imports changed. Stale, as before.
    """
    if not server_running:
        return "no_server"
    sh = (started_head or "").strip()
    cur = (current_head or "").strip()
    if not sh or not cur:
        return "unknown"
    if sh == cur:
        return "current"
    if watched_changed is False:
        return "current"
    return "stale"


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

    THE CONSERVATIVE BRANCHES LIVE IN THE STATES, NOT HERE. A marker whose pid cannot be
    read, a status.json that says running, a bridge that answers but not intelligibly --
    each is handed in as (True, True) by :func:`_run_states`, so this stays a plain ``any``
    and the "unreadable counts as live" rule cannot be lost by calling it directly.
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


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _marker_state(path):
    """(present, pid_alive) for one active-run marker, conservative on a bad pid.

    Not a dict (absent, empty, corrupt) -> (False, False): there is no marker to honour.
    A dict whose pid cannot be read -> (True, True): a marker exists and nothing proves its
    run is dead, so it counts as live -- the rule _run_appears_live always had.
    """
    marker = _load_json(path)
    if not isinstance(marker, dict):
        return (False, False)
    try:
        pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return (True, True)
    return (True, _pid_alive(pid))


def _run_states(fleet_dir: str):
    """The (present, alive) pairs :func:`fleet_is_running` is asked about, for <fleet_dir>.

    1. The fleet run: fleet_run_active.json when it is a usable marker (live iff its pid
       is alive -- a dead pid is a crashed run and is NOT rescued by status.json, exactly
       as before); only when there is no marker does status.json running==True count.
    2. The review run: review_run_active.json, written by bench/review_run.py and resumed
       by the supervisor. It was NOT READ HERE AT ALL, so a server swap could land in the
       middle of a live review pipeline -- the one run the "never drop a live run" rule was
       written for by name.
    """
    fleet = _marker_state(os.path.join(fleet_dir, "fleet_run_active.json"))
    if not fleet[0]:
        status = _load_json(os.path.join(fleet_dir, "status.json"))
        running = isinstance(status, dict) and status.get("running") is True
        fleet = (running, running)
    review = _marker_state(os.path.join(fleet_dir, "review_run_active.json"))
    return [fleet, review]


def _run_appears_live(fleet_dir: str) -> bool:
    """Best-effort: does <fleet_dir> show a LIVE fleet/review run?

    Mirrors relay/fleet_reaper.py's authoritative signal WITHOUT its side effects
    (we never finalize/reap here -- start_all only needs a read). See :func:`_run_states`
    for the order; anything unreadable or ambiguous counts as live (withhold the swap).

    The pid-liveness check reuses relay.fleet_reaper's own helper when importable, so
    this cannot drift from what the reaper considers alive; if that import fails we
    fall back to os.kill(pid, 0)/psutil and, failing even that, to the safe default.
    """
    return fleet_is_running(_run_states(fleet_dir))


def _bridge_state(url):
    """(present, busy) for the chat bridge's /status, the second half of the supervisor's
    idle rule (Invoke-StaleServerCycle: a fleet run OR the bridge reporting turn_running /
    busy). A bridge turn can be in the middle of a tool call through this server.

    Nothing listening -> (False, False): no bridge, no turn -- the ordinary state right after
    a reboot, so it must not block the restart the daily start exists to make. Answered with
    JSON -> busy iff turn_running or busy is true. Anything else (timeout, an error page,
    garbage) -> (True, True): unreadable is busy, as in the supervisor.

    GET /status only. It holds no page lock and drives nothing; the bridge's page-touching
    endpoints are never called from here.
    """
    if not url:
        return (False, False)
    import urllib.error
    import urllib.request
    # NO PROXY. A corporate HTTP_PROXY in the environment would otherwise send a request for
    # 127.0.0.1 to the proxy, and its error page would read as "busy" for ever.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
            return (False, False)
        return (True, True)
    except ConnectionRefusedError:
        return (False, False)
    except Exception:
        return (True, True)
    if not isinstance(body, dict):
        return (True, True)
    return (True, body.get("turn_running") is True or body.get("busy") is True)


def stale_ui_targets(ui_dir: str, targets, extra_inputs=("app.manifest",)):
    """[(name, reason)] for each UI target whose exe has to be (re)built before it is run.

    ``targets`` is [(name, [source.cs, ...])] -- the Build lines of ui/rebuild_ui.ps1 as
    bench/ui_build_check.py parses them, never a list written out here. reason is one of
    ``missing``, ``empty`` (a zero-length exe: an interrupted csc or copy) or
    ``older-than:<file>`` (a source, or the manifest the build embeds, changed after the exe
    was written -- a manual ``git pull`` that nothing rebuilt).

    TRUSTED BY EXISTENCE, IT RAN YESTERDAY'S BINARY. start_all launched ui\\<name>.exe
    whenever the file existed; only its own update path rebuilt, so a pull made by hand left
    the windows on the old code with nothing saying so, and a 0-byte exe was "built"
    (new-PC analysis D27).
    """
    out = []
    for name, sources in targets:
        exe = os.path.join(ui_dir, name + ".exe")
        try:
            st = os.stat(exe)
        except OSError:
            out.append((name, "missing"))
            continue
        if st.st_size == 0:
            out.append((name, "empty"))
            continue
        newest = None
        for rel in list(sources) + [p for p in extra_inputs
                                    if os.path.isfile(os.path.join(ui_dir, p))]:
            try:
                m = os.path.getmtime(os.path.join(ui_dir, rel))
            except OSError:
                continue
            if m > st.st_mtime and (newest is None or m > newest[1]):
                newest = (rel, m)
        if newest:
            out.append((name, "older-than:" + newest[0]))
    return out


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


def _cli_server_action(rest) -> int:
    """start_all's single question: should the running server be stopped?

        --server-action --fleet-dir D [--bridge-status URL] --started-epoch E [--repo R]
            "did anything the server imports change after it started?" -- every file
            tools/deploy_freshness.newer_than finds, which walks tools/ and relay/ RECURSIVELY
            (the old PowerShell scan read their top level only) plus main.py.
        --server-action --fleet-dir D [--bridge-status URL]
            with the ``git diff --name-only`` list on stdin: the post-update form.

    Prints ``why:`` evidence lines, then ONE verdict word as the last line: noop,
    report-only or swap-needed. Anything that goes wrong raises, exits non-zero and prints
    no verdict, which the caller must read as "leave the server alone".
    """
    import argparse

    ap = argparse.ArgumentParser(prog="stale_server_check.py --server-action")
    ap.add_argument("--fleet-dir", required=True)
    ap.add_argument("--bridge-status", default=None)
    ap.add_argument("--started-epoch", type=float, default=None)
    ap.add_argument("--repo", default=_REPO_ROOT)
    a = ap.parse_args(rest)
    if a.started_epoch is not None:
        from tools.deploy_freshness import newer_than
        newer = newer_than(a.started_epoch, a.repo)
        changed = bool(newer)
        for rel, _m in newer[:5]:
            print("why: newer than the running server: %s" % rel.replace("\\", "/"))
    else:
        paths = sys.stdin.read().splitlines()
        changed = python_side_changed(paths)
        for p in [p for p in paths if is_server_code_path(p)][:5]:
            print("why: the update changed %s" % _normalize(p))
    # The markers and the bridge are read only when the code changed: an unchanged server is
    # a no-op whatever is running, which is decide_post_update_action's own first rule.
    states = []
    if changed:
        states = _run_states(a.fleet_dir) + [_bridge_state(a.bridge_status)]
    verdict = decide_post_update_action(changed, fleet_is_running(states))
    for label, (present, alive) in zip(("a fleet run", "a review run", "a bridge turn"), states):
        if present and alive:
            print("why: %s is live (or cannot be proven finished)" % label)
    print(verdict)
    return 0


def _cli_ui_stale() -> int:
    """One line per UI target, ``<name> ok`` or ``<name> rebuild <reason>``, from the Build
    lines bench/ui_build_check.py parses out of ui/rebuild_ui.ps1. That parser raises
    SystemExit when it finds no Build line, which is a non-zero exit here too: a list that
    cannot be read must not come back as "everything is current"."""
    from bench.ui_build_check import UI, targets_from_rebuild_script

    targets = targets_from_rebuild_script()
    stale = dict(stale_ui_targets(UI, targets))
    for name, _sources in targets:
        print("%s rebuild %s" % (name, stale[name]) if name in stale else "%s ok" % name)
    return 0


def main(argv=None) -> int:
    """CLI.

    Default (doctor):
        stale_server_check.py <marker_path> <current_head> [<server_running>]
      prints one of no_server / unknown / current / stale.

    start_all.ps1:
        stale_server_check.py --server-action ...   see _cli_server_action
        stale_server_check.py --ui-stale            see _cli_ui_stale

    Older sub-commands, kept for anything that still calls them (start_all no longer does):
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
    if args and args[0] == "--server-action":
        return _cli_server_action(args[1:])
    if args and args[0] == "--ui-stale":
        return _cli_ui_stale()
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
