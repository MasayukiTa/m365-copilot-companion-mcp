"""Things the batch asks of the machine: stay awake, free disk, the owner's disk floor, a toast."""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
from datetime import datetime, timedelta

from tools.overnight.common import JST

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

#: Used only when the settings file cannot be read at all; the fleet's own default is 6 GB.
FALLBACK_FLOOR_GB = 6.0
#: Stop queuing a little before the floor, so the work already running can still write.
FLOOR_MARGIN_GB = 0.5


@contextlib.contextmanager
def keep_awake():
    """Ask Windows not to sleep while the batch runs (SetThreadExecutionState, released on exit).

    The flag belongs to the calling thread, so the whole batch runs on that thread. Elsewhere
    (Linux CI) this does nothing.
    """
    kernel = None
    if sys.platform == "win32":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            kernel = None
    try:
        yield kernel is not None
    finally:
        if kernel is not None:
            try:
                kernel.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass


def free_disk_gb(path) -> float:
    p = str(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p or ".").free / float(1 << 30)


def disk_floor_gb() -> float:
    """The owner's reserved free space, read the same way the fleet reads it (never written).

    settings.txt `disk_floor_gb=N` wins, then env SWE_DISK_FLOOR_GB, then the fleet default.
    """
    try:
        from tools.settings_path import settings_file
        path = settings_file()
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as fh:
                for ln in fh.read().splitlines():
                    if ln.startswith("disk_floor_gb="):
                        return float(ln.split("=", 1)[1].strip())
    except Exception:
        pass
    try:
        return float(os.environ.get("SWE_DISK_FLOOR_GB", FALLBACK_FLOOR_GB))
    except ValueError:
        return FALLBACK_FLOOR_GB


def toast(title: str, body: str, launch: str = "") -> None:
    """Windows toast through the repository's own helper. Failure is ignored."""
    if sys.platform != "win32":
        return
    try:
        from tools.notify_ops import notify_desktop
        notify_desktop(title, body, launch=launch)
    except Exception:
        pass


def parse_clock(text: str, now: datetime, after: datetime | None = None) -> datetime:
    """'HH:MM' or 'YYYY-MM-DD HH:MM' (JST) -> aware datetime.

    A bare HH:MM means the next time that clock reads so, counted from `after` (default now);
    except that a time at most 10 minutes in the past means "right now" so a late start still starts.
    """
    s = text.strip().replace("：", ":")
    base = (after or now).astimezone(JST)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=JST)
        except ValueError:
            pass
    try:
        hh, mm = s.split(":")
        t = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        raise ValueError("cannot read the time %r (use HH:MM)" % text)
    if after is None and base - t <= timedelta(minutes=10):
        return t if t >= base else base
    if t <= base:
        t += timedelta(days=1)
    return t
