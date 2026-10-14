"""A request record that survives a server restart.

WHY THIS EXISTS. `.setup/logs/server.log` is uvicorn's stdout as captured by the supervisor, and
the supervisor recreates it on every server launch. Its lines carry no timestamp, and the
previous launch is kept only as the last 400 lines (`server.out.history.log`). So when a
restart happened, the record of which `POST /mcp` calls arrived before it was gone -- on
2026-10-13 a restart cut roughly 140,000 lines to 10,000 and took five days of requests with
it, exactly the evidence an incident analysis needed.

WHAT THIS DOES. The server process itself (not the supervisor, whose running copy cannot be
changed without restarting it) writes uvicorn's `uvicorn.access` and `uvicorn.error` records to
`.setup/logs/access.log`:

  * append-only (mode "a"), never truncated at start; size-rotated, `access.log.1` .. `.N`;
  * every line starts with a UTC ISO 8601 timestamp with milliseconds and a trailing `Z`;
  * one `START` line per process: pid, start time, git HEAD -- the restart boundaries;
  * query-string VALUES are replaced by `<redacted>` (names are kept). uvicorn already logs
    only the request line and status, never headers or bodies, but a client could still put a
    token in a query string, and an access log must not become a second place secrets live;
  * a write failure never reaches a request. It is reported on stderr once per process.

The existing stdout output is left alone (the supervisor still writes server.log); this file is
an additional copy, not a replacement.

It lives under `.setup/logs`, not `.fleet`: `relay/fleet_retention.py` only ever sweeps `.fleet`
(and its `*.log.<n>` rotation rule would delete exactly these files if they were there).
"""
from __future__ import annotations

import copy
import datetime
import logging
import logging.handlers
import os
import re
import subprocess
import sys
from pathlib import Path

#: Base name. Retention must never treat it (or access.log.<n>) as disposable.
ACCESS_LOG_NAME = "access.log"

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BACKUPS = 10

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HANDLER_NAME = "durable_access_file"
_FORMATTER_NAME = "durable_access"

#: `"GET /path?a=b&c=d HTTP/1.1"` -> keep the path and the parameter names, drop the values.
_REQUEST_QUERY = re.compile(r'("[A-Z]+ [^\s?"]*)\?([^\s"]*)')


def log_dir() -> Path:
    """Where the file goes: the same `.setup/logs` the supervisor already writes into."""
    override = os.environ.get("MCP_ACCESS_LOG_DIR")
    if override:
        return Path(override)
    return _REPO_ROOT / ".setup" / "logs"


def log_path() -> Path:
    return log_dir() / ACCESS_LOG_NAME


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def _redact_query(match: "re.Match[str]") -> str:
    pairs = []
    for part in match.group(2).split("&"):
        if not part:
            continue
        name = part.split("=", 1)[0]
        pairs.append(name + "=<redacted>")
    return match.group(1) + "?" + "&".join(pairs)


def redact(text: str) -> str:
    return _REQUEST_QUERY.sub(_redact_query, text)


def utc_stamp(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


class AccessFormatter(logging.Formatter):
    """`<UTC ISO 8601 ms>Z <logger> <LEVEL> <message>`, query values redacted."""

    def __init__(self, *args, **kwargs):
        super().__init__("%(asctime)s %(name)s %(levelname)s %(message)s")

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API)
        return utc_stamp(record.created)

    def format(self, record):
        # The message is rendered by the base class, then redacted, so a query string that
        # arrives through record.args is covered as well as one already in the message.
        return redact(super().format(record))


_reported = False


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Append-only rotating file whose failures stay inside the logging system."""

    def __init__(self, filename, mode="a", maxBytes=DEFAULT_MAX_BYTES,
                 backupCount=DEFAULT_BACKUPS, encoding="utf-8", delay=True):
        # mode is accepted so a config cannot silently turn this into a truncating handler.
        super().__init__(filename, mode="a", maxBytes=maxBytes, backupCount=backupCount,
                         encoding=encoding, delay=True)

    def handleError(self, record):  # noqa: N802 (logging API)
        global _reported
        if not _reported:
            _reported = True
            try:
                sys.stderr.write("access log: cannot write %s (further failures not "
                                 "reported)\n" % self.baseFilename)
            except Exception:
                pass

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


def _git_head() -> str:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_REPO_ROOT),
                             capture_output=True, text=True, timeout=5,
                             creationflags=flags)
        sha = (out.stdout or "").strip()
        return sha if out.returncode == 0 and sha else "unknown"
    except Exception:
        return "unknown"


def write_start_marker(path=None, now=None) -> bool:
    """Append one line saying a server process began: the restart boundary."""
    try:
        path = Path(path) if path else log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        import time
        stamp = utc_stamp(time.time() if now is None else now)
        line = "%s START pid=%d started=%s git=%s\n" % (stamp, os.getpid(), stamp, _git_head())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        return False


def build_log_config(base: dict, path=None) -> dict:
    """`base` (uvicorn's LOGGING_CONFIG) plus a durable file handler on uvicorn's loggers.

    Passed to uvicorn.run(log_config=...). Adding the handler to the logger objects beforehand
    would not work: uvicorn applies its own dictConfig at startup and that removes handlers it
    did not configure.
    """
    cfg = copy.deepcopy(base)
    target = str(Path(path) if path else log_path())
    cfg.setdefault("formatters", {})[_FORMATTER_NAME] = {"()": AccessFormatter}
    cfg.setdefault("handlers", {})[_HANDLER_NAME] = {
        "class": "tools.access_log.SafeRotatingFileHandler",
        "formatter": _FORMATTER_NAME,
        "filename": target,
        "mode": "a",
        "maxBytes": _int_env("MCP_ACCESS_LOG_MAX_BYTES", DEFAULT_MAX_BYTES),
        "backupCount": _int_env("MCP_ACCESS_LOG_BACKUPS", DEFAULT_BACKUPS),
        "encoding": "utf-8",
        "delay": True,
    }
    loggers = cfg.setdefault("loggers", {})
    # uvicorn.error propagates to "uvicorn", so one handler there covers both of its streams.
    for name in ("uvicorn", "uvicorn.access"):
        entry = loggers.setdefault(name, {})
        entry["handlers"] = list(entry.get("handlers", [])) + [_HANDLER_NAME]
    return cfg


def prepare(base: dict):
    """Write the start marker and return the log_config to hand to uvicorn.

    Never raises; if anything fails the caller gets `base` back unchanged, so the server
    starts exactly as it did before this module existed.
    """
    try:
        write_start_marker()
        return build_log_config(base)
    except Exception:
        return base
