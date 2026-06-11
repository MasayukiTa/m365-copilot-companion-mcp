"""relay_fleet.py -- run N AUTONOMOUS relays in parallel (spec §1 fleet x §3/§4 loop).

Where the official Cowork is one autonomous track per user, this drives MANY goals
at once: N Copilot conversations, each pursued to DONE by its own deterministic
relay loop, advanced from a single thread in a non-blocking round-robin. While the
client does one cheap poll, all N agents are thinking server-side in parallel, so
their (slow) turns overlap -- that's the throughput edge.

Each worker reuses the same loop policy as run_relay (PROTOCOL framing; decide
DONE / STUCK / no-progress / FAIL->fix / CONTINUE per turn) but as a non-blocking
state machine so N of them interleave. No threads, no async. The real ceiling is
Microsoft's per-user concurrency / fair-use -- keep N modest (2-5).

  results = run_relay_fleet(context, [goalA, goalB, goalC], agent_url)
"""
from __future__ import annotations

import time

from .copilot_autopilot_relay import (
    CONTINUE_JOB, COPILOT_SELECTORS, CopilotWebDriver, FIX_JOB, PROTOCOL,
    _is_processing, default_notify,
)

TERMINAL = ("done", "stuck", "maxturns", "error")


def _open_fresh(context, url):
    """Open a NEW tab on a fresh chat of the agent. Tolerant of slow navigation
    (a busy Edge can miss the 30s domcontentloaded) -- we proceed and wait for the
    composer to render either way."""
    pg = context.new_page()
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    for _ in range(45):
        pg.wait_for_timeout(1000)
        if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            return pg
    return pg


class RelayWorker:
    """One conversation running one goal to completion, as a non-blocking machine."""

    def __init__(self, page, goal, name, max_turns=12, dwell_s=4.0,
                 per_turn_timeout_s=240, max_no_progress=3):
        self.page = page
        self.drv = CopilotWebDriver(page)
        self.goal = goal
        self.name = name
        self.max_turns = max_turns
        self.dwell_s = dwell_s
        self.per_turn_timeout_s = per_turn_timeout_s
        self.max_no_progress = max_no_progress
        self.job = PROTOCOL + goal
        self.turn = 0
        self.no_progress = 0
        self.last_norm = None
        self.status = "ready"      # ready | waiting | done | stuck | maxturns | error
        self.outcome = None
        self.reason = ""
        self.last_response = ""
        self._count_before = 0
        self._last_text = None
        self._stable_since = None
        self._t_send = 0.0

    def _begin_send(self):
        if self.turn >= self.max_turns:
            self.status, self.outcome, self.reason = "maxturns", "MAXTURNS", "reached max_turns"
            return
        self.turn += 1
        try:
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(self.job)
        except Exception as e:
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "send failed: " + type(e).__name__ + ": " + str(e)
            return
        self._last_text, self._stable_since, self._t_send = None, None, time.time()
        self.status = "waiting"

    def _decide(self, resp):
        self.last_response = resp
        norm = " ".join(resp.lower().split())[:300]
        self.no_progress = self.no_progress + 1 if norm and norm == self.last_norm else 0
        self.last_norm = norm
        up = resp.upper()
        last_line = (resp.strip().splitlines() or [""])[-1].upper()
        if "STUCK" in up:
            self.status, self.outcome, self.reason = "stuck", "STUCK", "agent reported STUCK"
            return
        if "DONE" in up and "FAIL" not in last_line:
            self.status, self.outcome = "done", "DONE"
            return
        if self.no_progress >= self.max_no_progress:
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "no progress for %d turns" % (self.no_progress + 1)
            return
        self.job = FIX_JOB if "FAIL" in last_line else CONTINUE_JOB
        self.status = "ready"

    def poll(self):
        """Advance one non-blocking step. Returns True when terminal."""
        if self.status in TERMINAL:
            return True
        if self.status == "ready":
            self._begin_send()
            return self.status in TERMINAL
        if self.status == "waiting":
            if time.time() - self._t_send > self.per_turn_timeout_s:
                self.status, self.outcome, self.reason = "stuck", "STUCK", "turn timeout"
                return True
            try:
                if self.drv._answers().count() <= self._count_before:
                    return False
            except Exception:
                return False
            t = self.drv.read_last_response()
            if _is_processing(t):
                self._last_text, self._stable_since = None, None
                return False
            if t == self._last_text:
                if self._stable_since and (time.time() - self._stable_since) >= self.dwell_s:
                    self._decide(t)
                    return self.status in TERMINAL
                return False
            self._last_text, self._stable_since = t, time.time()
            return False
        return False


def run_relay_fleet(context, goals, agent_url, max_turns=12, poll_s=1.0,
                    notify=default_notify, on_tick=None):
    """Drive len(goals) autonomous relays in parallel to completion. Returns a list
    of {name, goal, outcome, turns, reason} in goal order. `on_tick(workers)` is
    called after each round-robin sweep (use it to log live progress)."""
    workers = []
    for i, g in enumerate(goals):
        pg = _open_fresh(context, agent_url)       # one fresh chat per goal
        workers.append(RelayWorker(pg, g, "w%d" % i, max_turns=max_turns))

    while any(w.status not in TERMINAL for w in workers):
        for w in workers:
            if w.status in TERMINAL:
                continue
            try:
                w.poll()
            except Exception as e:
                w.status, w.outcome = "error", "ERROR"
                w.reason = type(e).__name__ + ": " + str(e)
        if on_tick:
            try:
                on_tick(workers)
            except Exception:
                pass
        time.sleep(poll_s)

    notify("🛰 並列自律フリート 完了",
           "%d ゴール: %s" % (len(workers), ", ".join(w.outcome or "?" for w in workers)))
    return [{"name": w.name, "goal": w.goal, "outcome": w.outcome,
             "turns": w.turn, "reason": w.reason} for w in workers]
