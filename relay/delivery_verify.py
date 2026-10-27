# -*- coding: utf-8 -*-
"""Before giving up a timed-out acting turn, ask whether it was DELIVERED.

THE PROBLEM, MEASURED (.fleet/status.json, 2026-10-27 15:53). After a reboot the fleet ran ~300
workers at 90-100%% CPU with 1.6-2.5 GB RAM free. 90 workers went STUCK with
``not re-sent: the turn may already have been delivered and this goal performs an action
(delivery=unknown, turn timeout without a reply)``. run_id r6ae04307_a0; turn 2 x72, 1 x16, 3 x2;
mostly fan-out children carrying acting goals (implement a call feature, confirm PR 180, fix PR
184, PR #304). Under load a turn's reply timed out, and the harness refused to re-send to avoid a
duplicate -- so it stopped.

WHY THE REFUSAL WAS TOO BLUNT. The timeout path in relay/relay_fleet.py hard-coded
``delivery="unknown"`` when it called ``_refuse_resend``: it never looked at whether THIS turn had
actually been delivered. The evidence already exists -- the socket driver keeps ``partial_text()``
(tokens that arrived for this turn) and ``_turn_answered`` -- and the reconnect path already reads
exactly that as ``saw_output``. The timeout refusal just did not consult it.

WHAT DELIVERY EVIDENCE IS, AND WHAT IT IS NOT. A turn that produced OUTPUT reached the model: the
frame was delivered and the model began acting, so re-sending it would ask for the act again --
the worker should keep WAITING for the reply, not end STUCK. A turn that produced NO output at all
is the honest ``unknown``: the frame may or may not have arrived (a dropped socket mid-flight
delivers nothing observable either way), so the careful refuse-on-landed default is kept unless
the effect itself can be checked absent (``resend_decision_for_landed_act``).

So this is not a new guess bolted on. It replaces a HARD-CODED ``unknown`` with the delivery
status the driver already measured, and only the ``delivered`` reading changes behaviour: it
turns a premature STUCK into continued waiting.

DEFAULT ON (``verify_delivery_before_giving_up``). With it off nothing here runs: the timeout
refusal keeps its old hard-coded ``delivery="unknown"`` and ends the worker exactly as before.
"""
from __future__ import annotations

import os

KEY = "verify_delivery_before_giving_up"
DEFAULT = "on"

_cache = {}


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def mode(path=None) -> str:
    """`on` or `off`, from settings.txt. Default on; anything but an explicit `off` is `on`. Never raises.

    One os.stat per read and a re-read only when the file changed, so a change made in the cockpit
    reaches the next decision with no restart (declared each_gate in tools/settings_keys.py).
    """
    try:
        p = _settings_path(path)
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = DEFAULT
        with open(p, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = "off" if v == "off" else "on"
                    break
        _cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT


def verify_on(path=None) -> bool:
    return mode(path) == "on"
