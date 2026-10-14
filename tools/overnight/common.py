"""Small shared pieces: JST clock (injectable), atomic file writes, append-only log."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


class Clock:
    """Real clock. Tests pass a fake with the same two methods."""

    def now(self) -> datetime:
        return datetime.now(JST)

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


def to_epoch(dt: datetime) -> float:
    return dt.timestamp()


def from_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, JST)


def fmt_dt(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, (int, float)):
        dt = from_epoch(dt)
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_text(path, text: str) -> None:
    """Write via a temp file in the same directory, then replace: a crash leaves the old file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, p)


def atomic_write_json(path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


class Log:
    """Append-only, timestamped; also echoed to stdout. Never raises."""

    def __init__(self, path, clock: Clock | None = None, echo: bool = True):
        self.path = Path(path)
        self.clock = clock or Clock()
        self.echo = echo

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (fmt_dt(self.clock.now()), msg)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            try:
                print(line, flush=True)
            except Exception:
                try:
                    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
                except Exception:
                    pass
