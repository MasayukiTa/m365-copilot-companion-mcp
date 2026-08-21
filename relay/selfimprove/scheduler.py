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


def preconditions(*, recent_decisions=None, lock_path=None, activate=False,
                  operator_approved_activation=False, budget_candidates=None,
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
        history = [{"kept": (d.get("state") or "").upper() == "KEEP"}
                   for d in recent_decisions]
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
    decisions = [{"state": (e.get("gate_verdict") or "").upper()} for e in archive.all()][-20:]

    # `baseline_path` REACHES THE PRECONDITION, NOT ONLY THE TRIPWIRE. It was forwarded only
    # to `tripwires_after`, so a caller with a custom baseline had the DEFAULT frozen set
    # checked before the run and its own checked after -- a corrupt custom baseline burned the
    # whole night and then fired frozen_changed, which is the exact waste the precondition
    # exists to prevent, and a corrupt default blocked a run that was never going to use it.
    reasons = preconditions(recent_decisions=decisions, lock_path=lock_path,
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
