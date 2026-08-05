"""Per-scope folder allow-lists for the file tools.

What existed before this module: one process-wide list parsed from MCP_ALLOWED_BASE at
import time. That is all-or-nothing (every chat and every fleet lane shares one policy) and
frozen until the server restarts, so there was no way to say "this lane may only touch
D:/work" without also constraining the interactive chat.

This adds three things and changes nothing when it is off:

  * an explicit on/off switch, so restriction is a setting rather than a side effect of
    whether an environment variable happens to be set;
  * per-scope lists, resolved most-specific-first (scope -> global);
  * re-read on change, so editing the settings file takes effect on the next call instead
    of at the next restart -- an access rule you cannot turn on without downtime is one
    people leave off.

Scope identity comes from the authenticated client_id, which is the only thing that
distinguishes callers at tool-call time (the client IP is the tunnel's for everyone, and
tool arguments are supplied by the model and cannot be trusted for an access decision).
That means separating chats or lanes requires issuing a token per scope in main.py's
StaticTokenVerifier; with the single shared token everything resolves to "global", which is
exactly the behaviour that was there before.

Settings file (.fleet/folder_access.json), all keys optional:

    {
      "enabled": false,
      "global":  ["~"],
      "scopes":  {"fleet-w1": ["D:/work/w1"], "chat-main": ["~/Desktop", "~/Documents"]}
    }

DEFAULT-OPEN stays the default: a missing, empty, or unparsable file means unrestricted.
A policy file that cannot be read must never silently lock the tools down -- that failure
mode is indistinguishable from a break-in for the person using it, and the safe direction
here is the one that keeps a working install working.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

POLICY_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "folder_access.json"

_LOCK = threading.Lock()
_CACHE: Optional[dict] = None
_CACHE_MTIME: Optional[float] = None
_CACHE_CHECKED = 0.0
# Re-stat at most this often. The file is read on the hot path of every file tool call, so
# this keeps a settings edit effective within a second without stat-ing per call.
_RECHECK_INTERVAL_S = 1.0


def _empty() -> dict:
    return {"enabled": False, "global": [], "scopes": {}}


def _normalise(raw) -> dict:
    if not isinstance(raw, dict):
        return _empty()
    scopes = raw.get("scopes")
    if not isinstance(scopes, dict):
        scopes = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "global": [s for s in (raw.get("global") or []) if isinstance(s, str) and s.strip()],
        "scopes": {k: [s for s in (v or []) if isinstance(s, str) and s.strip()]
                   for k, v in scopes.items() if isinstance(k, str)},
    }


def load_policy(force: bool = False) -> dict:
    """Current policy, re-read when the file changes. Never raises."""
    global _CACHE, _CACHE_MTIME, _CACHE_CHECKED
    now = time.time()
    with _LOCK:
        if not force and _CACHE is not None and (now - _CACHE_CHECKED) < _RECHECK_INTERVAL_S:
            return _CACHE
        _CACHE_CHECKED = now
        try:
            mtime = POLICY_FILE.stat().st_mtime
        except Exception:
            _CACHE, _CACHE_MTIME = _empty(), None
            return _CACHE
        if not force and _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        try:
            with open(POLICY_FILE, "r", encoding="utf-8") as f:
                _CACHE = _normalise(json.load(f))
        except Exception:
            # Unreadable/corrupt -> stay open rather than lock everyone out.
            _CACHE = _empty()
        _CACHE_MTIME = mtime
        return _CACHE


def current_scope() -> str:
    """The authenticated caller's scope name, or "" when there is no request context."""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
    except Exception:
        return ""
    for attr in ("client_id", "scope_name"):
        try:
            val = getattr(getattr(req, "state", None), attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
    try:
        hdr = req.headers.get("x-mcp-scope", "")
        return hdr.strip() if isinstance(hdr, str) else ""
    except Exception:
        return ""


def _expand(entries: List[str]) -> List[Path]:
    out = []
    for part in entries:
        part = part.strip().strip('"')
        if not part:
            continue
        if len(part) == 2 and part[1] == ":":     # bare drive -> its root
            part = part + "/"
        try:
            out.append(Path(part).expanduser().resolve())
        except Exception:
            continue
    return out


def allowed_bases(scope: Optional[str] = None) -> Optional[List[Path]]:
    """Roots this caller may reach, or None for unrestricted.

    None is returned when the switch is off, and also when the switch is on but no list
    applies to this caller -- "restriction is enabled" and "this caller is restricted" are
    different statements, and turning the feature on must not lock out a scope nobody has
    written a rule for yet.
    """
    pol = load_policy()
    if not pol.get("enabled"):
        return None
    name = current_scope() if scope is None else scope
    entries = pol["scopes"].get(name) if name else None
    if not entries:
        entries = pol.get("global") or []
    bases = _expand(entries)
    return bases or None


def describe(scope: Optional[str] = None) -> dict:
    """Read-only view for the settings UI and for explaining a refusal."""
    pol = load_policy()
    name = current_scope() if scope is None else scope
    bases = allowed_bases(scope)
    return {
        "enabled": bool(pol.get("enabled")),
        "scope": name,
        "matched": "scope" if (name and pol["scopes"].get(name)) else
                   ("global" if pol.get("global") else "none"),
        "allowed": [str(b) for b in (bases or [])],
        "restricted": bases is not None,
        "policy_file": str(POLICY_FILE),
    }
