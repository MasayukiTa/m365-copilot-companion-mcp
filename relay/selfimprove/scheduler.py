"""Phase 11: running the loop on a schedule, and the conditions under which it must not.

WHAT SCHEDULED EVOLUTION IS

A campaign that runs without someone starting it. That is the whole of the feature and
almost none of the work; the work is the set of conditions under which an unattended run
should decline to start, because an unattended loop is exactly the arrangement in which a
quiet defect compounds.

THE PRECONDITIONS, AND WHAT EACH ONE PREVENTS

  frozen set intact      A run whose judge changed produces numbers nobody can trust, and
                         unattended they land in the archive looking like every other row.
  no run in flight       Two campaigns sharing an archive and an active manifest interleave
                         their candidates, and the second one's baseline is the first one's
                         half-applied state.
  the harness is well    If the last window was mostly INFRA_ABORT, more candidates produce
                         more aborts. Fix the environment first; this is the same judgement
                         harness_feedback makes, reused rather than restated.
  activation is off      unless an operator explicitly turned it on for this schedule. A
                         scheduled run that can install its own winner is a system that
                         changes while nobody is watching, and the whole gate structure
                         upstream assumes someone chose.
  a budget               so a loop that finds a productive-looking direction cannot spend a
                         night on it.

WHY IT REPORTS RATHER THAN RETRIES

A precondition that fails is information. Retrying past it converts "the environment is
broken" into "the environment is broken and we have burned four hours", and the record shows
a busy night rather than a blocked one.
"""
from __future__ import annotations

import json
import os
import time

from relay.selfimprove import frozen as F
from relay.selfimprove import harness_feedback as HF

#: A lock the scheduler holds while a campaign is in flight. A file rather than a process
#: check: the run that matters may be on the other side of a reboot, and a stale lock with a
#: readable timestamp is easier to reason about than a missing process.
DEFAULT_LOCK = os.path.join(os.path.dirname(__file__), "campaign.lock")

#: How long a lock may be held before it is presumed abandoned. Long enough that a real
#: campaign is never interrupted; short enough that a crash does not stop tomorrow's run.
STALE_LOCK_S = 6 * 3600


class Blocked(RuntimeError):
    """Raised when a scheduled run must not start, with the reason it must not."""


def preconditions(*, recent_decisions=None, lineage_decisions=None, lock_path=None,
                  activate=False, operator_approved_activation=False, budget_candidates=None,
                  baseline_path=None, level="B", plateau_k=5) -> list:
    """Every reason this run should not start. Empty means it may.

    Returns reasons rather than raising so a caller can log all of them at once: fixing one
    and rediscovering the next on the following night is how a scheduled loop spends a week
    not running.

    `level` is the autonomy rung (see `relay.selfimprove.autonomy`). It defaults to B because
    a SCHEDULED run is by definition one the system started, and that is what B means -- level
    A says a human starts the experiment, so a nightly campaign at level A is a contradiction
    rather than a configuration.
    """
    from relay.selfimprove import autonomy as AU

    reasons = []

    if not AU.permits(level, AU.START_EXPERIMENT):
        reasons.append(
            "level %s does not permit the system to start an experiment; at that rung a human "
            "starts it, so an unattended campaign is not a stricter version of this schedule "
            "-- it is a different one" % AU.normalise(level))

    ok, changed = _frozen(baseline_path)
    if not ok:
        reasons.append("the frozen set is not intact (%s); a run whose judge changed produces "
                       "numbers nobody can trust, and unattended they look like any other row"
                       % ", ".join(changed[:3]))

    held = lock_held(lock_path)
    if held:
        reasons.append("a campaign has been in flight since %s; two sharing an archive "
                       "interleave their candidates and the second one's baseline is the "
                       "first one's half-applied state" % held)

    # LEVEL C IS WHERE SELF-ACTIVATION LIVES. Below it, activation still happens -- a human
    # approves this schedule's winner, which is level B as the brief describes it. So the
    # per-run approval is not redundant with the rung: it is what B looks like in practice,
    # and the rung is what makes C not need it.
    if activate and not AU.permits(level, AU.ACTIVATE_CONFIG, change_kind="parameters",
                                   gates_all_passed=True) and not operator_approved_activation:
        reasons.append("activation is on but no operator approved it for this schedule; a "
                       "scheduled run that installs its own winner changes the system while "
                       "nobody is watching (level %s; self-activation begins at C)"
                       % AU.normalise(level))

    fired = halt_on_record(lock_path)
    if fired.get("fired"):
        reasons.append(
            "a tripwire fired on an earlier run (%s) and has not been cleared; the next "
            "scheduled night is exactly when nobody is watching, and re-deriving the "
            "preconditions from scratch would let it start as though that had not happened"
            % ", ".join(fired["fired"]))

    if budget_candidates is not None and int(budget_candidates) <= 0:
        reasons.append("no candidate budget; an unbounded scheduled loop can spend a night "
                       "on a direction that looked productive at 2am")

    if recent_decisions:
        for observation in HF.observe(decisions=recent_decisions):
            if "unwell" in observation["finding"]:
                reasons.append("%s -- %s" % (observation["finding"], observation["evidence"]))

        # PLATEAU (L3, relay.selfimprove.policy). A different question from "is the harness
        # well": the environment can be perfectly healthy while the last K candidates all
        # failed their gate, and continuing then is spending nights on a direction that has
        # stopped paying. `harness_feedback` watches for aborts; this watches for a run of
        # honest rejections, which look fine one at a time.
        from relay.selfimprove import policy as POL
        # `kept`, NOT `keep`. `policy._keep_flag` reads `final_keep` or `kept`; a dict with
        # neither reads as a rejection, so an adapter using the wrong spelling makes EVERY
        # decision look failed -- and the plateau then fires on five consecutive KEEPs, which
        # is a precondition that blocks the schedule permanently and looks like a finding.
        #
        # TWO POPULATIONS, NOT ONE. The health check above reads every recent decision,
        # because an INFRA_ABORT is a property of the instrument and is a reason not to run
        # tonight whichever line the run would follow. The plateau reads ONE LINEAGE, because
        # "has this direction stopped paying" stops meaning anything the moment a second
        # branch exists: a KEEP on branch B would reset the plateau for branch A, and the loop
        # would keep spending nights on a line that has failed every time.
        #
        # Falls back to the flat list when no lineage is supplied, which is the single-branch
        # world and the behaviour every existing caller had.
        history = [{"kept": (d.get("state") or "").upper() == "KEEP"}
                   for d in (lineage_decisions if lineage_decisions is not None
                             else recent_decisions)]
        if POL.plateaued(history, plateau_k):
            reasons.append(
                "no candidate has passed its gate in the last %d decisions; a scheduled loop "
                "that keeps proposing into a plateau is buying rows rather than findings, and "
                "the next move is a human choosing a different direction" % plateau_k)

    return reasons



