"""Compare two branches with the same discipline the loop uses on itself.

WHAT THIS IS NOT

It is not an A/B testing tool. The word promises something this instrument cannot deliver: it
measures the commit charge a fleet run creates in Edge, calibrated for a transport hypothesis,
and its noise floor was measured at 130-180 MB against a decision threshold of 300 MB. Two
branches differing in `memory_max_items` have no reason to move Edge memory at all, and
comparing them here would spend twenty minutes to return INCONCLUSIVE for a structural reason
rather than an empirical one. So the vocabulary is deliberately "this comparison on this
instrument", and `instrument_can_see()` says up front when the answer is foreseeable.

That matters more than it sounds. A tool that mostly returns "no difference" and offers a
re-run button is a p-hacking machine: run it enough times and one ordering will clear the
threshold. Every attempt on a pair is therefore recorded and shown, never the best one.

WHY THE REFUSALS ARE CHECKED TWICE

Once when the request is made, so the operator learns while they are looking at the screen,
and once immediately before the arms run, because the twenty minutes in between are enough for
the frozen set to change, a tripwire to fire, or another comparison to take the lock. A check
that only runs at enqueue time is a check that agrees with a state that has since gone.

THE ARMS ARE THE TWO BRANCHES, AND NOTHING ELSE DIFFERS

Same goals, same order-crossover, same per-arm memory isolation, same warm-up, same frozen
graders. The transport switch is ON for both, because a branch's own `transport` component is
part of what is being compared and pinning one arm to tabs would attribute the transport
difference to the genome.
"""
from __future__ import annotations

import json
import os
import time

from relay.selfimprove import branches as BR
from relay.selfimprove import manifest as M

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QUEUE_PATH = os.path.join(REPO, ".fleet", "selfimprove", "compare_queue.jsonl")
RESULTS_PATH = os.path.join(REPO, ".fleet", "selfimprove", "comparisons.jsonl")

def instrument_measures():
    """What the active instrument declares it can see, and one line describing it.

    READ FROM THE INSTRUMENT, NOT HELD HERE. The first version kept a tuple in this file, which
    works exactly until a second evaluator exists -- at which point this module's list still
    describes the first one and nothing says so. The scope is a property of the measurement, so
    it travels with the measurement.
    """
    from relay.selfimprove import route_evaluator as RV
    return tuple(getattr(RV, "MEASURES", ())), getattr(RV, "MEASURES_NOTE", "")

VERDICT_A, VERDICT_B, VERDICT_NONE = "A", "B", "INCONCLUSIVE"


class CompareError(RuntimeError):
    """A comparison that must not run. Not a result about the branches."""


def _p(path, env, default):
    return path or os.environ.get(env, "").strip() or default



#: Names that mean "the harness as it shipped" when used as a COMPARISON OPERAND.
#:
#: Deliberately not branch refs -- `branches.RESERVED` still refuses to create them, so base
#: keeps its single definition in `manifest.base_manifest()` and `reset_to_base()` keeps
#: meaning "what shipped". But "is my branch better than what shipped" is the question an
#: operator asks first, and a tool that could only compare two named branches could not
#: express it: the archive holds candidates, and the base is not a candidate, so there is
#: never a row to point a second label at.
BASE_OPERANDS = frozenset({"base", "main", "BASE", "MAIN"})


def is_base(label) -> bool:
    return str(label) in BASE_OPERANDS


def resolve_operand(label: str, *, archive, path=None) -> dict:
    """A branch by name, or the base harness. Same shape either way.

    The base is CONSTRUCTED here rather than read from anywhere, which is the whole reason it
    cannot go stale or be lost: there is no file to drift.
    """
    if is_base(label):
        manifest = M.base_manifest()
        return {"label": "base", "genome_id": None, "genome": {},
                "manifest": manifest, "harness_id": M.harness_id(manifest)}
    return BR.resolve(label, archive=archive, path=path)


