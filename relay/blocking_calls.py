# -*- coding: utf-8 -*-
"""A process-wide register of the synchronous browser calls that are holding the sweep thread.

WHY THIS EXISTS. The stall watchdog (fleet_runner._watchdog) treats "status.json stopped
advancing" as "the Edge is wedged" and hard-resets it. But the sweep thread stops writing
status.json for a second, entirely ordinary reason as well: it is inside a synchronous browser
call -- opening a tab, a token capture, a consent click-through -- that is slow on a loaded
machine. Only one such call (the mid-run socket->tab fallback) ever told the watchdog so, by
setting a per-worker deadline that rode out through status.json. Every other call that blocks
for the same reason was invisible, so the watchdog killed the Edge underneath the very call it
was waiting on, the call died with TargetClosedError, recovery restarted the same call, and the
cycle repeated until the recovery budget ran out (2026-10-14, four consecutive runs).

THE FIX IS TO MAKE DECLARING A BLOCK THE ONLY WAY TO BLOCK, and to make it reach the watchdog
without going through the thing that is frozen. The watchdog lives in the same process as the
sweep, so it can read this register directly: no snapshot has to be flushed before the freeze,
no field has to be remembered by whoever adds the next synchronous call. A caller wraps the
call in ``with blocking("tab_open", ceiling_s)`` and is covered from the first instruction.

EVERY DECLARATION HAS A DEADLINE. A block that never ends is a wedge, and the watchdog must still
be able to recover from one: once ``ceiling_s`` has passed the entry stops counting. The ceiling
is the operation's own bound (for a tab open: three navigations plus composer waits), not a
generous round number.
"""
from __future__ import annotations

import contextlib
import itertools
import os
import threading
import time

#: The bound on one synchronous token capture on the caller's thread: tier 2 (page open, about
#: a minute worst case) plus tier 3 (a real turn: a tab open of up to 210s and two 60s
#: captures). The async helper's 90s kill does not apply to this path.
SYNC_CAPTURE_CEILING_S = float(os.environ.get("MCP_SYNC_CAPTURE_CEILING_S", "420") or 420)

#: The bound on one synchronous tab open (_open_fresh): three navigations of 45s with a 25s
#: composer wait each, or ~300s behind a sign-in page.
TAB_OPEN_CEILING_S = float(os.environ.get("MCP_TAB_OPEN_CEILING_S", "300") or 300)

_LOCK = threading.Lock()
_ENTRIES = {}                      # id -> (kind, started, deadline)
_IDS = itertools.count(1)


def _now():
    return time.time()


def begin(kind, ceiling_s, now=None):
    """Declare a blocking call; returns a token for ``end``. Never raises."""
    t = _now() if now is None else float(now)
    try:
        ceiling = max(0.0, float(ceiling_s))
    except (TypeError, ValueError):
        ceiling = 0.0
    with _LOCK:
        token = next(_IDS)
        _ENTRIES[token] = (str(kind), t, t + ceiling)
    return token


def end(token):
    """Withdraw a declaration. Idempotent."""
    with _LOCK:
        _ENTRIES.pop(token, None)


@contextlib.contextmanager
def blocking(kind, ceiling_s):
    """``with blocking("tab_open", 300): ...`` -- declared before, withdrawn after, always."""
    token = begin(kind, ceiling_s)
    try:
        yield token
    finally:
        end(token)


def active(now=None):
    """The declarations still inside their own ceiling: [(kind, started, deadline), ...].

    An entry past its deadline is NOT returned. It stays in the register until its owner
    ``end``s it (the owner may still be stuck), but it no longer vouches for the browser.
    """
    t = _now() if now is None else float(now)
    with _LOCK:
        return sorted(e for e in _ENTRIES.values() if e[2] > t)


def clear():
    """Test seam."""
    with _LOCK:
        _ENTRIES.clear()
