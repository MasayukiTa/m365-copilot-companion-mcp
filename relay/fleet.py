"""Fleet -- drive N Copilot conversations concurrently from one laptop (spec §1).

The trick: a single thread polls every worker non-blockingly in a round-robin.
While the client is doing one cheap poll, ALL N agents are thinking server-side in
parallel, so their (slow) turn times overlap. A work queue with work-stealing keeps
every worker busy: the first worker to go idle grabs the next pending subtask.

This is "small platoon", not a farm: real parallelism is capped by Microsoft's
per-user concurrency / fair-use ceiling (spec §1, §6), NOT by the laptop. Keep N
modest (2-4). No threads, no async -- one thread, cooperative polling.

  results = run_fleet(context, [agent_url]*3, ["task A", "task B", "task C", "task D"])
  # 3 workers chew through 4 subtasks; results are returned in subtask order.
"""
from __future__ import annotations

import time

from .copilot_autopilot_relay import (
    COPILOT_SELECTORS, CopilotWebDriver, _is_processing, default_notify,
)


def open_fresh(context, agent_url: str):
    """Open a NEW page on a fresh chat of the given agent and wait for its composer."""
    pg = context.new_page()
    pg.goto(agent_url, wait_until="domcontentloaded")
    for _ in range(40):
        pg.wait_for_timeout(1000)
        if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            break
    return pg


class FleetWorker:
    """One conversation tab, driven as a non-blocking state machine."""

    def __init__(self, page, name: str):
        self.page = page
        self.drv = CopilotWebDriver(page)
        self.name = name
        self.state = "idle"        # idle | waiting
        self.cur_idx = None
        self.count_before = 0
        self.last_text = None
        self.stable_since = None
        self.t_start = 0.0
        self.result = None

    def start(self, idx: int, task: str) -> None:
        self.cur_idx = idx
        try:
            self.count_before = self.drv._answers().count()
        except Exception:
            self.count_before = 0
        self.drv._count_before = self.count_before
        self.last_text, self.stable_since = None, None
        self.t_start = time.time()
        self.state = "waiting"
        self.drv.send(task)        # one-shot Q->A; no CONTINUE/DONE protocol per task

    def poll(self, dwell_s: float, timeout_s: float) -> bool:
        """One non-blocking check. Returns True when the answer is final (result set)."""
        if self.state != "waiting":
            return False
        if time.time() - self.t_start > timeout_s:
            self.result = "(timeout)"
            self.state = "idle"
            return True
        try:
            if self.drv._answers().count() <= self.count_before:
                return False
        except Exception:
            return False
        t = self.drv.read_last_response()
        if _is_processing(t):
            self.last_text, self.stable_since = None, None
            return False
        if t == self.last_text:
            if self.stable_since and (time.time() - self.stable_since) >= dwell_s:
                self.result = t
                self.state = "idle"
                return True
        else:
            self.last_text, self.stable_since = t, time.time()
        return False


def run_fleet(context, agent_urls, subtasks, dwell_s: float = 4.0,
              per_task_timeout_s: float = 600.0, poll_s: float = 2.0,
              notify=default_notify):
    """Run `subtasks` across len(agent_urls) parallel workers with work-stealing.
    Returns results in the SAME order as `subtasks`. Each agent_url gets its own
    fresh-chat page; pass [agent_url]*N to fan out N chats on a single agent."""
    workers = [FleetWorker(open_fresh(context, u), f"w{i}") for i, u in enumerate(agent_urls)]
    queue = list(enumerate(subtasks))          # (idx, task) preserving order
    results: dict[int, str] = {}

    def assign(w):
        if queue:
            idx, task = queue.pop(0)
            w.start(idx, task)
            return True
        return False

    for w in workers:                          # prime every worker
        assign(w)

    while queue or any(w.state == "waiting" for w in workers):
        for w in workers:
            if w.state == "waiting":
                if w.poll(dwell_s, per_task_timeout_s):
                    results[w.cur_idx] = w.result
                    assign(w)                  # work-stealing: grab the next pending
            elif w.state == "idle":
                assign(w)
        time.sleep(poll_s)

    notify("🧹 Fleet 完了", f"{len(subtasks)} サブタスクを {len(workers)} 並列で処理")
    return [results.get(i, "(missing)") for i in range(len(subtasks))]
