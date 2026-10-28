"""Refuse a fleet worker's write into the TRACKED SOURCE of the production checkout.

WHAT BROKE, AND WHY THIS EXISTS. 2026-10-27 14:10-14:27 Fleet Cockpit crashed three times
on launch with an unhandled NullReferenceException. The cause was not the UI code by itself:
a fleet worker (ui_perf, the "cockpit responsive with many workers" change) wrote an
UNFINISHED edit DIRECTLY into the production checkout's tracked UI source
(``ui/FleetCockpit.History.cs`` / ``.Rows.cs`` / ``.Tick.cs``, +112 lines) instead of into a
clone, ``ui/rebuild_ui.ps1`` then rebuilt FROM the production checkout, and the half-written
binary died on its first tick. A worker must work in a clone; the checkout is the thing the
operator runs.

WHAT THIS GUARD DOES. The write tools all funnel path validation through
``file_ops._validate_path`` (``write_file``, ``create_directory``, ``copy_path``, and every
file write ``run_python`` / ``shell`` / ``edit_and_verify`` perform through it). On a WRITE
(``for_write=True``) this guard asks one question: does the resolved path land on a TRACKED
SOURCE file under the production checkout? "Tracked source" means a path whose top-level
segment is one git actually tracks -- ``ui``, ``relay``, ``tools``, ``bridge``, ``scripts``
and the rest of ``git ls-files`` -- not ``.fleet`` runtime state, not an untracked scratch
file (that is ``workarea_guard``'s concern), not anything outside the checkout.

WHO IS REFUSED, AND HOW OPERATIONS IS TOLD APART FROM A WORKER. The production checkout is
updated by OPERATIONS (a ``git merge``, ``rebuild_ui.ps1``), and those never go through these
write tools -- git and PowerShell write the files directly, below this layer. So "a write tool
was used to change tracked source under the checkout" is, by construction, a WORKER write, and
that is what this refuses. The one escape for a human operator who deliberately drives a write
tool at the checkout is an explicit opt-out they set on their OWN session:
``MCP_LIVE_CHECKOUT_WRITE_OK=1`` (or the ``live_checkout_write_allow`` scope list naming the
authenticated client_id). Absent that marker the caller is treated as a worker.

THE SWITCH. Settings key ``live_checkout_write_guard`` in settings.txt, read live like every
other each_gate key, mtime-cached:

  off    do nothing.
  warn   (DEFAULT) record the write to ``.fleet/live_write_refused.jsonl`` and LET IT PROCEED.
         The count in that ledger is the evidence for flipping it to ``block``.
  block  refuse the write with ``PermissionError`` carrying the reason and the
         "work in a clone" instruction, AND record it. Blocking does not stop the worker's
         job -- the refusal is returned to it so it can redo the write against a clone (e.g.
         ``git worktree add C:/Users/<you>/AppData/Local/Temp/m365-work/<name>``).

The environment variable ``MCP_LIVE_CHECKOUT_WRITE_GUARD`` (off|warn|block) beats the file,
for tests and emergencies, and is reported as a conflict by status.

FAIL OPEN. Any error deciding the verdict (git missing, unreadable settings, an unresolvable
path) leaves the write alone. A guard that cannot read its own inputs must never be the reason
a working install stops writing.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

#: The production checkout root: the parent of the ``tools`` package this file lives in. Same
#: anchor ``folder_policy`` and ``workarea_guard`` use, so all three agree on "the checkout".
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent

#: Where refusals/warnings are recorded, so the switch can be flipped on evidence.
LEDGER_PATH = CHECKOUT_ROOT / ".fleet" / "live_write_refused.jsonl"

#: Settings keys, read from the shared settings.txt (tools/settings_path.py) like every other
#: each_gate control, mtime-cached so an edit reaches the next write with no restart.
MODE_KEY = "live_checkout_write_guard"
ALLOW_KEY = "live_checkout_write_allow"
_MODES = ("off", "warn", "block")
DEFAULT_MODE = "warn"

#: The operator opt-out env marker. A human running a write tool at the checkout on purpose
#: sets this on their own session; a fleet worker's child environment does not carry it.
OPERATOR_ENV = "MCP_LIVE_CHECKOUT_WRITE_OK"

#: Env override of the mode itself (tests / emergencies). off|warn|block.
MODE_ENV = "MCP_LIVE_CHECKOUT_WRITE_GUARD"

_LOCK = threading.Lock()
_SETTINGS_CACHE: dict = {}

_TRACKED_TOPS_CACHE: Optional[frozenset] = None
_TRACKED_AT = 0.0
_TRACKED_TTL_S = 5.0


def _settings_values() -> dict:
    """{MODE_KEY, ALLOW_KEY} from settings.txt, mtime-cached. Never raises."""
    try:
        from tools.settings_path import settings_file
        path = settings_file()
        if not path:
            return {}
        st = os.stat(path)
        ckey = (path, st.st_mtime_ns, st.st_size)
        with _LOCK:
            hit = _SETTINGS_CACHE.get(path)
            if hit is not None and hit[0] == ckey:
                return hit[1]
        out: dict = {}
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                for k in (MODE_KEY, ALLOW_KEY):
                    if ln.startswith(k + "="):
                        out[k] = ln.split("=", 1)[1].strip()
        with _LOCK:
            _SETTINGS_CACHE[path] = (ckey, out)
        return out
    except Exception:
        return {}


def mode() -> str:
    """Current guard mode: 'off' | 'warn' | 'block'. Settings decide; default 'warn'.

    Read fresh from settings.txt on every call (mtime-cached), so flipping the switch reaches
    the next write with no restart. ``MCP_LIVE_CHECKOUT_WRITE_GUARD`` beats the file.
    """
    env = (os.environ.get(MODE_ENV) or "").strip().lower()
    if env in _MODES:
        return env
    v = (_settings_values().get(MODE_KEY) or "").strip().lower()
    return v if v in _MODES else DEFAULT_MODE


def _allow_scopes() -> frozenset:
    """The authenticated client_ids an operator declared may write the live checkout.

    ``live_checkout_write_allow`` is a comma/space separated list of scope names. Empty by
    default: the normal way an operator opts out is the per-session env marker, this list is
    for a named scope that should always be treated as operations.
    """
    raw = (_settings_values().get(ALLOW_KEY) or "").strip()
    if not raw:
        return frozenset()
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    return frozenset(parts)


def _is_operator() -> bool:
    """True when THIS caller is operations, not a fleet worker.

    Two ways to be operations: the per-session env marker is set, or the authenticated scope
    (folder_policy.current_scope -> the token client_id) is on the allow-list. A fleet worker
    carries neither. Never raises.
    """
    try:
        if (os.environ.get(OPERATOR_ENV) or "").strip() in ("1", "true", "yes", "on"):
            return True
    except Exception:
        pass
    try:
        allow = _allow_scopes()
        if allow:
            from . import folder_policy
            scope = (folder_policy.current_scope() or "").strip()
            if scope and scope in allow:
                return True
    except Exception:
        pass
    return False


def _tracked_tops() -> frozenset:
    """Lower-cased set of top-level names git tracks at the checkout root, TTL-cached.

    Derived from ``git ls-files`` so it reflects the real tree rather than a hand list:
    ``ui``, ``relay``, ``tools``, ``bridge``, ``scripts`` and every other tracked top plus
    tracked root files (e.g. ``main.py``). Empty on any git failure -> guard fails open.
    """
    global _TRACKED_TOPS_CACHE, _TRACKED_AT
    now = time.time()
    with _LOCK:
        if _TRACKED_TOPS_CACHE is not None and (now - _TRACKED_AT) < _TRACKED_TTL_S:
            return _TRACKED_TOPS_CACHE
    tops: frozenset = frozenset()
    try:
        from tools import childproc
        r = childproc.run(
            ["git", "-C", str(CHECKOUT_ROOT), "ls-files"],
            timeout=10,
        )
        if r.returncode == 0:
            seg = set()
            for ln in r.stdout.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                top = ln.split("/", 1)[0]
                if top:
                    seg.add(top.lower())
            tops = frozenset(seg)
    except Exception:
        tops = frozenset()
    with _LOCK:
        _TRACKED_TOPS_CACHE, _TRACKED_AT = tops, now
    return tops


def _top_segment(resolved: Path) -> Optional[str]:
    """The first path segment under the checkout root, or None if not under it."""
    try:
        rel = resolved.relative_to(CHECKOUT_ROOT)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return parts[0]


def classify(path: str) -> Tuple[str, str, Optional[str]]:
    """Decide a write to ``path``. Returns (verdict, reason, top_segment).

    verdict is one of:
      'outside'  -- not under the checkout root; not this guard's concern.
      'untracked'-- under the root but NOT into a tracked top (workarea_guard's concern, or
                    legitimate runtime state like .fleet); this guard leaves it alone.
      'tracked'  -- a write that lands on a tracked-source top under the production checkout.
    Never raises: an undecidable path is reported 'outside' so the caller fails open.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception as e:
        return "outside", "unresolvable path, left alone: %s" % e, None
    seg = _top_segment(resolved)
    if seg is None:
        return "outside", "outside the production checkout", None
    if resolved == CHECKOUT_ROOT:
        return "outside", "the checkout root itself", None
    if seg.lower() in _tracked_tops():
        return "tracked", "tracked source under the production checkout", seg
    return "untracked", "not a tracked source path", seg