def tripwires_after(result, *, prev_pass=None, baseline_path=None) -> dict:
    """Which L3 tripwires the finished run fires, and therefore whether to page a human.

    THE COUNTERPART TO `preconditions`, which only ever answers "may this start". A run that
    started legitimately can still end somewhere a person needs to see: the judge changed
    while it ran, the pass rate jumped further than a real change plausibly could, the
    environment aborted a third of the episodes, or the sentinel regressed.

    Assembled from what the result actually carries. A tripwire whose input is absent is NOT
    evaluated -- `policy.evaluate_tripwires` is built that way on purpose, so a partial state
    cannot fire one, and a missing measurement never becomes a fired alarm.
    """
    from relay.selfimprove import policy as POL

    def _sub(name):
        """A nested section, only if it is a dict. A truthy STRING passed `or {}` and then
        `"regressed" in "nothing regressed"` matched as a substring, and the next line called
        .get on a str -- an AttributeError thrown AFTER the campaign, destroying the night's
        whole report over a malformed field."""
        value = (result or {}).get(name)
        return value if isinstance(value, dict) else {}

    state = {}
    ok, _changed = _frozen(baseline_path)
    state["frozen_ok"] = ok

    new_pass = (result or {}).get("pass_at_1")
    if isinstance(new_pass, (int, float)):
        state["new_pass"] = new_pass
        if isinstance(prev_pass, (int, float)):
            # ONLY WHEN THERE IS ONE. Putting `prev_pass=None` in the state made the jump
            # tripwire evaluable-looking while it could never fire, which reads in
            # `evaluated` as a check that ran.
            state["prev_pass"] = prev_pass

    infra = _sub("infra")
    slice_ids = (result or {}).get("slice_ids") or []
    # BOTH ARMS. Counting only the candidate's infra failures halves the rate: a baseline-side
    # abort is the same sick harness, and the tripwire is about the harness rather than about
    # the candidate.
    infra_ids = set(_sub("on").get("infra_ids") or []) | set(_sub("off").get("infra_ids") or [])
    if infra.get("aborted"):
        # An abort is a total loss whether or not a slice was already chosen. Dividing by the
        # slice made the run that aborted AFTER selection read as a low rate.
        state["infra_rate"] = 1.0
    elif slice_ids:
        state["infra_rate"] = len(infra_ids) / float(len(slice_ids))

    sentinel = _sub("sentinel")
    if "regressed" in sentinel:
        state["sentinel_regressed"] = bool(sentinel.get("regressed"))

    fired = POL.evaluate_tripwires(state)
    # WHICH TRIPWIRES, NOT WHICH STATE KEYS. `len(state)` counted `prev_pass` as a tripwire,
    # so the one field whose whole job is to say what was checked reported three where two
    # existed.
    checked = sorted({"frozen_changed" if k == "frozen_ok" else
                      "implausible_jump" if k in ("new_pass", "prev_pass") else
                      "infra_spike" if k == "infra_rate" else
                      "sentinel_regressed"
                      for k in state})
    unreadable = [name for name in ("pass_at_1", "slice_ids", "sentinel")
                  if name not in (result or {})]
    return {
        "fired": fired,
        "halt": bool(fired),
        "state": state,
        # SAID EXPLICITLY. "No tripwire fired" and "no tripwire could be evaluated" are
        # different, and only the first is reassuring.
        "evaluated": checked,
        # AND WHY THE REST COULD NOT BE. The production caller was handing this a campaign
        # SUMMARY rather than a paired-evaluation report, so only `frozen_ok` was ever
        # present and the report said "nothing fired" every night -- wiring rot that reads
        # exactly like health.
        "unreadable_inputs": unreadable,
        "note": ("%d tripwire(s) fired; a scheduled run does not decide what to do about "
                 "this -- it records the halt and says so" % len(fired)) if fired
                else "nothing fired among the %d tripwire(s) this result could answer%s"
                     % (len(checked),
                        "" if not unreadable
                        else "; %s absent from the result, so %d tripwire(s) were not checked"
                             % (", ".join(unreadable), 4 - len(checked))),
    }


def _frozen(baseline_path):
    try:
        if baseline_path:
            return F.frozen_intact(baseline_path=baseline_path)
        return F.frozen_intact()
    except Exception as exc:
        # Unable to check is not intact. The scheduled path is the one where nobody is
        # watching, so it is the last place to be generous about this.
        return False, ["frozen check failed: %s" % exc]


# --------------------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------------------

def lock_held(lock_path=None):
    """The ISO timestamp a live lock was taken, or "" if none is held."""
    path = lock_path or DEFAULT_LOCK
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    started = float(data.get("started_at") or 0)
    if time.time() - started > STALE_LOCK_S:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(started))


def take_lock(lock_path=None, *, note=""):
    """Take the lock, or raise. The create is ATOMIC.

    Checking `lock_held` and then opening for write is two operations, and two schedulers
    that both check before either writes both proceed -- which is the exact situation the
    lock exists to prevent, arriving only under the timing that makes it hardest to see
    afterwards. O_CREAT|O_EXCL makes the creation itself the test.
    """
    path = lock_path or DEFAULT_LOCK
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if lock_held(path):
            raise Blocked("a campaign is already in flight")
        # A lock file older than STALE_LOCK_S is an abandoned one; take it over rather than
        # letting yesterday's crash block every night from here on.
        release_lock(path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Blocked("a campaign is already in flight")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"started_at": time.time(), "pid": os.getpid(), "note": note}, fh)
    return path


def release_lock(lock_path=None):
    try:
        os.remove(lock_path or DEFAULT_LOCK)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------

def scheduled_run(run_campaign, *, budget_candidates=5, lock_path=None,
                  recent_decisions=None, activate=False,
                  operator_approved_activation=False, baseline_path=None) -> dict:
    """Check the preconditions, run the campaign once, release. Never retries.

    `run_campaign(budget)` does the work and returns whatever it likes; this is only the
    part that decides whether it happens, and records why when it does not.
    """
    reasons = preconditions(
        recent_decisions=recent_decisions, lock_path=lock_path, activate=activate,
        operator_approved_activation=operator_approved_activation,
        budget_candidates=budget_candidates, baseline_path=baseline_path)
    if reasons:
        return {"ran": False, "blocked_by": reasons, "result": None,
                "note": "a failed precondition is information; retrying past it converts a "
                        "blocked night into a busy one and the record stops saying which"}

    take_lock(lock_path, note="scheduled campaign")
    try:
        result = run_campaign(budget_candidates)
    finally:
        release_lock(lock_path)
    return {"ran": True, "blocked_by": [], "result": result, "note": ""}


