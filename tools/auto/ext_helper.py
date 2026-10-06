# -*- coding: utf-8 -*-
"""Optional analysis helper: a thin, disabled-by-default wrapper around an external program.

Nothing here is active unless the operator sets MCP_EXT_HELPER=1 in the environment. When it is
not set, the tools below are not registered at all (see `_auto_tools_enabled`, which
tools/auto_loader.py consults) and, as a second gate, each function refuses to run.

The program itself is NOT part of this repository. It lives in a folder the operator names with
MCP_EXT_HELPER_DIR and must be a file called `helper_entry.py` directly inside that folder. It is
started as a child process: JSON request on stdin, JSON result on the last line of stdout.
MCP_EXT_HELPER_PYTHON optionally names the interpreter (default: the server's own).

Every public function returns a JSON string and never raises into the server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools import childproc

_ENTRY_NAME = "helper_entry.py"
_MAX_OUT = 60000                  # characters accepted from the child; a safety net, not a design
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VER_RE = re.compile(r"^[A-Za-z0-9._-]{0,16}$")
_SET_RE = re.compile(r"^[A-Za-z0-9_.-]{0,32}$")
_TIMEOUTS = {"judge": 360, "negctl": 960, "verify": 150, "list": 60, "explain": 60}
_ENV_ALLOW = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
              "HOMEPATH", "APPDATA", "LOCALAPPDATA")


def _enabled() -> bool:
    """Exactly "1" enables; anything else (unset, "0", "true", "") does not."""
    return os.environ.get("MCP_EXT_HELPER", "0") == "1"


def _entry():
    """The helper program's path, or None. It must sit directly inside the named folder."""
    raw = os.environ.get("MCP_EXT_HELPER_DIR", "").strip()
    if not raw:
        return None
    try:
        folder = Path(raw).resolve()
        entry = (folder / _ENTRY_NAME).resolve()
        if entry.parent != folder or not entry.is_file():
            return None
        return entry
    except Exception:
        return None


def _auto_tools_enabled() -> bool:
    """The loader's gate: register these tools only when enabled and the program is present."""
    if not _enabled():
        return False
    if _entry() is None:
        sys.stderr.write("[ext_helper] enabled but entry not found\n")
        return False
    return True


def _err(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code}, ensure_ascii=False)


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() in _ENV_ALLOW or k.upper().startswith("MCP_EXT_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _call(op: str, args: dict) -> str:
    """Run the helper program for one request and return its JSON text."""
    try:
        if not _enabled():
            return _err("DISABLED", "disabled")
        entry = _entry()
        if entry is None:
            return _err("NOT_AVAILABLE", "not available")
        py = os.environ.get("MCP_EXT_HELPER_PYTHON", "").strip() or sys.executable
        payload = json.dumps({"op": op, "args": args}, ensure_ascii=False).encode("utf-8")
        proc = subprocess.Popen(
            [py, "-B", str(entry)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(entry.parent), env=_child_env(), shell=False,
            **childproc.tree_popen_kwargs(headless=True))
        try:
            raw_out, _raw_err = proc.communicate(input=payload, timeout=_TIMEOUTS.get(op, 60))
        except subprocess.TimeoutExpired:
            childproc.kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return _err("TIMEOUT", "timed out")
        text = childproc.decode(raw_out).strip()
        last = text.splitlines()[-1].strip() if text else ""
        if not last:
            return _err("NOT_AVAILABLE", "not available")
        if len(last) > _MAX_OUT:
            return _err("OUTPUT_TRUNCATED", "output too large")
        try:
            json.loads(last)
        except Exception:
            return _err("BAD_OUTPUT", "bad output")
        return last
    except Exception:
        return _err("NOT_AVAILABLE", "not available")


def _bad(name: str) -> str:
    return _err("BAD_ARGS", "invalid " + name)


def _version_ok(v) -> bool:
    return isinstance(v, str) and _VER_RE.match(v) is not None


def validity_list_claims(engine_version: str = "", set_name: str = "") -> str:
    """Optional analysis helper, list (disabled by default)."""
    if not _enabled():
        return _err("DISABLED", "disabled")
    if not _version_ok(engine_version):
        return _bad("engine_version")
    if not isinstance(set_name, str) or not _SET_RE.match(set_name):
        return _bad("set_name")
    return _call("list", {"engine_version": engine_version, "set_name": set_name})


def validity_judge(config_id: str, engine_version: str = "") -> str:
    """Optional analysis helper, run (disabled by default)."""
    if not _enabled():
        return _err("DISABLED", "disabled")
    if not isinstance(config_id, str) or not _ID_RE.match(config_id):
        return _bad("config_id")
    if not _version_ok(engine_version):
        return _bad("engine_version")
    return _call("judge", {"config_id": config_id, "engine_version": engine_version})


def validity_negative_control(config_id: str, m: int = 200, engine_version: str = "") -> str:
    """Optional analysis helper, repeat (disabled by default)."""
    if not _enabled():
        return _err("DISABLED", "disabled")
    if not isinstance(config_id, str) or not _ID_RE.match(config_id):
        return _bad("config_id")
    if not _version_ok(engine_version):
        return _bad("engine_version")
    if isinstance(m, bool):
        return _bad("m")
    try:
        m = max(1, min(1000, int(m)))
    except Exception:
        return _bad("m")
    return _call("negctl", {"config_id": config_id, "m": m, "engine_version": engine_version})


def validity_verify_lock(engine_version: str = "") -> str:
    """Optional analysis helper, check (disabled by default)."""
    if not _enabled():
        return _err("DISABLED", "disabled")
    if not _version_ok(engine_version):
        return _bad("engine_version")
    return _call("verify", {"engine_version": engine_version})


def validity_explain_reason(code: str, engine_version: str = "") -> str:
    """Optional analysis helper, describe (disabled by default)."""
    if not _enabled():
        return _err("DISABLED", "disabled")
    if not isinstance(code, str) or len(code) > 128:
        return _bad("code")
    if not _version_ok(engine_version):
        return _bad("engine_version")
    return _call("explain", {"code": code.strip(), "engine_version": engine_version})


_TOOL_NAMES = ("validity_list_claims", "validity_judge", "validity_negative_control",
               "validity_verify_lock", "validity_explain_reason")


def _apply_docs(folder=None) -> None:
    """Take the tools' descriptions from the operator's folder (tool_docs.json), if present.

    Called at import only when enabled, i.e. before the loader registers the functions.
    Never raises."""
    try:
        base = Path(folder) if folder else Path(os.environ.get("MCP_EXT_HELPER_DIR", "").strip())
        data = json.loads((base / "tool_docs.json").read_text(encoding="utf-8-sig"))
        for name, doc in data.items():
            fn = globals().get(name) if name in _TOOL_NAMES else None
            if fn is not None and isinstance(doc, str) and doc.strip():
                fn.__doc__ = doc
    except Exception:
        pass


if _enabled():
    _apply_docs()
