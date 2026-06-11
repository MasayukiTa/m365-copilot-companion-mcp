"""relay_fleet.py -- run N AUTONOMOUS relays in parallel (spec §1 fleet x §3/§4 loop).

Where the official Cowork is one autonomous track per user, this drives MANY goals
at once: N Copilot conversations, each pursued to DONE by its own deterministic
relay loop, advanced from a single thread in a non-blocking round-robin. While the
client does one cheap poll, all N agents are thinking server-side in parallel, so
their (slow) turns overlap -- that's the throughput edge.

MEMORY DISCIPLINE (why this is not just "open N tabs"):
  Each M365 Copilot tab is a heavy SPA (~0.3-0.6 GB). On a 16 GB laptop already
  running other work, opening many at once exhausts RAM -- Edge then crashes, and
  when it auto-restarts WITHOUT --remote-debugging-port the CDP endpoint is gone and
  the whole run dies (observed). So this fleet:
    * never opens all N tabs up front -- it keeps at most `max_concurrent` open,
    * sizes `max_concurrent` to *available* physical memory (GlobalMemoryStatusEx),
    * CLOSES each conversation's tab the instant it reaches a terminal state, which
      frees that RAM and lets the next queued goal open. Resuming = just run again;
      a fresh tab is opened for each goal.

Each worker reuses the same loop policy as run_relay (PROTOCOL framing; decide
DONE / STUCK / no-progress / FAIL->fix / CONTINUE per turn) but as a non-blocking
state machine so the open ones interleave. No threads, no async.

  results = run_relay_fleet(context, [goalA, goalB, goalC], agent_url)
"""
from __future__ import annotations

import ctypes
import time

from .acceptance import Check, normalize_checks
from .copilot_autopilot_relay import (
    CONTINUE_JOB, COPILOT_SELECTORS, CopilotWebDriver, FIX_JOB, PROTOCOL,
    REFUTE_FIX_JOB, VERIFY_FIX_JOB, _is_processing, default_notify,
)

TERMINAL = ("done", "stuck", "maxturns", "error", "cancelled")
# non-terminal but not yet occupying a tab; counts as "still running" for the loop.
PENDING = "pending"


class FleetContextLost(Exception):
    """Raised when the underlying Edge/CDP context died mid-run (wedged or hard-reset).
    Carries the goals that had not finished, so the runner can reconnect and resume."""
    def __init__(self, unfinished):
        super().__init__("fleet CDP context lost")
        self.unfinished = unfinished


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def avail_phys_mb() -> float:
    """Available physical memory in MB (Windows). Best-effort; ~4 GB on failure."""
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / (1024.0 * 1024.0)
    except Exception:
        return 4096.0


def auto_concurrency(n_goals, per_tab_mb=700, headroom_mb=2048, hard_cap=4):
    """How many heavy M365 tabs we can afford open at once, given free RAM right now.
    Keep `headroom_mb` for the user's other work; budget `per_tab_mb` per Copilot tab;
    never exceed `hard_cap` (Microsoft per-user fair-use also wants N modest)."""
    fit = int((avail_phys_mb() - headroom_mb) / per_tab_mb)
    return max(1, min(n_goals, fit, hard_cap))


def _open_fresh(context, url):
    """Open a NEW tab on a fresh chat of the agent. Tolerant of slow navigation
    (a busy Edge can miss the 30s domcontentloaded) -- we proceed and wait for the
    composer to render either way. If a sign-in page appears, the background Edge is
    surfaced once so the user can authenticate."""
    from .edge_recover import surface, looks_like_login
    pg = context.new_page()
    surfaced = False
    # Up to 3 navigation attempts: a failed goto leaves the tab on about:blank, and
    # waiting 45s for a composer that will never come just leaves about:blank on screen.
    # Detect about:blank early (~4s) and RE-navigate instead of staring at it.
    for attempt in range(3):
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        for k in range(25):
            pg.wait_for_timeout(1000)
            if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                return pg
            try:
                u = pg.url or ""
                if not surfaced and looks_like_login(u):
                    surface(); surfaced = True
                elif u == "about:blank" and k >= 3:
                    break                      # stuck on about:blank -> re-navigate
            except Exception:
                pass
    return pg