def instrument_can_see(manifest_a: dict, manifest_b: dict) -> tuple:
    """(bool, note). Whether this instrument could detect the difference between two harnesses.

    Structural, not statistical: if the two branches differ only in components the dependent
    variable has no mechanism to reflect, the run will return INCONCLUSIVE whatever happens,
    and twenty minutes will have bought nothing. Told BEFORE the run, because a limitation
    disclosed afterwards reads as an excuse.
    """
    # `M.diff` returns FLAT keys -- {"components.transport": (from, to)} -- not a nested
    # dict. The first version of this read it as nested, found nothing, and reported every
    # comparison as "the two harnesses are identical": a refusal that would have blocked the
    # whole feature while looking like a considered check.
    delta = M.diff(manifest_a, manifest_b)
    changed = {key.split(".", 1)[-1] for key in delta}
    if not changed:
        return False, "the two harnesses are identical"
    measures, note = instrument_measures()
    visible = changed & set(measures)
    if visible:
        return True, "differs in %s, which this instrument measures" % ", ".join(sorted(visible))
    return False, (
        "differs in %s. This instrument measures %s, and nothing in that difference has a "
        "mechanism to move it -- the run would take about twenty minutes to return "
        "INCONCLUSIVE for a structural reason rather than an empirical one"
        % (", ".join(sorted(changed)), note))



def transport_versions_differ(manifest_a: dict, manifest_b: dict, goals) -> tuple:
    """(bool, note). Do the two harnesses' transport policies actually decide differently?

    THE SEVENTH DOOR THE SAME DEFECT WALKED THROUGH.

    `same_program` compares MANIFESTS, and two manifests can name two different versions that
    behave identically. Measured, not hypothesised: `transport/v1` and `transport/v2` differed
    only in a Work IQ carve-out, that carve-out was removed once socket-borne Graph results
    were shown to match Work IQ, and from that moment the two versions returned the same
    transport for every goal. A real comparison was run between them and returned +15 MB and
    +27 MB -- the null floor, correctly reported as INCONCLUSIVE, for a reason no reader could
    have recovered from the number.

    So for the ONE component this instrument measures, ask the policies. Undecidable in
    general; entirely decidable for a pure function over the goals the comparison will use.
    """
    from relay import transport_policy as TP

    va = (manifest_a.get("components") or {}).get("transport")
    vb = (manifest_b.get("components") or {}).get("transport")
    if va == vb:
        return False, "both harnesses name %s" % va
    fa, fb = TP.TRANSPORT_VERSIONS.get(va), TP.TRANSPORT_VERSIONS.get(vb)
    if fa is None or fb is None:
        return True, "one of %s / %s is not in the version table; cannot compare them here"             % (va, vb)
    texts = []
    for goal in goals or []:
        texts.append(goal.get("text") or goal.get("goal") or ""
                     if isinstance(goal, dict) else str(goal))
    disagree = [t for t in texts if fa(t) != fb(t)]
    if disagree:
        return True, "%s and %s choose differently on %d of %d goals"             % (va, vb, len(disagree), len(texts))
    return False, (
        "%s and %s return the same transport for every one of these %d goals. The manifests "
        "differ and the behaviour does not, so the two arms would be the same program -- the "
        "difference this instrument would report is its own noise" % (va, vb, len(texts)))