# --------------------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------------------

def nightly(*, budget_candidates=5, activate=False, operator_approved_activation=False,
            evaluate=None, archive_path=None, lock_path=None,
            trace_days=7, trace_dir=None, trace_ledger_path=None,
            baseline_path=None) -> dict:
    """One scheduled campaign, with the phases that decide WHAT to run wired in.

    This exists because the parts had no caller. Phase 9 selected a replay set, Phase 6
    judged the harness, Phase 11 decided whether to start -- each tested, none reachable
    from anything a person could run, which is a way of being finished that does not survive
    someone trying to use it. Phase 8 is the same story: `trace_to_eval.classify`/`promote`
    were tested but nothing read a real corrections log, classified it, or kept a record of
    what it had already promoted -- see `trace_to_eval.nightly_step` for what "reachable" means
    when, as of this wiring, no production caller writes to that log yet.

    The order is the argument. The recent decisions are read FIRST, because they answer two
    different questions: whether the harness is well enough to run at all (Phase 6, a
    precondition here) and which failures the next run should replay (Phase 9). Running a
    campaign against a harness that mostly aborts produces more aborts, and choosing what to
    replay from that history chooses noise. Phase 8's corrections are read and classified only
    once the run is not blocked, for the same reason replay selection is: a blocked night's
    report should say why it was blocked, not do unrelated bookkeeping first.

    `trace_days`/`trace_dir`/`trace_ledger_path` exist so a caller (a test, an operator
    pointing at a non-default log) can control where Phase 8 reads and writes without
    touching the module-level defaults; see `trace_to_eval.nightly_step`.
    """
    from relay.selfimprove import archive as A
    from relay.selfimprove import campaign as C
    from relay.selfimprove import coreset as CS
    from relay.selfimprove import trace_to_eval as T
    from relay.selfimprove.controller import EvolutionController

    archive = A.Archive(archive_path) if archive_path else A.Archive()
    # `Archive`'s public accessor is `.all()` -- `.entries()` does not exist on it, which
    # meant this line raised AttributeError the moment archive_path pointed at a real
    # Archive instance. Nothing had ever called `nightly()` to notice.
    # `gate_verdict`, WHICH IS THE KEY THE ARCHIVE ACTUALLY WRITES (archive.Archive.add).
    # This read `verdict`, which no row has ever carried, so every state was "" -- and that is
    # byte-for-byte the failure the commit one line down claims to have fixed, recommitted
    # against the adjacent key. Two things died from it at once: `plateaued` saw nothing
    # KEEP and fired on any archive with five rows including five KEEPs, and `HF.observe`
    # counted zero of every state so "the harness is unwell" could never fire either.
    def _states(entries):
        return [{"state": (e.get("gate_verdict") or "").upper()} for e in entries]

    # GLOBAL: every recent decision, for "is the instrument well enough to run".
    decisions = _states(archive.all())[-20:]
    # SCOPED: the line the loop is actually on, for "has this direction stopped paying". The
    # tip is the most recent row -- the branch the last night extended.
    tip = archive.tip()
    lineage_decisions = _states(archive.lineage((tip or {}).get("id", "")))[-20:] or None

    # `baseline_path` REACHES THE PRECONDITION, NOT ONLY THE TRIPWIRE. It was forwarded only
    # to `tripwires_after`, so a caller with a custom baseline had the DEFAULT frozen set
    # checked before the run and its own checked after -- a corrupt custom baseline burned the
    # whole night and then fired frozen_changed, which is the exact waste the precondition
    # exists to prevent, and a corrupt default blocked a run that was never going to use it.
    reasons = preconditions(recent_decisions=decisions,
                            lineage_decisions=lineage_decisions, lock_path=lock_path,
                            activate=activate,
                            operator_approved_activation=operator_approved_activation,
                            budget_candidates=budget_candidates,
                            baseline_path=baseline_path)
    if reasons:
        return {"ran": False, "blocked_by": reasons, "result": None,
                "note": "a failed precondition is information; retrying past it converts a "
                        "blocked night into a busy one and the record stops saying which"}

    replay = CS.select(_recent_failures(archive), budget=budget_candidates)
    trace_eval = T.nightly_step(days=trace_days, dir_=trace_dir, ledger_path=trace_ledger_path)

    def run(budget):
        controller = EvolutionController(activate=activate)
        return C.sweep(controller, evaluate=evaluate or _refuse,
                       on_result=lambda row: None)

    out = scheduled_run(run, budget_candidates=budget_candidates, lock_path=lock_path,
                        recent_decisions=decisions, activate=activate,
                        operator_approved_activation=operator_approved_activation,
                        baseline_path=baseline_path)
    out["replay_set"] = replay
    out["trace_to_eval"] = trace_eval
    # AFTER THE RUN, NOT INSTEAD OF THE GATES. The gates decide about the candidate; the
    # tripwires decide whether the RUN itself is still trustworthy, and those are different
    # questions with different answers -- a candidate can be correctly rejected by a run whose
    # judge changed underneath it.
    out["tripwires"] = tripwires_after((out.get("result") or {}),
                                       prev_pass=_previous_pass(archive),
                                       baseline_path=baseline_path)
    # A HALT THAT LIVES ONLY IN A RETURNED DICT IS NOT A HALT. Nothing read it, nothing was
    # written, the exit status was 0 whether the night fired four tripwires or ran clean, and
    # the next night re-derived everything from scratch -- so a fired sentinel or jump left no
    # trace the following run could see. Recording it is the difference between "stops and
    # says so" and "says so to a dict".
    if out["tripwires"]["halt"]:
        out["halt_recorded"] = _record_halt(out["tripwires"], lock_path=lock_path)
    return out


#: Where a fired tripwire is written down. Beside the lock, because it is the same kind of
#: thing: state about the schedule that has to outlive the process that produced it.
def _halt_path(lock_path=None) -> str:
    base = lock_path or DEFAULT_LOCK
    return os.path.join(os.path.dirname(base) or ".", "tripwire_halt.json")


