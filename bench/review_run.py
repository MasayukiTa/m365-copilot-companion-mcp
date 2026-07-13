"""Orchestrate a full code/security review run on the M365 Copilot fleet.

Ties together the pure goal-builder (bench/review_build_goals.py) and the pure aggregator
(bench/review_aggregate.py) with the one impure step in between: launching
relay.fleet_runner and waiting for it to finish.

  python bench/review_run.py --kind review --dry-run          # plan only, no fleet launch
  python bench/review_run.py --kind review                    # full repo, launches the fleet
  python bench/review_run.py --kind security --mode diff       # review only the working diff
  python bench/review_run.py --kind review --no-refute         # skip the refuter pass
  python bench/review_run.py --kind review --refute-panel      # 3-lens refuter panel/finding
  python bench/review_run.py --kind review --behavioral        # + behavioral-verify CONFIRMED
  python bench/review_run.py --kind review --behavioral --behavioral-severity high
  python bench/review_run.py --kind review --loop              # repeat rounds until dry
  python bench/review_run.py --kind review --loop --max-rounds 5 --dry-rounds 1
  python bench/review_run.py --kind review --completeness      # + one completeness-critic pass

FLOW:
  1. enumerate_files -> optional --target-path filter -> group_files -> fan out one
     build_dimension_goal per (dimension, file-group) pair, across every dimension
     applicable to --kind (or the --dimensions subset) -> write_goals_jsonl to
     <out-dir>/goals_<stamp>.jsonl.
  2. --dry-run prints the plan (goal count, per-goal dimension + files, the exact fleet
     argv, output paths) and returns WITHOUT launching anything.
  3. Otherwise: run_fleet() launches relay.fleet_runner and blocks until it exits, then
     aggregate()/render_markdown()/render_json() turn .fleet/status.json into
     <out-dir>/review_report_<stamp>.{md,json}.
  4. By DEFAULT (skip with --no-refute): every finding from step 3 is handed to the EXISTING
     adversarial refuter (relay/refuter.py, via refute_findings()) -- an independent skeptic
     that tries to REFUTE the finding, run as its own fleet pass. REFUTED findings get
     verify_verdict="false_positive" (bench/review_fix.py's filter_findings already drops
     these); UPHELD -> "confirmed"; unparseable/UNCLEAR -> "unclear". --refute-panel widens
     this to relay.refuter.PANEL_LENSES (3 independent lenses per finding, majority vote)
     instead of one lens per finding. This SUPERSEDES the old bespoke VERIFY_RUBRIC 2nd-pass
     verifier (removed) -- there is now exactly one verification mechanism. --verify is kept
     as a deprecated alias that also enables panel mode, for old callers/scripts.
  5. OPT-IN via --behavioral (default OFF -- this executes real code, unlike the
     pure-reasoning refuter): every finding whose verify_verdict == "confirmed" is handed to
     behavioral_verify(), its own fleet pass that asks a fresh worker to actually REPRODUCE the
     finding with a minimal, READ-ONLY run_python/shell_exec repro (build_behavioral_verify_goal
     in bench/review_build_goals.py) and report BEHAVIOR_VERDICT (reproduced/not_reproduced/
     inconclusive) + BEHAVIOR_EVIDENCE (parse_behavior_verdict). "reproduced" findings are
     rendered as DEMONSTRATED and ranked first in the report (bench/review_aggregate.py);
     "not_reproduced" CONFIRMED findings are flagged reasoned-but-not-reproduced.
     --behavioral-severity restricts this pass to the given severity(ies) to bound cost.
  6. P3, OPT-IN via --loop (default OFF; run_review_loop()): repeats steps 1/3/4/5 across
     multiple rounds, accumulating UNIQUE findings (deduped by (normpath(file), line,
     lowercased title) so a REFUTED finding reappearing in a later round never counts as
     "new") until --dry-rounds (default 2) consecutive rounds add zero new findings, or
     --max-rounds (default 3) is hit -- hitting the cap is always printed, never silent.
     Without --loop, behavior is byte-for-byte identical to before P3.
  7. P3, OPT-IN via --completeness (default OFF; build_completeness_goal() +
     run_completeness_critic()): spawns one extra goal that audits COVERAGE instead of
     hunting new findings -- which REVIEW_DIMENSIONS ran, which files were examined, and
     which current findings are asserted-but-unverified -- and reports it via its own
     GAPS_BEGIN/GAPS_END block (parse_completeness_gaps). Works standalone (one pass after
     the single review run) or, combined with --loop, its "missing_dimensions"/
     "missing_files" seed the next round's plan_goals.

Everything that actually launches a subprocess lives in run_fleet() -- the only function
tests must monkeypatch to stay hermetic. Every other step (planning, refute-goal building,
aggregation wiring) is reachable and tested without the fleet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")

# bench/ has no __init__.py (implicit namespace package); when this file is run directly as
# a script (python bench\review_run.py, not `python -m bench.review_run`), only bench/'s own
# directory ends up on sys.path -- REPO itself does not. Add it so `bench.review_build_goals`
# / `bench.review_aggregate` (which cross-import each other by that dotted name) resolve.
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench.review_aggregate import aggregate, render_json, render_markdown, worker_final_text
from bench.review_build_goals import (
    BEHAVIOR_EVIDENCE_TAG,
    BEHAVIOR_VERDICT_TAG,
    BEHAVIOR_VERDICTS,
    FINDINGS_BEGIN,
    FINDINGS_END,
    GAPS_BEGIN,
    GAPS_END,
    build_behavioral_verify_goal,
    build_completeness_goal,
    build_dimension_goal,
    build_refute_goal,
    dimensions_for_kind,
    enumerate_files,
    group_files,
    write_goals_jsonl,
    CLEAR_FRAMING_PREAMBLE,
)
from bench.review_adjudicate import adjudicate_findings
from bench.review_decompose import (
    MAX_CHILDREN_PER_PARENT, MAX_DECOMPOSITION_DEPTH, MAX_TOTAL_RECOVERY_GOALS,
    build_child_envelopes, build_decomposer_goal, parse_subtasks, validate_subtasks,
)
from bench.review_state import derive_finding_state
from relay.refuter import PANEL_LENSES, aggregate_panel, parse_verdict
from relay.review_resilience import (
    TaskEnvelope, goal_dict_from_envelope, task_envelope_from_goal,
)

DEFAULT_OUT_DIR = ".fleet/review"
DEFAULT_ALL_GROUP_SIZE = 20
FLEET_MAX_TURNS = "40"

# Fan-out multiplies goal count by len(dimensions) vs. the old one-rubric-per-file-group
# scheme, so --max-concurrent (still user-configurable, default unchanged/conservative below)
# is additionally clamped to a hard ceiling -- override via env var, never via a CLI flag
# that could be raised without noticing the multiplier. This keeps concurrency bounded even
# if a caller passes an unreasonably large --max-concurrent by habit from the old, smaller
# goal counts.
DEFAULT_MAX_CONCURRENT = 4
MAX_CONCURRENT_CEILING = int(os.environ.get("REVIEW_MAX_CONCURRENT_CEILING", "8") or "8")


def _clamp_max_concurrent(requested):
    """Bound `requested` to (1, MAX_CONCURRENT_CEILING]. A ceiling <= 0 disables clamping
    (explicit opt-out via REVIEW_MAX_CONCURRENT_CEILING=0), matching the "configurable via
    env" requirement without silently ignoring an operator's explicit choice."""
    if MAX_CONCURRENT_CEILING <= 0:
        return max(1, int(requested))
    return max(1, min(int(requested), MAX_CONCURRENT_CEILING))


def _stamp():
    """Wall-clock timestamp used in output filenames. A bare module-level function (not
    inlined at each call site) so tests can monkeypatch bench.review_run._stamp for
    deterministic, collision-free filenames."""
    return time.strftime("%Y%m%d_%H%M%S")