def refusals(label_a: str, label_b: str, *, archive, branches_path=None, lock_path=None,
             free_mb=None, token_ok=None, check_live=True, goals=None) -> list:
    """Every reason this comparison must not run. Empty means it may.

    Reasons rather than an exception so the operator sees all of them at once; fixing one and
    discovering the next twenty minutes later is how a comparison spends an afternoon not
    running.
    """
    from relay.selfimprove import frozen as F
    from relay.selfimprove import route_evaluator as RV
    from relay.selfimprove import scheduler as S

    out = []
    if label_a == label_b:
        out.append("a branch cannot be compared with itself")

    resolved = {}
    for label in (label_a, label_b):
        if label in resolved:
            continue
        try:
            resolved[label] = resolve_operand(label, archive=archive, path=branches_path)
        except BR.BranchError as exc:
            out.append(str(exc))

    if len(resolved) == 2:
        a, b = resolved[label_a], resolved[label_b]
        # THE MATERIALISED HARNESS, NOT THE GENOME ID. Two genome ids can be one program: a
        # genome naming a parameter at its default value has its own id and materialises to a
        # manifest identical to the base's. An A/A comparison wearing two names is the one
        # thing an experiment may never be.
        if a["harness_id"] == b["harness_id"]:
            out.append(
                "%s and %s materialise to the same harness (%s); the two arms would be the "
                "same program, which is the one thing a comparison may never be"
                % (label_a, label_b, a["harness_id"][:12]))
        else:
            # DIFFERENT MANIFESTS ARE NOT YET DIFFERENT BEHAVIOUR. Asked of the component this
            # instrument measures, over the goals this comparison will actually send.
            differ, why = transport_versions_differ(a["manifest"], b["manifest"],
                                                    goals or _default_goals())
            if not differ and "both harnesses name" not in why:
                out.append("%s and %s: %s" % (label_a, label_b, why))

    if check_live:
        ok, changed = F.frozen_intact()
        if not ok:
            out.append("the frozen set is not intact (%s); a comparison whose judge changed "
                       "produces numbers nobody can trust" % ", ".join(changed[:3]))
        held = S.lock_held(lock_path)
        if held:
            out.append(
                "a campaign or comparison has held the lock since %s. Two running at once "
                "share one Edge, and the dependent variable counts every msedge process -- so "
                "each would be measuring the other. This refuses rather than queues, because "
                "a comparison that waits silently is one whose result arrives attributed to "
                "the wrong afternoon" % held)
        fired = S.halt_on_record(lock_path)
        if fired.get("fired"):
            out.append("a tripwire fired on an earlier run (%s) and has not been cleared"
                       % ", ".join(fired.get("fired", []))[:80])
        if free_mb is None:
            from relay.relay_fleet import avail_phys_mb
            free_mb = avail_phys_mb()
        if token_ok is None:
            # PROBE, DO NOT ASSERT. This defaulted to True, which is the same defect that was
            # fixed inside the evaluator earlier the same day: a precondition that states its
            # own conclusion. It cost a real run -- the token could not be captured, one
            # ordering was refused at preflight, and the comparison went ahead on the other.
            token_ok = _token_capturable()
        out.extend(RV.preflight(free_mb=free_mb, token_ok=token_ok))

    return out



