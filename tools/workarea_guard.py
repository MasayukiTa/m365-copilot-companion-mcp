"""Keep new, untracked files and folders out of the production checkout's ROOT.

THE PROBLEM THIS MEASURES AND THEN STOPS. Workers (and external clients sharing the same
token) were creating fresh top-level folders and files directly under the checkout
(the production checkout root) instead of under the folder a request named or under the per-run work area.
The 2026-10-26 census of ``.fleet/tool_events.jsonl`` over ten days found 114 write-tool
calls whose path sat directly at the checkout root; the offenders were new untracked names
like ``secure_desktop``/``audit_fixes_20261022``/``reviews``/``_pr_body.md``, alongside a
``kasure679_out`` that a request had explicitly pointed at ``%TEMP%`` yet also appeared at the
root. Nothing in the server refused any of it: ``AGENTS.md`` asks for temp under
``%TEMP%\\m365-companion`` and ``relay/fleet_retention.sweep_temp_home`` only sweeps THAT home,
so the checkout root had a convention and no enforcement.

WHAT IS ALLOWED, decided from the real layout rather than a hand-written list:

  * Anything OUTSIDE the checkout. This rule is about one directory; the per-run work area
    (``%TEMP%\\m365-companion\\agents\\<scope>``) and any folder a request named elsewhere are
    not its concern.
  * Writing INTO a path whose top-level segment is already TRACKED by git
    (``git ls-tree --name-only HEAD`` at the root). Updating ``tools/foo.py`` or adding
    ``docs/x/y.md`` lands inside a tracked top-level entry (``tools``/``docs``) and is fine;
    this rule is only about NEW top-level names.
  * A NEW top-level name that the product itself manages at runtime -- the carve-out list in
    ``PRODUCT_ROOT_ALLOW`` (``.fleet``, ``.jobs``, ``generated`` ...), plus anything an
    operator adds through the ``workarea_allow`` setting. The two the brief named explicitly
    (``docs/private/ledger.d`` and ``tools/auto``) already sit inside tracked tops, so they
    pass the tracked test; they need no special entry.

WHAT IS REFUSED: creating a NEW top-level name under the checkout root that is neither
tracked nor on an allow-list.

DEFAULT IS WARN, NOT ENFORCE. The switch (settings key ``workarea_guard`` in settings.txt,
read live like every other each_gate key) ships as ``warn``: every violation is recorded to
``.fleet/workarea_refusals.jsonl`` and the write proceeds. The count in that ledger is the
evidence for flipping it to ``enforce`` -- the same measure-before-you-close posture the
command judge and the folder policy already take, and the one this repository was corrected
for skipping once. ``off`` disables it entirely.

FAIL OPEN. Any error deciding the verdict (git missing, unreadable settings, a path that will
not resolve) leaves the write alone. A guard that cannot read its own inputs must not be the
reason a working install stops writing.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

#: The production checkout root: the parent of the ``tools`` package this file lives in. Same
#: anchor ``folder_policy`` uses, so the two agree on what "the checkout" is.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent

#: Where refusals/warnings are recorded, so the switch can be flipped on evidence.
LEDGER_PATH = CHECKOUT_ROOT / ".fleet" / "workarea_refusals.jsonl"

#: Settings keys, read from the shared settings.txt (tools/settings_path.py) like every other
#: each_gate control, mtime-cached so an edit reaches the next write with no restart.
MODE_KEY = "workarea_guard"
ALLOW_KEY = "workarea_allow"
_MODES = ("off", "warn", "enforce")
DEFAULT_MODE = "warn"

#: New top-level names the PRODUCT creates and manages as part of its own operation. These are
#: allowed to appear at the root even when untracked. Kept here (not only in git) because some
#: are gitignored runtime state that never gets tracked yet is legitimate.
PRODUCT_ROOT_ALLOW = frozenset({
    ".fleet",            # fleet state (ledgers, commands, swe clones)
    ".jobs",             # background job records (tools/jobs.py)
    ".config",           # the shared settings.txt lives here
    ".companion_runs",   # run transcripts
    ".companion_gates",  # approval queue
    ".setup",            # resumable bootstrap (setup.bat)
    "generated",         # connector helper embeds (gitignored except template)
    ".update_backups",
    ".update_conflicts",
})

_LOCK = threading.Lock()
_SETTINGS_CACHE: dict = {}

_TRACKED_CACHE: Optional[frozenset] = None
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
        out = {}
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
    """Current enforcement mode: 'off' | 'warn' | 'enforce'. Settings decide; default 'warn'.

    Read fresh from settings.txt on every call (mtime-cached), so an operator flipping the
    switch reaches the next write with no restart. The environment variable
    ``MCP_WORKAREA_GUARD`` beats the file, for tests and emergencies, and is reported as a
    conflict by status.
    """
    env = (os.environ.get("MCP_WORKAREA_GUARD") or "").strip().lower()
    if env in _MODES:
        return env
    v = (_settings_values().get(MODE_KEY) or "").strip().lower()
    return v if v in _MODES else DEFAULT_MODE


def _allow_extra() -> List[str]:
    """Operator-added top-level names from the ``workarea_allow`` setting (comma/; separated)."""
    raw = _settings_values().get(ALLOW_KEY) or ""
    parts: List[str] = []
    for chunk in raw.replace(";", ",").split(","):
        c = chunk.strip().strip('"').rstrip("/").lower()
        if c:
            parts.append(c)
    return parts


def _tracked_tops() -> frozenset:
    """Top-level names tracked by git at the checkout root. Cached briefly; empty on any error."""
    global _TRACKED_CACHE, _TRACKED_AT
    now = time.time()
    with _LOCK:
        if _TRACKED_CACHE is not None and (now - _TRACKED_AT) < _TRACKED_TTL_S:
            return _TRACKED_CACHE
    try:
        from tools import childproc
        r = childproc.run(
            ["git", "-C", str(CHECKOUT_ROOT), "ls-tree", "--name-only", "HEAD"],
            timeout=10,
        )
        tops = frozenset(n.strip().rstrip("/").lower()
                         for n in r.stdout.splitlines() if n.strip()) if r.returncode == 0 else frozenset()
    except Exception:
        tops = frozenset()
    with _LOCK:
        _TRACKED_CACHE, _TRACKED_AT = tops, now
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
      'allow'    -- under the root but into a tracked top or a product-managed name.
      'violation'-- a NEW top-level name that is neither tracked nor product-managed.
    Never raises: an undecidable path is reported 'allow' with the reason, so the caller
    fails open.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception as e:
        return "allow", "unresolvable path, left alone: %s" % e, None
    seg = _top_segment(resolved)
    if seg is None:
        return "outside", "outside the checkout root", None
    if resolved == CHECKOUT_ROOT:
        return "allow", "the checkout root itself", None
    low = seg.lower()
    if low in PRODUCT_ROOT_ALLOW:
        return "allow", "product-managed root entry", seg
    if low in _allow_extra():
        return "allow", "operator allow-list (workarea_allow)", seg
    if low in _tracked_tops():
        return "allow", "tracked top-level entry", seg
    return "violation", "new untracked top-level name at the checkout root", seg


def _allowed_places_text() -> str:
    return ("(a) \u4f9d\u983c\u306e\u6587\u9762\u3067\u5229\u7528\u8005\u304c\u6307\u5b9a\u3057\u305f\u30d5\u30a9\u30eb\u30c0\u3001"
            "(b) \u8d70\u884c\u3054\u3068\u306e\u4f5c\u696d\u5834\u6240 %TEMP%\\m365-companion\\agents\\<\u8d70\u884c>\\\u3001"
            "(c) \u65e2\u306b\u8ffd\u8de1\u3055\u308c\u3066\u3044\u308b\u5834\u6240\u3078\u306e\u66f4\u65b0\u3002")


def _record(path: str, resolved_seg: Optional[str], enforced: bool, reason: str) -> None:
    """Append one line to the refusals ledger. Never raises."""
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 3),
            "path": str(path),
            "top": resolved_seg,
            "action": "refused" if enforced else "warned",
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
    """Enforce the placement rule for a write to ``path``.

    Called from the single path chokepoint (``file_ops._validate_path``). In 'enforce' it
    raises ``PermissionError`` for a violation; in 'warn' it records and returns; in 'off' it
    does nothing. Everything but a genuine violation returns silently.
    """
    m = mode()
    if m == "off":
        return
    try:
        verdict, reason, seg = classify(path)
    except Exception:
        return  # fail open
    if verdict != "violation":
        return
    enforced = (m == "enforce")
    _record(path, seg, enforced, reason)
    if enforced:
        raise PermissionError(
            "\u672c\u756a\u30c1\u30a7\u30c3\u30af\u30a2\u30a6\u30c8\u306e\u76f4\u4e0b\u306b\u306f\u65b0\u3057\u3044\u30d5\u30a9\u30eb\u30c0\u30fb\u30d5\u30a1\u30a4\u30eb\u3092\u4f5c\u308c\u307e\u305b\u3093"
            " (%s)\u3002\u8a31\u3055\u308c\u308b\u5834\u6240: " % path + _allowed_places_text())


def _reset_for_test() -> None:
    """Drop the settings and tracked caches. For tests that rewrite settings.txt in place."""
    global _TRACKED_CACHE, _TRACKED_AT
    with _LOCK:
        _SETTINGS_CACHE.clear()
        _TRACKED_CACHE, _TRACKED_AT = None, 0.0