def _record_halt(tripwires, *, lock_path=None) -> str:
    """Persist a fired tripwire. Returns the path written, or the reason it could not."""
    path = _halt_path(lock_path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = json.dumps({"at": time.time(), "fired": tripwires.get("fired") or [],
                              "state": tripwires.get("state") or {}},
                             ensure_ascii=False, sort_keys=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload + "\n")
        os.replace(tmp, path)
        return path
    except OSError as exc:
        return "could not record the halt: %s" % exc


def halt_on_record(lock_path=None) -> dict:
    """A tripwire fired on an earlier night and nobody has cleared it, or {}."""
    try:
        with open(_halt_path(lock_path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _previous_pass(archive):
    """The most recent recorded pass@1, so the jump tripwire has something to compare to.

    `prev_pass` was never passed, so `tw_implausible_jump` returned False unconditionally --
    while the previous night's number sat in the archive the scheduler had just read.
    """
    try:
        for entry in reversed(archive.all()):
            value = entry.get("pass_at_1")
            if isinstance(value, (int, float)):
                return value
    except Exception:
        pass
    return None


def _recent_failures(archive) -> list:
    """Episode-level failures from the archive's recent entries, for the replay coreset."""
    rows = []
    for entry in archive.all()[-20:]:
        # `descriptors.episode_results`, which is where the controller puts them. This read
        # `entry["results"]["episodes"]` -- absent on every row -- so the replay coreset was
        # always empty. The same class as the `.entries()` AttributeError memorialised above,
        # except this one returned quietly instead of raising.
        episodes = ((entry.get("descriptors") or {}).get("episode_results") or {})
        for row in (list(episodes.get("candidate") or [])
                    + list(episodes.get("baseline") or [])):
            if not row.get("success", True):
                rows.append(row)
    return rows



def companionbench_evaluator(*, agent_kind="fleet", tmpdir=None, base_manifest=None, **kw):
    """The real evaluator: a paired CompanionBench run, baseline against candidate.

    Kept out of `nightly()`'s default on purpose. A default evaluator would mean a campaign
    that always finds SOMETHING to measure against, including when the operator has not said
    what they want measured -- and the whole reason `_refuse` exists is that a measurement
    nobody asked for is worth less than an honest refusal. This is the thing an operator
    passes when they do want one.

    Imported lazily because scheduler.py is imported by things that have no business pulling
    in Playwright and the bench pools.
    """
    import tempfile

    from bench.companionbench.baseline import build_agent
    from bench.companionbench.runner import make_evaluator

    return make_evaluator(build_agent(agent_kind),
                          tmpdir=tmpdir or tempfile.mkdtemp(prefix="campaign_"),
                          base_manifest=base_manifest, **kw)




#: Where a campaign's own fallback rows go. NOT the live socket_route log.
#:
#: The rows an arm produces look like production rows and are not: both arms force a transport,
#: so the socket arm carries goals the classifier would have sent to a tab. Those rows are the
#: most informative labels available -- they are the only way to learn a tab was unnecessary --
#: but appending them unmarked to the file the classifier trains from teaches it from a
#: distribution production never shows. The test suite already polluted two production ledgers
#: in this repository by exactly this route.
CAMPAIGN_SOCKET_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs", "research", "results", "route_campaign_socket.jsonl")


def _cdp_owner_pid(cdp_url):
    """The pid listening on the CDP port, or None if it cannot be resolved.

    Returned rather than raised, and the caller records which population it ended up with: a
    resolver that fails quietly would put the old, unscoped measurement back without saying so.
    """
    try:
        import psutil
    except Exception:
        return None
    try:
        port = int(str(cdp_url).rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
                return conn.pid
    except Exception:
        return None
    return None


def route_evaluator_for(goals, *, agent_url=None, cdp_url="http://127.0.0.1:9222",
                        max_concurrent=2, log_path=None, candidate_first=False,
                        warmup=False, null_arm=False,
                        transcript_dir=None, isolate_memory=True,
                        control_manifest=None, control_socket=False,
                        arm_reset=None):
    """An `evaluate(manifest, experiment_id)` that measures transport, not pass@1.

    CompanionBench answers "did the candidate solve more episodes". For a transport hypothesis
    that is the CONSTRAINT, not the question: the point of a socket is memory, and an evaluator
    that only counted completions would score two arms identical and return inconclusive
    forever -- the loop would look like it was measuring while learning nothing.

    THE ARMS DIFFER BY THE ROUTE SWITCH, NOT BY THE COMPONENT

    `transport` decides WHICH goals may use a socket; `MCP_FLEET_SOCKET` decides whether any
    may. Running the candidate against a route that is off would compare two arms that both
    open tabs -- the same identity defect that correctly killed the four coordinates before
    this one. So the control is tabs-everywhere and the candidate is the route as configured.

    THE SINGLETON IS REBUILT BETWEEN ARMS, AND THAT IS NOT TIDINESS

    `SocketRoute.enabled` is read at CONSTRUCTION, and the fleet caches one instance per
    process. Flipping the environment variable between arms would leave the second arm running
    the first arm's route, so the candidate would silently run tabs and report a clean null.
    """
    import os as _os

    from relay.selfimprove import route_evaluator as RV

    #: The lowest free physical memory seen at any sample, across both arms.
    #:
    #: `preflight` checks the floor ONCE, before the run. That makes it a start condition, and
    #: a start condition is not what the argument needs: an arm that begins at 2.2 GB and opens
    #: two tabs can cross the floor halfway through and spend the rest of the run measuring the
    #: page file. The floor has to hold FOR the measurement, not merely at its beginning, so
    #: every sample records it and the result is thrown out below if it broke.
    _floor = {"min_free_mb": None}

    #: Bumped per arm so each gets its own memory store path.
    _arm_seq = [0]

    #: When the current arm started, so its fallback rows can be told from the previous arm's
    #: in a log both of them append to.
    _arm_t0 = [0.0]

    #: What `_run` learned that `measure_arm` does not carry.
    #:
    #: `measure_arm` is frozen and returns a FIXED set of keys, so anything `_run` returns
    #: beyond done/fallbacks is dropped before the verdict ever sees it. The route-closure
    #: reason and the turn count were wired into `_run` and stopped there -- a rule that could
    #: never fire, in the same commit that added the rule, which is the defect this repository
    #: has now found in six components. The arm merges these afterwards, the way
    #: `new_renderers` already did.
    _extras = {}

    #: The processes that already existed when this arm started. Set per arm, never shared.
    _attr = {"baseline_pids": None, "peak_new_pids": 0,
             "root_pid": None, "population": None}

    def _begin_attribution():
        """Forget the previous arm's baseline. Called before each arm, not inside the sampler.

        `measure_arm` takes its first sample BEFORE running anything, so a sampler that
        lazily initialised itself would inherit the previous arm's baseline for exactly one
        call -- the call whose value becomes `start_mb`.
        """
        _attr["baseline_pids"] = None
        _attr["peak_new_pids"] = 0

    def _edge_mb():
        """Commit charge of msedge processes THIS ARM CREATED. Not total Edge memory.

        THE FIRST VERSION MEASURED PEAK TOTAL RSS AND IT MEASURED THE WRONG THING.

        Two campaigns run with the arms swapped returned opposite signs (+449 MB, then
        -666 MB), and in both the arm that ran FIRST showed the larger rise. Total RSS across
        a browser shared with other sessions cannot be attributed to an arm at all: another
        session opening a tab lands entirely on whichever arm is running, Windows trims
        working sets under pressure so RSS tracks system-wide pressure rather than demand,
        and the second arm inherits a high-water mark that leaves it almost no room to move.

        WHAT IS ATTRIBUTABLE IS PER-PROCESS GROWTH, NOT NEW PROCESSES.

        The first attempt at this counted only processes that did not exist when the arm
        began, on the reasoning that a socket goal creates no renderer. The measurement said
        otherwise: after a warm-up pass, an arm that opened four Copilot tabs created ONE new
        process worth 18 MB. Edge keeps a warm renderer pool and reuses it, so a tab's cost
        lands as growth inside processes that were already there -- which the new-process
        rule scores as zero and which is most of the real number.

        So the sum is over every msedge process, of how much MORE commit it holds than when
        the arm started. New processes fall out of the same rule with a baseline of zero.
        Commit (`private`) rather than RSS because commit is what the process asked for and
        does not move when the OS trims a working set under pressure.

        This still cannot separate another session's tab from ours -- nothing sampling from
        outside the browser can. It removes the two effects that dominated the first design:
        the absolute level the arm inherited, and trimming.
        """
        try:
            import psutil
        except Exception:
            return 0.0
        try:
            free = psutil.virtual_memory().available / (1024.0 * 1024.0)
            if _floor["min_free_mb"] is None or free < _floor["min_free_mb"]:
                _floor["min_free_mb"] = free
        except Exception:
            pass

        # THE POPULATION IS THE FLEET'S OWN BROWSER, NOT EVERY EDGE ON THE MACHINE.
        #
        # This summed every process named msedge.exe. Measured 2026-08-24 on this box: 45 such
        # processes holding 6,181 MB, of which the fleet's Edge (:9222) was 1,559 and the
        # bridge's (:9223) was 1,002 -- FIFTY-NINE PERCENT belonged to neither and moved for
        # reasons that had nothing to do with any arm. A socket arm that opens no tab was
        # reported at 1,070 MB, and two identical arms came back 707 MB apart. That is not the
        # route; it is the operator's own browsing landing on whichever arm was running.
        #
        # The fleet's browser is the process listening on the CDP port we are driving, plus its
        # children. Resolving that costs a connection-table scan, so the root is cached; the
        # TREE is re-walked every sample because Edge spawns and kills renderers constantly.
        if _attr["root_pid"] is None or not psutil.pid_exists(_attr["root_pid"]):
            _attr["root_pid"] = _cdp_owner_pid(cdp_url)
            # SAY WHICH POPULATION WAS USED, in the result. If the owner cannot be resolved this
            # falls back to every Edge -- which is the old, wrong measurement -- and a reader who
            # is not told cannot know which of the two produced the number.
            _attr["population"] = ("fleet-edge-tree" if _attr["root_pid"] is not None
                                   else "all-edge-unscoped")
        current = {}
        if _attr["root_pid"] is not None:
            try:
                root = psutil.Process(_attr["root_pid"])
                procs = [root] + root.children(recursive=True)
            except Exception:
                procs = []
        else:
            procs = psutil.process_iter(["pid", "name", "memory_info"])
        for proc in procs:
            try:
                if (proc.name() or "").lower() != "msedge.exe":
                    continue
                mi = proc.memory_info()
                current[proc.pid] = float(getattr(mi, "private", mi.rss))
            except Exception:
                continue

        if _attr["baseline_pids"] is None:
            # The whole map, not just the key set: a process that already existed can still
            # GROW because of this arm, and on a warm browser that is where the cost lands.
            _attr["baseline_pids"] = dict(current)
            return 0.0
        base = _attr["baseline_pids"]
        new_pids = [pid for pid in current if pid not in base]
        if len(new_pids) > _attr["peak_new_pids"]:
            _attr["peak_new_pids"] = len(new_pids)
        # Per-process GROWTH since the arm began, summed. Processes that shrank contribute
        # zero rather than a negative: a renderer the arm never touched, trimmed by the OS
        # while the arm ran, would otherwise pay the arm a credit it did not earn.
        total = 0.0
        for pid, value in current.items():
            grew = value - base.get(pid, 0.0)
            if grew > 0:
                total += grew
        return total / (1024.0 * 1024.0)

    def _fresh_route(enabled):
        """Build the route this arm will use and install it as the fleet's singleton."""
        from relay import relay_fleet as RF
        from relay.socket_route import SocketRoute, capture_via_tab, websocket_connect
        route = SocketRoute(capture_fn=capture_via_tab, connect_fn=websocket_connect,
                            enabled=enabled, log_path=log_path or CAMPAIGN_SOCKET_LOG,
                            log=lambda m: print(m, flush=True))
        RF._SOCKET_ROUTE = route
        return route

    def _activate(candidate_manifest):
        """Point the harness at the candidate WITHOUT touching the operator's active file.

        `runtime_config` already provides the override for exactly this: an A/B that rewrites
        the live configuration is not an A/B, it is two sequential deployments. The control arm
        clears it, so the two arms differ by the manifest and by nothing that leaked from the
        previous one.
        """
        import json
        import tempfile

        from relay.selfimprove import runtime_config as RC
        if candidate_manifest is None:
            _os.environ.pop(RC.OVERRIDE_ENV, None)
        else:
            fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
            json.dump(candidate_manifest, fh)
            fh.close()
            _os.environ[RC.OVERRIDE_ENV] = fh.name
        # The cache is keyed on (path, mtime); a new path invalidates it, but clearing the
        # override returns to a path that may still be cached from before the arm.
        RC.active_manifest(refresh=True)



    def _evidence_lines(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().splitlines()
        except Exception:
            return []

    def _evidence_intact(path, before):
        """True iff the fallback log only GREW since `before`.

        THE RULE READS THIS FILE, SO THE FILE HAS TO BE WHAT IT WAS.

        `fallback_verdict` decides whether an arm stopped being itself by counting rows here.
        A log that was rewritten, reordered or truncated during the run makes that count a
        statement about a file rather than about the arm -- and it would be a quiet one,
        because a shorter log reads as "fewer fallbacks", which is the direction that flatters
        the candidate. `frozen.burned_append_only` already encodes exactly this prefix rule for
        the burned registry, for exactly this reason, so it is reused rather than re-argued.
        """
        try:
            from relay.selfimprove import frozen as F
            return F.burned_append_only(before, _evidence_lines(path))
        except Exception:
            return True

    def _task_fallbacks(route):
        """How many of this arm's fallbacks were the GOAL's fault rather than the route's.

        A token that expired or a socket that dropped says nothing about which goals need a
        tab; counting those would teach "tasks at this hour need tabs". Only task-caused
        reasons are evidence about a classification, and `transport_policy.classify_fallback`
        is where that line already lives -- read from the arm's own log so the count belongs
        to the arm rather than to the day.
        """
        try:
            import json as _json

            from relay import transport_policy as TP
            path = log_path or CAMPAIGN_SOCKET_LOG
            count = 0
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = _json.loads(line)
                    except Exception:
                        continue
                    if row.get("event") != "fallback" or float(row.get("ts", 0)) < _arm_t0[0]:
                        continue
                    if TP.classify_fallback(row.get("reason") or "") == "task":
                        count += 1
            return count
        except Exception:
            return 0

    def _run(goal_list, socket_on, sample, manifest=None):
        from playwright.sync_api import sync_playwright

        from relay import socket_route as SR
        from relay.relay_fleet import run_relay_fleet
        _os.environ["MCP_FLEET_SOCKET"] = "1" if socket_on else "0"
        SR.ENABLED = bool(socket_on)

        # A FRESH MEMORY STORE PER ARM. THE ARMS WERE NOT INDEPENDENT UNITS WITHOUT IT.
        #
        # The fleet prepends this theme's past work notes to the body it sends
        # (`_with_theme_memory`) and writes them back on completion (`record_task`). Both arms
        # run the SAME goals, so arm 2 was reading what arm 1 had just written -- a transcript
        # from one of these runs opens with "The task is already complete per prior work
        # memory". Arm 2 was not doing the work, so its commit charge was not the cost of the
        # work, and the dependent variable fixed earlier today was being corrupted by a
        # channel that had nothing to do with memory measurement.
        #
        # This is not a global switch: the store relocates for the arm only, so an operator's
        # real fleet keeps its memory. The estimand this picks is the COLD one -- what the
        # transport costs per unit of actual work -- which is the question the hypothesis
        # asks. A steady-state estimand would keep the memory and randomise instead.
        # THE WORKSPACE IS SHARED BETWEEN ARMS EVEN WHEN MEMORY IS NOT.
        #
        # Memory isolation above stops arm 2 READING arm 1's notes. It does nothing about arm 1's
        # OUTPUT FILES: both arms run the same goals against the same folder, so arm 2 opens on
        # finished work and its acceptance checks pass on arm 1's answers. The bias favours
        # whichever arm ran second, which is the arm order rather than the treatment.
        if arm_reset:
            arm_reset()
        if isolate_memory:
            store = _os.path.join(
                _os.environ.get("TEMP", "."), "route_arm_%s_%d"
                % ("socket" if socket_on else "tab", _arm_seq[0]))
            _arm_seq[0] += 1
            _os.makedirs(store, exist_ok=True)
            _os.environ["FLEET_STATE_DIR"] = store
        _activate(manifest)
        _arm_t0[0] = time.time()
        route = _fresh_route(socket_on)
        url = agent_url or _os.environ.get("MCP_FLEET_AGENT_URL", "")
        done = 0
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # TRANSCRIPTS, BECAUSE DONE PARITY IS NOT QUALITY PARITY.
            #
            # Both arms have returned 4/4 in every run so far, and a count cannot tell a
            # real answer from a plausible empty one. That distinction is the whole of the
            # safety argument for which goals may take a socket, so the arm has to leave the
            # text behind or the argument is being made from a number that cannot carry it.
            tx = None
            if transcript_dir:
                tx = _os.path.join(transcript_dir,
                                   "socket" if socket_on else "tab")
                _os.makedirs(tx, exist_ok=True)
            rows = run_relay_fleet(context, list(goal_list), url,
                                   max_concurrent=max_concurrent,
                                   transcript_dir=tx,
                                   on_tick=lambda *a, **k: sample())
            for row in (rows or []) if isinstance(rows, list) else []:
                if str((row or {}).get("outcome", "")).upper() == "DONE":
                    done += 1
        status = route.status()
        # THE RULE IS INERT WITHOUT THESE. `fallback_verdict` reads `route_closed_reason` and
        # `task_fallbacks`; an arm that does not carry them makes the check a branch that can
        # never be taken -- which is the defect this repository has found in five components
        # and would be reintroducing in the very commit that adds the check.
        # TURNS COME FROM THE SAME LOG THE ARM ALREADY READS.
        #
        # `planner_evaluator` measures turns per goal, and `worker_done` rows carry the count
        # per goal -- which is what made `planner` the cheapest of the six coordinates audited.
        # Carried on the arm for the same reason the closure reason is: a rule whose input the
        # arm does not carry is a branch that can never be taken.
        from relay.selfimprove import planner_evaluator as _PE
        seen = _PE.turns_from_log(log_path or CAMPAIGN_SOCKET_LOG, since_ts=_arm_t0[0])
        # THE PER-CLASS BREAKDOWN, from the same rows. Without it `decide`'s cancellation
        # check is a branch that can never be taken -- the defect this repository has found in
        # six components, and the reason the check is wired in the same commit that adds it.
        try:
            by_class = _PE.turns_by_class(log_path or CAMPAIGN_SOCKET_LOG, goals,
                                          since_ts=_arm_t0[0])
        except Exception:
            by_class = None
        _extras.clear()
        _extras.update({
            "by_class": by_class,
            "route_closed_reason": str(status.get("closed_reason") or ""),
            "task_fallbacks": _task_fallbacks(route),
            "turns": seen["turns"], "logged_goals": seen["goals"]})
        return {"done": done,
                "fallbacks": int(status.get("fallbacks", 0) or 0),
                "route_closed_reason": str(status.get("closed_reason") or ""),
                "task_fallbacks": _task_fallbacks(route),
                "turns": seen["turns"], "logged_goals": seen["goals"]}

    def _token_is_capturable():
        """Try once, for real. Cost: one tab opened and closed -- what the route itself does.

        Asserting a token exists without capturing one is how the socket arm becomes the tab
        arm while the run reports success, which is the single failure this precondition is
        for. It has to actually look.
        """
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                from relay.socket_route import capture_via_tab, expires_in
                token, _template = capture_via_tab(
                    context, agent_url or _os.environ.get("MCP_FLEET_AGENT_URL", ""))
                return bool(token) and expires_in(token) > 0
        except Exception as exc:
            print("[route_eval] token capture failed: %s: %s"
                  % (type(exc).__name__, str(exc)[:160]), flush=True)
            return False


    def _turns_gain(control, candidate):
        """control's turns per goal minus the candidate's, or None if either did nothing."""
        try:
            from relay.selfimprove import planner_evaluator as _PE
            a, b = _PE.turns_per_goal(control), _PE.turns_per_goal(candidate)
            return None if a is None or b is None else round(a - b, 3)
        except Exception:
            return None


    def _judge_for(candidate_manifest, base=None):
        """The instrument declaring it can see whatever this candidate changed. RV by default.

        Falls back to the route evaluator when the change spans several instruments or none,
        because that is this adapter's original contract and a caller that gets a verdict has
        to be able to say which ruler produced it -- `instrument` is carried on the result for
        exactly that.
        """
        try:
            from relay.selfimprove import compare as _C
            from relay.selfimprove import manifest as _M
            found = _C.instrument_for_pair(candidate_manifest, base or _M.base_manifest())
            return found or RV
        except Exception:
            return RV

    def evaluate(candidate_manifest, experiment_id, base=None):
        from relay.relay_fleet import avail_phys_mb
        refusals = RV.preflight(free_mb=avail_phys_mb(), token_ok=_token_is_capturable())
        if refusals:
            return {"gate": None, "sentinel": None, "security": None, "regression": None,
                    "infra": {"aborted": True, "reason": "; ".join(refusals)},
                    "experiment_id": experiment_id}

        # The control is tabs-everywhere under the BASE harness; the candidate is the route
        # under the manifest being judged. Without the manifest the candidate arm would run
        # whatever is active, and transport/v1 and transport/v2 would measure identically --
        # the same identity defect one level up.
        # ARM ORDER IS A PARAMETER BECAUSE IT IS A CONFOUND.
        #
        # Arms run in sequence and Edge does not give its memory back between them, so the
        # second arm starts from the first one's residue and under pressure the OS trims its
        # working set -- which biases the measurement in a direction that depends only on
        # which arm went second. The first two campaigns both ran control-first and returned
        # OPPOSITE signs, so order cannot be waved away by argument; it has to be swapped and
        # the sign checked.
        def _control():
            """The arm the candidate is measured against.

            NOT ALWAYS "TABS UNDER THE BASE HARNESS". That is the right control for "is the
            route better than tabs", and it is the wrong one for two branches, where both arms
            are harnesses and the transport switch is not what is being tested -- pinning the
            control to socket-off there would compare branch A over tabs against branch B over
            a socket and attribute the transport difference to the genome.

            Defaults reproduce the original behaviour exactly, so every existing caller keeps
            the control it was written against.
            """
            _begin_attribution()
            out = RV.measure_arm(
                lambda g, s, smp: _run(g, s, smp, manifest=control_manifest),
                goals=goals, socket_on=bool(control_socket), peak_sampler=_edge_mb)
            out["new_renderers"] = _attr["peak_new_pids"]
            out["memory_population"] = _attr["population"]
            out.update(_extras)
            return out

        def _candidate():
            """The candidate arm -- or, under `null_arm`, a SECOND CONTROL.

            A null run answers the question a threshold cannot be set without: how far apart
            do two identical arms land on this instrument. The 300 MB figure in the frozen
            judge was derived from the total-RSS measurements, which are now known to have
            been measuring arm order, so it is calibrated against an instrument that no
            longer exists. Lowering it to fit an observed result would be tuning the ruler to
            the object; measuring two identical arms is the version of that question that
            cannot be gamed, because nothing in the comparison differs.
            """
            _begin_attribution()
            out = RV.measure_arm(
                lambda g, s, smp: _run(g, s, smp,
                                       manifest=(control_manifest if null_arm
                                                 else candidate_manifest)),
                goals=goals,
                socket_on=bool(control_socket) if null_arm else True,
                peak_sampler=_edge_mb)
            out["new_renderers"] = _attr["peak_new_pids"]
            out["memory_population"] = _attr["population"]
            out.update(_extras)
            out["is_null"] = bool(null_arm)
            return out

        def _warmup():
            """One pass whose numbers are thrown away, run BEFORE EVERY ARM, ALWAYS OVER TABS.

            The first arm of a session pays for renderer creation, authentication and the
            Copilot session handshake, and that cost lands entirely on whichever arm went
            first. It does not remove the other order effects -- a shared browser and a
            shared machine still put another session's tab on whichever arm is running --
            but it removes the one that is certain to be there.

            ONCE PER RUN, ON THE CONTROL'S TRANSPORT, WAS NOT THE SAME CONDITION IN BOTH
            COLUMNS. Edge keeps a warm renderer pool and reuses it, so warming it is something
            only a TAB pass does. The warm-up followed `control_socket`, so a socket-vs-socket
            null warmed nothing while a tabs-vs-socket treatment warmed the pool -- the two
            groups being compared differed in whether renderer creation had already been paid
            for, and no null flavour could price that difference. It would have arrived as a
            clean p on the wrong mechanism.

            So: always a tab pass, and before EACH arm rather than once, so neither arm
            inherits pool state from the other and the baseline each arm is measured against
            is taken with the pool already warm. Costs about a minute per arm and removes a
            confound that the whole series would otherwise carry.
            """
            _begin_attribution()
            RV.measure_arm(
                lambda g, s, smp: _run(g, s, smp, manifest=control_manifest),
                goals=goals[:1], socket_on=False, peak_sampler=_edge_mb)
            _floor["min_free_mb"] = None

        evidence_path = log_path or CAMPAIGN_SOCKET_LOG
        evidence_before = _evidence_lines(evidence_path)
        try:
            if candidate_first:
                if warmup:
                    _warmup()
                candidate = _candidate()
                if warmup:
                    _warmup()
                control = _control()
            else:
                if warmup:
                    _warmup()
                control = _control()
                if warmup:
                    _warmup()
                candidate = _candidate()
        finally:
            _activate(None)
        if not _evidence_intact(evidence_path, evidence_before):
            # NOT A VERDICT. The rows the route rule counts were rewritten while the arms ran,
            # so what the count means is unknown -- and a truncated log reads as "fewer
            # fallbacks", the direction that flatters the candidate.
            return {"gate": None, "sentinel": None, "security": None, "regression": None,
                    "infra": {"aborted": True,
                              "reason": "the fallback log was rewritten during the run (%s); "
                                        "the evidence the route rule reads is not the evidence "
                                        "that was there when it started" % evidence_path},
                    "experiment_id": experiment_id,
                    "control": control, "candidate": candidate,
                    "actual_effect": {"control": control, "candidate": candidate,
                                      "evidence_rewritten": True}}

        low = _floor["min_free_mb"]
        if low is not None and low < RV.MIN_FREE_MB:
            # NOT a gate result. The comparison ran, and what it measured is unknown -- which
            # is a different thing from the routes being indistinguishable, and recording it as
            # inconclusive would put a swap measurement into the archive wearing a verdict.
            return {"gate": None, "sentinel": None, "security": None, "regression": None,
                    "infra": {"aborted": True,
                              "reason": "free memory fell to %.0f MB during the run, under the "
                                        "%.0f MB floor. The quantity under test is memory, so "
                                        "from that point the arms were measuring the page file."
                                        % (low, RV.MIN_FREE_MB)},
                    "experiment_id": experiment_id,
                    "control": control, "candidate": candidate, "min_free_mb": round(low, 1),
                    "arm_order": "candidate,control" if candidate_first else "control,candidate",
                    "actual_effect": {"control": control, "candidate": candidate,
                                      "min_free_mb": round(low, 1), "aborted": True}}
        # THE INSTRUMENT THAT CAN JUDGE THIS CANDIDATE, not always the memory one.
        #
        # The same defect one path along: `compare.run` was fixed to pick an instrument and
        # this adapter -- the one `nightly` goes through -- was still calling RV.decide for
        # everything. A planner candidate would have been scored against a 300 MB memory
        # threshold it has no mechanism to reach, and the loop would have recorded
        # INCONCLUSIVE about a quantity nobody measured.
        judge = _judge_for(candidate_manifest, base)
        per_class = ({"control": control.get("by_class") or {},
                      "candidate": candidate.get("by_class") or {}}
                     if control.get("by_class") and candidate.get("by_class") else None)
        try:
            verdict = judge.decide(control, candidate, per_class=per_class)
        except TypeError:
            # The memory judge takes no breakdown: Edge memory is not attributable per goal,
            # so there is nothing to split. Calling it the old way is correct, not a fallback.
            verdict = judge.decide(control, candidate)
        return {
            "gate": {"keep": verdict["verdict"] == "keep",
                     "verdict": verdict["verdict"],
                     "reason": verdict["why"]},
            "sentinel": None, "security": None, "regression": None,
            "infra": {"aborted": False},
            "experiment_id": experiment_id,
            "control": control, "candidate": candidate,
            "instrument": judge.__name__.rsplit(".", 1)[-1],
            "memory_gain_mb": verdict.get("memory_gain_mb"),
            # THE OTHER INSTRUMENT'S QUANTITY, from the same two arms.
            #
            # Running the arms is expensive and instrument-agnostic: the same warm-up, the same
            # crossover, the same isolation. Only the number read off them differs. Computing
            # both here means a planner comparison does not need its own campaign -- and it
            # means `compare.decide` finds `turns_gain` populated, rather than reading zero and
            # reporting "no difference" about a quantity nobody measured.
            "turns_gain": _turns_gain(control, candidate),
            "min_free_mb": round(low, 1) if low is not None else None,
            "arm_order": "candidate,control" if candidate_first else "control,candidate",
            "warmup": bool(warmup),
            "null_run": bool(null_arm),
            "isolated_memory": bool(isolate_memory),
            # THE MECHANISM, WHICH IS WORTH MORE THAN THE STATISTIC HERE. A socket goal should
            # create no renderer at all, so the fleet-scale difference is arithmetic --
            # renderers avoided times commit per renderer -- rather than an effect that has to
            # be detected against a swing four times the decision threshold.
            "renderers": {"control": control.get("new_renderers"),
                          "candidate": candidate.get("new_renderers")},
            # THE KEY THE LEDGER ACTUALLY READS.
            #
            # The first `nightly()` run recorded actual_effect {} while the evaluator was
            # returning both arms, the gain, the renderer counts and the memory floor. The
            # controller was not dropping them -- the contract names one field and this
            # evaluator had never filled it, so the only number that survived into the
            # durable record was the one that happened to be inside a sentence. A record
            # that keeps the verdict and loses the measurement cannot be re-read later.
            "actual_effect": {
                "control": control, "candidate": candidate,
                # BOTH QUANTITIES, whichever judged. The first planner row carried
                # turns_gain=None beside a verdict quoting 0.00, because this dict was written
                # before the turns figure existed and nobody added it -- a durable record that
                # cannot reproduce the sentence next to it.
                "instrument": judge.__name__.rsplit(".", 1)[-1],
                "turns_gain": _turns_gain(control, candidate),
                "memory_gain_mb": verdict.get("memory_gain_mb"),
                "renderers": {"control": control.get("new_renderers"),
                              "candidate": candidate.get("new_renderers")},
                "min_free_mb": round(low, 1) if low is not None else None,
                "arm_order": "candidate,control" if candidate_first else "control,candidate",
                "isolated_memory": bool(isolate_memory),
                "null_run": bool(null_arm),
            },
        }

    return evaluate

def _refuse(*_a, **_k):
    raise Blocked("nightly() needs an evaluator; it will not invent one and call the result "
                  "a measurement")


if __name__ == "__main__":                                   # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="One scheduled evolution campaign.")
    ap.add_argument("--budget", type=int, default=5)
    ap.add_argument("--activate", action="store_true",
                    help="install the winner (needs --operator-approved as well)")
    ap.add_argument("--operator-approved", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the preconditions and stop")
    ap.add_argument("--evaluator", default="", choices=("", "companionbench"),
                    help="how candidates are measured. Empty means none, and the campaign "
                         "will refuse rather than invent one -- which is what every run "
                         "before this flag existed did, 1018 times")
    ap.add_argument("--agent", default="fleet",
                    help="which target the evaluator drives (bench|fleet)")
    ap.add_argument("--min-n", type=int, default=0,
                    help="episodes per arm; 0 uses the evaluator's own default")
    args = ap.parse_args()

    evaluate = None
    if args.evaluator == "companionbench":
        kw = {"min_n": args.min_n} if args.min_n else {}
        evaluate = companionbench_evaluator(agent_kind=args.agent, **kw)

    if args.dry_run:
        for reason in preconditions(budget_candidates=args.budget, activate=args.activate,
                                    operator_approved_activation=args.operator_approved):
            print("BLOCKED:", reason)
        else:
            print("preconditions OK")
    else:
        print(json.dumps(nightly(budget_candidates=args.budget, activate=args.activate,
                                 operator_approved_activation=args.operator_approved,
                                 evaluate=evaluate),
                         ensure_ascii=False, indent=2, default=str))
