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


def main():
    test_disk_floor_predicate()
    test_hysteresis_no_thrash()
    test_continuous_admission_no_barrier()
    test_verifying_counts_in_cap()
    test_disk_floor_blocks_in_loop()
    test_stop_cancels_running_fleet()
    test_pause_freezes_then_resumes()
    test_fleet_research_nonblocking()
    test_research_session_ram_gated_open()
    print("\n=== %d/%d admission checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