def _filter_target_path(files, target_path):
    """Restrict an enumerate_files() list to paths under target_path. Paths from
    enumerate_files are repo-root-relative and forward-slashed; normalize target_path the
    same way. No-op if target_path is falsy. Matches the path itself or anything nested
    under it (not an arbitrary prefix -- "src" must not match "srcfoo.py")."""
    if not target_path:
        return files
    norm = target_path.replace("\\", "/").strip("/")
    if not norm:
        return files
    prefix = norm + "/"
    return [f for f in files if f == norm or f.startswith(prefix)]


def plan_goals(kind, mode, repo_root, base_ref=None, cached=False, target_path=None,
               group_size=None, dimension_keys=None, extra_files=None):
    """Pure planning step (aside from enumerate_files' own git subprocess calls): enumerate
    -> filter by target_path -> optionally restrict to extra_files -> group -> fan out one
    goal per (dimension, file-group).

    Replaces the old "one all-in-one rubric per file-group" scheme: for --kind review, one
    goal per review-applicable REVIEW_DIMENSIONS entry x file-group; for --kind security,
    one per security-applicable entry x file-group (see bench.review_build_goals.
    dimensions_for_kind). dimension_keys optionally restricts to a subset of the kind's
    applicable dimensions (raises ValueError if a requested key is unknown or not
    applicable to `kind`); default None = every dimension applicable to `kind`.

    extra_files (P3 piece A: additive, default None -- no behavior change for any existing
    caller) optionally restricts the enumerated+target_path-filtered file list to only paths
    also present in extra_files -- used by run_review_loop to scope a later round to the
    file/area names a completeness-critic goal (build_completeness_goal) flagged as
    unexamined, without needing a second file-listing code path. A falsy extra_files is a
    complete no-op.

    Returns (files, groups, goals, goal_meta). group_size=None picks a mode-appropriate
    default: DEFAULT_ALL_GROUP_SIZE for "all", or every changed file in ONE goal for "diff"
    (a diff is usually small, and a reviewer benefits from seeing the whole changeset
    together). goal_meta is a list parallel to `goals`: [{"dimension": key, "files": group},
    ...] -- used for --dry-run printing and for the report's dimension-coverage note; it is
    NEVER written to the goals JSONL (write_goals_jsonl only ever sees the plain
    {"text","cwd"} dicts in `goals`)."""
    files = enumerate_files(mode, repo_root, base_ref=base_ref, cached=cached)
    files = _filter_target_path(files, target_path)
    if extra_files:
        wanted = set(extra_files)
        files = [f for f in files if f in wanted]

    if group_size is None:
        group_size = len(files) if mode == "diff" else DEFAULT_ALL_GROUP_SIZE

    groups = group_files(files, group_size)

    dims = dimensions_for_kind(kind)
    if dimension_keys:
        wanted = set(dimension_keys)
        applicable_keys = {d["key"] for d in dims}
        unknown = wanted - applicable_keys
        if unknown:
            raise ValueError(
                "--dimensions has unknown or inapplicable key(s) for kind=%r: %s "
                "(applicable: %s)" % (kind, sorted(unknown), sorted(applicable_keys)))
        dims = [d for d in dims if d["key"] in wanted]

    goals = []
    goal_meta = []
    for dim in dims:
        for g in groups:
            goals.append(build_dimension_goal(dim, g, repo_root))
            goal_meta.append({"dimension": dim["key"], "files": list(g)})
    return files, groups, goals, goal_meta


def fleet_cmd(goals_path, max_concurrent, effort, state_dir=None,
              resilience_profile=None, max_turns=None):
    """The exact, verified relay.fleet_runner launch contract. Do not add, rename, or drop
    the core flags here -- other bench orchestrators (bench/swe_solve_decoupled.py) rely on
    this same shape and it has been confirmed live against relay/fleet_runner.py's argparse.

    state_dir is OPTIONAL and additive: when given, a --state-dir flag is appended so the run
    writes its status.json / transcripts under that dir instead of the default .fleet. Callers
    that omit it get the exact original argv (no behaviour change for existing orchestrators)."""
    cmd = [VENVPY, "-m", "relay.fleet_runner",
           "--goals-file", goals_path,
           "--max-concurrent", str(max_concurrent),
           "--max-turns", str(max_turns if max_turns is not None else FLEET_MAX_TURNS),
           "--disk-floor-gb", "0",
           "--effort", effort]
    if state_dir:
        cmd += ["--state-dir", state_dir]
    if resilience_profile and resilience_profile != "off":
        cmd += ["--resilience-profile", resilience_profile, "--max-fresh-replays", "1"]
    return cmd