def _token_capturable(cdp_url="http://127.0.0.1:9222", agent_url=None) -> bool:
    """Open a tab, capture, close. Costs one tab and answers the question for real.

    Without a token the socket arm silently becomes the tab arm and the two arms are the same
    program -- the one thing a comparison may never be, arriving through the door marked "the
    experiment ran fine".
    """
    try:
        from playwright.sync_api import sync_playwright

        from relay.socket_route import capture_via_tab, expires_in
        url = agent_url or os.environ.get("MCP_FLEET_AGENT_URL", "")
        if not url:
            for line in open(os.path.join(REPO, ".env"), encoding="utf-8", errors="ignore"):
                if line.startswith("MCP_FLEET_AGENT_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            token, _template = capture_via_tab(context, url)
            return bool(token) and expires_in(token) > 0
    except Exception as exc:
        print("[compare] token probe failed: %s: %s" % (type(exc).__name__, str(exc)[:160]),
              flush=True)
        return False


def enqueue(label_a: str, label_b: str, *, archive, note: str = "", branches_path=None,
            queue_path=None, now=None, goals=None) -> dict:
    """Record a request to compare two branches. Refuses on any reason found now.

    The refusals run here so the operator learns while they are looking at the screen, and
    again in `run` because the state can change in between.
    """
    reasons = refusals(label_a, label_b, archive=archive, branches_path=branches_path,
                       goals=goals)
    if reasons:
        raise CompareError("; ".join(reasons))

    a = resolve_operand(label_a, archive=archive, path=branches_path)
    b = resolve_operand(label_b, archive=archive, path=branches_path)
    visible, why = instrument_can_see(a["manifest"], b["manifest"])
    stamp = int(time.time() if now is None else now())
    row = {
        "id": "cmp-%d-%s-%s" % (stamp, label_a, label_b),
        "requested_at": stamp,
        "a": {"label": label_a, "genome_id": a["genome_id"], "harness_id": a["harness_id"]},
        "b": {"label": label_b, "genome_id": b["genome_id"], "harness_id": b["harness_id"]},
        "diff": M.diff(a["manifest"], b["manifest"]),
        "instrument_can_see": visible,
        "instrument_note": why,
        # Said at request time, not at result time. "About twenty minutes, and INCONCLUSIVE is
        # the most common outcome" is information the operator needs before waiting, and an
        # apology afterwards is not the same thing.
        "expectation": ("about 20 minutes; the decision threshold is 300 MB and the measured "
                        "noise floor is 130-180 MB, so INCONCLUSIVE is the usual result"),
        "note": str(note or ""),
        "state": "queued",
    }
    target = _p(queue_path, "MCP_SELFIMPROVE_COMPARE_QUEUE", QUEUE_PATH)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def pending(queue_path=None, results_path=None) -> list:
    """Queued requests that have no recorded attempt yet, oldest first."""
    target = _p(queue_path, "MCP_SELFIMPROVE_COMPARE_QUEUE", QUEUE_PATH)
    done = {r.get("request_id") for r in read_results(results_path)}
    rows = []
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("id") not in done:
                    rows.append(row)
    except Exception:
        return []
    return rows


def read_results(path=None) -> list:
    target = _p(path, "MCP_SELFIMPROVE_COMPARISONS", RESULTS_PATH)
    rows = []
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows


def attempts_for(label_a: str, label_b: str, path=None) -> list:
    """EVERY attempt on this pair, in order. Never the best one.

    A comparison that shows only its most favourable run, next to a button that starts another,
    is an instrument for manufacturing whichever answer the operator wants. The history is the
    guard, and it only guards if it is the thing on the screen.
    """
    pair = {label_a, label_b}
    gone = withdrawn_ids(path)
    rows = []
    for r in read_results(path):
        labels = {(r.get("a") or {}).get("label"), (r.get("b") or {}).get("label")}
        if labels != pair:
            continue
        if r.get("request_id") in gone and r.get("verdict") is not None:
            # Shown, not hidden. That it was believed is part of the record.
            r = dict(r, verdict="WITHDRAWN", original_verdict=r.get("verdict"))
        rows.append(r)
    return rows


def decide(order_1: dict, order_2: dict, *, min_gain_mb=None) -> dict:
    """The verdict from the two orderings. A sign that does not survive the swap is not a sign.

    Both arms run twice, once in each order, because arm position was measured to be worth more
    than the effect: the same comparison run control-first and candidate-first returned +449 MB
    and -666 MB on 2026-08-21. Requiring the sign to hold across the swap is what turns that
    from a confound into a control.
    """
    from relay.selfimprove import route_evaluator as RV
    floor = RV.MIN_MEMORY_GAIN_MB if min_gain_mb is None else min_gain_mb
    g1 = float(order_1.get("memory_gain_mb") or 0.0)
    g2 = float(order_2.get("memory_gain_mb") or 0.0)

    # AN ORDERING THAT DID NOT RUN IS NOT AN ORDERING THAT TIED.
    #
    # Found by running this for real: the first ordering was refused at preflight because no
    # socket token could be captured, so it carried no arms at all. `.get("done", 0)` turned
    # that into 0 == 0, the pair looked like a tie in one direction and a difference in the
    # other, and a WINNER was declared from a comparison where half of it never happened.
    # That is the ledger's INFRA_ABORT-is-not-a-verdict rule broken on the result side, which
    # is the same discipline this file's own threshold rests on.
    for name, order in (("first", order_1), ("second", order_2)):
        aborted = (order.get("infra") or {}).get("aborted")
        missing = not (order.get("control") and order.get("candidate"))
        if aborted or missing:
            reason = (order.get("infra") or {}).get("reason") or "the arms carried no result"
            return {"verdict": VERDICT_NONE, "aborted": True,
                    "why": "the %s ordering did not run (%s). A comparison with one ordering "
                           "is a comparison with an uncontrolled arm position, which is the "
                           "confound both orderings exist to remove -- so this is not a "
                           "finding about either branch." % (name, reason[:160]),
                    "gains": [order_1.get("memory_gain_mb"), order_2.get("memory_gain_mb")]}

    for order in (order_1, order_2):
        c = int((order.get("control") or {}).get("done", 0))
        d = int((order.get("candidate") or {}).get("done", 0))
        if c != d:
            worse = "B" if d < c else "A"
            return {"verdict": VERDICT_A if worse == "B" else VERDICT_B,
                    "why": "completion differed (%d vs %d): a harness that finishes fewer "
                           "goals loses whatever the memory says" % (c, d),
                    "gains": [g1, g2]}

    if (g1 > 0) != (g2 > 0):
        return {"verdict": VERDICT_NONE,
                "why": "the sign flipped when the arms were swapped (%.0f MB then %.0f MB); "
                       "arm position was worth more than the difference" % (g1, g2),
                "gains": [g1, g2]}
    if abs(g1) >= floor and abs(g2) >= floor:
        winner = VERDICT_A if g1 > 0 else VERDICT_B
        return {"verdict": winner,
                "why": "both orderings agreed and cleared the %.0f MB threshold (%.0f, %.0f)"
                       % (floor, g1, g2),
                "gains": [g1, g2]}
    return {"verdict": VERDICT_NONE,
            "why": "the difference held its sign but stayed under the %.0f MB this instrument "
                   "can distinguish from noise (%.0f, %.0f). That is not a finding that either "
                   "branch is worse" % (floor, g1, g2),
            "gains": [g1, g2]}


def record(request: dict, order_1: dict, order_2: dict, verdict: dict, *, path=None,
           now=None) -> dict:
    row = {
        "request_id": request.get("id"),
        "at": int(time.time() if now is None else now()),
        "a": request.get("a"), "b": request.get("b"),
        "diff": request.get("diff"),
        "instrument_can_see": request.get("instrument_can_see"),
        "verdict": verdict.get("verdict"),
        "why": verdict.get("why"),
        "orders": [order_1, order_2],
    }
    target = _p(path, "MCP_SELFIMPROVE_COMPARISONS", RESULTS_PATH)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def record_refusal(request: dict, reasons: list, *, path=None, now=None) -> dict:
    """A refusal is recorded against the request too.

    Otherwise a request that was refused at run time looks queued forever, and the operator
    re-enqueues it -- which is the same refusal again, twenty minutes later, silently.
    """
    row = {
        "request_id": request.get("id"),
        "at": int(time.time() if now is None else now()),
        "a": request.get("a"), "b": request.get("b"),
        "verdict": None,
        "refused": list(reasons),
        "why": "; ".join(reasons),
    }
    target = _p(path, "MCP_SELFIMPROVE_COMPARISONS", RESULTS_PATH)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def run(request: dict, *, archive, evaluator_for=None, branches_path=None, lock_path=None,
        results_path=None, goals=None, agent_url=None) -> dict:
    """Run one queued comparison under the campaign lock. Returns the recorded row.

    THE LOCK IS TAKEN AROUND THE WHOLE THING, INCLUDING THE SECOND REFUSAL CHECK.

    Checking and then taking would leave a window where two comparisons both saw a free lock.
    The dependent variable counts every msedge process on the machine, so two running at once
    do not merely queue badly -- each measures the other, and both results look ordinary.

    `evaluator_for(control_manifest, candidate_manifest, candidate_first)` is injected so this
    is testable without a browser. The default builds the same evaluator the loop uses on
    itself: same goals, same warm-up, same per-arm memory isolation, same frozen graders.
    """
    from relay.selfimprove import scheduler as S

    a = resolve_operand(request["a"]["label"], archive=archive, path=branches_path)
    b = resolve_operand(request["b"]["label"], archive=archive, path=branches_path)

    if not S.take_lock(lock_path, note="compare %s" % request.get("id")):
        reasons = ["could not take the campaign lock; another run holds it"]
        return record_refusal(request, reasons, path=results_path)
    try:
        # SECOND CHECK, INSIDE THE LOCK. The enqueue-time check agreed with a state that may
        # be twenty minutes old: the frozen set can change, a tripwire can fire.
        reasons = refusals(request["a"]["label"], request["b"]["label"], archive=archive,
                           branches_path=branches_path, lock_path=lock_path)
        # The lock is ours, so "the lock is held" is not a reason to refuse ourselves.
        reasons = [r for r in reasons if "held the lock" not in r]
        if reasons:
            return record_refusal(request, reasons, path=results_path)

        build = evaluator_for or _default_evaluator_for(goals=goals, agent_url=agent_url)
        # BOTH ORDERS. Arm position was measured to be worth more than the effect
        # (+449 MB then -666 MB on the same comparison, 2026-08-21), so a sign that does not
        # survive the swap is not a sign.
        order_1 = build(a["manifest"], b["manifest"], False)(b["manifest"], request["id"])
        order_2 = build(a["manifest"], b["manifest"], True)(b["manifest"], request["id"])
        for order, arms in ((order_1, "a,b"), (order_2, "b,a")):
            if isinstance(order, dict):
                order["arm_order_labels"] = arms
        verdict = decide(order_1, order_2)
        # `touch` on a base operand is a no-op: there is no ref to stamp, which is correct --
        # base has no freshness to go stale.
        BR.touch(request["a"]["label"], path=branches_path)
        BR.touch(request["b"]["label"], path=branches_path)
        return record(request, order_1, order_2, verdict, path=results_path)
    finally:
        S.release_lock(lock_path)


def _default_evaluator_for(*, goals=None, agent_url=None):
    """The evaluator the loop uses on itself, with branch A as the control arm.

    The transport switch is ON for both arms. A branch's own `transport` component is part of
    what is being compared, and pinning one arm to tabs would hand the transport difference to
    the genome.
    """
    from relay.selfimprove import scheduler as S

    def build(manifest_a, manifest_b, candidate_first):
        return S.route_evaluator_for(
            goals or _default_goals(), agent_url=agent_url, warmup=True,
            candidate_first=candidate_first,
            control_manifest=manifest_a, control_socket=True)

    return build


def _default_goals():
    from scripts.run_route_campaign import GOALS
    return GOALS


# ------------------------------------------------------------------------------------------
# CLI. The first entry point on purpose: a path an operator can run and read is a path that
# can be debugged, and a dashboard button that calls into an undebuggable path is worse than
# no button. Nothing here runs inside an HTTP request -- a comparison takes about twenty
# minutes, so the only honest shape is "write the request, run it, read the record".
# ------------------------------------------------------------------------------------------

def _archive():
    from relay.selfimprove import archive as A
    return A.Archive(os.path.join(REPO, ".fleet", "selfimprove", "archive.jsonl"))


def _cli(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="python -m relay.selfimprove.compare",
                                description="Compare two harness branches on this instrument.")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("branches", help="list branches and what is running now")
    ls.set_defaults(cmd="branches")

    mk = sub.add_parser("branch", help="name an archive row")
    mk.add_argument("label")
    mk.add_argument("genome_id")
    mk.add_argument("--note", default="")

    rm = sub.add_parser("unbranch", help="forget a name (the archive row is untouched)")
    rm.add_argument("label")

    q = sub.add_parser("enqueue", help="request a comparison")
    q.add_argument("a")
    q.add_argument("b")
    q.add_argument("--note", default="")

    sub.add_parser("pending", help="requests with no recorded attempt yet")

    rn = sub.add_parser("run", help="run the next queued comparison")
    rn.add_argument("--id", default="")

    hs = sub.add_parser("history", help="every attempt on a pair, oldest first")
    hs.add_argument("a")
    hs.add_argument("b")

    args = p.parse_args(argv)
    arc = _archive()

    if args.cmd == "branches":
        where = BR.describe_active(archive=arc)
        print("running now: %s%s" % (where["kind"],
                                     " (%s)" % where["label"] if where["label"] else ""))
        if where["kind"] == "unnamed":
            print("  ^ no branch points at this harness. Nobody named what is running.")
        for label, ref in sorted(BR.read().items()):
            last = ref.get("last_run_at")
            print("  %-20s %s  last run %s"
                  % (label, str(ref.get("genome_id"))[:12],
                     time.strftime("%Y-%m-%d", time.localtime(last)) if last else "never"))
        return 0

    if args.cmd == "branch":
        BR.create(args.label, args.genome_id, archive=arc, note=args.note)
        print("branch %s -> %s" % (args.label, args.genome_id))
        return 0

    if args.cmd == "unbranch":
        print("deleted" if BR.delete(args.label) else "no such branch")
        return 0

    if args.cmd == "enqueue":
        row = enqueue(args.a, args.b, archive=arc, note=args.note)
        print("queued %s" % row["id"])
        print("  diff: %s" % (row["diff"] or "(none)"))
        if not row["instrument_can_see"]:
            print("  WARNING: %s" % row["instrument_note"])
        print("  %s" % row["expectation"])
        return 0

    if args.cmd == "pending":
        rows = pending()
        print("%d pending" % len(rows))
        for row in rows:
            print("  %s  %s vs %s%s" % (row["id"], row["a"]["label"], row["b"]["label"],
                                        "" if row["instrument_can_see"]
                                        else "   [instrument cannot see this difference]"))
        return 0

    if args.cmd == "run":
        rows = pending()
        if args.id:
            rows = [r for r in rows if r.get("id") == args.id]
        if not rows:
            print("nothing to run")
            return 1
        out = run(rows[0], archive=arc)
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
        return 0

    if args.cmd == "history":
        rows = attempts_for(args.a, args.b)
        print("%d attempt(s) on %s vs %s -- ALL of them, not the best one"
              % (len(rows), args.a, args.b))
        for row in rows:
            print("  %s  %-13s %s"
                  % (time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("at", 0))),
                     row.get("verdict") or "REFUSED", (row.get("why") or "")[:90]))
        return 0
    return 1                                                    # pragma: no cover


