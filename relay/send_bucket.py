# -*- coding: utf-8 -*-
"""A per-minute token bucket for pacing sends to a shared upstream quota.

WHY A BUCKET AND NOT A MINIMUM GAP. The fleet paced socket sends with "at least 1.5 s since the
previous send", asked by each worker as the sweep polled it. A sweep that takes tens of seconds
polls every ready worker within milliseconds, so the first one sent and every other one in that
sweep saw "1.5 s has not passed" and waited for the NEXT sweep: one send per sweep, about two
a minute, against a limit of a hundred (2026-10-14: 189 goals, 143 minutes, 5% of the quota
used at peak). A gap is a statement about spacing in time; the sweep has no time between its
polls to spend it in. The upstream's limit is a RATE, and a bucket states a rate: tokens accrue
continuously, a send takes one, and however many are in the bucket when the sweep arrives may
go out in that sweep. The capacity bounds the burst, which is the thing that was measured to be
refused (twenty at once; eight spaced over twelve seconds were not).

Pure and clock-injected: the caller passes `now`, the rate and the capacity on every call, so
the rate can follow the operator's live setting and a throttle without the bucket holding a
second copy of either.
"""
from __future__ import annotations

import threading


class TokenBucket:
    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = None          # None = full on first use
        self._last = None

    def _refill(self, now, rate_per_min, capacity):
        cap = max(0.0, float(capacity))
        if self._tokens is None or self._last is None:
            self._tokens, self._last = cap, float(now)
            return
        dt = max(0.0, float(now) - self._last)
        self._last = float(now)
        # A lowered capacity (a throttle) clips what is already stored: a bucket that kept its
        # old burst through a throttle would spend it into the quota that just refused us.
        self._tokens = min(cap, self._tokens + dt * max(0.0, float(rate_per_min)) / 60.0)

    def available(self, now, rate_per_min, capacity):
        """Whole sends that may go out right now."""
        with self._lock:
            self._refill(now, rate_per_min, capacity)
            return int(self._tokens + 1e-9)

    def take(self, now, rate_per_min, capacity, n=1):
        """Spend `n` tokens if present; False (and nothing spent) otherwise."""
        with self._lock:
            self._refill(now, rate_per_min, capacity)
            if self._tokens + 1e-9 >= n:
                self._tokens = max(0.0, self._tokens - n)
                return True
            return False

    def spend(self, now, rate_per_min, capacity, n=1):
        """Record a send that already happened (never goes below zero)."""
        with self._lock:
            self._refill(now, rate_per_min, capacity)
            self._tokens = max(0.0, self._tokens - n)

    def reset(self):
        with self._lock:
            self._tokens = None
            self._last = None
