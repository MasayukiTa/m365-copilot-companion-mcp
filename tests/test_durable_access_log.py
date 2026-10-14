"""The request record must outlive a server restart (tools/access_log.py).

`.setup/logs/server.log` is recreated on every launch and has no timestamps; a restart erased
five days of `POST /mcp` evidence. These tests pin the properties of the replacement: appended,
never truncated, timestamped, restart-delimited, secret-free, rotating, and unable to hurt a
request when the disk misbehaves.

Run: python -m pytest -q tests/test_durable_access_log.py
"""
from __future__ import annotations

import logging
import logging.config
import os
import re

import pytest
import uvicorn.config

from tools import access_log

STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"


@pytest.fixture
def uvicorn_loggers():
    """Apply the config the way uvicorn does, then put the loggers back as they were."""
    names = ("uvicorn", "uvicorn.access", "uvicorn.error")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate,
                 logging.getLogger(n).level) for n in names}
    yield
    for n, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(n)
        for h in list(lg.handlers):
            if h not in handlers:
                try:
                    h.close()
                except Exception:
                    pass
        lg.handlers = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def _apply(path):
    cfg = access_log.build_log_config(uvicorn.config.LOGGING_CONFIG, path=path)
    logging.config.dictConfig(cfg)
    return cfg


def _flush():
    for n in ("uvicorn", "uvicorn.access"):
        for h in logging.getLogger(n).handlers:
            h.flush()


def test_handler_is_a_rotating_append_handler(tmp_path, uvicorn_loggers):
    path = tmp_path / "access.log"
    _apply(path)
    handlers = [h for h in logging.getLogger("uvicorn.access").handlers
                if isinstance(h, access_log.SafeRotatingFileHandler)]
    assert len(handlers) == 1
    h = handlers[0]
    assert isinstance(h, logging.handlers.RotatingFileHandler)
    assert h.mode == "a"
    assert h.maxBytes > 0 and h.backupCount >= 10
    # uvicorn.error propagates to "uvicorn", which carries the handler too.
    assert any(isinstance(x, access_log.SafeRotatingFileHandler)
               for x in logging.getLogger("uvicorn").handlers)
    # and uvicorn's own stdout/stderr handlers are still there.
    assert len(logging.getLogger("uvicorn.access").handlers) >= 2


def test_two_starts_keep_earlier_lines(tmp_path, uvicorn_loggers):
    path = tmp_path / "access.log"
    _apply(path)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "POST", "/mcp", "1.1", 200)
    _flush()
    first = path.read_text(encoding="utf-8")
    assert "POST /mcp" in first

    # a second process start: new config applied, marker written, file not truncated
    for h in list(logging.getLogger("uvicorn.access").handlers):
        h.close()
    assert access_log.write_start_marker(path)
    _apply(path)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5001", "GET", "/health", "1.1", 200)
    _flush()
    text = path.read_text(encoding="utf-8")
    assert text.startswith(first)
    assert "GET /health" in text
    assert " START pid=%d " % os.getpid() in text


def test_lines_start_with_utc_iso_milliseconds(tmp_path, uvicorn_loggers):
    path = tmp_path / "access.log"
    _apply(path)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "POST", "/mcp", "1.1", 200)
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    _flush()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert re.match("^" + STAMP + " uvicorn", line), line
    assert "uvicorn.access INFO" in lines[0]
    assert "uvicorn.error INFO" in lines[1]


def test_start_marker_has_pid_time_and_git(tmp_path):
    path = tmp_path / "access.log"
    assert access_log.write_start_marker(path, now=0)
    line = path.read_text(encoding="utf-8").strip()
    assert re.match("^1970-01-01T00:00:00.000Z START pid=%d started=1970-01-01T00:00:00.000Z "
                    "git=\\S+$" % os.getpid(), line), line


def test_query_values_are_not_recorded(tmp_path, uvicorn_loggers):
    path = tmp_path / "access.log"
    _apply(path)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET",
        "/mcp?token=SECRETVALUE&api_key=ABC123&x=1", "1.1", 401)
    _flush()
    text = path.read_text(encoding="utf-8")
    assert "SECRETVALUE" not in text and "ABC123" not in text
    assert "token=<redacted>" in text and "api_key=<redacted>" in text
    assert "GET /mcp?" in text and " 401" in text


def test_write_failure_never_raises_and_reports_once(tmp_path, uvicorn_loggers, capsys,
                                                      monkeypatch):
    # the "file" is a directory, so every write fails
    blocked = tmp_path / "access.log"
    blocked.mkdir()
    monkeypatch.setattr(access_log, "_reported", False)
    _apply(blocked)
    lg = logging.getLogger("uvicorn.access")
    for _ in range(3):
        lg.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 200)
    err = capsys.readouterr().err
    assert err.count("access log: cannot write") == 1
    assert access_log.write_start_marker(blocked) is False


def test_prepare_falls_back_to_the_base_config(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(access_log, "build_log_config", boom)
    monkeypatch.setattr(access_log, "write_start_marker", lambda *a, **k: True)
    base = {"version": 1}
    assert access_log.prepare(base) is base


def test_rotation_keeps_numbered_generations(tmp_path):
    h = access_log.SafeRotatingFileHandler(str(tmp_path / "access.log"), maxBytes=200,
                                           backupCount=3)
    h.setFormatter(access_log.AccessFormatter())
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "x" * 80, None, None)
    for _ in range(20):
        h.handle(rec)
    h.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["access.log", "access.log.1", "access.log.2", "access.log.3"]


def test_default_location_is_setup_logs_and_not_fleet():
    p = access_log.log_path().as_posix()
    assert p.endswith(".setup/logs/access.log")
    assert "/.fleet/" not in p


def test_retention_does_not_delete_access_log_generations(tmp_path):
    from relay import fleet_retention
    fleet = tmp_path / ".fleet"
    logs = tmp_path / ".setup" / "logs"
    fleet.mkdir()
    logs.mkdir(parents=True)
    names = ["access.log"] + ["access.log.%d" % i for i in range(1, 8)]
    for n in names:
        (logs / n).write_text("line\n")
        (fleet / n).write_text("line\n")          # even if someone points it into .fleet
    (fleet / "faulthandler.log.5").write_text("x")  # control: ordinary rotations still go
    fleet_retention.apply(str(fleet), now=4_000_000_000)
    assert sorted(os.listdir(logs)) == sorted(names)
    assert sorted(n for n in os.listdir(fleet) if n.startswith("access.log")) == sorted(names)
    assert not (fleet / "faulthandler.log.5").exists()
