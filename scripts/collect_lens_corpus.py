"""Build the all-lenses corpus section 18 needs, with ground truth from the grader.

WHY THE GRADER AND NOT THE ACCEPTANCE CHECKS

The fleet already runs acceptance checks beside the review panel, which makes them the
convenient source. They are also the wrong one, and not merely because "the task passed" and
"this answer should have been refuted" are different predicates. The checks are ENDOGENOUS to
the pipeline being measured: the solver iterates against them, so a candidate that reaches
the panel has already been shaped to pass. Under `bad := checks failed` the bad rows are
nearly absent by construction, and the panel's whole marginal value -- passes the checks and
is wrong anyway -- is defined out of existence. The measured-optimal policy then becomes RUN
ZERO LENSES: no cost, no latency, no false accepts, by definition.

CompanionBench graders are independent of that loop. They inspect the final state of a
workdir, nobody optimises against them, and they do not gate what enters the corpus. The cost
is a distribution commitment, stated rather than hidden: the frontier this produces describes
policy behaviour on bench tasks, and the adaptive arm's memory is warmed on bench features.

GROUND TRUTH KEEPS THE GRADER'S SHAPE

Not one boolean. The panel makes three different claims -- wrong, fragile, unsafe -- and
`GradeResult.security_coverage` exists in this repository precisely because a boolean pass
was being read as "it did not happen" when the evidence supported only "we did not see it".
Collapsing the grade into `bad: bool` repeats that, and repeats it where it does most damage:
with `bad := not functional_success`, every CORRECT security refutation scores as a false
reject, so the frontier punishes policies that spend on the security lens and the adaptive
arm learns to stop running it.

So `security` maps straight from the grader's own coverage vocabulary, including the
unevaluable state, and `simulate` excludes those rows from the security denominator rather
than counting them as passes.

WHAT THIS SCRIPT DOES NOT DO

It does not judge. It records what the grader said and what each lens said, one row per
candidate, and leaves every decision about how to read them to `reviewer_allocation`.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.companionbench import episode as EP            # noqa: E402
from bench.companionbench.calibration import CALIBRATION_KEY as CAL_KEY  # noqa: E402
from relay.selfimprove import reviewer_allocation as A    # noqa: E402

#: How long to let one lens think before recording UNCLEAR. Generous: a lens that timed out
#: is recorded as having produced no evidence, which is what UNCLEAR means, but a timeout that
#: is really impatience would fill the corpus with them.
LENS_TIMEOUT_S = 420.0

#: Written explicitly so the file is LF on every platform, and so no escape has to survive
#: the shell that generated this module.
NL = chr(10)


class NotEnoughRoom(RuntimeError):
    """The box cannot open a reviewer tab, so no lens can produce a verdict."""


def require_room_for_lenses():
    """Refuse to collect when the reviewer cannot get a tab. Measured, not assumed.

    `RefuterSession` defers opening its side page until `ram_room_for_tab()` clears a 2000 MB
    floor, and after `timeout_s` of never clearing it the session finishes ("UNCLEAR", "") --
    the SAME value a reviewer returns when it looked and was unsure. At runtime that is the
    right call: UNCLEAR means do not block. In a corpus it is a lie, and a cheap one to tell
    at scale: the first live run here spent twenty-two minutes producing one row whose three
    verdicts were all UNCLEAR, because a load generator left over from an unrelated experiment
    had the box at 954 MB free. Every policy scores identically on such a corpus, and the
    conclusion reads "which lenses you run does not matter".

    The floor is not lowered to get past this. It exists because side pages crowding a low-RAM
    box is what wedges the sweep and trips the watchdog.
    """
    from relay.relay_fleet import avail_phys_mb, ram_room_for_tab
    if not ram_room_for_tab():
        raise NotEnoughRoom(
            "%.0f MB physical RAM free, and a reviewer tab needs 2000 MB. Every lens would "
            "time out and record UNCLEAR, which is indistinguishable from a reviewer that "
            "looked and was unsure -- so the run would produce a corpus on which every policy "
            "scores the same. Free memory and start again; do not lower the floor."
            % avail_phys_mb())


def truth_from_grade(grade) -> dict:
    """Ground truth in the grader's shape. Absent evidence stays absent.

    NOT `grade.success`, which is the conjunction of all three scores. Folding security into
    the functional axis double-counts every violation -- it lands in `functional=False` AND in
    `security=violation` -- and then a policy that misses only security failures shows up as
    missing functional ones too, which is precisely the per-class blindness the split exists
    to remove.

    `side_effect_score` joins the functional axis rather than the security one: touching what
    the task did not ask for is the task done wrong, and it is what a correctness or edge lens
    is looking at. The security axis stays exactly the security grader's own claim.

    `security_coverage` is carried through rather than flattened: COVERAGE_PARTIAL means the
    grader could not see the channels a violation would use, and reading that as a pass is
    the exact substitution the class was written to refuse.
    """
    coverage = getattr(grade, "security_coverage", None)
    score = float(getattr(grade, "security_score", 1.0))
    if coverage == EP.COVERAGE_VIOLATION or score < 1.0:
        # A LOW SCORE IS A VIOLATION WHATEVER THE COVERAGE SAYS. The two are set
        # independently and a grader may pass an explicit coverage alongside a failing score;
        # reading the label and ignoring the number would record a seen violation as a pass.
        security = A.SECURITY_VIOLATION
    elif coverage == EP.COVERAGE_COMPLETE:
        security = A.SECURITY_PASS
    else:
        security = A.SECURITY_UNEVALUABLE
    functional = (float(getattr(grade, "functional_score", 0.0)) >= 1.0
                  and float(getattr(grade, "side_effect_score", 1.0)) >= 1.0)
    return {"functional": functional, "security": security}


def run_lenses(context, agent_url, goal, reply, lenses, *, timeout_s=LENS_TIMEOUT_S) -> dict:
    """Every lens against one candidate. Returns {lens: {"verdict": ..., "reason": ...}}.

    ALL of them, which is the point: a policy that runs two of three cannot be scored without
    knowing what the third would have said, and a corpus that only records the chosen ones
    measures "did the lenses that ran agree with each other".

    THE REASON IS KEPT, and the first version of this function threw it away. `RefuterSession`
    finishes with ("UNCLEAR", "") for every infrastructure failure it meets -- no composer
    within forty seconds, a navigation error, a page that never answers -- because at runtime
    UNCLEAR means "do not block", which is the safe reading there. Here it is not a reading at
    all: it records a browser that never loaded as a reviewer's considered opinion. The first
    live run came back UNCLEAR on all three lenses and, without the reason, that is
    indistinguishable from three lenses that looked and were unsure.
    """
    from relay.refuter import RefuterSession

    out = {}
    for lens in lenses:
        started = time.time()
        session = RefuterSession(context, agent_url, goal, reply, lens=lens,
                                 timeout_s=timeout_s).start()
        deadline = started + timeout_s + 60
        verdict, reason = None, "the poll loop gave up before the session finished"
        while time.time() < deadline:
            got = session.poll()
            if got is not None:
                verdict, reason = got
                break
            time.sleep(1.0)
        # A LENS THAT NEVER ANSWERED PRODUCED NO EVIDENCE, which is what UNCLEAR means. It is
        # not "the lens looked and found nothing" -- recording it as UPHELD would credit the
        # policy that ran it with a clean result it never obtained.
        # ELAPSED IS RECORDED SO THE COST AXIS CAN BE MEASURED RATHER THAN ASSUMED. A
        # frontier trades false accepts against review calls, and treating every lens as
        # equally expensive is a modelling choice that quietly favours whichever lens is in
        # fact the slow one. Timed-out lenses are excluded downstream: their elapsed time is
        # the timeout, not the lens.
        out[lens] = {"verdict": verdict if verdict in A.VERDICTS else A.UNCLEAR,
                     "reason": reason or "",
                     "elapsed_s": round(time.time() - started, 1)}
    return out


def verdicts_only(detail) -> dict:
    return {lens: d["verdict"] for lens, d in detail.items()}


def all_unclear(detail) -> bool:
    return all(d["verdict"] == A.UNCLEAR for d in detail.values())


def collect(*, cdp_url, agent_url, episodes, agent, out_path, lenses=None,
            calibrate=True) -> dict:
    """Run each episode, grade it, then run every lens over its reply. Append-only.

    `calibrate` seeds known-bad SECURITY candidates alongside the real run. It defaults on
    because without them the security denominator on these pools is empty -- measured, not
    assumed: the recorded 22-episode baseline has nine bad candidates, all functional, and
    all three security episodes unevaluable. Every row is flagged, so the frontier can be
    read with and without them.
    """
    from playwright.sync_api import sync_playwright

    from relay.refuter import PANEL_LENSES

    require_room_for_lenses()

    # THE EPISODE CONTRACT DIRECTLY, NOT `run_episode`. That returns a graded row and NOT the
    # reply -- so a collector built on it skips every candidate for "no reply recorded" and
    # can only ever produce an empty corpus, which reads exactly like a clean run. The reply
    # is what the lenses review, so it has to be in hand here.
    #
    # The cost, stated: this loop does not reproduce the runner's delivery evidence or its
    # trace handling. It needs (prompt, reply, grade) and nothing else, and adding the reply
    # to `run_episode` would mean re-blessing the frozen judge for a convenience.
    lenses = list(lenses or PANEL_LENSES)
    rows, skipped = [], []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        for episode in episodes:
            workdir = tempfile.mkdtemp(prefix="lenscorpus_")
            try:
                prompt = episode.setup(workdir)
                reply = agent(prompt, workdir) or ""
                grade = episode.grade_final_state(workdir, reply=reply)
            except Exception as exc:
                skipped.append({"candidate_id": episode.episode_id,
                                "why": "%s: %s" % (type(exc).__name__, exc)})
                continue
            finally:
                try:
                    episode.cleanup(workdir)
                except Exception:
                    pass
                shutil.rmtree(workdir, ignore_errors=True)

            if getattr(grade, "infra_failure", False):
                # AN EPISODE THE ENVIRONMENT COULD NOT RUN IS NOT A CANDIDATE. Recording it
                # with a failed grade would hand every policy a bad row nothing could have
                # caught, which depresses the whole frontier for a reason unrelated to review.
                skipped.append({"candidate_id": episode.episode_id, "why": "infra_failure"})
                continue
            if not reply.strip():
                skipped.append({"candidate_id": episode.episode_id, "why": "empty reply"})
                continue

            detail = run_lenses(context, agent_url, prompt, reply, lenses)
            verdicts = verdicts_only(detail)
            if all_unclear(detail):
                # EVERY LENS SILENT IS A HARNESS SYMPTOM BEFORE IT IS DATA. Recorded as a skip
                # with the reasons attached, because a corpus of all-UNCLEAR rows makes every
                # policy score identically and reads as "the choice of lenses does not matter".
                skipped.append({"candidate_id": episode.episode_id,
                                "why": "no lens produced a verdict: %s"
                                       % {ln: d["reason"] for ln, d in detail.items()}})
                print("  %-28s SKIPPED: no lens answered -- %s"
                      % (episode.episode_id,
                         {ln: (d["reason"] or "(no reason given)")[:60]
                          for ln, d in detail.items()}), flush=True)
                continue
            rows.append({
                "candidate_id": episode.episode_id,
                "bad": truth_from_grade(grade),
                "verdicts": verdicts,
                "lens_detail": detail,
                "features": {"kind": episode.category or "unknown"},
                CAL_KEY: False,
            })
            with io.open(out_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rows[-1], ensure_ascii=False, sort_keys=True) + "\n")
            print("  %-28s functional=%s security=%s  %s"
                  % (episode.episode_id, rows[-1]["bad"]["functional"],
                     rows[-1]["bad"]["security"],
                     " ".join("%s=%s" % (l, v[:1]) for l, v in verdicts.items())), flush=True)
        if calibrate:
            from bench.companionbench.calibration import known_bad_rows
            seeds = [e for e in episodes if getattr(e, "category", "") == "security"]
            for episode, style, prompt, reply, grade in known_bad_rows(seeds):
                detail = run_lenses(context, agent_url, prompt, reply, lenses)
                verdicts = verdicts_only(detail)
                if all_unclear(detail):
                    skipped.append({"candidate_id": "%s#%s" % (episode.episode_id, style),
                                    "why": "no lens produced a verdict"})
                    continue
                rows.append({
                    "candidate_id": "%s#%s" % (episode.episode_id, style),
                    "bad": truth_from_grade(grade),
                    "verdicts": verdicts,
                    "features": {"kind": episode.category or "unknown"},
                    CAL_KEY: True,
                    "reply_style": style,
                    "lens_detail": detail,
                })
                with io.open(out_path, "a", encoding="utf-8", newline=NL) as fh:
                    fh.write(json.dumps(rows[-1], ensure_ascii=False, sort_keys=True) + NL)
                print("  %-28s [calibration/%s] security=%s  %s"
                      % (episode.episode_id, style, rows[-1]["bad"]["security"],
                         " ".join("%s=%s" % (ln, v[:1]) for ln, v in verdicts.items())),
                      flush=True)
    return {"rows": rows, "skipped": skipped, "lenses": lenses}


def load_corpus(path) -> list:
    rows = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-url", default=os.environ.get("MCP_FLEET_CDP_URL",
                                                        "http://127.0.0.1:9222"))
    ap.add_argument("--agent-url", default=os.environ.get("MCP_FLEET_AGENT_URL", ""))
    ap.add_argument("--out", default=str(ROOT / ".fleet" / "lens_corpus.jsonl"))
    ap.add_argument("--pool", default="evolution")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-calibration", action="store_true",
                    help="omit the known-bad security candidates. The security axis then has "
                         "an empty denominator on these pools and cannot discriminate.")
    args = ap.parse_args()

    if not args.agent_url:
        print("set MCP_FLEET_AGENT_URL (or pass --agent-url): the lenses need a chat to "
              "review in, and pointing them at the wrong agent produces a scoping question "
              "rather than a verdict")
        return 2

    from bench.companionbench.baseline import build_agent
    from bench.companionbench.pools import REGISTRY

    episodes = list(REGISTRY.get(args.pool))
    if args.limit:
        episodes = episodes[:args.limit]
    print("collecting %d candidate(s) from the %s pool, every lens on each"
          % (len(episodes), args.pool))
    # WARM THE MEMORY WHILE COLLECTING. With an empty store the adaptive policy returns the
    # panel's own order, which IS the fixed policy -- so a frontier drawn against a cold
    # memory puts one policy on it twice under two names.
    os.environ.setdefault("MCP_REFUTER_MEMORY_RECORD", "1")

    try:
        require_room_for_lenses()
    except NotEnoughRoom as exc:
        print("REFUSED: %s" % exc)
        return 3
    got = collect(cdp_url=args.cdp_url, agent_url=args.agent_url, episodes=episodes,
                  agent=build_agent("fleet"), out_path=args.out,
                  calibrate=not args.no_calibration)
    print()
    print("recorded %d row(s) to %s" % (len(got["rows"]), args.out))
    if got["skipped"]:
        print("skipped %d: %s" % (len(got["skipped"]),
                                  ", ".join("%s (%s)" % (s["candidate_id"], s["why"])
                                            for s in got["skipped"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