def _clone_hint() -> str:
    base = os.path.join(os.environ.get("TEMP", r"%TEMP%"), "m365-work", "<name>")
    return ("Work in a CLONE, never the production checkout. Create one from the branch you "
            "need, e.g.  git worktree add \"%s\" origin/main  (or git clone), make the edit "
            "there, and build/test from the clone. The production checkout is the binary the "
            "operator runs; a half-written edit in it crashes the live UI." % base)


def _record(path: str, seg: Optional[str], blocked: bool, reason: str) -> None:
    """Append one line to the refusals ledger. Never raises."""
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 3),
            "path": str(path),
            "top": seg,
            "action": "refused" if blocked else "warned",
            "reason": reason,
            "mode": mode(),
        }
        try:
            from . import folder_policy as _fp
            row["scope"] = _fp.current_scope()
        except Exception:
            pass
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def check_write(path: str) -> None:
    """Enforce the live-checkout rule for a write to ``path``.

    Called from the single path chokepoint (``file_ops._validate_path``, for_write only). In
    'block' it raises ``PermissionError`` for a tracked-source write by a worker; in 'warn' it
    records and returns; in 'off' it does nothing. An operator (env marker or allow-listed
    scope) is never refused. Everything but a genuine worker write to tracked source returns
    silently.
    """
    m = mode()
    if m == "off":
        return
    try:
        verdict, reason, seg = classify(path)
    except Exception:
        return  # fail open
    if verdict != "tracked":
        return
    try:
        if _is_operator():
            return  # operations updates the checkout through git/PowerShell paths of its own
    except Exception:
        return  # cannot tell -> fail open, do not block
    blocked = (m == "block")
    _record(path, seg, blocked, reason)
    if blocked:
        raise PermissionError(
            "Refused: a fleet worker must not write the production checkout's tracked source "
            "(%s) under %s. %s" % (path, describe()["checkout_root"], _clone_hint()))


def describe() -> dict:
    """Read-only view for the settings UI and for explaining a refusal."""
    return {
        "mode": mode(),
        "default": DEFAULT_MODE,
        "modes": list(_MODES),
        "checkout_root": str(CHECKOUT_ROOT),
        "ledger": str(LEDGER_PATH),
        "operator_env": OPERATOR_ENV,
        "allow_scopes": sorted(_allow_scopes()),
    }


def _reset_for_test() -> None:
    """Drop the settings and tracked caches. For tests that rewrite settings.txt in place."""
    global _TRACKED_TOPS_CACHE, _TRACKED_AT
    with _LOCK:
        _SETTINGS_CACHE.clear()
        _TRACKED_TOPS_CACHE, _TRACKED_AT = None, 0.0
