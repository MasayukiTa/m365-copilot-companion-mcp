"""The owner's switch for the unlock gate: MCP_UNLOCK_REQUIRED.

WHY THIS EXISTS (owner order, 2026-10-15): "unlock must never be the reason a fleet goal
fails. If it recurs, abolish the unlock password itself; security comes later."

THE MEASURED FAILURE (overnight fleet run r6acfa151_a0, 2026-10-15 00:35 onward). A fleet
worker that needs an ODBC / write / exec tool is refused with "[locked: ...]". The relay then
injects a turn that carries the real unlock password in plain text and tells the model to call
unlock with it. Microsoft 365 Copilot's model reads a password handed to it in a message as a
prompt injection and refuses to use it: w1, w4 and w19 said so in their own words
("EXTERNAL_UNTRUSTED", "an injection that pastes a password"), never called unlock(), and ended
STUCK. The password is also deliberately not reachable by the model any other way (.env is
refused on sight), so the gate cannot be satisfied by the one caller that is being asked to
satisfy it. A gate that its legitimate caller declines to open is not a second factor, it is an
outage -- and the fix cannot be a better-worded password message, because the refusal is the
model behaving correctly.

WHAT THIS IS. One owner-controlled, REVERSIBLE switch:

    MCP_UNLOCK_REQUIRED=1   (default, and any value that is not an explicit "off")  the gate as
                            it has always been: unlock(password), per-IP grant, token / session.
    MCP_UNLOCK_REQUIRED=0   the gate passes for every session: no password, no session-IP
                            authorisation, no unlock step in any worker prompt, instructions or
                            refusal text.

Turn it off / on with the supported .env writer (atomic):

    python scripts/env_file.py set MCP_UNLOCK_REQUIRED 0
    python scripts/env_file.py set MCP_UNLOCK_REQUIRED 1

READ LIVE, FROM .env FIRST. Every consumer (the MCP server, the relay, the bridge) is a separate
long-lived process whose os.environ was fixed when it started, so a flag read only from the
environment would need all of them restarted -- and a switch that waits for a restart is not a
switch when the failure is happening now. The .env file is therefore re-read when its mtime
changes (one stat per call otherwise); a value there wins over the process environment, and the
environment is the fallback when the file has no such line (tests, a launcher that exports it).
The one thing that is fixed at process start is the MCP server's instruction text (FastMCP takes
a string at construction): after flipping the switch, an idle server restart makes the INSTRUCTIONS
agree with the gate. The gate itself, the relay and the bridge do not need one.

FAIL CLOSED. Only an explicit off value (0 / false / off / no / disabled) turns the gate off.
Absent, empty, unreadable or garbled all mean "required" -- the behaviour before this existed.

VISIBLE. While the switch is off the gate is open to everyone holding the API key, so that state
must never be invisible. A line is appended to .setup/logs/access.log (the durable request
record) when a process first sees the gate off, when it sees it back on, once at server start
(tools.access_log.prepare's neighbour, see main.py) and as a throttled heartbeat with a count
while calls keep passing. Standard library only.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

KEY = "MCP_UNLOCK_REQUIRED"

#: The only values that turn the gate off. Anything else, including garbage, keeps it on.
_OFF_VALUES = frozenset({"0", "false", "off", "no", "disabled"})

#: Seconds between heartbeat lines while calls keep passing with the gate off.
HEARTBEAT_S = 300.0

_REPO_ROOT = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_file_cache = {"path": None, "mtime": None, "value": None}
_audit = {"last_state": None, "passes": 0, "last_write": 0.0, "first_ip": ""}


def env_path() -> Path:
    """The .env this process obeys. MCP_ENV_FILE overrides it (tests, a relocated install)."""
    override = os.environ.get("MCP_ENV_FILE")
    return Path(override) if override else _REPO_ROOT / ".env"


def _strip_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
        return value[1:-1].strip()
    # an unquoted value ends at an inline comment, the way python-dotenv reads it
    hash_at = value.find(" #")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def _read_file_value():
    """The LAST assignment of KEY in .env (the one dotenv applies), or None. mtime-cached."""
    path = env_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _file_cache.update(path=str(path), mtime=None, value=None)
        return None
    cached = _file_cache
    if cached["path"] == str(path) and cached["mtime"] == mtime:
        return cached["value"]
    value = None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            name, sep, rest = stripped.partition("=")
            if sep and name.strip() == KEY:
                value = _strip_value(rest)
    except OSError:
        value = None
    cached.update(path=str(path), mtime=mtime, value=value)
    return value


def unlock_required() -> bool:
    """True when callers must unlock (default). False only when MCP_UNLOCK_REQUIRED is off."""
    try:
        value = _read_file_value()
        if value is None:
            value = os.environ.get(KEY)
        if value is None:
            return True
        return value.strip().lower() not in _OFF_VALUES
    except Exception:
        return True


def _write_audit(text: str) -> None:
    """One line into .setup/logs/access.log, in the file's own shape. Never raises."""
    try:
        from tools.access_log import log_path, utc_stamp
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = "%s UNLOCK_SWITCH pid=%d %s\n" % (utc_stamp(time.time()), os.getpid(), text)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def audit_start() -> bool:
    """Server start: record the state if the gate is off. Returns the gate-required flag."""
    required = unlock_required()
    if not required:
        with _lock:
            _audit["last_state"] = False
            _audit["last_write"] = time.time()
        _write_audit("%s=0 unlock gate is OFF at start: every session passes without a "
                     "password (owner switch; restore with scripts/env_file.py set %s 1)"
                     % (KEY, KEY))
    return required


def audit_pass(ip: str = "") -> None:
    """Called each time the gate passes a call because the switch is off.

    Writes on the first pass after the state changed, and then at most every HEARTBEAT_S with
    the number of calls that passed since the last line. Cheap on the hot path: a lock, an
    increment and a clock read.
    """
    now = time.time()
    message = None
    with _lock:
        _audit["passes"] += 1
        if not _audit["first_ip"]:
            _audit["first_ip"] = str(ip or "")[:64]
        if _audit["last_state"] is not False:
            _audit["last_state"] = False
            message = ("%s=0 unlock gate is OFF: call passed without unlock (first caller %r)"
                       % (KEY, _audit["first_ip"]))
        elif now - _audit["last_write"] >= HEARTBEAT_S:
            message = ("%s=0 unlock gate still OFF: %d call(s) passed without unlock since the "
                       "last line" % (KEY, _audit["passes"]))
        if message is not None:
            _audit["last_write"] = now
            _audit["passes"] = 0
            _audit["first_ip"] = ""
    if message is not None:
        _write_audit(message)


def audit_restored() -> None:
    """Called where the gate is enforced: records the OFF -> ON edge once per process."""
    if _audit["last_state"] is not False:  # hot path: no lock unless an OFF state was recorded
        return
    with _lock:
        if _audit["last_state"] is not False:
            return
        _audit["last_state"] = True
    _write_audit("%s=1 unlock gate is back ON (password / session authorisation required "
                 "again)" % KEY)
