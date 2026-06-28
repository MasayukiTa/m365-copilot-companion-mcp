"""General-use quality validate driver -- the closed loop around OUTPUT_DISCIPLINE.

This is the QUALITY analogue of loop.py. loop.py validates a *benchmark* scaffold change by
solving/grading SWE-bench under guards.py; this validates a *discipline* change (the OUTPUT_DISCIPLINE
clamp in copilot_autopilot_relay.py) for the GENERAL user, where there is no benchmark -- only the
fleet (M365/Copilot) producing real answers and a judge deciding whether each answer leaked an
unsolicited advisor/lecturer/ego persona.

  measure   : usage.persona_leak_rate crosses a threshold  -> check_degradation() trips the loop
  propose   : a candidate discipline text (human- or meta-agent-authored)
  validate  : run a fixed persona-eliciting PROBE_SUITE through TWO arms --
                baseline arm (discipline_override=None, the shipped default) and
                proposed arm (discipline_override=<candidate>) --
              judge each output leak/clean, then gate "did proposed raise the CLEAN rate
              significantly?" via guards.significance_gate (clean == resolved-equivalent).
  keep/revert/enlarge : the gate's verdict, surfaced as a recommendation. NEVER auto-commits;
              at most it stages the proposed text and writes a report for a human to review.

DISCIPLINE (mirrors loop.py):
  - The CORE -- run_arm / judge_arm / gate_arms / validate's assembly -- is fully injectable
    (runner_fn, judge_fn) so it is 100% offline unit-testable and DETERMINISTIC. The only
    non-deterministic part is the report timestamp, which lives in the live driver, not the core.
  - The real fleet / bridge are touched ONLY under --live. The default runner/judge are wired in
    main() and only when --live is passed.
  - Defensive: a missing usage module, a flaky judge, an unreadable file -> degrade, never raise.
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
RELAY = os.path.join(REPO, "relay", "copilot_autopilot_relay.py")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.selfimprove import guards as G
from relay.selfimprove import quality as Q


# --------------------------------------------------------------------------------------------------
# Probe suite -- fixed, domain-general, persona-eliciting prompts.
# --------------------------------------------------------------------------------------------------
# These are the prompts most likely to tempt the agent into advisor/lecturer/ego mode: comparisons,
# "explain X for beginners", "what should I learn first" -- exactly where a leaked persona surfaces.
# Kept FIXED and ordered so an A/B over them is a paired comparison on the SAME probe set (the
# significance gate pairs by id). domain-general: no repo/instance/file names (so guards.overfit_lint
# stays clean if this list is ever fed to the linter).
PROBE_SUITE = [
    {"id": "probe_merge",
     "prompt": "git の merge と rebase の違いを教えてください。どちらをいつ使うべきですか。"},
    {"id": "probe_async_beginner",
     "prompt": "プログラミング初心者向けに、非同期処理(async)とは何かを説明してください。"},
    {"id": "probe_code_review",
     "prompt": "コードレビューで見るべき要点を挙げてください。"},
    {"id": "probe_learn_first",
     "prompt": "これからプログラミングを始める人が最初に学ぶべきことは何ですか。"},
    {"id": "probe_rest_graphql",
     "prompt": "REST と GraphQL の違いと、それぞれの向き不向きを教えてください。"},
    {"id": "probe_git_undo",
     "prompt": "直前のコミットを取り消したいときの方法を教えてください。"},
    {"id": "probe_test_strategy",
     "prompt": "小さな個人プロジェクトに、どの程度テストを書けばよいですか。"},
    {"id": "probe_sql_index",
     "prompt": "データベースのインデックスは何のために張るのですか。簡潔に説明してください。"},
]


def probe_ids(probe_suite=None):
    """The ordered list of probe ids (the paired comparison's instance set)."""
    suite = probe_suite if probe_suite is not None else PROBE_SUITE
    return [p["id"] for p in suite if isinstance(p, dict) and p.get("id")]


# --------------------------------------------------------------------------------------------------
# Core: run one arm, judge an arm, gate two arms. All injectable -> offline-testable.
# --------------------------------------------------------------------------------------------------

def run_arm(arm_name, discipline_override, probe_suite, runner_fn):
    """Run every probe through ONE arm and return {probe_id: output_text}.

    `runner_fn(probe_suite, discipline_override) -> {probe_id: text}` is injected: tests pass a
    deterministic fake; production passes `_fleet_runner_fn` (which shells out to the relay with
    SI_DISCIPLINE_OVERRIDE set to discipline_override, or unset for the baseline arm).
    `discipline_override=None` means the baseline (shipped default) arm.
    """
    outputs = runner_fn(probe_suite, discipline_override)
    if not isinstance(outputs, dict):
        return {}
    # keep only probes we asked about, coerce values to str (defensive against a flaky runner)
    wanted = set(probe_ids(probe_suite))
    clean = {}
    for pid, txt in outputs.items():
        if pid in wanted:
            clean[pid] = txt if isinstance(txt, str) else ("" if txt is None else str(txt))
    return clean


def judge_arm(arm_outputs, judge_fn):
    """Judge every output in an arm -> {probe_id: is_leak(bool)}.

    `judge_fn(text) -> bool` is injected. A None judge_fn falls back to the offline heuristic
    `quality.score_text(text)["persona_leak"]`, so the core still works with no LLM. A judge that
    raises on a given probe degrades to the heuristic for that probe (never aborts the arm).
    """
    verdicts = {}
    for pid, text in (arm_outputs or {}).items():
        if judge_fn is None:
            verdicts[pid] = bool(Q.score_text(text or "").get("persona_leak"))
            continue
        try:
            verdicts[pid] = bool(judge_fn(text or ""))
        except Exception:
            # a flaky judge must not sink the whole arm; fall back to the deterministic heuristic
            verdicts[pid] = bool(Q.score_text(text or "").get("persona_leak"))
    return verdicts


def gate_arms(baseline_leaks, proposed_leaks, ids, min_n=None):
    """Gate "did the proposed arm raise the CLEAN rate?" via guards.significance_gate.

    clean == NOT leak. We treat a CLEAN probe as "resolved" so a discipline that turns leaks into
    clean answers reads as a positive A/B. The gate pairs by id over the SAME probe set, so a probe
    that is clean only under proposed is a "helped" pair (b) and one clean only under baseline is a
    "hurt" pair (c) -- exactly the paired structure significance_gate expects.

    min_n defaults to len(ids): a probe suite is small by design, so we set the powered floor to the
    suite size and let the gate return verdict="underpowered" when even that is too small -- the loop
    then RECOMMENDS only (no auto-commit), per spec. Returns the gate dict plus a human-readable
    summary of the clean counts.
    """
    ids = list(ids)
    n = len(ids)
    min_n = n if min_n is None else min_n

    baseline_leaks = baseline_leaks or {}
    proposed_leaks = proposed_leaks or {}
    # clean id-sets = probes that did NOT leak in each arm (default to "leaked" if a probe is missing,
    # the conservative reading: an output we could not judge does not count as a clean win).
    off_clean = [pid for pid in ids if not baseline_leaks.get(pid, True)]
    on_clean = [pid for pid in ids if not proposed_leaks.get(pid, True)]

    gate = G.significance_gate(on_resolved=on_clean, off_resolved=off_clean, instances=ids,
                               min_n=min_n)
    gate["baseline_clean"] = len(off_clean)
    gate["proposed_clean"] = len(on_clean)
    gate["probe_n"] = n
    gate["summary"] = ("baseline clean %d/%d, proposed clean %d/%d -> verdict=%s keep=%s"
                       % (len(off_clean), n, len(on_clean), n, gate["verdict"], gate["keep"]))
    return gate


def _recommendation(gate):
    """Map a gate verdict to a one-word recommendation for the operator.

    keep                       -> "keep"   (proposed significantly raised the clean rate; adopt)
    underpowered / suggestive  -> "enlarge"(direction is fine but N can't decide -> add probes /
                                            re-run; do NOT commit, do NOT revert a maybe-good change)
    everything else            -> "revert" (non-positive / negligible -> keep the shipped default)

    Both "underpowered" (n < min_n) and "suggestive" (powered but p>=alpha, positive direction) mean
    "we need more evidence", which is the spec's enlarge-N path -- distinct from a real regression.
    """
    if gate.get("keep"):
        return "keep"
    if gate.get("verdict") in ("underpowered", "suggestive"):
        return "enlarge"
    return "revert"


def validate(proposed_discipline, runner_fn, judge_fn, probe_suite=None, min_n=None):
    """Run baseline vs proposed arms over the probe suite, judge, gate, and assemble a report dict.

    baseline arm uses discipline_override=None (shipped default); proposed arm uses
    proposed_discipline. The whole thing is deterministic given deterministic runner_fn/judge_fn --
    no timestamps or randomness here (the live driver adds the report ts when it writes the file).

    Returns: {probe_n, baseline_clean, proposed_clean, gate, recommendation, proposed_excerpt}.
    """
    suite = probe_suite if probe_suite is not None else PROBE_SUITE
    ids = probe_ids(suite)

    baseline_out = run_arm("baseline", None, suite, runner_fn)
    proposed_out = run_arm("proposed", proposed_discipline, suite, runner_fn)

    baseline_leaks = judge_arm(baseline_out, judge_fn)
    proposed_leaks = judge_arm(proposed_out, judge_fn)

    gate = gate_arms(baseline_leaks, proposed_leaks, ids, min_n=min_n)

    excerpt = (proposed_discipline or "")
    excerpt = " ".join(excerpt.split())[:200]

    return {
        "probe_n": gate["probe_n"],
        "baseline_clean": gate["baseline_clean"],
        "proposed_clean": gate["proposed_clean"],
        "gate": gate,
        "recommendation": _recommendation(gate),
        "proposed_excerpt": excerpt,
    }


# --------------------------------------------------------------------------------------------------
# Degradation trigger: read usage.persona_leak_rate, trip when it exceeds the threshold.
# --------------------------------------------------------------------------------------------------

def check_degradation(threshold, usage_fn=None):
    """Decide whether the quality loop should fire, by reading the live persona-leak rate.

    `usage_fn() -> dict` is injected (default: relay.selfimprove.usage.usage_section). We read
    `persona_leak_rate`; tripped == it is not None AND strictly greater than `threshold`. A None rate
    (no scorable runs yet) NEVER trips -- we do not act on the absence of a signal. Any failure to
    load/read degrades to tripped=False with leak_rate=None (defensive: a broken usage module must
    not spuriously kick off an A/B).
    """
    leak_rate = None
    try:
        if usage_fn is None:
            from relay.selfimprove.usage import usage_section as usage_fn  # soft dependency
        section = usage_fn()
        if isinstance(section, dict):
            leak_rate = section.get("persona_leak_rate")
    except Exception:
        leak_rate = None

    tripped = (leak_rate is not None) and (leak_rate > threshold)
    return {"tripped": bool(tripped), "leak_rate": leak_rate, "threshold": threshold}


# --------------------------------------------------------------------------------------------------
# Live wiring (only reached under --live). NOT exercised by the offline tests.
# --------------------------------------------------------------------------------------------------

def _fleet_runner_fn(probe_suite, discipline_override):
    """Production runner: drive the relay for each probe with the given discipline override.

    Shells out to copilot_autopilot_relay.py once per probe, setting SI_DISCIPLINE_OVERRIDE to
    `discipline_override` (or leaving it unset for the baseline arm), and collects each probe's final
    answer. The exact relay invocation depends on the live conversation wiring; this is intentionally
    a thin, replaceable shell-out so the core stays pure. Returns {probe_id: text}.

    NOTE: only called under --live. It is defensive (a failed probe yields "" so the arm still
    completes and the judge counts it as a non-clean, conservative miss).
    """
    import subprocess
    out = {}
    for p in probe_suite:
        pid = p.get("id")
        prompt = p.get("prompt", "")
        if not pid:
            continue
        env = dict(os.environ)
        env.pop("SI_DISCIPLINE_OVERRIDE", None)
        env.pop("SI_DISCIPLINE_VARIANT", None)
        if discipline_override:
            env["SI_DISCIPLINE_OVERRIDE"] = discipline_override
        try:
            # The relay's exact single-shot flag set is environment-specific; we pass the probe as the
            # goal and capture stdout. Wired here as the integration seam, kept thin on purpose.
            r = subprocess.run([VENVPY, RELAY, "--goal", prompt, "--max-turns", "1"],
                               cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
            out[pid] = (r.stdout or "").strip()
        except Exception:
            out[pid] = ""
    return out


def _bridge_judge_fn(text):
    """Production judge: ask the bridge/Copilot whether `text` leaked a persona.

    Builds the prompt with quality.judge_prompt, sends it through the bridge (the integration seam),
    and parses the reply with quality.parse_judge_verdict. Only called under --live. If the bridge is
    unreachable this raises, and judge_arm falls back to the offline heuristic for that probe.
    """
    prompt = Q.judge_prompt(text or "")
    reply = _bridge_send(prompt)  # integration seam; raises if the bridge is down
    return Q.parse_judge_verdict(reply)


def _bridge_send(prompt):
    """Send a judge prompt to the bridge and return the reply text. LIVE-only integration seam.

    Left unimplemented here on purpose: it is wired to the running bridge in the live environment.
    Raising NotImplementedError (caught by judge_arm -> heuristic fallback) is the safe default so a
    --live run without a configured bridge silently degrades to the heuristic rather than crashing.
    """
    raise NotImplementedError("bridge send is wired in the live environment only")


# --------------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------------

def _read_proposed(path):
    """Read the proposed discipline text from a file (utf-8, stripped). Defensive -> '' on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_report(report):
    """Write the validate report to .fleet/swe/selfimprove_quality_report_<ts>.json. Returns path."""
    os.makedirs(SWEDIR, exist_ok=True)
    out = os.path.join(SWEDIR, "selfimprove_quality_report_%s.json" % time.strftime("%m%d%H%M"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return out


def _stage_proposed(proposed_text):
    """Stage the proposed discipline text for a human to review/adopt. NEVER auto-commits.

    Writes it to a staging file the operator can point SI_DISCIPLINE_VARIANT at to try it live, or
    paste into _DEFAULT_DISCIPLINE if they decide to adopt. Returns the staging path.
    """
    os.makedirs(SWEDIR, exist_ok=True)
    out = os.path.join(SWEDIR, "selfimprove_quality_proposed.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(proposed_text or "")
    return out


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def main():
    ap = argparse.ArgumentParser(description="General-use quality (persona-leak) validate driver.")
    ap.add_argument("--proposed-file", default="",
                    help="path to the candidate discipline text (required unless --dry-run)")
    ap.add_argument("--threshold", type=float, default=0.15,
                    help="persona_leak_rate above which the loop is considered triggered")
    ap.add_argument("--min-n", type=int, default=0,
                    help="powered-floor for the gate (0 = use probe-suite size)")
    ap.add_argument("--live", action="store_true",
                    help="actually drive the fleet/bridge (default: refuse, core is offline-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the probe suite + plan and exit (no arms run)")
    a = ap.parse_args()

    min_n = a.min_n if a.min_n > 0 else None

    if a.dry_run:
        plan = {
            "probe_n": len(PROBE_SUITE),
            "probes": [{"id": p["id"], "prompt": p["prompt"]} for p in PROBE_SUITE],
            "threshold": a.threshold,
            "min_n": min_n if min_n is not None else len(PROBE_SUITE),
            "live": a.live,
            "note": "dry-run: no arms run, no fleet/bridge touched, nothing committed",
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    # Trigger check (informational here -- the operator chose to run validate regardless).
    deg = check_degradation(a.threshold)
    log("degradation check: " + json.dumps(deg, ensure_ascii=False))

    if not a.proposed_file:
        log("no --proposed-file given; nothing to validate. (use --dry-run to inspect the plan)")
        return
    proposed = _read_proposed(a.proposed_file)
    if not proposed:
        log("proposed discipline text is empty/unreadable: %s" % a.proposed_file)
        return

    if not a.live:
        log("NOT --live: the core needs a real runner/judge to produce outputs. Refusing to invent "
            "results offline. Re-run with --live to drive the fleet, or import validate() with "
            "injected runner_fn/judge_fn for an offline/unit context.")
        return

    # LIVE path: wire the real runner + judge. Still NEVER auto-commits.
    log("LIVE validate: baseline vs proposed over %d probes ..." % len(PROBE_SUITE))
    report = validate(proposed, runner_fn=_fleet_runner_fn, judge_fn=_bridge_judge_fn, min_n=min_n)
    report_path = _write_report(report)
    staged = _stage_proposed(proposed)
    log("recommendation=%s | %s" % (report["recommendation"], report["gate"]["summary"]))
    log("report: %s" % report_path)
    log("proposed staged (NOT committed): %s" % staged)


if __name__ == "__main__":
    main()
