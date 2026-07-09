"""Tests for capacity-aware CONTINUOUS admission control (2026-06-14 fleet rewrite).

No browser. We drive run_relay_fleet with a fake CDP context and a monkeypatched RelayWorker
so attach/poll/close are deterministic, and mock the RAM / disk readings. Proves the four
properties of the new design:

  (a) disk floor gates admission  -- with C: free below the floor, NO new tab is admitted;
                                     raise the free space and admission resumes.
  (b) completion -> immediate next -- when a running job finishes and frees its slot, the next
                                     queued goal is admitted on the NEXT sweep (no batch
                                     barrier: more goals than the cap, all eventually run, and
                                     at no point are more than `cap` tabs open at once).
  (c) verify tab counts in the cap -- a worker in 'verifying' still HOLDS its slot, so a tab in
                                     verify is counted by _active_open and a 1-cap fleet does
                                     NOT open a second tab while one verifies.
  (d) hysteresis damps thrash      -- with an up-margin dead-band, RAM jitter around the line no
                                     longer oscillates the cap 1<->N; it HOLDS at the water level
                                     (and still drains immediately on a real deficit).

Run:  .venv\\Scripts\\python.exe relay\\test_admission.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import relay.relay_fleet as rf
from relay.relay_fleet import (
    disk_admission_ok, ram_target_cap, run_relay_fleet, RelayWorker, TERMINAL,
)

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class FakeContext:
    """Stand-in for a Playwright CDP context: cookies() must not raise (a raise means the
    Edge died -> FleetContextLost), and new_page() is never reached because we monkeypatch
    RelayWorker.attach. We still give it for completeness."""
    def cookies(self):
        return []


# ── (a) disk-floor admission predicate ────────────────────────────────────────────────────
def test_disk_floor_predicate():
    # plenty of free space, default 6 GB floor -> OK
    check("disk_ok_when_ample", disk_admission_ok(floor_gb=6, free_gb=50.0) is True)
    # exactly at the floor -> OK (>=)
    check("disk_ok_at_floor", disk_admission_ok(floor_gb=6, free_gb=6.0) is True)
    # below the floor -> NOT OK
    check("disk_blocks_below_floor", disk_admission_ok(floor_gb=6, free_gb=5.9) is False)
    # eval look-ahead: 8 GB free, 6 floor, but the next eval might use 3 -> 8-3=5 < 6 -> blocked
    check("disk_lookahead_blocks", disk_admission_ok(floor_gb=6, eval_gb=3, free_gb=8.0) is False)
    check("disk_lookahead_ok", disk_admission_ok(floor_gb=6, eval_gb=1, free_gb=8.0) is True)
    # floor <= 0 disables the gate entirely (normal, non-bench use)
    check("disk_floor_zero_disables", disk_admission_ok(floor_gb=0, free_gb=0.1) is True)
    check("disk_floor_neg_disables", disk_admission_ok(floor_gb=-1, free_gb=0.0) is True)
    # in-flight reservation: 20 GB free, 6 floor, eval 5 GB. With 0 building the new eval needs
    # 20-5=15>=6 OK. But with 2 already building, reserve 5*(2+1)=15 -> 20-15=5 < 6 -> BLOCKED.
    # This is the fix for the concurrent-cold-build crash: each open build is reserved, not just one.
    check("disk_building0_ok", disk_admission_ok(floor_gb=6, eval_gb=5, free_gb=20.0, building=0) is True)
    check("disk_building1_ok", disk_admission_ok(floor_gb=6, eval_gb=5, free_gb=20.0, building=1) is True)   # 20-10=10>=6
    check("disk_building2_blocks", disk_admission_ok(floor_gb=6, eval_gb=5, free_gb=20.0, building=2) is False)  # 20-15=5<6
    # eval_gb=0 keeps legacy floor-only behavior even with builds in flight (no look-ahead)
    check("disk_building_noop_when_evalgb0", disk_admission_ok(floor_gb=6, eval_gb=0, free_gb=7.0, building=4) is True)
    # explicit reserve_gb (per-repo path): exact reserve wins over eval_gb/building
    check("disk_reserve_ok", disk_admission_ok(floor_gb=6, free_gb=12.0, reserve_gb=6.0) is True)   # 12-6=6>=6
    check("disk_reserve_blocks", disk_admission_ok(floor_gb=6, free_gb=12.0, reserve_gb=6.1) is False)  # 12-6.1<6
    # per-repo weights: heavy reserves ~floor-headroom (solo), light reserves little (pairs)
    check("repo_gb_matplotlib_heavy", rf.repo_eval_gb("matplotlib__matplotlib-23987") == 9.0)
    check("repo_gb_requests_light", rf.repo_eval_gb("psf__requests-2148") == 2.5)
    check("repo_gb_sklearn_calibrated", rf.repo_eval_gb("scikit-learn__scikit-learn-10508") == 5.0)
    # two sklearn (5+5=10) fit at C:13.7/min3 -> pair (was blocked by the old 7GB overestimate)
    check("two_sklearn_pair", disk_admission_ok(floor_gb=3, free_gb=13.7,
          reserve_gb=rf.repo_eval_gb("scikit-learn__scikit-learn-1")*2) is True)
    check("repo_gb_default", rf.repo_eval_gb("unknown__unknown-1") == rf.DEFAULT_REPO_EVAL_GB)
    # at C:12/floor6: ONE matplotlib (6) admits, TWO (12) blocked -> heavy stays solo
    check("two_matplotlib_blocked", disk_admission_ok(floor_gb=6, free_gb=12.0,
          reserve_gb=rf.repo_eval_gb("matplotlib__matplotlib-1")*2) is False)
    # two xarray (3+3=6) admit -> light pairs
    check("two_xarray_pair", disk_admission_ok(floor_gb=6, free_gb=12.0,
          reserve_gb=rf.repo_eval_gb("pydata__xarray-1")*2) is True)


# ── (d) anti-thrash hysteresis ─────────────────────────────────────────────────────────────
def test_hysteresis_no_thrash():
    cap = ram_target_cap
    PER, HEAD = 700, 1400

    # Simulate the thrash trigger: free RAM hovers just around the up/down line while the cap
    # sits at 2 with 2 tabs open. WITHOUT a dead-band (legacy up_margin=0) a tiny surplus makes
    # it ramp to 3; with a dead-band it HOLDS at 2.
    #   raw = open_now + (avail - HEAD)//PER. open_now=2.
    #   avail = HEAD + 100 (a small surplus, < PER): raw = 2 + 0 = 2 -> never ramps up anyway.
    #   avail = HEAD + PER + 50 (just over one tab's worth): raw = 2 + 1 = 3 -> WOULD ramp up.
    rf.avail_phys_mb = lambda: float(HEAD + PER + 50)      # 50 MB into the next tab's budget

    # legacy (no margin): a 50 MB surplus over a full tab budget is enough -> ramps up to 3
    check("legacy_ramps_on_tiny_surplus",
          cap(2, 2, 4, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=0) == 3)
    # hysteresis: require 700 MB EXTRA headroom on top of the tab budget -> 50 MB is inside the
    # dead-band -> HOLD at 2 (no thrash)
    check("hysteresis_holds_in_deadband",
          cap(2, 2, 4, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=PER) == 2)

    # a genuine, sustained surplus (well past the dead-band) DOES ramp up one step
    rf.avail_phys_mb = lambda: float(HEAD + 2 * PER + 100)
    check("hysteresis_ramps_on_real_surplus",
          cap(2, 2, 4, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=PER) == 3)

    # the dead-band NEVER blocks a DOWN drain: a real RAM deficit still drops the cap at once,
    # regardless of up_margin (down is immediate, only the up side has the dead-band).
    rf.avail_phys_mb = lambda: float(HEAD - 2 * PER)       # deficit of two tabs
    check("hysteresis_drain_still_immediate",
          cap(3, 3, 8, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=PER) == 1)

    # Stability over a JITTERY sequence: feed an oscillating RAM reading and confirm the cap
    # under hysteresis never grows past the settled level, while legacy does grow.
    jitter = [HEAD + PER + 30, HEAD + PER - 30, HEAD + PER + 60, HEAD + PER - 10,
              HEAD + PER + 40, HEAD + PER + 20]
    # hysteresis run
    capn = 2
    grew_h = False
    for j in jitter:
        rf.avail_phys_mb = (lambda v: (lambda: float(v)))(j)
        new = cap(2, capn, 4, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=PER)
        if new > capn:
            grew_h = True
        capn = new
    check("hysteresis_no_growth_under_jitter", grew_h is False and capn == 2)
    # legacy run on the SAME jitter -> it WOULD grow (proves the dead-band is what damps it)
    capn = 2
    grew_l = False
    for j in jitter:
        rf.avail_phys_mb = (lambda v: (lambda: float(v)))(j)
        new = cap(2, capn, 4, per_tab_mb=PER, headroom_mb=HEAD, up_margin_mb=0)
        if new > capn:
            grew_l = True
        capn = new
    check("legacy_grows_under_jitter", grew_l is True)


# ── shared harness for the loop-level tests (b) and (c) ─────────────────────────────────────
def _install_fake_worker(monkey_state):
    """Monkeypatch RelayWorker so attach/poll/close are deterministic and browser-free.

    Each worker, when attached, gets a sentinel page and goes 'waiting'. Its poll() consults
    a per-name control dict: 'verifying' keeps it busy (non-terminal, holds the tab); 'done'
    flips it terminal. close() just drops the page (frees the slot)."""
    orig = {"attach": RelayWorker.attach, "poll": RelayWorker.poll, "close": RelayWorker.close}

    def fake_attach(self, context, agent_url):
        self.page = object()              # sentinel: a non-None page = holds a tab
        self.status = "waiting"
        return True

    def fake_poll(self):
        if self.status in TERMINAL:
            return True
        cmd = monkey_state["control"].get(self.name, "waiting")
        if cmd == "verifying":
            self.status = "verifying"     # bounded eval in flight -- still HOLDS the tab
            return False
        if cmd == "done":
            self.status, self.outcome = "done", "DONE"
            self.verified = True
            return True
        self.status = "waiting"
        return False

    def fake_close(self):
        self.closed = True
        self.page = None
        self.drv = None

    RelayWorker.attach = fake_attach
    RelayWorker.poll = fake_poll
    RelayWorker.close = fake_close
    return orig


def _restore_worker(orig):
    RelayWorker.attach = orig["attach"]
    RelayWorker.poll = orig["poll"]
    RelayWorker.close = orig["close"]


# ── (b) continuous admission: completion frees a slot -> next admitted, no barrier ───────────
def test_continuous_admission_no_barrier():
    rf.avail_phys_mb = lambda: 64000.0       # RAM never the constraint here
    rf.free_disk_gb = lambda path=None: 500.0  # disk never the constraint here
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        # 5 goals, cap of 2: the 2-tab budget must be respected at every instant, AND all 5
        # must complete (continuous re-admission as slots free, not "batch of 2 barrier").
        goals = ["g0", "g1", "g2", "g3", "g4"]
        max_open_seen = {"v": 0}
        sweeps = {"n": 0}

        def on_tick(workers):
            open_now = sum(1 for w in workers
                           if getattr(w, "page", None) is not None and w.status not in TERMINAL)
            max_open_seen["v"] = max(max_open_seen["v"], open_now)
            sweeps["n"] += 1
            # complete the OLDEST currently-open worker each sweep so a slot frees and the
            # next queued goal must be admitted on the following sweep (the continuous flow).
            for w in workers:
                if getattr(w, "page", None) is not None and w.status == "waiting":
                    state["control"][w.name] = "done"
                    break

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=2,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None)
        all_done = all(r["outcome"] == "DONE" for r in res)
        check("continuous_all_complete", len(res) == 5 and all_done)
        check("continuous_cap_never_exceeded", max_open_seen["v"] <= 2)
        # if there were a "finish all then next batch" barrier with 5 goals / cap 2, we'd need
        # far more idle sweeps; a continuous flow finishes in ~5-7 sweeps. Just assert progress.
        check("continuous_made_progress", sweeps["n"] <= 12)
    finally:
        _restore_worker(orig)


# ── (c) verify-in-flight counts toward the cap ─────────────────────────────────────────────
def test_verifying_counts_in_cap():
    rf.avail_phys_mb = lambda: 64000.0
    rf.free_disk_gb = lambda path=None: 500.0
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        # cap of 1: the first worker goes into 'verifying' and STAYS there for several sweeps.
        # A verifying tab still holds its slot, so the SECOND goal must NOT be admitted while
        # w0 verifies. We let w0 verify for a few sweeps, then finish it; only then may w1 open.
        goals = ["g0", "g1"]
        observed = {"second_open_during_verify": False, "ticks": 0}

        def on_tick(workers):
            observed["ticks"] += 1
            by = {w.name: w for w in workers}
            w0, w1 = by.get("w0"), by.get("w1")
            # drive w0 into verifying for the first few sweeps, then let it finish
            if observed["ticks"] <= 3:
                if w0 is not None and w0.page is not None:
                    state["control"]["w0"] = "verifying"
            else:
                if w0 is not None and w0.page is not None:
                    state["control"]["w0"] = "done"
            # the violation we are testing for: w1 holding a tab while w0 is still verifying
            if (w1 is not None and getattr(w1, "page", None) is not None
                    and w0 is not None and w0.status == "verifying"):
                observed["second_open_during_verify"] = True
            # once w0 is terminal, let w1 finish too so the loop can exit (it was admitted only
            # after w0 freed its slot -- exactly the behavior under test).
            if w1 is not None and getattr(w1, "page", None) is not None and w1.status == "waiting":
                state["control"]["w1"] = "done"

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=1,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None)
        check("verify_blocks_second_admit",
              observed["second_open_during_verify"] is False)
        check("verify_both_eventually_done",
              len(res) == 2 and all(r["outcome"] == "DONE" for r in res))
    finally:
        _restore_worker(orig)


# ── (a-loop) disk floor blocks admission inside the live loop ───────────────────────────────
def test_disk_floor_blocks_in_loop():
    rf.avail_phys_mb = lambda: 64000.0       # RAM never the constraint
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        # Disk starts BELOW the floor -> the first goal must NOT be admitted. Then we raise the
        # free space above the floor and the goal IS admitted and runs to DONE. Floor = 6 GB.
        goals = ["g0"]
        disk = {"free": 3.0}                  # below the 6 GB floor at first
        rf.free_disk_gb = lambda path=None: disk["free"]
        phase = {"ticks": 0, "opened_while_low": False}

        def on_tick(workers):
            phase["ticks"] += 1
            w0 = workers[0]
            if disk["free"] < 6.0 and getattr(w0, "page", None) is not None:
                phase["opened_while_low"] = True     # VIOLATION: admitted under the floor
            if phase["ticks"] == 3:
                disk["free"] = 50.0                  # free up the disk -> admission may resume
            # once admitted (page present), let it finish so the loop can exit
            if getattr(w0, "page", None) is not None and w0.status == "waiting":
                state["control"]["w0"] = "done"

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=1,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None,
                              disk_floor_gb=6.0)
        check("disk_floor_blocked_admit_while_low", phase["opened_while_low"] is False)
        check("disk_floor_admits_after_freed",
              len(res) == 1 and res[0]["outcome"] == "DONE")
    finally:
        _restore_worker(orig)


# ── (e) stop_box: graceful abort cancels all workers and ends the run ─────────────────────────
def test_stop_cancels_running_fleet():
    rf.avail_phys_mb = lambda: 64000.0
    rf.free_disk_gb = lambda path=None: 500.0
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        goals = ["g0", "g1", "g2"]
        stop_box = [False]
        sweeps = {"n": 0}

        def on_tick(workers):
            sweeps["n"] += 1
            if sweeps["n"] == 2:        # workers attached + waiting -> request stop
                stop_box[0] = True
            if sweeps["n"] >= 8:        # safety net: a broken stop must not hang the test
                for w in workers:
                    state["control"][w.name] = "done"

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=2,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None,
                              stop_box=stop_box)
        # stop ended the run WITHOUT any goal completing as DONE (none were set ->done pre-stop)
        check("stop_no_goal_done", all(r["outcome"] != "DONE" for r in res))
        check("stop_workers_cancelled", any(r["outcome"] == "CANCELLED" for r in res))
        check("stop_exits_promptly", sweeps["n"] <= 4)
    finally:
        _restore_worker(orig)


# ── (f) pause_box: freeze in place (no tabs opened), then resume to completion ────────────────
def test_pause_freezes_then_resumes():
    rf.avail_phys_mb = lambda: 64000.0
    rf.free_disk_gb = lambda path=None: 500.0
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        goals = ["g0", "g1"]
        pause_box = [True]             # start frozen
        sweeps = {"n": 0}
        opened_while_paused = {"v": False}

        def on_tick(workers):
            sweeps["n"] += 1
            if pause_box[0]:
                # while frozen admission is skipped -> NO worker should hold a tab
                if any(getattr(w, "page", None) is not None for w in workers):
                    opened_while_paused["v"] = True
                if sweeps["n"] >= 3:
                    pause_box[0] = False     # resume after a few frozen sweeps
            else:
                for w in workers:            # after resume, drive each to completion
                    if getattr(w, "page", None) is not None and w.status == "waiting":
                        state["control"][w.name] = "done"
            if sweeps["n"] >= 30:            # safety net against a broken resume
                pause_box[0] = False
                for w in workers:
                    state["control"][w.name] = "done"

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=2,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None,
                              pause_box=pause_box)
        check("pause_no_tab_opened_while_frozen", opened_while_paused["v"] is False)
        check("pause_resumes_to_completion",
              len(res) == 2 and all(r["outcome"] == "DONE" for r in res))
        check("pause_actually_held_some_sweeps", sweeps["n"] >= 3)
    finally:
        _restore_worker(orig)


# ── (g) fleet RESEARCH: delegation -- NON-BLOCKING: worker enters 'researching', the sweep keeps
#        moving, and the report is injected when the side-session yields it ─────────────────────
def test_fleet_research_nonblocking():
    import relay.agent_profiles as ap

    class FakeRS:
        """Stand-in for ResearchSession: poll() returns None (pending) once, then the report."""
        def __init__(self, context, query, **kw):
            self.query = query
            self._n = 0
        def start(self):
            return self
        def poll(self):
            self._n += 1
            return None if self._n < 2 else "REPORT: the answer is 42 (q=%s)" % self.query
        def close(self):
            pass

    w = RelayWorker("do the thing", "w0", max_research=2)
    w._context = object()                 # non-None -> the research path is enabled
    orig_xr = rf.extract_research
    orig_rs = ap.ResearchSession
    rf.extract_research = lambda resp: "what is the answer?" if "RESEARCH" in resp else ""
    ap.ResearchSession = FakeRS
    try:
        # _decide on RESEARCH: must NOT block -- it kicks off the session and enters 'researching'
        w._decide("Looking.\nRESEARCH: what is the answer?")
        check("research_enters_researching", w.status == "researching" and w.research_count == 1)
        check("research_session_started", w._research_session is not None)
        # poll() while researching returns quickly (False) -> the round-robin keeps stepping others
        r1 = w.poll()
        check("research_poll_nonblocking", r1 is False and w.status == "researching")
        # next poll: the session yields the report -> inject it and continue
        r2 = w.poll()
        check("research_report_injected", "REPORT: the answer is 42" in w.job and w.status == "ready")
        check("research_session_cleared", w._research_session is None)
        # cap: a 2nd delegation hits the cap (2); a 3rd is refused without starting a session
        w._decide("RESEARCH: again")          # 2nd -> count 2 (== max)
        check("research_second_ok", w.research_count == 2 and w.status == "researching")
        w._research_session = None
        w._decide("RESEARCH: a third time")    # over cap -> refused, no session
        check("research_capped", w.research_count == 2 and w._research_session is None)
        check("research_cap_message", "上限到達" in w.job)
    finally:
        rf.extract_research = orig_xr
        ap.ResearchSession = orig_rs


# ── (h) sub-agent side-page tabs are RAM-gated: open is deferred until there's free RAM ───────
def test_research_session_ram_gated_open():
    import relay.agent_profiles as ap
    import relay.relay_fleet as rf2
    rs = ap.ResearchSession(object(), "q")     # context non-None
    rs.start()                                  # must DEFER the open (no page yet)
    opened = {"n": 0}

    def fake_open():
        opened["n"] += 1
        rs._pending_open = False               # simulate a successful open
    rs._do_open = fake_open
    orig = rf2.ram_room_for_tab
    try:
        rf2.ram_room_for_tab = lambda floor_mb=2000.0: False   # no RAM -> defer
        r = rs.poll()
        check("ram_gate_defers_when_low", r is None and rs._pending_open and opened["n"] == 0)
        rf2.ram_room_for_tab = lambda floor_mb=2000.0: True    # RAM frees -> open now
        rs.poll()
        check("ram_gate_opens_when_free", opened["n"] == 1 and rs._pending_open is False)
    finally:
        rf2.ram_room_for_tab = orig


# ── (i) dead-agent / dead-path detector: repeated SystemError / admin-block -> STUCK fast ─────
def test_dead_agent_detector():
    import time as _t
    err = ("申し訳ありません。予期しないエラーが発生しました。エラー コード: SystemError。時刻: ")
    # NETWORK RESILIENCE: a brief outage (errors all WITHIN the window) keeps retrying, does NOT
    # stuck -- a momentary blip must never end the worker.
    w = RelayWorker("do the thing", "w0")
    w._decide(err + "t1"); w._decide(err + "t2"); w._decide(err + "t3")
    check("dead_within_window_retries", w.status != "stuck" and w._copilot_err_streak == 3)

    # FIX #2 (2026-07): the wall-clock STUCK point now SPLITS on which marker family matched and
    # whether our own infra is confirmed healthy, instead of treating every AGENT_DEAD_MARKERS hit
    # the same. Case (a): a GENERIC transient-error string persisting past the window, with infra
    # reported healthy -- this must NEVER be blamed on the agent (that was the false positive a
    # previous change tried to fix by just deleting the notify). It must land as a re-queueable
    # INFRA_STUCK, with NO "disabled" desktop toast, and a reason about network/connection.
    notify_calls = []
    orig_notify = rf.default_notify
    rf.default_notify = lambda *a, **k: notify_calls.append((a, k))
    orig_infra_fn = rf._infra_healthy
    rf._infra_healthy = lambda *a, **k: True
    try:
        w._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w._decide(err + "t4")
        check("transient_infra_ok_stuck_is_infra", w.status == "stuck" and w.outcome == "INFRA_STUCK")
        check("transient_infra_ok_reason_says_network",
              ("接続" in (w.reason or "")) or ("ネットワーク" in (w.reason or "")))
        check("transient_infra_ok_reason_not_disabled", "無効化" not in (w.reason or ""))
        check("transient_infra_ok_no_desktop_notify", len(notify_calls) == 0)
    finally:
        rf.default_notify = orig_notify
        rf._infra_healthy = orig_infra_fn

    # Case (b): same GENERIC transient error past the window, but infra is reported UNHEALTHY too
    # (a real outage) -- still must NOT fire the disabled-agent toast (transient wording never
    # qualifies for the true-positive path regardless of infra state).
    notify_calls_b = []
    rf.default_notify = lambda *a, **k: notify_calls_b.append((a, k))
    rf._infra_healthy = lambda *a, **k: False
    try:
        w_b = RelayWorker("do the thing", "wb")
        w_b._decide(err + "t1")
        w_b._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_b._decide(err + "t2"); w_b._decide(err + "t3")
        check("transient_infra_down_stuck_is_infra", w_b.status == "stuck" and w_b.outcome == "INFRA_STUCK")
        check("transient_infra_down_no_desktop_notify", len(notify_calls_b) == 0)
    finally:
        rf.default_notify = orig_notify
        rf._infra_healthy = orig_infra_fn

    # Case (c): TRUE POSITIVE restored -- an ADMIN_BLOCK-worded reply (only "contact the
    # administrator" family, no generic transient text) persisting past the window, WITH infra
    # confirmed healthy (our own MCP path is fine) -> this really does look like the agent itself
    # being stopped/disabled. Desktop notify MUST fire exactly once with the "停止/無効化" wording.
    notify_calls_c = []
    rf.default_notify = lambda *a, **k: notify_calls_c.append((a, k))
    rf._infra_healthy = lambda *a, **k: True
    try:
        admin_msg = "管理者に問い合わせてください。セッション ID: "
        w_c = RelayWorker("g", "wc")
        w_c._on_redirect_page = lambda: False   # no drift signal -> re-nav-first is a no-op
        w_c._decide(admin_msg + "1")
        w_c._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_c._decide(admin_msg + "2"); w_c._decide(admin_msg + "3")
        check("admin_block_infra_ok_stuck", w_c.status == "stuck" and w_c.outcome == "STUCK")
        check("admin_block_msg_points_to_copilot_studio", "Copilot Studio" in (w_c.reason or ""))
        check("admin_block_msg_says_disabled", "停止/無効化" in (w_c.reason or ""))
        check("admin_block_notify_called_once", len(notify_calls_c) == 1)
        check("admin_block_notify_wording",
              "停止/無効化" in (notify_calls_c[0][0][0] if notify_calls_c and notify_calls_c[0][0] else ""))
    finally:
        rf.default_notify = orig_notify
        rf._infra_healthy = orig_infra_fn

    # Case (d): ADMIN_BLOCK wording past the window, but infra reported UNHEALTHY -- a network
    # problem must NOT be blamed on the agent. No desktop notify; classified as INFRA_STUCK.
    notify_calls_d = []
    rf.default_notify = lambda *a, **k: notify_calls_d.append((a, k))
    rf._infra_healthy = lambda *a, **k: False
    try:
        admin_msg = "管理者に問い合わせてください。セッション ID: "
        w_d = RelayWorker("g", "wd")
        w_d._on_redirect_page = lambda: False
        w_d._decide(admin_msg + "1")
        w_d._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_d._decide(admin_msg + "2"); w_d._decide(admin_msg + "3")
        check("admin_block_infra_down_is_infra_stuck", w_d.status == "stuck" and w_d.outcome == "INFRA_STUCK")
        check("admin_block_infra_down_no_notify", len(notify_calls_d) == 0)
    finally:
        rf.default_notify = orig_notify
        rf._infra_healthy = orig_infra_fn

    # Case (e): drift case -- re-nav-first recovers the tab (_maybe_renav_before_signal -> True via
    # the underlying _on_redirect_page/_renav_to_agent_surface monkeypatch), so _decide returns
    # BEFORE even reaching the wall-clock STUCK point. Neither STUCK nor a notify; it just retries.
    notify_calls_e = []
    rf.default_notify = lambda *a, **k: notify_calls_e.append((a, k))
    try:
        class _FakePageE:
            url = "https://copilot.example/chat/?redirfrom=CsrToSSR&auth=2"
        w_e = RelayWorker("do the thing", "we")
        w_e.page = _FakePageE()
        w_e._agent_url = "https://copilot.example/chat/?titleId=abc"
        w_e._on_redirect_page = lambda: True
        def _fake_renav_ok_e():
            w_e._redirect_renavs += 1
            return True
        w_e._renav_to_agent_surface = _fake_renav_ok_e
        w_e._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_e._copilot_err_streak = 5
        w_e._decide(err + "t1")
        check("drift_recovers_not_stuck", w_e.status != "stuck")
        check("drift_recovers_retries", w_e.status == "ready")
        check("drift_recovers_no_notify", len(notify_calls_e) == 0)
    finally:
        rf.default_notify = orig_notify

    # English failure trips the TRANSIENT (infra-side) path the same way (after the window),
    # confirming the split is language-agnostic, not just JP.
    rf._infra_healthy = lambda *a, **k: True
    try:
        w_en = RelayWorker("g", "wen")
        w_en._on_redirect_page = lambda: False
        en = "Sorry, an unexpected error occurred. If the problem persists, contact your administrator."
        w_en._decide(en + "1"); w_en._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_en._decide(en + "2"); w_en._decide(en + "3")
        check("dead_detector_english", w_en.status == "stuck")
        # this EN string carries BOTH a generic phrase ("unexpected error") AND the admin-block
        # phrase ("contact your administrator") -- admin-block wins (any ADMIN_BLOCK_MARKERS hit
        # is enough) and infra is healthy here, so it's the TRUE POSITIVE path.
        check("dead_detector_english_is_true_positive", w_en.outcome == "STUCK")
    finally:
        rf._infra_healthy = orig_infra_fn

    # a real (non-error) reply resets the streak AND the window so a one-off blip doesn't accumulate
    w3 = RelayWorker("g", "w2")
    w3._decide(err + "x")
    w3._decide("ファイルを修正しました。続けます。CONTINUE")
    check("err_streak_resets_on_real_reply", w3._copilot_err_streak == 0 and w3._agent_err_ts == 0.0)


def test_tool_unreachable_infra():
    import time as _t
    # The agent's "my tools don't exist / 再試行では解消しません" self-lock is INFRA-FALSE (devtunnel
    # blip), not a miss: re-send the goal to ride it out, and only INFRA_STUCK (not a coding miss)
    # past the window.
    msg = ("STUCK: 環境にローカルファイル操作・コード実行ツールが存在しないため、ソースの編集・検証は"
           "不可能です。恒常的制約のため再試行では解消しません。完遂には当環境へのツール有効化、または"
           "検証側での差分適用が必要です。")
    w = RelayWorker("g", "w0")
    w._decide(msg)
    check("toolerr_within_window_resends_goal",
          w.status == "ready" and w.goal in (w.job or "") and w.outcome != "STUCK")
    # past the window -> INFRA_STUCK (a re-queueable infra stuck, NOT a coding miss)
    w._toolerr_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
    w._decide(msg)
    check("toolerr_past_window_infra_stuck", w.status == "stuck" and w.outcome == "INFRA_STUCK")
    check("toolerr_msg_says_infra", "インフラ" in (w.reason or ""))
    # a normal coding reply does NOT trip it (no false positive)
    w2 = RelayWorker("g", "w1")
    w2._decide("ファイルを修正しました。テストも通りました。DONE")
    check("toolerr_no_false_positive", w2.status != "stuck" and w2._toolerr_ts == 0.0)


def test_transient_outage_window():
    import time as _t
    # The transient retry rides out an OUTAGE on a wall-clock window, not a 10-count budget that
    # exhausted in ~55s. Many retries within the window all schedule.
    w = RelayWorker("g", "w0")
    oks = [w._retry_transient() for _ in range(15)]
    check("transient_rides_out_blip", all(oks) and w.transient == 15)   # 15 > old max_transient=10
    # past the window -> give up (caller goes terminal)
    w.first_transient_ts = _t.time() - rf.NET_RETRY_WINDOW_S - 1
    check("transient_gives_up_past_window", w._retry_transient() is False)


def test_consent_detector():
    # REGULATION (2026-07 fix): MCP connection-consent must be resolved FULLY AUTOMATICALLY --
    # the automatic tiers (Tier0 Allow / re-nav / _auto_consent) are tried first and NEVER
    # surface. Surfacing the Edge is the LAST RESORT: only once those tiers are genuinely
    # exhausted (streak >= 2) does the worker call surface() ONCE with open_url=the agent's own
    # URL. If that surface() call succeeds (True), the worker does NOT hard-STUCK -- it retries
    # the goal so the re-invoke succeeds once the user approves. If surface() fails (False), the
    # worker STUCKs with an HONEST manual-recovery reason (never a false "surfaced!" claim).
    import time as _t
    import relay.edge_recover as er
    card = ("desktopfile操作\nまずは接続して、必要な情報を探します。この資格情報を 接続マネージャーを開く で"
            "検証してください。接続の準備が整ったら、この要求をやり直してください。再試行 キャンセル")

    # ---- (1) surface() succeeds at exhaustion -> not called during the automatic tiers, called
    # exactly once at exhaustion with open_url=the worker's _agent_url, worker does NOT hard-STUCK.
    calls = []
    orig = er.surface
    er.surface = lambda *a, **k: (calls.append((a, k)), True)[1]
    notify_calls = []
    orig_notify = rf.default_notify
    rf.default_notify = lambda *a, **k: notify_calls.append((a, k))
    try:
        w = RelayWorker("fix the bug", "w0")
        w._agent_url = "https://copilot.example/chat/agent/T_abc?titleId=xyz"
        w._decide(card)                       # 1st -> auto tried (page=None -> fails) -> RETRY
        check("consent_auto_attempted_first", w._consent_auto_tried)
        check("consent_no_surface_on_first", len(calls) == 0 and w.status != "stuck")
        w._decide(card)                       # 2nd -> automatic tiers exhausted -> last-resort surface()
        check("consent_surface_called_once_at_exhaustion", len(calls) == 1)
        _, kwargs = calls[0]
        check("consent_surface_open_url_is_agent_url", kwargs.get("open_url") == w._agent_url)
        check("consent_not_hard_stuck_when_surfaced_ok", w.status != "stuck")
        check("consent_retried_after_surface", w.status != "stuck" and (w.job or "") != "")
        check("consent_notify_truthful_surfaced",
              any("前面に出しました" in (a[1] if len(a) > 1 else "") for a, k in notify_calls))
        # a further sighting (still no approval yet) keeps retrying, bounded -- does not surface again
        w._decide(card)
        check("consent_surface_still_only_once", len(calls) == 1)
    finally:
        er.surface = orig
        rf.default_notify = orig_notify

    # ---- (2) surface() fails at exhaustion -> worker STUCKs with the HONEST manual-recovery
    # reason (the -Foreground PowerShell hint), and the notify is that honest message, NOT a
    # "surfaced!" claim.
    calls2 = []
    er.surface = lambda *a, **k: (calls2.append((a, k)), False)[1]
    notify_calls2 = []
    rf.default_notify = lambda *a, **k: notify_calls2.append((a, k))
    try:
        wfail = RelayWorker("fix the bug", "wfail")
        wfail._agent_url = "https://copilot.example/chat/agent/T_abc?titleId=xyz"
        wfail._decide(card)
        wfail._decide(card)                   # exhaustion -> surface() called, returns False
        check("consent_surface_fail_called_once", len(calls2) == 1)
        check("consent_surface_fail_hard_stuck", wfail.status == "stuck" and wfail.outcome == "STUCK")
        check("consent_surface_fail_honest_reason",
              "-Foreground" in (wfail.reason or "") and "start_companion_edge.ps1" in (wfail.reason or ""))
        check("consent_surface_fail_notify_honest",
              len(notify_calls2) == 1
              and "前面に出しました" not in (notify_calls2[0][0][1] if len(notify_calls2[0][0]) > 1 else "")
              and "-Foreground" in (notify_calls2[0][0][1] if len(notify_calls2[0][0]) > 1 else ""))
    finally:
        er.surface = orig
        rf.default_notify = orig_notify

    # an English consent card trips the automatic ladder the same way (bounded consent streak,
    # not asserting on surface behavior here -- that's covered above).
    er.surface = lambda *a, **k: True
    try:
        w2 = RelayWorker("g", "w1")
        en = "Please open connection manager and verify your credential, then retry."
        w2._decide(en); w2._decide(en)
        check("consent_detector_english", w2._consent_streak >= 2)
    finally:
        er.surface = orig
    # a real tool result (no card) does NOT trip it
    w3 = RelayWorker("g", "w2")
    w3._decide('{"platform":"win32","python_version":"3.11"} 完了。CONTINUE')
    check("consent_no_false_positive", w3.status != "stuck" and w3._consent_streak == 0)
    # A consent card is a recoverable auth state, not evidence that the agent is disabled.
    # If an older admin/SystemError window is left over, consent surfacing must clear it so
    # the next post-consent retry does not immediately raise a false disabled-agent toast.
    notify_calls_stale = []
    orig_notify_stale = rf.default_notify
    orig_infra_stale = rf._infra_healthy
    rf.default_notify = lambda *a, **k: notify_calls_stale.append((a, k))
    rf._infra_healthy = lambda *a, **k: True
    er.surface = lambda *a, **k: True
    try:
        w_stale = RelayWorker("g", "w_stale")
        w_stale._agent_url = "https://copilot.example/chat/agent/T_abc?titleId=xyz"
        w_stale._agent_err_ts = _t.time() - rf.AGENT_ERR_WINDOW_S - 1
        w_stale._copilot_err_streak = 5
        w_stale._consent_tier0_allow = lambda: False
        w_stale._maybe_renav_before_signal = lambda: False
        w_stale._auto_consent = lambda skip_tier0=False: False
        en_consent = "Please open connection manager and verify your credential, then retry."
        w_stale._decide(en_consent)
        check("consent_resets_stale_agent_error_first",
              w_stale._agent_err_ts == 0.0 and w_stale._copilot_err_streak == 0)
        w_stale._decide(en_consent)
        check("consent_surface_keeps_agent_error_reset",
              w_stale._agent_err_ts == 0.0 and w_stale._copilot_err_streak == 0
              and len(notify_calls_stale) == 1)
        w_stale._on_redirect_page = lambda: False
        admin_msg = "If the problem persists, contact your administrator."
        w_stale._decide(admin_msg)
        check("post_consent_admin_block_waits_full_window",
              w_stale.status != "stuck" and w_stale._copilot_err_streak == 1
              and len(notify_calls_stale) == 1)
    finally:
        rf.default_notify = orig_notify_stale
        rf._infra_healthy = orig_infra_stale
        er.surface = orig
    # AUTO-CONSENT SUCCESS: when the click-through completes, re-invoke (RETRY) -- no surface, no STUCK.
    # _decide now tries Tier 0 / re-nav-first before falling back to _auto_consent(skip_tier0=True)
    # (2026-07 re-nav-first fix), so the stub must accept that kwarg like the real method does.
    calls3 = []
    er.surface = lambda *a, **k: calls3.append((a, k))
    try:
        wok = RelayWorker("g", "wok")
        wok._auto_consent = lambda skip_tier0=False: True
        wok._decide(card)
        check("consent_auto_success_no_surface", len(calls3) == 0 and wok.status != "stuck"
              and wok._consent_auto_tried)
    finally:
        er.surface = orig


def test_tab_load_accounting():
    # tab_load = main tab + OPEN sub-agent side-pages (research/refuter). Pending (page=None)
    # side-pages don't count; this is the RAM-accounting unit the tab-budget admission gates on.
    w = RelayWorker("g", "w0")
    check("tabload_zero_no_page", w.tab_load() == 0)
    w.page = object()
    check("tabload_main_only", w.tab_load() == 1)
    class _Open:  page = object()
    class _Pending: page = None
    w._research_session = _Open()
    check("tabload_plus_research", w.tab_load() == 2)
    w._refuter_session = _Open()
    check("tabload_plus_refuter", w.tab_load() == 3)
    w._research_session = _Pending()      # a not-yet-opened side-page must NOT count
    check("tabload_pending_uncounted", w.tab_load() == 2)


def test_tab_budget_admission():
    # A worker that fans out to 3 tabs consumes the whole 3-tab budget, so a 2nd worker waits --
    # "3 open tabs == parallelism 3", reactive, no human cap. All goals still complete (continuous).
    rf.avail_phys_mb = lambda: 64000.0
    rf.free_disk_gb = lambda path=None: 500.0
    state = {"control": {}}
    orig = _install_fake_worker(state)
    try:
        goals = ["g0", "g1", "g2"]
        max_tabs = {"v": 0}
        max_mains = {"v": 0}
        class _Open: page = object()

        def on_tick(workers):
            # fan the currently-open worker out to 3 tabs BEFORE completing it
            cur = [w for w in workers if getattr(w, "page", None) is not None and w.status == "waiting"]
            for w in cur:
                if w.tab_load() < 3:
                    w._research_session = _Open(); w._refuter_session = _Open()
            max_tabs["v"] = max(max_tabs["v"], sum(w.tab_load() for w in workers))
            mains = sum(1 for w in workers if getattr(w, "page", None) is not None and w.status not in TERMINAL)
            max_mains["v"] = max(max_mains["v"], mains)
            # once it is at full fan-out, complete it so the next can be admitted
            for w in cur:
                if w.tab_load() >= 3:
                    w._research_session = None; w._refuter_session = None
                    state["control"][w.name] = "done"
                    break

        res = run_relay_fleet(FakeContext(), goals, "http://agent", max_concurrent=3,
                              poll_s=0, on_tick=on_tick, notify=lambda *a, **k: None)
        check("tab_budget_all_complete", len(res) == 3 and all(r["outcome"] == "DONE" for r in res))
        check("tab_budget_total_tabs_capped", max_tabs["v"] <= 3)     # 3-tab budget held
        check("tab_budget_one_main_when_fanned", max_mains["v"] == 1)  # a 3-tab worker runs solo
    finally:
        _restore_worker(orig)


def test_renav_first_on_consent_and_dead_agent():
    # 2026-07 fix: the consent card / "agent dead" reply is frequently a SYMPTOM of a drifted tab
    # (SPA normalized the URL but silently dropped the loaded custom agent), not genuine consent-
    # needed or genuine dead-agent. _decide must try RE-NAV to the agent surface FIRST -- before
    # the fragile popup click-through tiers (consent) and before counting toward the wall-clock
    # STUCK window (agent-dead). Hermetic: no real browser, no page -- we monkeypatch the redirect
    # detector and the low-level re-nav mechanics so we can observe call order without Playwright.
    import relay.edge_recover as er

    class _FakePage:
        """Just enough of a Playwright Page for _agent_url/_renav_budget_ok gating to treat this
        worker as having a real page (so re-nav-first's precondition checks don't short-circuit
        on page is None, exactly as they would with a real browser)."""
        url = "https://copilot.example/chat/?redirfrom=CsrToSSR&auth=2"

    # ---- CONSENT branch: no Allow button, tab looks drifted, budget available -> re-nav fires
    # BEFORE the fragile _auto_consent popup tiers (which we tripwire to prove they're unreached).
    w = RelayWorker("fix the bug", "wconsent")
    w.page = _FakePage()
    w._agent_url = "https://copilot.example/chat/?titleId=abc"
    w._consent_tier0_allow = lambda: False          # no real Allow button on this drifted page
    w._on_redirect_page = lambda: True              # drifted-tab signal
    def _fake_renav_ok():                           # re-nav succeeds, composer back (spends budget,
        w._redirect_renavs += 1                     # like the real _renav_to_agent_surface does)
        return True
    w._renav_to_agent_surface = _fake_renav_ok
    auto_consent_calls = {"n": 0}
    w._auto_consent = lambda *a, **k: auto_consent_calls.__setitem__("n", auto_consent_calls["n"] + 1) or False
    card = ("desktopfile操作\nまずは接続して、必要な情報を探します。この資格情報を 接続マネージャーを開く で"
            "検証してください。接続の準備が整ったら、この要求をやり直してください。再試行 キャンセル")
    surface_calls = {"n": 0}
    orig_surface = er.surface
    er.surface = lambda *a, **k: surface_calls.__setitem__("n", surface_calls["n"] + 1)
    try:
        w._decide(card)
        check("consent_renav_first_not_stuck", w.status != "stuck")
        check("consent_renav_first_skips_auto_consent_tiers", auto_consent_calls["n"] == 0)
        check("consent_renav_first_never_surfaces", surface_calls["n"] == 0)
        check("consent_renav_first_spent_budget", w._redirect_renavs == 1)
        check("consent_renav_first_retries_job", "renav" in (w.reason or "").lower() or "再ナビ" in (w.reason or ""))
    finally:
        er.surface = orig_surface

    # ---- CONSENT branch, budget exhausted -> falls through to the existing _auto_consent tiers
    # (proves re-nav-first is BOUNDED, not an infinite loop / permanent bypass of the old path).
    w2 = RelayWorker("fix the bug", "wconsent2")
    w2.page = _FakePage()
    w2._agent_url = "https://copilot.example/chat/?titleId=abc"
    w2._redirect_renavs = w2.max_redirect_renavs     # budget already spent
    w2._consent_tier0_allow = lambda: False
    w2._on_redirect_page = lambda: True
    fallback_calls = {"n": 0}
    w2._auto_consent = lambda *a, **k: fallback_calls.__setitem__("n", fallback_calls["n"] + 1) or False
    w2._decide(card)
    check("consent_budget_exhausted_falls_back", fallback_calls["n"] == 1)

    # ---- AGENT_DEAD branch: drifted tab + budget available -> re-nav+RETRY instead of
    # immediately accumulating toward the wall-clock STUCK window.
    w3 = RelayWorker("do the thing", "wdead")
    w3.page = _FakePage()
    w3._agent_url = "https://copilot.example/chat/?titleId=abc"
    w3._on_redirect_page = lambda: True
    def _fake_renav_ok3():
        w3._redirect_renavs += 1
        return True
    w3._renav_to_agent_surface = _fake_renav_ok3
    err = "申し訳ありません。予期しないエラーが発生しました。エラー コード: SystemError。時刻: "
    w3._decide(err + "t1")
    check("dead_renav_first_not_stuck", w3.status != "stuck")
    check("dead_renav_first_retries", w3.status == "ready")
    check("dead_renav_first_spent_budget", w3._redirect_renavs == 1)

    # ---- AGENT_DEAD branch, no redirect signal (genuine outage) -> re-nav-first is a no-op and
    # the existing wall-clock window logic is UNCHANGED (still rides out the outage, still STUCKs
    # only after the window -- the genuine-outage handling is not weakened). `err` here is the
    # GENERIC transient-error wording (no ADMIN_BLOCK phrase), so per the false-positive fix its
    # past-window terminal state is INFRA_STUCK (network/connection), never the "agent
    # disabled" STUCK -- that classification is reserved for admin-block wording + healthy infra.
    w4 = RelayWorker("do the thing", "wdead2")
    w4._on_redirect_page = lambda: False   # not on a redirect page: real agent, real outage
    w4._decide(err + "t1"); w4._decide(err + "t2"); w4._decide(err + "t3")
    check("dead_genuine_outage_still_rides_out",
          w4.status != "stuck" and w4._copilot_err_streak == 3)
    w4._agent_err_ts = __import__("time").time() - rf.AGENT_ERR_WINDOW_S - 1
    w4._decide(err + "t4")
    check("dead_genuine_outage_still_stucks_past_window",
          w4.status == "stuck" and w4.outcome == "INFRA_STUCK")


def main():
    test_disk_floor_predicate()
    test_tab_load_accounting()
    test_tab_budget_admission()
    test_hysteresis_no_thrash()
    test_continuous_admission_no_barrier()
    test_verifying_counts_in_cap()
    test_disk_floor_blocks_in_loop()
    test_stop_cancels_running_fleet()
    test_pause_freezes_then_resumes()
    test_fleet_research_nonblocking()
    test_research_session_ram_gated_open()
    test_dead_agent_detector()
    test_tool_unreachable_infra()
    test_transient_outage_window()
    test_consent_detector()
    test_renav_first_on_consent_and_dead_agent()
    print("\n=== %d/%d admission checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
