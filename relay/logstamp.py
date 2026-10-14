# -*- coding: utf-8 -*-
"""A wall-clock prefix for coordinator log lines that carry none.

The incident logs of 2026-10-14 had `[watchdog]`, `[recover]`, `[socket_route] captured` and
`[fanout] rehydrated` lines with no time on them, so the order of a stall, a reset and a capture
could only be inferred from line numbers. `stamp(msg)` puts a local time in front.
"""
import time


def stamp(msg):
    """'HH:MM:SS msg'. A leading newline stays leading so console layouts are unchanged."""
    s = str(msg)
    lead = ""
    while s.startswith("\n"):
        lead += "\n"
        s = s[1:]
    return "%s%s %s" % (lead, time.strftime("%H:%M:%S"), s)