def main(argv=None):                                            # pragma: no cover
    """The CLI with refusals printed as refusals.

    A refusal is the tool working, not the tool breaking, and a traceback says the opposite to
    the person reading it -- which is how a considered check gets mistaken for a bug and
    routed around.
    """
    try:
        return _cli(argv)
    except (CompareError, BR.BranchError) as exc:
        print("refused: %s" % exc)
        return 2



def withdraw(request_id: str, reason: str, *, path=None, now=None) -> dict:
    """Append a withdrawal of an earlier verdict. Nothing is rewritten.

    The record is append-only for the same reason the hypothesis ledger is: a verdict that can
    be edited once it is known to be wrong leaves no trace that it was ever believed, and the
    fact that it WAS believed is part of what a later reader needs. So a withdrawal is another
    row, and `attempts_for` returns it alongside the verdict it withdraws.
    """
    row = {
        "request_id": request_id,
        "at": int(time.time() if now is None else now()),
        "verdict": None,
        "withdraws": request_id,
        "why": reason,
    }
    target = _p(path, "MCP_SELFIMPROVE_COMPARISONS", RESULTS_PATH)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def withdrawn_ids(path=None) -> set:
    """Request ids whose verdict has been withdrawn."""
    return {r["withdraws"] for r in read_results(path) if r.get("withdraws")}


# THE ENTRY POINT LIVES AT THE VERY BOTTOM, AND THAT IS NOT STYLE.
#
# `withdraw` and `withdrawn_ids` were appended after this block. Under `python -m`, module
# execution reaches this line and calls main() while the rest of the file is still undefined,
# so `history` died with NameError -- on a module whose tests were all green, because a test
# IMPORTS the module and an import runs to the end before anything is called. Running it is
# what found this, and the entry point stays last so a later append cannot recreate it.
if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