def run_fleet(goals_path, max_concurrent, effort, state_dir=None,
              resilience_profile=None, max_turns=None):
    """The ONE function that touches the fleet subprocess -- isolated so tests can
    monkeypatch it out entirely without a real Popen. Blocks until the fleet run exits
    (fleet_runner drives the M365 Copilot fleet on companion Edge :9222); returns its
    return code. Writes <state_dir or .fleet>/status.json and .../transcripts/* as a side
    effect."""
    cmd = fleet_cmd(goals_path, max_concurrent, effort, state_dir=state_dir,
                    resilience_profile=resilience_profile, max_turns=max_turns)
    print("fleet: " + " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, cwd=REPO, env=dict(os.environ))
    proc.wait()
    return proc.returncode


_P2C_PROHIBITED_ACTIONS = (
    "do not remove or weaken the authorization preamble",
    "do not expand the parent file scope",
    "do not add external-system access or credentials",
    "do not output secret values",
    "do not edit source files during review",
)


def _prepare_resilience_goals(goals, goal_meta, kind, stamp):
    """Add immutable Task Envelope metadata without changing any goal text/cwd."""
    campaign_id = "%s-%s" % (kind, stamp)
    prepared = []
    envelopes = {}
    for i, (goal, meta) in enumerate(zip(goals, goal_meta), 1):
        task_id = "%s-%s-%04d" % (kind, meta.get("dimension", "review"), i)
        metadata = {
            "scope": list(meta.get("files") or []),
            "files": list(meta.get("files") or []),
            "dimension": meta.get("dimension", ""),
            "output_contract": "FINDINGS",
            "authorization_preamble": CLEAR_FRAMING_PREAMBLE,
            "prohibited_actions": list(_P2C_PROHIBITED_ACTIONS),
            "resilience_profile": kind,
        }
        envelope = TaskEnvelope(
            task_id=task_id,
            parent_task_id=None,
            campaign_id=campaign_id,
            role="producer",
            goal_text=goal.get("text", ""),
            cwd=goal.get("cwd", ""),
            depth=0,
            metadata=metadata,
        )
        goal_dict = goal_dict_from_envelope(envelope)
        # Preserve any acceptance/check fields a future goal builder may add.
        for key in ("check", "checks"):
            if key in goal:
                goal_dict[key] = goal[key]
        prepared.append(goal_dict)
        envelopes[task_id] = envelope
    return prepared, envelopes


def _load_status(state_dir):
    try:
        with open(os.path.join(state_dir, "status.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resilience_finding_key(finding):
    detail = " ".join(str(finding.get("detail", "") or "").lower().split())
    detail_hash = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:16]
    return (
        os.path.normcase(os.path.normpath(str(finding.get("file", "") or ""))),
        finding.get("line"),
        str(finding.get("title", "") or "").strip().lower(),
        detail_hash,
    )


def _merge_recovery_aggregate(target, child):
    seen = {_resilience_finding_key(f) for f in target.get("findings", [])
            if isinstance(f, dict)}
    for finding in child.get("findings", []):
        if not isinstance(finding, dict):
            continue
        key = _resilience_finding_key(finding)
        if key in seen:
            continue
        seen.add(key)
        target.setdefault("findings", []).append(finding)
    target["workers_total"] = target.get("workers_total", 0) + child.get("workers_total", 0)
    target["parse_errors"] = target.get("parse_errors", 0) + child.get("parse_errors", 0)
    by_severity = {"high": [], "medium": [], "low": []}
    for finding in target.get("findings", []):
        sev = str(finding.get("severity", "low")).lower()
        by_severity[sev if sev in by_severity else "low"].append(finding)
    target["by_severity"] = by_severity


def recover_content_refusals(agg, initial_state_dir, envelopes, kind, out_dir, stamp,
                             max_concurrent, effort, repo_root):
    """Decompose only twice-refused workers, bounded by depth/children/total budgets."""
    initial_status = _load_status(initial_state_dir)
    initial_workers = initial_status.get("workers", []) or []
    pending = []
    for worker in initial_workers:
        if not isinstance(worker, dict):
            continue
        if worker.get("status") != "content_refused" and worker.get("outcome") != "CONTENT_REFUSED":
            continue
        envelope = envelopes.get(worker.get("task_id"))
        if envelope is not None:
            pending.append((envelope, worker))

    metrics = {
        "enabled": True,
        "profile": kind,
        "fresh_replays": sum(int(w.get("fresh_replay_count", 0) or 0)
                             for w in initial_workers if isinstance(w, dict)),
        "content_refusals": len(pending),
        "decomposed_parents": 0,
        "child_goals": 0,
        "unresolved_refusals": 0,
        "validation_errors": [],
        "budget_truncated_children": 0,
        "events": [],
    }
    # A refusal is an expected P2c state, not a malformed FINDINGS response.
    if pending:
        agg["parse_errors"] = max(0, agg.get("parse_errors", 0) - len(pending))

    recovery_budget = min(MAX_TOTAL_RECOVERY_GOALS, max(1, len(envelopes) * 4))
    recovery_goals_used = 0
    depth = 0
    while pending and depth < MAX_DECOMPOSITION_DEPTH and recovery_goals_used < recovery_budget:
        depth += 1
        decomposable = [(env, w) for env, w in pending if env.depth < MAX_DECOMPOSITION_DEPTH]
        metrics["unresolved_refusals"] += len(pending) - len(decomposable)
        if not decomposable:
            break

        room = recovery_budget - recovery_goals_used
        decomposable = decomposable[:room]
        decomp_goals = []
        for env, worker in decomposable:
            summaries = [str(x.get("response", ""))[:800]
                         for x in (worker.get("refusal_history") or []) if isinstance(x, dict)]
            decomp_goals.append(build_decomposer_goal(env, summaries))
        recovery_goals_used += len(decomp_goals)
        decomp_path = os.path.join(out_dir, "recovery_decomposer_goals_%s_d%d.jsonl" % (stamp, depth))
        decomp_state = os.path.join(out_dir, "recovery_decomposer_state_%s_d%d" % (stamp, depth))
        write_goals_jsonl(decomp_goals, decomp_path)
        run_fleet(decomp_path, min(max_concurrent, max(1, len(decomp_goals))), effort,
                  state_dir=decomp_state, resilience_profile=kind, max_turns=4)
        decomp_workers = _load_status(decomp_state).get("workers", []) or []

        child_envelopes = []
        for index, (parent, _worker) in enumerate(decomposable):
            dw = decomp_workers[index] if index < len(decomp_workers) \
                and isinstance(decomp_workers[index], dict) else {}
            text = worker_final_text(dw, os.path.join(decomp_state, "transcripts"))
            subtasks, parse_errors = parse_subtasks(text)
            valid, errors = validate_subtasks(parent, subtasks, MAX_CHILDREN_PER_PARENT)
            if parse_errors:
                errors.append("%s: decomposer output was not parseable" % parent.task_id)
            if errors:
                metrics["validation_errors"].extend(errors)
            if not valid:
                metrics["unresolved_refusals"] += 1
                metrics["events"].append({"task_id": parent.task_id,
                                          "result": "decomposition_failed", "depth": parent.depth})
                continue
            built = build_child_envelopes(parent, valid)
            remaining = recovery_budget - recovery_goals_used - len(child_envelopes)
            admitted = built[:max(0, remaining)]
            child_envelopes.extend(admitted)
            metrics["budget_truncated_children"] += max(0, len(built) - len(admitted))
            metrics["decomposed_parents"] += 1
            metrics["events"].append({"task_id": parent.task_id,
                                      "result": "decomposed", "children": len(admitted),
                                      "children_requested": len(built),
                                      "depth": parent.depth})

        if not child_envelopes:
            pending = []
            break
        child_goals = [goal_dict_from_envelope(env) for env in child_envelopes]
        recovery_goals_used += len(child_goals)
        metrics["child_goals"] += len(child_goals)
        child_path = os.path.join(out_dir, "recovery_children_goals_%s_d%d.jsonl" % (stamp, depth))
        child_state = os.path.join(out_dir, "recovery_children_d%d_state_%s" % (depth, stamp))
        write_goals_jsonl(child_goals, child_path)
        run_fleet(child_path, min(max_concurrent, max(1, len(child_goals))), effort,
                  state_dir=child_state, resilience_profile=kind, max_turns=12)
        child_status = _load_status(child_state)
        child_workers = child_status.get("workers", []) or []
        metrics["fresh_replays"] += sum(int(w.get("fresh_replay_count", 0) or 0)
                                        for w in child_workers if isinstance(w, dict))
        metrics["content_refusals"] += sum(
            1 for w in child_workers if isinstance(w, dict)
            and (w.get("status") == "content_refused" or w.get("outcome") == "CONTENT_REFUSED"))
        child_agg = aggregate(os.path.join(child_state, "status.json"),
                              os.path.join(child_state, "transcripts"), now=time.time())
        refused_count = sum(1 for w in child_workers if isinstance(w, dict)
                            and (w.get("status") == "content_refused"
                                 or w.get("outcome") == "CONTENT_REFUSED"))
        child_agg["parse_errors"] = max(0, child_agg.get("parse_errors", 0) - refused_count)
        _merge_recovery_aggregate(agg, child_agg)

        by_id = {env.task_id: env for env in child_envelopes}
        pending = []
        for worker in child_workers:
            if not isinstance(worker, dict):
                continue
            if worker.get("status") == "content_refused" or worker.get("outcome") == "CONTENT_REFUSED":
                env = by_id.get(worker.get("task_id"))
                if env is not None:
                    pending.append((env, worker))

    metrics["unresolved_refusals"] += len(pending)
    metrics["recovery_goals_used"] = recovery_goals_used
    metrics["recovery_goal_budget"] = recovery_budget
    agg["resilience"] = metrics
    return metrics


_WORKER_NAME_RE = re.compile(r"w(\d+)$")


def _worker_index(worker):
    """Recover the 0-based goal index from a relay.relay_fleet worker's "name" field
    ("w0", "w1", ...). relay.relay_fleet.py assigns names as "w%d" % i in the SAME order
    the goals list was submitted (workers = [RelayWorker(g, "w%d" % i, ...) for i, g in
    enumerate(goals)]), and its final status.json workers[] list preserves that same order
    -- so this index reliably maps a finished worker back to the goal (and, via
    refute_findings' own goal_index_map, the finding/lens) it was given. Returns None if the
    name is missing or doesn't match the "w<N>" shape (never raises)."""
    name = worker.get("name", "") if isinstance(worker, dict) else ""
    m = _WORKER_NAME_RE.search(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


_BEHAVIOR_VERDICT_RE = re.compile(
    re.escape(BEHAVIOR_VERDICT_TAG) + r"\s*[:：]\s*(\w+)", re.IGNORECASE
)
_BEHAVIOR_EVIDENCE_RE = re.compile(
    re.escape(BEHAVIOR_EVIDENCE_TAG) + r"\s*[:：]\s*(.+)", re.IGNORECASE
)
_VALID_BEHAVIOR_VERDICTS = frozenset(BEHAVIOR_VERDICTS)


def parse_behavior_verdict(text):
    """Parse a behavioral-verify worker's final reply into (verdict, evidence).

    verdict is one of bench.review_build_goals.BEHAVIOR_VERDICTS ("reproduced" /
    "not_reproduced" / "inconclusive") -- the exact 3-way contract
    build_behavioral_verify_goal's own prompt asks for (see its _BEHAVIOR_VERDICT_SPEC).
    Tolerant like relay.refuter.parse_verdict: scans the WHOLE text for a
    "BEHAVIOR_VERDICT: <word>" line (case-insensitive, half/full-width colon), last
    recognized match wins (an agent may restate its reasoning before the final line, so the
    latest valid tag is treated as the final answer); an unrecognized word, missing tag, or
    empty/garbage text all fold to ("inconclusive", ""). evidence is the last
    "BEHAVIOR_EVIDENCE: <...>" line found, or "" if absent. Never raises."""
    if not text:
        return ("inconclusive", "")

    verdict = "inconclusive"
    for m in _BEHAVIOR_VERDICT_RE.finditer(text):
        candidate = m.group(1).strip().lower()
        if candidate in _VALID_BEHAVIOR_VERDICTS:
            verdict = candidate

    evidence = ""
    for m in _BEHAVIOR_EVIDENCE_RE.finditer(text):
        evidence = m.group(1).strip()

    return (verdict, evidence)


def refute_findings(findings, kind, out_dir, now, panel=False, max_concurrent=None,
                     effort="auto", repo_root=None, stamp=None, resilience_profile=None):
    """Run EACH finding through the existing adversarial refuter (relay/refuter.py) as its own
    fleet pass, and return a verdicts list shaped for merge_verdicts(findings, verdicts).

    One refute goal per finding (default, single lens picked by build_refute_goal); with
    panel=True, PANEL_LENSES (correctness/edge/security) goals per finding, later combined by
    relay.refuter.aggregate_panel's majority vote. Goal order is deterministic (finding 0's
    lens(es), then finding 1's, ...) and _worker_index() recovers each finished worker's goal
    index from its "w<N>" name -- this is how a specific verdict is mapped back to the
    specific finding it refutes (relay.refuter's own verdict text carries no file/line/title).

    Never raises: a missing goals dir write, a fleet run that produces no status.json, or an
    unparseable worker reply all degrade to that finding getting NO verdict entry (merge_verdicts
    leaves its verify_verdict as None, same as "the refuter pass never ran" for it) -- a
    refutation is a bonus signal, never a hard requirement for the review report to exist.

    Returns [] immediately (no fleet launch) if `findings` is empty."""
    if not findings:
        return []
    repo_root = repo_root or REPO
    stamp = stamp or _stamp()
    max_concurrent = _clamp_max_concurrent(
        max_concurrent if max_concurrent is not None else DEFAULT_MAX_CONCURRENT)

    lens_variants = list(PANEL_LENSES) if panel else [None]

    goal_dicts = []
    goal_index_map = []  # parallel to goal_dicts: (finding_index, lens_or_None)
    for fi, finding in enumerate(findings):
        for lens in lens_variants:
            text = build_refute_goal(finding, kind, lens=lens)
            goal_dicts.append({"text": text, "cwd": repo_root})
            goal_index_map.append((fi, lens))

    goals_path = os.path.join(out_dir, "refute_goals_%s.jsonl" % stamp)
    write_goals_jsonl(goal_dicts, goals_path)

    # Own state dir (mirrors the old verify pass) so this run's status.json/transcripts never
    # collide with -- or get silently read back as -- the review pass's own .fleet/status.json.
    refute_state_dir = os.path.join(out_dir, "refute_state_%s" % stamp)
    refute_status_path = os.path.join(refute_state_dir, "status.json")
    refute_transcripts_dir = os.path.join(refute_state_dir, "transcripts")

    if resilience_profile and resilience_profile != "off":
        run_fleet(goals_path, max_concurrent, effort, state_dir=refute_state_dir,
                  resilience_profile=resilience_profile, max_turns=6)
    else:
        run_fleet(goals_path, max_concurrent, effort, state_dir=refute_state_dir)

    if not os.path.isfile(refute_status_path):
        print("WARNING: refuter pass produced no status.json at %s; skipping (findings keep "
              "verify_verdict=None)" % refute_status_path)
        return []

    try:
        with open(refute_status_path, encoding="utf-8") as f:
            status = json.load(f)
    except Exception as e:
        print("WARNING: could not read refuter status.json: %s" % e)
        return []
    workers = status.get("workers", []) if isinstance(status, dict) else []
    if not isinstance(workers, list):
        workers = []

    # finding_index -> list of (lens, verdict_kind, reason)
    per_finding = {}
    for w in workers:
        if not isinstance(w, dict):
            continue
        idx = _worker_index(w)
        if idx is None or idx < 0 or idx >= len(goal_index_map):
            continue
        fi, lens = goal_index_map[idx]
        text = worker_final_text(w, refute_transcripts_dir)
        vkind, vreason = parse_verdict(text)
        per_finding.setdefault(fi, []).append((lens or "", vkind, vreason))

    verdicts = []
    for fi, finding in enumerate(findings):
        results = per_finding.get(fi)
        if not results:
            continue
        if panel:
            vkind, vreason = aggregate_panel(results)
        else:
            vkind, vreason = results[0][1], results[0][2]
        if vkind == "REFUTED":
            vv = "false_positive"
        elif vkind == "UPHELD":
            vv = "confirmed"
        else:
            vv = "unclear"
        verdicts.append({
            "file": finding.get("file", ""),
            "line": finding.get("line"),
            "title": finding.get("title", ""),
            "verdict": vv,
            "reason": vreason,
            "refuter_verdict": vkind,
        })
    return verdicts


def behavioral_verify(findings, out_dir, now, max_concurrent=None, effort="auto",
                       repo_root=None, stamp=None, severity_filter=None, max_findings=None,
                       resilience_profile=None):
    """P2 piece A: run each CONFIRMED finding (verify_verdict == "confirmed", set by
    refute_findings + merge_verdicts) through a BEHAVIORAL-VERIFY fleet pass -- a fresh worker
    tries to actually REPRODUCE the finding with a minimal, READ-ONLY run_python/shell_exec
    check (build_behavioral_verify_goal) instead of only reasoning about it, and reports
    BEHAVIOR_VERDICT/BEHAVIOR_EVIDENCE (parse_behavior_verdict). Mutates the matching finding
    dicts IN PLACE with "behavioral_verdict" and "behavioral_evidence" (same objects
    aggregate() already shares between agg["findings"] and agg["by_severity"], mirroring how
    merge_verdicts mutates in place) and also returns the list of dicts it touched.

    Only findings with verify_verdict == "confirmed" are ever selected -- false_positive/
    unclear/None-verdict findings never get a behavioral pass (there is nothing to
    demonstrate for a finding that was never upheld in the first place). severity_filter
    (e.g. {"high"}) further restricts the CONFIRMED set to bound cost; max_findings
    additionally caps the absolute count. max_concurrent is clamped via
    _clamp_max_concurrent exactly like refute_findings.

    OPT-IN AT THE CALLER LAYER: this function itself always runs when given findings -- the
    "--behavioral defaults to OFF" gate lives in main() (this executes real code, unlike the
    pure-reasoning refuter, so bench/review_run.py's CLI keeps it opt-in; see build_arg_parser
    --behavioral). Kept ungated here so tests (and any other caller) can drive it directly.

    Goal order is deterministic (one goal per selected finding, in the same relative order
    they appear in `findings`) and _worker_index() recovers each finished worker's goal index
    from its "w<N>" name -- identical wiring to refute_findings' own goal_index_map.

    Never raises: a missing goals dir write, a fleet run producing no status.json, or an
    unparseable worker reply all degrade to that finding getting NO behavioral_verdict
    attached (same graceful-degradation contract as refute_findings). Returns [] immediately
    (no fleet launch) if there is nothing selected to verify."""
    repo_root = repo_root or REPO
    stamp = stamp or _stamp()
    max_concurrent = _clamp_max_concurrent(
        max_concurrent if max_concurrent is not None else DEFAULT_MAX_CONCURRENT)

    selected = []  # list of (index into `findings`, finding dict)
    for fi, finding in enumerate(findings or []):
        if not isinstance(finding, dict):
            continue
        if finding.get("verify_verdict") != "confirmed":
            continue
        if severity_filter:
            sev = str(finding.get("severity", "")).lower()
            if sev not in severity_filter:
                continue
        selected.append((fi, finding))

    if max_findings is not None:
        selected = selected[:max_findings]

    if not selected:
        return []

    goal_dicts = []
    goal_index_map = []  # parallel to goal_dicts: index into `findings`
    for fi, finding in selected:
        text = build_behavioral_verify_goal(finding)
        goal_dicts.append({"text": text, "cwd": repo_root})
        goal_index_map.append(fi)

    goals_path = os.path.join(out_dir, "behavioral_goals_%s.jsonl" % stamp)
    write_goals_jsonl(goal_dicts, goals_path)

    # Own state dir (mirrors refute_findings' refute_state_dir) so this run's status.json/
    # transcripts never collide with the review pass's or the refuter pass's own.
    behavioral_state_dir = os.path.join(out_dir, "behavioral_state_%s" % stamp)
    behavioral_status_path = os.path.join(behavioral_state_dir, "status.json")
    behavioral_transcripts_dir = os.path.join(behavioral_state_dir, "transcripts")

    if resilience_profile and resilience_profile != "off":
        run_fleet(goals_path, max_concurrent, effort, state_dir=behavioral_state_dir,
                  resilience_profile=resilience_profile, max_turns=10)
    else:
        run_fleet(goals_path, max_concurrent, effort, state_dir=behavioral_state_dir)

    if not os.path.isfile(behavioral_status_path):
        print("WARNING: behavioral-verify pass produced no status.json at %s; skipping "
              "(findings keep no behavioral_verdict)" % behavioral_status_path)
        return []

    try:
        with open(behavioral_status_path, encoding="utf-8") as f:
            status = json.load(f)
    except Exception as e:
        print("WARNING: could not read behavioral-verify status.json: %s" % e)
        return []
    workers = status.get("workers", []) if isinstance(status, dict) else []
    if not isinstance(workers, list):
        workers = []

    attached = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        idx = _worker_index(w)
        if idx is None or idx < 0 or idx >= len(goal_index_map):
            continue
        fi = goal_index_map[idx]
        if fi < 0 or fi >= len(findings):
            continue
        text = worker_final_text(w, behavioral_transcripts_dir)
        verdict, evidence = parse_behavior_verdict(text)
        finding = findings[fi]
        finding["behavioral_verdict"] = verdict
        finding["behavioral_evidence"] = evidence
        attached.append(finding)

    return attached


# --- P3 piece B: completeness critic ---------------------------------------------------------

_GAPS_RE = re.compile(re.escape(GAPS_BEGIN) + r"\s*(.*?)\s*" + re.escape(GAPS_END), re.DOTALL)

_EMPTY_GAPS = {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def parse_completeness_gaps(text):
    """Parse a completeness-critic worker's final reply (bench.review_build_goals.
    build_completeness_goal's GAPS_BEGIN/GAPS_END contract) into a dict with exactly the keys
    "missing_dimensions", "missing_files", "unverified_claims" (each a list of str).

    The ONE place this delimiter pair is parsed (mirrors parse_behavior_verdict being the one
    place BEHAVIOR_VERDICT/BEHAVIOR_EVIDENCE are parsed). Never raises: missing text, a missing
    GAPS_BEGIN/GAPS_END pair, invalid JSON inside it, or a JSON value that isn't an object all
    degrade to a fresh copy of _EMPTY_GAPS (all-empty lists) -- same graceful-degradation
    contract as parse_behavior_verdict/refute_findings/behavioral_verify. Non-list values for a
    recognized key, and non-str/int/float items inside a list, are dropped rather than raising."""
    if not text:
        return dict(_EMPTY_GAPS)
    m = _GAPS_RE.search(text)
    if not m:
        return dict(_EMPTY_GAPS)
    try:
        data = json.loads(m.group(1))
    except Exception:
        return dict(_EMPTY_GAPS)
    if not isinstance(data, dict):
        return dict(_EMPTY_GAPS)
    out = dict(_EMPTY_GAPS)
    for key in out:
        val = data.get(key)
        if isinstance(val, list):
            out[key] = [str(v) for v in val if isinstance(v, (str, int, float))]
    return out


def run_completeness_critic(dimensions_run, files_covered, findings_so_far, out_dir, repo_root,
                             effort="auto", stamp=None):
    """P3 piece B: spawn ONE completeness-critic goal (build_completeness_goal) as its own
    single-goal, own-state-dir'd fleet pass (mirrors refute_findings'/behavioral_verify's own
    state_dir isolation, so this run's status.json/transcripts never collide with the review
    pass's or any other P1/P2 pass's), then parse its GAPS block (parse_completeness_gaps) and
    return the resulting gaps dict.

    Never raises: a missing goals dir write, a fleet run that produces no status.json, no
    workers in that status.json, or an unparseable reply all degrade to a fresh copy of
    _EMPTY_GAPS (all-empty lists) -- same graceful-degradation contract as refute_findings /
    behavioral_verify. Shared by both bench.review_run.main()'s standalone --completeness pass
    and run_review_loop's per-round seeding, so the wiring exists in exactly one place."""
    stamp = stamp or _stamp()
    goal = build_completeness_goal(dimensions_run, files_covered, findings_so_far, repo_root)
    goals_path = os.path.join(out_dir, "completeness_goals_%s.jsonl" % stamp)
    write_goals_jsonl([goal], goals_path)

    state_dir = os.path.join(out_dir, "completeness_state_%s" % stamp)
    status_path = os.path.join(state_dir, "status.json")
    transcripts_dir = os.path.join(state_dir, "transcripts")

    run_fleet(goals_path, _clamp_max_concurrent(1), effort, state_dir=state_dir)

    if not os.path.isfile(status_path):
        print("WARNING: completeness-critic pass produced no status.json at %s; skipping "
              "(no gaps reported)" % status_path)
        return dict(_EMPTY_GAPS)
    try:
        with open(status_path, encoding="utf-8") as f:
            status = json.load(f)
    except Exception as e:
        print("WARNING: could not read completeness-critic status.json: %s" % e)
        return dict(_EMPTY_GAPS)
    workers = status.get("workers", []) if isinstance(status, dict) else []
    if not isinstance(workers, list) or not workers:
        return dict(_EMPTY_GAPS)

    text = worker_final_text(workers[0], transcripts_dir)
    return parse_completeness_gaps(text)


# --- P3 piece A: loop-until-dry orchestration -------------------------------------------------

def _dedupe_key(finding):
    """Stable dedupe key for one finding, per the P3 spec: (normpath(file), line,
    title.strip().lower()). Used by run_review_loop to recognize "the same finding reported
    again" across rounds regardless of a later round's verify_verdict -- so a finding the
    refuter REFUTED in an earlier round still counts as "already seen" and never re-triggers
    a "new finding this round" count (which would otherwise stall the loop at dry_rounds=0
    forever, since a refuted finding can keep reappearing every round)."""
    file_ = os.path.normpath(str(finding.get("file", "") or ""))
    line = finding.get("line")
    title = str(finding.get("title", "") or "").strip().lower()
    return (file_, line, title)


def run_review_loop(kind, mode, repo_root, out_dir, base_stamp, base_ref=None, cached=False,
                     target_path=None, group_size=None, dimension_keys=None,
                     max_concurrent=DEFAULT_MAX_CONCURRENT, effort="auto", run_refute=True,
                     panel=False, run_behavioral=False, behavioral_severity=None,
                     completeness=False, max_rounds=3, dry_rounds=2):
    """P3 piece A: repeat the existing single-round pipeline (plan_goals -> run_fleet ->
    aggregate -> optional refute_findings -> optional behavioral_verify) across multiple
    rounds, accumulating UNIQUE findings via _dedupe_key, until the run goes dry.

    Stops when EITHER:
      - `dry_rounds` CONSECUTIVE rounds each add zero new (unseen-by-_dedupe_key) findings
        (stopped_reason="dry"), or
      - `max_rounds` rounds have run without going dry (stopped_reason="max_rounds" -- NO
        SILENT CAP: this is reported both via a printed line and via the returned loop_meta,
        never silently truncated), or
      - a round's plan_goals yields no goals at all (stopped_reason="no_files"), or
      - a round's fleet run produces no status.json (stopped_reason="fleet_failure").

    Every finding ever seen (regardless of round or later verify_verdict) is added to a
    persistent `seen` set BEFORE the next round's dedupe check runs, so a finding REFUTED in
    round N does not reappear as "new" in round N+1 (a refuted finding recurring every round
    would otherwise keep the loop "wet" forever instead of going dry).

    completeness=True (P3 piece B) additionally spawns run_completeness_critic after each
    round that didn't just stop the loop, and feeds its "missing_dimensions" (validated
    against dimensions_for_kind(kind), unknown/inapplicable keys silently ignored -- a critic
    reply is untrusted free-form model output, never allowed to raise) and "missing_files"
    into the NEXT round's plan_goals (dimension_keys union, extra_files scope). completeness
    is independent of dimension_keys/target_path being narrowed by the CLI -- a run with no
    --dimensions restriction already covers every dimension, so the critic's suggestions are
    then a no-op union (nothing new to add), which is intentional, not a bug.

    Returns (agg, loop_meta):
      agg     -- same shape as bench.review_aggregate.aggregate()'s return (so callers can
                 pass it straight to render_markdown/render_json unchanged): "generated_at",
                 "workers_total" (always 0 here -- this is an accumulation across N worker
                 pools, not one pool), "parse_errors" (always 0 -- per-round parse errors are
                 already folded into whether a finding made it into `findings` at all),
                 "findings" (deduped, accumulated across every round), "by_severity". Also
                 carries "completeness_gaps" (the LAST round's gaps dict) iff completeness=True
                 and at least one critic round actually ran.
      loop_meta -- {"rounds_run": int, "stopped_reason": str, "max_rounds": int,
                    "dry_rounds_target": int, "unique_findings": int}.
    """
    seen = set()
    all_findings = []
    consecutive_dry = 0
    rounds_run = 0
    stopped_reason = None
    extra_dimension_keys = None
    extra_target_files = None
    last_gaps = None
    any_critic_ran = False

    kind_dims = {d["key"] for d in dimensions_for_kind(kind)}

    for round_idx in range(1, max_rounds + 1):
        rounds_run = round_idx
        round_stamp = "%s_r%d" % (base_stamp, round_idx)

        this_dims = dimension_keys
        if extra_dimension_keys:
            base = set(dimension_keys) if dimension_keys else set(kind_dims)
            this_dims = sorted(base | set(extra_dimension_keys))

        files, groups, goals, goal_meta = plan_goals(
            kind, mode, repo_root, base_ref=base_ref, cached=cached, target_path=target_path,
            group_size=group_size, dimension_keys=this_dims, extra_files=extra_target_files)
        dims_used = sorted({m["dimension"] for m in goal_meta})

        goals_path = os.path.join(out_dir, "goals_%s.jsonl" % round_stamp)
        write_goals_jsonl(goals, goals_path)

        if not goals:
            print("loop round %d/%d: no files matched -- stopping loop" % (round_idx, max_rounds))
            stopped_reason = "no_files"
            break

        print("loop round %d/%d: launching %d goal(s) across dimensions: %s..." %
              (round_idx, max_rounds, len(goals), ", ".join(dims_used)))
        run_fleet(goals_path, max_concurrent, effort)

        status_path = os.path.join(repo_root, ".fleet", "status.json")
        transcripts_dir = os.path.join(repo_root, ".fleet", "transcripts")
        if not os.path.isfile(status_path):
            print("WARNING: loop round %d produced no status.json -- stopping loop early "
                  "(rounds may remain unrun)" % round_idx)
            stopped_reason = "fleet_failure"
            break

        agg_round = aggregate(status_path, transcripts_dir, now=time.time())
        round_findings = agg_round.get("findings", [])

        if run_refute and round_findings:
            verdicts = refute_findings(round_findings, kind, out_dir, time.time(), panel=panel,
                                        max_concurrent=max_concurrent, effort=effort,
                                        repo_root=repo_root, stamp=round_stamp)
            merge_verdicts(agg_round, verdicts)

        if run_behavioral and round_findings:
            confirmed = [f for f in round_findings if f.get("verify_verdict") == "confirmed"]
            if behavioral_severity:
                confirmed = [f for f in confirmed
                             if str(f.get("severity", "")).lower() in behavioral_severity]
            if confirmed:
                behavioral_verify(round_findings, out_dir, time.time(),
                                   max_concurrent=max_concurrent, effort=effort,
                                   repo_root=repo_root, stamp=round_stamp,
                                   severity_filter=behavioral_severity)

        new_count = 0
        for f in round_findings:
            if not isinstance(f, dict):
                continue
            key = _dedupe_key(f)
            if key in seen:
                continue
            seen.add(key)
            all_findings.append(f)
            new_count += 1

        print("loop round %d/%d: %d finding(s) this round, %d new (unseen)" %
              (round_idx, max_rounds, len(round_findings), new_count))

        consecutive_dry = consecutive_dry + 1 if new_count == 0 else 0
        if consecutive_dry >= dry_rounds:
            stopped_reason = "dry"
            break

        if completeness:
            files_covered = sorted({f for g in groups for f in g})
            gaps = run_completeness_critic(dims_used, files_covered, all_findings, out_dir,
                                            repo_root, effort=effort, stamp=round_stamp)
            any_critic_ran = True
            last_gaps = gaps
            extra_dimension_keys = [d for d in gaps.get("missing_dimensions", [])
                                     if d in kind_dims] or None
            extra_target_files = gaps.get("missing_files") or None
            if any(gaps.get(k) for k in _EMPTY_GAPS):
                print("loop round %d/%d: completeness critic reported gaps: "
                      "missing_dimensions=%s missing_files=%s unverified_claims=%d" %
                      (round_idx, max_rounds, gaps.get("missing_dimensions"),
                       gaps.get("missing_files"), len(gaps.get("unverified_claims") or [])))
    else:
        # for/else: the loop ran every round in range() without ever `break`-ing (never went
        # dry, never hit a no-files/fleet-failure early stop) -- it was the max_rounds cap
        # itself that ended things, not dryness. NO SILENT CAPS: report this explicitly.
        stopped_reason = "max_rounds"

    if stopped_reason == "max_rounds":
        print("NO SILENT CAP: loop stopped because max-rounds=%d was reached, NOT because "
              "findings went dry -- rounds may remain; re-run with a higher --max-rounds to "
              "keep looking." % max_rounds)

    by_severity = {"high": [], "medium": [], "low": []}
    for f in all_findings:
        sev = str(f.get("severity", "low")).lower()
        if sev not in by_severity:
            sev = "low"
        by_severity[sev].append(f)

    agg = {
        "generated_at": time.time(),
        "workers_total": 0,
        "parse_errors": 0,
        "findings": all_findings,
        "by_severity": by_severity,
    }
    if any_critic_ran:
        agg["completeness_gaps"] = last_gaps

    loop_meta = {
        "rounds_run": rounds_run,
        "stopped_reason": stopped_reason,
        "max_rounds": max_rounds,
        "dry_rounds_target": dry_rounds,
        "unique_findings": len(all_findings),
    }
    return agg, loop_meta


def _finding_key(f):
    return (str(f.get("file", "")), str(f.get("line", "")), str(f.get("title", "")))


def merge_verdicts(agg, verdicts):
    """PURE: annotate each finding in agg["findings"] with a matching verdict's
    "verdict"/"reason", matched by (file, line, title). agg["by_severity"] holds the SAME
    dict objects (aggregate() puts references, not copies, into both), so mutating
    agg["findings"] entries in place also updates the by_severity view. Findings with no
    matching verdict get verify_verdict=None. Returns agg (mutated) for chaining."""
    by_key = {}
    for v in verdicts:
        if isinstance(v, dict):
            by_key.setdefault(_finding_key(v), v)
    for finding in agg.get("findings", []):
        v = by_key.get(_finding_key(finding))
        finding["verify_verdict"] = v.get("verdict") if v else None
        finding["verify_reason"] = (v.get("reason", "") if v else "")
        finding["refuter_verdict"] = v.get("refuter_verdict") if v else None
    return agg


def derive_and_attach_finding_states(findings):
    """Attach the P2c state while retaining verify_verdict for older report/fix readers."""
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        state = derive_finding_state(
            True,
            finding.get("refuter_verdict"),
            finding.get("adjudicator_verdict"),
            finding.get("behavioral_verdict"),
        )
        finding["finding_state"] = state.value
        if state.value in ("confirmed", "reproduced"):
            finding["verify_verdict"] = "confirmed"
        elif state.value == "disproved":
            finding["verify_verdict"] = "false_positive"
        elif state.value == "contested":
            finding["verify_verdict"] = "unclear"
    return findings


def _resolve_out_dir(out_dir, repo_root):
    return out_dir if os.path.isabs(out_dir) else os.path.join(repo_root, out_dir)


def build_arg_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["review", "security"], required=True)
    ap.add_argument("--resilience-profile", choices=["off", "review", "security"],
                    default="off", help=argparse.SUPPRESS)
    ap.add_argument("--no-auto-behavioral-high", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--mode", choices=["all", "diff"], default="all")
    ap.add_argument("--base-ref", default=None)
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("--target-path", default=None,
                     help="restrict enumeration to files under this subdir")
    ap.add_argument("--group-size", type=int, default=None,
                     help="files per goal (default: 20 for --mode all, all-in-one for diff)")
    ap.add_argument("--dimensions", default=None,
                     help="comma-separated REVIEW_DIMENSIONS key(s) to scope this run to "
                          "(default: every dimension applicable to --kind)")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT,
                     help="clamped to <= REVIEW_MAX_CONCURRENT_CEILING (env, default %d) -- "
                          "dimension fan-out multiplies goal count, so this stays bounded "
                          "regardless of the value passed here" % MAX_CONCURRENT_CEILING)
    ap.add_argument("--effort", choices=["min", "auto"], default="auto")
    ap.add_argument("--no-refute", action="store_true",
                     help="skip the adversarial refuter pass (relay/refuter.py) that runs by "
                          "default: normally every finding is handed to an independent "
                          "skeptic and REFUTED ones are dropped/demoted (costs a second "
                          "fleet run, one goal per finding)")
    ap.add_argument("--refute-panel", action="store_true",
                     help="use a 3-lens refuter panel (relay.refuter.PANEL_LENSES: "
                          "correctness/edge/security, majority vote) per finding instead of "
                          "one lens per finding -- higher cost, more robust")
    ap.add_argument("--verify", action="store_true",
                     help="DEPRECATED alias for the refuter pass (which already runs by "
                          "default) -- also enables --refute-panel, for old callers/scripts")
    ap.add_argument("--behavioral", action="store_true",
                     help="OPT-IN (default OFF): after refutation, run a behavioral-verify "
                          "fleet pass on CONFIRMED findings that tries to actually REPRODUCE "
                          "each one via a READ-ONLY run_python/shell_exec repro instead of "
                          "just reasoning about it. This executes real code -- unlike the "
                          "pure-reasoning refuter it is explicit opt-in, never the default")
    ap.add_argument("--behavioral-severity", default=None,
                     help="comma-separated severity(ies) (low/medium/high) to restrict the "
                          "--behavioral pass to, to bound cost (default: every CONFIRMED "
                          "finding regardless of severity)")
    ap.add_argument("--dry-run", action="store_true",
                     help="build goals and print the plan without launching the fleet")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--loop", action="store_true",
                     help="P3 OPT-IN (default OFF): repeat plan_goals -> run_fleet -> "
                          "(refute) -> (behavioral) across multiple rounds, accumulating "
                          "UNIQUE findings (deduped by normalized file+line+lowercased title, "
                          "so a REFUTED finding never re-triggers a loop-must-continue signal "
                          "by reappearing), until --dry-rounds consecutive rounds add zero new "
                          "findings or --max-rounds is hit (see run_review_loop). Without "
                          "--loop, behavior is IDENTICAL to today's single pass")
    ap.add_argument("--max-rounds", type=int, default=3,
                     help="--loop only: stop after this many rounds even if never dry "
                          "(default 3) -- hitting this cap is always reported, never silent")
    ap.add_argument("--dry-rounds", type=int, default=2,
                     help="--loop only: consecutive rounds with zero new (unseen) findings "
                          "before stopping (default 2)")
    ap.add_argument("--completeness", action="store_true",
                     help="P3 OPT-IN (default OFF): spawn a completeness-critic goal "
                          "(build_completeness_goal) that reports (1) any REVIEW_DIMENSIONS "
                          "key not yet run, (2) any file/area not yet examined, (3) any "
                          "current finding whose claim is asserted but not verified. Runs "
                          "once after a normal single pass; under --loop it also runs after "
                          "each round and seeds the next round's plan_goals with its "
                          "suggested extra dimensions/files. Independent of --loop -- works "
                          "on its own")
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    resilience_profile = args.resilience_profile
    if resilience_profile != "off" and resilience_profile != args.kind:
        print("ERROR: --resilience-profile must be off or match --kind")
        return 2
    if resilience_profile != "off" and args.loop:
        print("ERROR: P2c resilience currently runs as a bounded single campaign; --loop is not "
              "combined with --resilience-profile (use /review-2 or /security-review-2 without --loop).")
        return 2

    repo_root = REPO
    out_dir = _resolve_out_dir(args.out_dir, repo_root)
    stamp = _stamp()

    dimension_keys = None
    if args.dimensions:
        dimension_keys = [s.strip() for s in args.dimensions.split(",") if s.strip()]

    max_concurrent = _clamp_max_concurrent(args.max_concurrent)

    files, groups, goals, goal_meta = plan_goals(
        args.kind, args.mode, repo_root, base_ref=args.base_ref, cached=args.cached,
        target_path=args.target_path, group_size=args.group_size,
        dimension_keys=dimension_keys,
    )
    dims_used = sorted({m["dimension"] for m in goal_meta})

    envelopes = {}
    if resilience_profile != "off":
        goals, envelopes = _prepare_resilience_goals(
            goals, goal_meta, resilience_profile, stamp)

    goals_path = os.path.join(out_dir, "goals_%s.jsonl" % stamp)
    write_goals_jsonl(goals, goals_path)
    cmd = fleet_cmd(goals_path, max_concurrent, args.effort,
                    resilience_profile=(resilience_profile if resilience_profile != "off" else None),
                    max_turns=(12 if resilience_profile != "off" else None))

    if args.dry_run:
        print("DRY RUN -- plan only, fleet NOT launched")
        print("kind=%s mode=%s target-path=%s" %
              (args.kind, args.mode, args.target_path or "(none)"))
        print("files matched: %d" % len(files))
        print("file groups: %d" % len(groups))
        print("dimensions: %s" % (", ".join(dims_used) if dims_used else "(none)"))
        print("goals: %d" % len(goals))
        for i, (g, meta) in enumerate(zip(goals, goal_meta)):
            print("  goal %d/%d: dimension=%s %d file(s): %s" %
                  (i + 1, len(goals), meta["dimension"], len(meta["files"]),
                   ", ".join(meta["files"])))
        if max_concurrent != args.max_concurrent:
            print("max-concurrent clamped: requested=%d effective=%d (ceiling=%d, override "
                  "via REVIEW_MAX_CONCURRENT_CEILING)" %
                  (args.max_concurrent, max_concurrent, MAX_CONCURRENT_CEILING))
        print("goals file: %s" % goals_path)
        print("fleet cmd: %s" % cmd)
        print("report would be written under: %s" % out_dir)
        if args.loop or args.completeness:
            print("note: --loop/--completeness are ignored under --dry-run -- the plan shown "
                  "above is for a single round only")
        return 0

    if not os.path.isfile(VENVPY):
        print("ERROR: .venv python not found at %s -- run quickstart.bat first." % VENVPY)
        return 1

    if not goals:
        print("no files matched --mode %s --target-path %s -- nothing to review" %
              (args.mode, args.target_path or "(none)"))
        return 0

    if args.verify:
        print("--verify is deprecated; routing to the refuter-based pass (which already runs "
              "by default) in panel mode")
    run_refute = not args.no_refute
    panel = args.refute_panel or args.verify
    behavioral_severity_set = None
    if args.behavioral_severity:
        behavioral_severity_set = {s.strip().lower() for s in args.behavioral_severity.split(",")
                                    if s.strip()}

    run_behavioral = args.behavioral or (
        resilience_profile == "security" and not args.no_auto_behavioral_high)
    if resilience_profile == "security" and not args.behavioral \
            and not args.no_auto_behavioral_high:
        behavioral_severity_set = {"high"}

    loop_meta = None
    if args.loop:
        # P3 piece A: loop-until-dry. Everything below this branch (plan_goals/run_fleet for
        # a single pass, and the single-pass refute/behavioral/completeness wiring in the
        # `else` branch) is UNCHANGED from before P3 -- --loop opts into a completely
        # separate code path instead of threading loop state through the old one, so the
        # default (no --loop) behavior can never regress.
        #
        # NOTE: the top-level plan_goals()/goals_path/goals_<stamp>.jsonl computed above (used
        # for the --dry-run preview and the "no files matched" pre-check) is NOT reused here --
        # run_review_loop plans and writes its OWN goals_<stamp>_r<N>.jsonl per round (round 1
        # may add completeness-suggested dimensions/files from a later round's critic, so it
        # cannot just replay the single plan computed before we knew --loop was even set).
        print("launching review LOOP (max_rounds=%d dry_rounds=%d completeness=%s)..." %
              (args.max_rounds, args.dry_rounds, args.completeness))
        agg, loop_meta = run_review_loop(
            args.kind, args.mode, repo_root, out_dir, stamp, base_ref=args.base_ref,
            cached=args.cached, target_path=args.target_path, group_size=args.group_size,
            dimension_keys=dimension_keys, max_concurrent=max_concurrent, effort=args.effort,
             run_refute=run_refute, panel=panel, run_behavioral=run_behavioral,
            behavioral_severity=behavioral_severity_set, completeness=args.completeness,
            max_rounds=args.max_rounds, dry_rounds=args.dry_rounds)
        agg["dimensions_covered"] = dims_used
        agg["loop_meta"] = loop_meta
    else:
        print("launching %d review goal(s) across dimensions: %s..." %
              (len(goals), ", ".join(dims_used)))
        primary_state_dir = (os.path.join(out_dir, "primary_state_%s" % stamp)
                             if resilience_profile != "off"
                             else os.path.join(repo_root, ".fleet"))
        if resilience_profile != "off":
            run_fleet(goals_path, max_concurrent, args.effort, state_dir=primary_state_dir,
                      resilience_profile=resilience_profile, max_turns=12)
        else:
            run_fleet(goals_path, max_concurrent, args.effort)

        status_path = os.path.join(primary_state_dir, "status.json")
        transcripts_dir = os.path.join(primary_state_dir, "transcripts")
        if not os.path.isfile(status_path):
            print("ERROR: fleet run finished but %s was not found -- the fleet run likely "
                  "failed to start or was killed before writing a snapshot; check the .fleet "
                  "logs." % status_path)
            return 1

        agg = aggregate(status_path, transcripts_dir, now=time.time())
        agg["dimensions_covered"] = dims_used

        if resilience_profile != "off":
            recover_content_refusals(
                agg, primary_state_dir, envelopes, resilience_profile, out_dir, stamp,
                max_concurrent, args.effort, repo_root)

        if run_refute:
            findings = agg.get("findings", [])
            if not findings:
                print("no findings to refute; skipping refuter pass")
            else:
                print("launching refuter pass: %d finding(s)%s..." %
                      (len(findings), " (panel)" if panel else ""))
                verdicts = refute_findings(findings, args.kind, out_dir, time.time(),
                                            panel=panel, max_concurrent=max_concurrent,
                                            effort=args.effort, repo_root=repo_root, stamp=stamp,
                                            resilience_profile=(resilience_profile
                                                                if resilience_profile != "off"
                                                                else None))
                merge_verdicts(agg, verdicts)

                if resilience_profile != "off":
                    adjudicate_findings(
                        agg.get("findings", []), args.kind, out_dir, max_concurrent,
                        args.effort, repo_root, stamp, run_fleet)
                    # Make adjudicator-confirmed findings eligible for the behavioral pass.
                    derive_and_attach_finding_states(agg.get("findings", []))

        # P2 piece A: OPT-IN behavioral-verify pass (executes real code, so unlike the refuter
        # above it never runs unless --behavioral was explicitly passed).
        if run_behavioral:
            confirmed_findings = [f for f in agg.get("findings", [])
                                   if f.get("verify_verdict") == "confirmed"]
            severity_filter = behavioral_severity_set
            if severity_filter:
                confirmed_findings = [f for f in confirmed_findings
                                       if str(f.get("severity", "")).lower() in severity_filter]
            if not confirmed_findings:
                print("no CONFIRMED findings to behaviorally verify; skipping --behavioral pass")
            else:
                print("launching behavioral-verify pass: %d confirmed finding(s)%s..." %
                      (len(confirmed_findings),
                       " (severity=%s)" % ",".join(sorted(severity_filter)) if severity_filter
                       else ""))
                behavioral_verify(agg.get("findings", []), out_dir, time.time(),
                                   max_concurrent=max_concurrent, effort=args.effort,
                                   repo_root=repo_root, stamp=stamp, severity_filter=severity_filter,
                                   resilience_profile=(resilience_profile
                                                       if resilience_profile != "off" else None))

        if resilience_profile != "off":
            derive_and_attach_finding_states(agg.get("findings", []))

        # P3 piece B: OPT-IN completeness critic, independent of --loop -- works standalone
        # too (a single extra goal after the single pass above).
        if args.completeness:
            gaps = run_completeness_critic(dims_used, files, agg.get("findings", []), out_dir,
                                            repo_root, effort=args.effort, stamp=stamp)
            agg["completeness_gaps"] = gaps
            if any(gaps.get(k) for k in _EMPTY_GAPS):
                print("completeness critic reported gaps: missing_dimensions=%s "
                      "missing_files=%s unverified_claims=%d" %
                      (gaps.get("missing_dimensions"), gaps.get("missing_files"),
                       len(gaps.get("unverified_claims") or [])))
            else:
                print("completeness critic: no gaps identified")

    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "review_report_%s.md" % stamp)
    json_path = os.path.join(out_dir, "review_report_%s.json" % stamp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(agg))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(render_json(agg), f, ensure_ascii=False, indent=2)

    by_sev = agg.get("by_severity", {})
    print("report: %s" % md_path)
    print("summary: high=%d medium=%d low=%d parse_errors=%d" % (
        len(by_sev.get("high", [])), len(by_sev.get("medium", [])),
        len(by_sev.get("low", [])), agg.get("parse_errors", 0),
    ))
    if run_refute:
        confirmed = sum(1 for f in agg.get("findings", []) if f.get("verify_verdict") == "confirmed")
        refuted = sum(1 for f in agg.get("findings", []) if f.get("verify_verdict") == "false_positive")
        unclear = sum(1 for f in agg.get("findings", []) if f.get("verify_verdict") == "unclear")
        print("refuter: confirmed=%d refuted/dropped=%d unclear=%d" % (confirmed, refuted, unclear))
    if run_behavioral:
        all_findings = agg.get("findings", [])
        reproduced = sum(1 for f in all_findings if f.get("behavioral_verdict") == "reproduced")
        not_repro = sum(1 for f in all_findings if f.get("behavioral_verdict") == "not_reproduced")
        inconclusive = sum(1 for f in all_findings if f.get("behavioral_verdict") == "inconclusive")
        print("behavioral: reproduced=%d not_reproduced=%d inconclusive=%d" %
              (reproduced, not_repro, inconclusive))
    if loop_meta is not None:
        print("loop: rounds_run=%d/%d stopped_reason=%s unique_findings=%d" %
              (loop_meta["rounds_run"], loop_meta["max_rounds"], loop_meta["stopped_reason"],
               loop_meta["unique_findings"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
