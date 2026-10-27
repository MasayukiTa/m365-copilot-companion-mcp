"""How many goals may run at once over the socket route: the cloud rate and a RAM safeguard.

REPLACES THE 2026-10-14 OVERRIDE ("1000 lanes, RAM ignored"). That override removed a
gate that measured the wrong resource (browser TABS, which a socket goal does not hold) by
removing the gate. The resources a socket goal actually uses are two, and each has its own bound:

  * THE CLOUD RATE -- the bound that decides throughput. It is enforced where it is spent, at
    the send: a token bucket (rate = min(100, rate_ceiling_rpm), burst 8, in relay_fleet:
    socket_send_due / note_socket_sent). Goals beyond what the rate can feed simply wait READY.
    No lane count can raise throughput past it. The default ceiling is 50, bounded by the
    historical rate estimate (tools/settings_keys.derive_lane_ceiling).

  * RAM IN THIS PROCESS -- what a held conversation costs. MEASURED, NOT ASSUMED (2026-08-25,
    nineteen cold runs): one worker at a time put the fleet process at 244.0 MB, three at a time
    at 237.7 MB -- a fixed residency of about 250 MB that does not grow with the worker count --
    and one conversation was measured at 1.9 MB. So the real marginal cost of a goal is a few
    MB at most. `socket_goal_mb` (settings, default 25) is deliberately more than ten times
    that: a conservative price per goal, settable, used only as headroom arithmetic:

        cap = min(lane_ceiling, active_goals + (free_RAM - ram_floor) // socket_goal_mb)

    HOW THE SAFEGUARD BEHAVES. It gates the ADMISSION of new goals and nothing else.
      - it never drains: `cap >= active_goals` always, so a RAM dip never stops, kills or pauses
        a goal that is running (the old autoscale shrank the cap below the running count);
      - it never deadlocks: with nothing running one goal is always admitted;
      - it moves only the next admission: goals already admitted keep their place.
    Free RAM is also a displayed metric (status.json avail_mb); it is not a concurrency number
    in any other place.

NOTHING HERE TOUCHES BROWSER TABS: a goal holds none (relay/no_tab.py).
"""
from __future__ import annotations

import os

#: Conservative price of one held socket conversation, MB. See the measurement above; the
#: declared default lives in tools/settings_keys.py (key `socket_goal_mb`) so the panel/doc
#: value and this one cannot drift.
from tools.settings_keys import default as _declared_default

GOAL_MB_DEFAULT = float(os.environ.get("MCP_SOCKET_GOAL_MB") or _declared_default("socket_goal_mb"))


def goal_cost_mb(default=None) -> float:
    """`socket_goal_mb` from settings.txt (read fresh), else the declared default. Never <= 0."""
    base = GOAL_MB_DEFAULT if default is None else float(default)
    try:
        from relay.fleet_runner import _settings_float
        value = float(_settings_float("socket_goal_mb", base))
    except Exception:
        value = base
    return value if value > 0 else base


def headroom_goals(avail_mb, floor_mb, cost_mb) -> int:
    """How many MORE goals fit: free RAM above the floor, divided by the per-goal price."""
    try:
        room = float(avail_mb) - float(floor_mb)
        return max(0, int(room // max(0.001, float(cost_mb))))
    except (TypeError, ValueError):
        return 0


def ram_safeguard_cap(active_goals, avail_mb, floor_mb, cost_mb, ceiling) -> int:
    """The cap on concurrent socket goals. Never below `active_goals` (no draining) and never
    below 1 (no deadlock); never above `ceiling` unless more are already running."""
    active = max(0, int(active_goals))
    cap = min(max(1, int(ceiling)), active + headroom_goals(avail_mb, floor_mb, cost_mb))
    return max(cap, active, 1)


def admits_another_goal(active_goals, cap) -> bool:
    """True when one more socket goal fits under `cap`. With nothing running, always True."""
    active = max(0, int(active_goals))
    return active == 0 or active < max(1, int(cap))