def goal_fields(goal):
    """Normalize a goal into (text, checks, cwd). A goal is either a plain string
    (no acceptance check -- legacy/back-compat) or a dict
    {"text"|"goal": str, "check"|"checks": dict|list, "cwd": str}. This is how a
    goal carries machine-checkable acceptance criteria into the verification gate."""
    if isinstance(goal, dict):
        text = goal.get("text") or goal.get("goal") or ""
        checks = normalize_checks(goal.get("checks") or goal.get("check"))
        cwd = goal.get("cwd") or None
        return text, checks, cwd
    return str(goal), [], None


class RelayWorker:
    """One conversation running one goal to completion, as a non-blocking machine.
    Starts WITHOUT a tab (status 'pending'); attach() opens one, close() frees it.

    Acceptance gate (spec 3-3): if the goal carries `checks`, a Copilot "DONE" does
    NOT end the worker -- it moves to the 'verifying' state, where the frame runs the
    checks LOCALLY (acceptance.Check). Pass -> real DONE; fail -> the actual failure is
    re-injected and the agent keeps working, up to max_verify_attempts (then STUCK with
    outcome VERIFY_FAILED). No checks -> DONE is accepted as before (back-compat)."""

    def __init__(self, goal, name, max_turns=1000, dwell_s=4.0,
                 per_turn_timeout_s=240, max_no_progress=3, max_verify_attempts=3,
                 refuter=False, max_refute=2):
        self.page = None
        self.drv = None
        text, checks, cwd = goal_fields(goal)
        self.goal = text
        self.checks = checks
        self.cwd = cwd
        self.max_verify_attempts = max_verify_attempts
        self.verify_attempts = 0
        self.verified = None          # None=not checked, True/False after a gate ran
        self.last_verify_detail = ""
        self._pending_checks = []     # acceptance.Check specs left to run this gate
        self._active_check = None     # the Check currently running (non-blocking)
        # operator B refuter (spec 4B): an independent reviewer on a candidate DONE.
        self.refuter = refuter
        self.max_refute = max_refute
        self.refute_count = 0
        self._refuter_session = None
        self._context = None          # stored at attach() so we can open the side page
        self._agent_url = ""          # bare agent URL -> a fresh independent chat
        self.name = name
        self.conv_url = ""         # filled once the conversation gets its /conversation/<id>
        self.steer_msgs = []       # user steering messages to inject on the next turn(s)
        self._last_was_steer = False   # so the FOLLOWING continue bridges off the steer
        self.max_turns = max_turns
        self.dwell_s = dwell_s
        self.per_turn_timeout_s = per_turn_timeout_s
        self.max_no_progress = max_no_progress
        self.job = PROTOCOL + self.goal
        self.turn = 0
        self.no_progress = 0
        self.last_norm = None
        self.status = PENDING      # pending | ready | waiting | done | stuck | maxturns | error
        self.outcome = None
        self.reason = ""
        self.last_response = ""
        self.closed = False        # True once its tab has been released
        self._count_before = 0
        self._last_text = None
        self._stable_since = None
        self._t_send = 0.0

    def attach(self, context, agent_url):
        """Open this worker's tab and make it ready to send. On failure -> error."""
        self._context = context
        self._agent_url = agent_url
        try:
            self.page = _open_fresh(context, agent_url)
            self.drv = CopilotWebDriver(self.page)
            self.status = "ready"
            return True
        except Exception as e:
            self.status, self.outcome = "error", "ERROR"
            self.reason = "open failed: " + type(e).__name__ + ": " + str(e)
            return False

    def close(self):
        """Release the tab (frees ~0.3-0.6 GB). Idempotent; never raises."""
        if self.closed:
            return
        self.closed = True
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        try:
            if self._refuter_session is not None:
                self._refuter_session.close()     # don't leak the side-page tab
        except Exception:
            pass
        self.page = None
        self.drv = None

    def cancel(self):
        """User asked to stop+release this one from the cockpit. Mark terminal so the
        loop won't reopen it, then free its tab."""
        if self.status in TERMINAL:
            self.close()
            return
        self.status, self.outcome = "cancelled", "CANCELLED"
        self.reason = "手動で停止・タブ解放しました"
        self.close()

    def _capture_url(self):
        try:
            if self.page is not None:
                u = self.page.url
                if "/conversation/" in u:
                    self.conv_url = u
        except Exception:
            pass

    def steer(self, text):
        """Queue a user steering message; injected as the worker's next turn (Codex-
        style mid-task redirection). Takes priority over CONTINUE/FIX."""
        if text:
            self.steer_msgs.append(text)

    def _begin_send(self):
        if self.turn >= self.max_turns:
            self.status, self.outcome, self.reason = "maxturns", "MAXTURNS", "reached max_turns"
            return
        # a queued steering message preempts the normal CONTINUE/FIX job for this turn
        if self.steer_msgs:
            self.job = ("【ユーザーからの追加指示】" + self.steer_msgs.pop(0)
                        + "\n上記を最優先で踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
            self._last_was_steer = True
        else:
            self._last_was_steer = False
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
            self._on_done_claimed()
            return
        if self.no_progress >= self.max_no_progress:
            self.status, self.outcome = "stuck", "STUCK"
            self.reason = "no progress for %d turns" % (self.no_progress + 1)
            return
        if "FAIL" in last_line:
            self.job = FIX_JOB
        elif self._last_was_steer:
            # bridge off the steer instead of a raw CONTINUE so the redirection sticks
            self.job = ("先ほどの追加指示を踏まえて作業を続行してください。"
                        "完了なら DONE、無理なら FAIL と理由を書いてください。")
        else:
            self.job = CONTINUE_JOB
        self.status = "ready"

    def _on_done_claimed(self):
        """Copilot reported DONE. With no acceptance checks, go straight to the candidate-
        done step (back-compat trust, unless a refuter is on). With checks, run the
        verification gate first."""
        if not self.checks:
            self.verified = False
            self._candidate_done()
            return
        self._pending_checks = list(self.checks)
        self._active_check = None
        self.status = "verifying"
        self._advance_check()

    def _advance_check(self):
        """Start the next pending check, or go to the candidate-done step if all passed."""
        if not self._pending_checks:
            self.verified = True
            self.reason = "acceptance verified (%d check(s))" % len(self.checks)
            self._candidate_done()
            return
        self._active_check = Check(self._pending_checks[0], cwd=self.cwd).start()

    def _candidate_done(self):
        """Machine checks passed (or none) -> a CANDIDATE done. If the refuter is enabled
        and within budget, open an independent reviewer (non-blocking) before accepting;
        otherwise finish now."""
        if (self.refuter and self._context is not None
                and self.refute_count < self.max_refute):
            from .refuter import RefuterSession
            self.refute_count += 1
            self._refuter_session = RefuterSession(
                self._context, self._agent_url or "", self.goal, self.last_response).start()
            self.status = "refuting"
            return
        self.status, self.outcome = "done", "DONE"

    def _poll_refute(self):
        """Drive the non-blocking refuter. REFUTED -> feed the reason back and keep
        working; UPHELD/UNCLEAR -> accept the DONE."""
        r = self._refuter_session.poll()
        if r is None:
            return False
        kind, reason = r
        self._refuter_session = None
        if kind == "REFUTED":
            self.job = REFUTE_FIX_JOB % (reason or "(no reason)")
            self.status = "ready"
            return False
        self.status, self.outcome = "done", "DONE"
        return True

    def _poll_verify(self):
        """Drive the running acceptance check non-blockingly. On pass, advance to the
        next check (all pass -> DONE). On fail, re-inject the GROUND TRUTH and let the
        agent keep working, up to max_verify_attempts (then STUCK/VERIFY_FAILED)."""
        if self._active_check is None:
            self._advance_check()
            return self.status in TERMINAL
        r = self._active_check.poll()
        if r is None:
            return False                 # still running -- the other workers keep moving
        passed, detail = r
        self.last_verify_detail = detail
        if passed:
            self._pending_checks.pop(0)
            self._active_check = None
            self._advance_check()
            return self.status in TERMINAL
        self.verify_attempts += 1
        self.verified = False
        if self.verify_attempts >= self.max_verify_attempts:
            self.status, self.outcome = "stuck", "VERIFY_FAILED"
            self.reason = ("acceptance check failed %d time(s): %s"
                           % (self.verify_attempts, (detail or "")[:200]))
            return True
        self.job = VERIFY_FIX_JOB % (detail or "(no detail)")
        self._pending_checks = []
        self._active_check = None
        self.status = "ready"
        return False

    def poll(self):
        """Advance one non-blocking step. Returns True when terminal."""
        if self.status in TERMINAL:
            return True
        if self.status == PENDING:
            return False                 # not attached yet; the fleet attaches it
        if self.status == "verifying":
            return self._poll_verify()
        if self.status == "refuting":
            return self._poll_refute()
        if self.status == "ready":
            self._begin_send()
            self._capture_url()
            return self.status in TERMINAL
        if self.status == "waiting":
            self._capture_url()
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


def run_relay_fleet(context, goals, agent_url, max_turns=1000, poll_s=1.0,
                    notify=default_notify, on_tick=None, max_concurrent=None,
                    mc_box=None, add_box=None, refuter=False, max_refute=2):
    """Drive len(goals) autonomous relays in parallel to completion, but never with
    more than `max_concurrent` tabs open at once (defaults to what free RAM allows).
    A goal's tab is opened only when a slot frees and CLOSED the moment it finishes.

    `mc_box`, if given, is a 1-element list whose value is read EACH loop -- so the
    cockpit can raise/lower the live concurrency cap mid-run (set_maxtabs command).

    Returns a list of {name, goal, outcome, turns, reason} in goal order. `on_tick`
    (workers) is called after each round-robin sweep -- use it to log live progress."""
    if max_concurrent is None:
        max_concurrent = auto_concurrency(len(goals))
    if mc_box is None:
        mc_box = [max_concurrent]
    workers = [RelayWorker(g, "w%d" % i, max_turns=max_turns,
                           refuter=refuter, max_refute=max_refute)
               for i, g in enumerate(goals)]
    pending = list(workers)            # FIFO queue of not-yet-attached workers

    def _active_open():
        return sum(1 for w in workers
                   if w.page is not None and w.status not in TERMINAL)

    def _unfinished():
        # reconstruct the full goal (incl. acceptance checks/cwd) so a resume after a
        # wedged Edge keeps verifying -- returning bare text would drop the gate.
        return [{"text": w.goal, "checks": w.checks, "cwd": w.cwd}
                for w in workers if w.outcome not in ("DONE", "CANCELLED")]

    while any(w.status not in TERMINAL for w in workers) or (add_box and len(add_box) > 0):
        # auto-recovery: if the Edge/CDP context has died (wedged, or hard-reset by the
        # watchdog) a LIVE probe raises -> bail out with the unfinished goals so the
        # runner can reconnect to a fresh Edge and resume them. NB: context.pages is a
        # cached property and never raises -- cookies() actually round-trips to CDP.
        try:
            context.cookies()
        except Exception:
            raise FleetContextLost(_unfinished())

        # goals added mid-run (e.g. from the native chat while at capacity) join the
        # queue here -- priority items jump to the front, but still wait for a free slot
        # so the tab budget is never exceeded.
        if add_box:
            while add_box:
                item = add_box.pop(0)
                # item may carry checks/cwd too; goal_fields reads them (priority ignored)
                nw = RelayWorker(item, "w%d" % len(workers), max_turns=max_turns,
                                 refuter=refuter, max_refute=max_refute)
                workers.append(nw)
                if item.get("priority"):
                    pending.insert(0, nw)
                else:
                    pending.append(nw)

        # fill free tab slots from the pending queue (memory-bounded, live cap)
        while pending and _active_open() < max(1, mc_box[0]):
            w = pending.pop(0)
            if w.status in TERMINAL:   # (shouldn't happen, but be safe)
                continue
            ok = w.attach(context, agent_url)
            if not ok:
                # attach failed. If the WHOLE Edge/context died mid-open (e.g. the
                # watchdog hard-reset it), don't burn this goal as a terminal ERROR --
                # probe the context, and if it's truly dead bail so the runner reconnects
                # and RESUMES every unfinished goal (this one included). A live context
                # means a one-off open failure -> leave the worker ERROR as before.
                try:
                    context.cookies()
                except Exception:
                    raise FleetContextLost(_unfinished())

        for w in workers:
            if w.status in TERMINAL or w.status == PENDING:
                continue
            try:
                w.poll()
            except Exception as e:
                w.status, w.outcome = "error", "ERROR"
                w.reason = type(e).__name__ + ": " + str(e)
            # the instant a worker is done, release its tab -> RAM for the next goal
            if w.status in TERMINAL and not w.closed:
                w.close()

        if on_tick:
            try:
                on_tick(workers)
            except Exception:
                pass
        time.sleep(poll_s)

    # make sure no tab is left behind
    for w in workers:
        if not w.closed:
            w.close()

    notify("🛰 並列自律フリート 完了",
           "%d ゴール: %s" % (len(workers), ", ".join(w.outcome or "?" for w in workers)))
    return [{"name": w.name, "goal": w.goal, "outcome": w.outcome,
             "turns": w.turn, "reason": w.reason,
             "verified": w.verified, "verify_attempts": w.verify_attempts}
            for w in workers]
