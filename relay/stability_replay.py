# -*- coding: utf-8 -*-
"""Replay the owner's own submissions through the connector-less (socket) route and
measure whether the new route stays stable under real use.

Why this exists. The connector-less route (docs/connector_less_route.md) is the default
on a new PC: the agent writes tool calls as fenced blocks and this PC runs them, with no
connector and no tunnel. The owner asked whether its stability is being confirmed with
REAL use -- the sentences the owner actually typed -- rather than with goals a handler
wrote for a test (the owner's standing rule: only what the owner typed may be an input).
This module is the harness that does that confirmation and reports the result.

What it measures, from the fleet's OWN records (not from a synthetic oracle):
  * submit-to-start latency (median / p90 / max), from history.json phase_events;
  * time to completion, from the same phase_events;
  * the STUCK fraction and the breakdown of its reasons, from socket_route.jsonl;
  * whether a 'continue' input reached its ORIGINAL conversation (resume_conv threaded,
    no stray new run), from the payloads and transcripts;
  * server / gateway response gaps (resend_refused, socket_retry, fallback counts);
  * C: free space and committed-memory change across the run.

SAFETY -- the default is a DRY RUN. By default nothing is submitted: the planner only
lists the owner inputs it WOULD send and writes the plan. Real submission happens only
when --execute is passed, and then:
  * --max-concurrent caps how many are in flight at once (default 2);
  * --cpu-threshold refuses to START when the machine's CPU is already above it
    (default 85%), because the owner's recorded failures cluster when the PC is pegged;
  * --interval spaces the submissions (the recorded cadence, which may be compressed).
The load of a real run is large, so the gate is closed unless the caller opens it.

This module imports nothing Windows-only at import time; the CPU probe degrades to
'unknown' off Windows (and in CI), so the hermetic tests run on the Linux runner.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# --------------------------------------------------------------------------------------
# Owner inputs. Only what the owner typed is an input (the standing rule). The verbatim
# file is the primary, provenance-carrying source. The realuse CSV holds 494 recorded
# submissions but has no submitter column, so it CANNOT prove a row was typed by the
# owner -- those rows are offered only as OPTIONAL continuation-shaped candidates and are
# never used unless --include-csv-continuations is passed. The design note records this
# limit; the test suite asserts the default excludes them.
# --------------------------------------------------------------------------------------

_VERBATIM = os.path.join(
    _ROOT, "tests", "realuse_owner_inputs", "owner_verbatim_submissions.jsonl")


@dataclass
class OwnerInput:
    """One line the owner typed, with where it came from."""
    text: str
    resume_conv: str = ""
    source_id: str = ""
    origin: str = ""          # 'verbatim' or 'csv_continuation'
    provenance: dict = field(default_factory=dict)


def load_verbatim_inputs(path: str = _VERBATIM) -> list[OwnerInput]:
    """Read the committed verbatim file -- never inline the owner's words here, so the
    harness and the recorded provenance cannot drift apart."""
    out: list[OwnerInput] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = " ".join(str(rec.get("text") or "").split())
            if not text:
                continue
            out.append(OwnerInput(
                text=text,
                resume_conv=rec.get("resume_conv", "") or "",
                source_id=rec.get("id", "") or "",
                origin="verbatim",
                provenance=rec.get("provenance", {}) or {},
            ))
    return out


def load_csv_continuation_candidates(path: str) -> list[OwnerInput]:
    """Continuation-shaped rows from the realuse CSV. These are NOT proven owner-typed:
    the CSV has no submitter column (see realuse_out/report.md 1.1). Returned only so a
    caller who passes --include-csv-continuations can widen the set deliberately, and
    each is tagged origin='csv_continuation' so the report never conflates them with the
    verbatim owner lines. goal_head200 is truncated at 200 chars, so these are heads, not
    whole goals -- another reason they are candidates, not primary inputs."""
    out: list[OwnerInput] = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # Normalise a possible BOM on the first column name.
            route = (row.get("route") or "").strip()
            head = (row.get("goal_head200") or "").strip()
            if route != "socket" or not head:
                continue
            # A continuation-shaped head: the recorded continue inputs are the JSON
            # {"text":...,"resume_conv":"sess:..."} payloads. Only keep heads that look
            # like a continuation so we do not pull long coding briefs (handler words).
            if "resume_conv" not in head:
                continue
            out.append(OwnerInput(
                text=" ".join(head.split()),
                resume_conv="",
                source_id=(row.get("worker") or "").strip(),
                origin="csv_continuation",
                provenance={"note": "CSV head, truncated 200ch, submitter unknown"},
            ))
    return out


def collect_inputs(include_csv_continuations: bool = False,
                   verbatim_path: str = _VERBATIM,
                   csv_path: str = "") -> list[OwnerInput]:
    """The inputs for a run. Verbatim always; CSV continuation candidates only when the
    caller opts in. The owner's rule is encoded in the default: the proven owner lines
    are the input, and nothing a handler may have written rides in by default."""
    inputs = load_verbatim_inputs(verbatim_path)
    if include_csv_continuations and csv_path:
        inputs = inputs + load_csv_continuation_candidates(csv_path)
    return inputs


# --------------------------------------------------------------------------------------
# Pass criteria. Thresholds are set from today's measurements (realuse_out/report.md),
# not invented. Each criterion carries the measured basis so a reader can check it.
# --------------------------------------------------------------------------------------

@dataclass
class Criterion:
    key: str
    threshold: float
    comparator: str      # 'le' => observed must be <= threshold; 'ge' => >=
    basis: str
    unit: str = ""


#: The bar. Basis strings cite realuse_out/report.md (the only same-PC measurements we
#: have of this route today). Latency floor/p90 come from phase_events (0.2s typical,
#: slow tail 14.5/20.8/27.1s); the STUCK fraction from the 10-25 22:00->10-26 08:40 soak
#: (236 worker_done, 87 STUCK ~= 0.37) which is the WORST observed overnight window --
#: the pass bar is set a little below it so a run that is no worse than that window is not
#: called a regression, and the design note explains the choice.
PASS_CRITERIA: list[Criterion] = [
    Criterion("submit_to_start_p90_s", 30.0, "le",
              "report.md 1.2: latency p90 well under 30s; slow tail maxes at 27.1s. "
              "A p90 above 30s means the start path regressed past its worst recorded tail.",
              "s"),
    Criterion("submit_to_start_median_s", 3.0, "le",
              "report.md 1.2: median 0.2-0.3s, mean 1.8s. 3s leaves headroom over the mean "
              "without admitting the 14-27s tail as typical.", "s"),
    Criterion("stuck_fraction", 0.37, "le",
              "report.md 2.H: overnight 10-25 22:00->10-26 08:40 had 87/236 STUCK (0.369), "
              "almost all 'no close frame'. A run worse than that worst window is a regression.",
              "fraction"),
    Criterion("continue_reached_fraction", 1.0, "ge",
              "report.md 2.C / bug C: every 'continue' input must reach its original "
              "conversation (resume_conv threaded). Anything below 1.0 is a dropped continuation.",
              "fraction"),
    Criterion("resend_refused_non_infra_fraction", 0.0, "le",
              "report.md summary: resend_refused was entirely connection/token-driven, not "
              "user refusals. Any resend refused for a NON-infra reason (dup guard, content) is "
              "a defect this route was meant to remove.", "fraction"),
]


def evaluate(metrics: dict, criteria: Iterable[Criterion] = PASS_CRITERIA) -> dict:
    """Apply the criteria to a metrics dict. A criterion whose metric is missing or None
    is reported as 'unmeasured' and does NOT silently pass -- an absent number is not a
    green light. Returns {passed: bool, results: [...]}. passed is True only when every
    measured criterion holds and none is unmeasured."""
    results = []
    all_ok = True
    for c in criteria:
        obs = metrics.get(c.key)
        if obs is None:
            results.append({"key": c.key, "status": "unmeasured",
                            "threshold": c.threshold, "observed": None,
                            "comparator": c.comparator, "basis": c.basis})
            all_ok = False
            continue
        if c.comparator == "le":
            ok = obs <= c.threshold
        elif c.comparator == "ge":
            ok = obs >= c.threshold
        else:
            raise ValueError("unknown comparator %r" % c.comparator)
        results.append({"key": c.key, "status": "pass" if ok else "fail",
                        "threshold": c.threshold, "observed": obs,
                        "comparator": c.comparator, "basis": c.basis})
        if not ok:
            all_ok = False
    return {"passed": all_ok, "results": results}


# --------------------------------------------------------------------------------------
# Metrics from the fleet's own records. Pure parsers over the jsonl, so a test can feed
# small fixtures and assert the aggregation, with no fleet running.
# --------------------------------------------------------------------------------------

def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def latency_metrics(history: list[dict]) -> dict:
    """submit-to-start and submit-to-done seconds from history.json phase_events.

    Each entry's phase_events carry timestamps for pending (Queued) -> ready (Starting)
    -> waiting (Running); completion is the entry's own done/ts if present. We read the
    seconds the same way report.md 1.2 did, so the numbers are comparable."""
    starts: list[float] = []
    dones: list[float] = []
    for e in history:
        pe = e.get("phase_events") or {}
        t_pending = _as_epoch(pe.get("pending"))
        t_waiting = _as_epoch(pe.get("waiting"))
        if t_pending is not None and t_waiting is not None and t_waiting >= t_pending:
            starts.append(t_waiting - t_pending)
        t_done = _as_epoch(e.get("done") or pe.get("done"))
        if t_pending is not None and t_done is not None and t_done >= t_pending:
            dones.append(t_done - t_pending)
    out = {
        "submit_to_start_n": len(starts),
        "submit_to_start_median_s": statistics.median(starts) if starts else None,
        "submit_to_start_p90_s": _percentile(starts, 90),
        "submit_to_start_max_s": max(starts) if starts else None,
        "submit_to_done_n": len(dones),
        "submit_to_done_median_s": statistics.median(dones) if dones else None,
        "submit_to_done_p90_s": _percentile(dones, 90),
        "submit_to_done_max_s": max(dones) if dones else None,
    }
    return out


def outcome_metrics(worker_done: list[dict]) -> dict:
    """STUCK fraction and the breakdown of STUCK reasons from socket_route worker_done
    rows. The fraction is STUCK over all terminal outcomes seen, matching report.md's
    DONE/STUCK/REFUSED accounting."""
    by_outcome: dict[str, int] = {}
    stuck_reasons: dict[str, int] = {}
    for r in worker_done:
        oc = (r.get("outcome") or "").strip() or "UNKNOWN"
        by_outcome[oc] = by_outcome.get(oc, 0) + 1
        if oc == "STUCK":
            reason = (r.get("reason") or "").strip() or "(no reason)"
            stuck_reasons[reason] = stuck_reasons.get(reason, 0) + 1
    total = sum(by_outcome.values())
    stuck = by_outcome.get("STUCK", 0)
    return {
        "terminal_n": total,
        "outcome_counts": by_outcome,
        "stuck_count": stuck,
        "stuck_fraction": (stuck / total) if total else None,
        "stuck_reason_counts": stuck_reasons,
    }


def resend_metrics(socket_events: list[dict]) -> dict:
    """server/gateway gaps: resend_refused, socket_retry, fallback counts and, for
    resend_refused, how many were NON-infra (not connection/token). report.md records
    that every real resend_refused was infra-driven; a non-infra one is a defect."""
    kinds: dict[str, int] = {}
    refused_non_infra = 0
    _INFRA = ("close frame", "keepalive", "timeout", "timeouterror", "token",
              "getaddrinfo", "dns", "401", "connection", "deadline")
    for ev in socket_events:
        kind = (ev.get("event") or ev.get("kind") or "").strip()
        if not kind:
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "resend_refused":
            reason = (ev.get("reason") or "").lower()
            if not any(tok in reason for tok in _INFRA):
                refused_non_infra += 1
    refused = kinds.get("resend_refused", 0)
    return {
        "event_kind_counts": kinds,
        "resend_refused": refused,
        "socket_retry": kinds.get("socket_retry", 0),
        "fallback": kinds.get("fallback", 0),
        "resend_refused_non_infra": refused_non_infra,
        "resend_refused_non_infra_fraction": (
            (refused_non_infra / refused) if refused else 0.0),
    }


def continuation_metrics(inputs: list[OwnerInput], pending_jobs: list[dict]) -> dict:
    """How many 'continue' inputs reached their original conversation: the submitted job
    must carry the same resume_conv and the owner's text byte-for-byte (bug C). Only
    inputs that carried a resume_conv are counted as continuations."""
    conts = [i for i in inputs if i.resume_conv]
    by_goal = {j.get("payload", {}).get("goal"): j for j in pending_jobs}
    reached = 0
    for i in conts:
        j = by_goal.get(i.text)
        if j is None:
            continue
        if (j.get("payload", {}) or {}).get("resume_conv") == i.resume_conv:
            reached += 1
    return {
        "continue_n": len(conts),
        "continue_reached": reached,
        "continue_reached_fraction": (reached / len(conts)) if conts else None,
    }


def _as_epoch(value: Any) -> Optional[float]:
    """Accept an epoch number or an ISO8601 string; return seconds or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _read_jsonl(path: str, limit: Optional[int] = None) -> list[dict]:
    out: list[dict] = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit is not None and len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------------------
# Resource sampling. CPU/mem/disk read at run start and end; the CPU probe is the START
# gate. Off Windows (and in CI) it returns None so nothing imports a Windows-only module
# at collection time.
# --------------------------------------------------------------------------------------

def sample_resources() -> dict:
    """A point-in-time sample: C: free GB, committed memory GB, CPU percent. Any field
    that cannot be read on this platform is None rather than a guessed number."""
    import shutil
    out: dict[str, Optional[float]] = {"c_free_gb": None, "commit_gb": None,
                                       "cpu_percent": None}
    try:
        out["c_free_gb"] = round(shutil.disk_usage("C:/").free / 1e9, 3)
    except Exception:
        pass
    out["cpu_percent"] = read_cpu_percent()
    try:
        import ctypes

        class _MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _MEMSTAT()
        m.dwLength = ctypes.sizeof(_MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):  # type: ignore[attr-defined]
            committed = (m.ullTotalPageFile - m.ullAvailPageFile)
            out["commit_gb"] = round(committed / 1e9, 3)
    except Exception:
        pass
    return out


def read_cpu_percent() -> Optional[float]:
    """System-wide CPU percent over a short sample, Windows only. None elsewhere, so the
    import and the probe never run on the CI runner."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        class _FT(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        def _get() -> tuple[int, int, int]:
            idle, kern, user = _FT(), _FT(), _FT()
            ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
            def v(ft): return (ft.high << 32) | ft.low
            return v(idle), v(kern), v(user)
        i0, k0, u0 = _get()
        time.sleep(0.25)
        i1, k1, u1 = _get()
        idle = i1 - i0
        busy = (k1 - k0) + (u1 - u0) - idle
        total = busy + idle
        if total <= 0:
            return None
        return round(100.0 * busy / total, 1)
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# The planner (dry run) and the executor (gated).
# --------------------------------------------------------------------------------------

@dataclass
class ReplayConfig:
    execute: bool = False
    max_concurrent: int = 2
    cpu_threshold: float = 85.0
    interval_s: float = 2.0
    include_csv_continuations: bool = False
    csv_path: str = ""
    verbatim_path: str = _VERBATIM
    out_dir: str = ""
    source_tag: str = "stability_replay"


def build_plan(cfg: ReplayConfig) -> dict:
    """The dry-run plan: the exact inputs that WOULD be submitted, in order, with the
    pacing and the gate settings. No side effects."""
    inputs = collect_inputs(cfg.include_csv_continuations, cfg.verbatim_path, cfg.csv_path)
    plan_items = []
    for idx, i in enumerate(inputs):
        plan_items.append({
            "order": idx,
            "source_id": i.source_id,
            "origin": i.origin,
            "is_continuation": bool(i.resume_conv),
            "resume_conv": i.resume_conv,
            "text": i.text,
            "provenance": i.provenance,
        })
    return {
        "mode": "execute" if cfg.execute else "dry-run",
        "input_count": len(inputs),
        "verbatim_count": sum(1 for i in inputs if i.origin == "verbatim"),
        "csv_candidate_count": sum(1 for i in inputs if i.origin == "csv_continuation"),
        "continuation_count": sum(1 for i in inputs if i.resume_conv),
        "gate": {
            "max_concurrent": cfg.max_concurrent,
            "cpu_threshold": cfg.cpu_threshold,
            "interval_s": cfg.interval_s,
        },
        "items": plan_items,
    }


def run_replay(cfg: ReplayConfig,
               submit_fn: Optional[Callable[..., str]] = None,
               cpu_fn: Optional[Callable[[], Optional[float]]] = None,
               sleep_fn: Callable[[float], None] = time.sleep) -> dict:
    """Execute (or dry-run) the plan and return a record of what happened.

    submit_fn/cpu_fn are injectable so the test drives this with no fleet and no real
    CPU. The default submit_fn is tools.fleet_intake.fleet_submit; the default cpu_fn is
    read_cpu_percent. The CPU gate and the concurrency cap are enforced here, before any
    submission, so the test can prove the harness refuses to start on a hot machine.
    """
    plan = build_plan(cfg)
    rec: dict[str, Any] = {"plan": plan, "submitted": [], "skipped": [],
                           "started": _now_iso(), "execute": cfg.execute}
    if not cfg.execute:
        rec["note"] = "dry-run: no submission performed"
        return rec

    cpu_fn = cpu_fn or read_cpu_percent
    cpu = cpu_fn()
    rec["cpu_at_start"] = cpu
    if cpu is not None and cpu > cfg.cpu_threshold:
        rec["aborted"] = (
            "CPU %.1f%% exceeds the %.1f%% start threshold; not starting (the owner's "
            "recorded failures cluster when the PC is pegged)." % (cpu, cfg.cpu_threshold))
        return rec

    if submit_fn is None:
        from tools import fleet_intake as FI
        submit_fn = FI.fleet_submit

    inputs = collect_inputs(cfg.include_csv_continuations, cfg.verbatim_path, cfg.csv_path)
    in_flight = 0
    for item, i in zip(plan["items"], inputs):
        # Re-check the CPU gate before each wave, not only at the start.
        cpu = cpu_fn()
        if cpu is not None and cpu > cfg.cpu_threshold:
            rec["skipped"].append({"source_id": i.source_id, "why":
                                   "cpu %.1f%% over threshold" % cpu})
            continue
        try:
            job = submit_fn(i.text, source=cfg.source_tag, resume_conv=i.resume_conv)
        except Exception as exc:  # a submit failure is data, not a crash
            rec["skipped"].append({"source_id": i.source_id, "why": "submit raised: %s" % exc})
            continue
        rec["submitted"].append({"source_id": i.source_id, "result": job,
                                 "resume_conv": i.resume_conv})
        in_flight += 1
        if in_flight >= cfg.max_concurrent:
            sleep_fn(cfg.interval_s)
            in_flight = 0
        else:
            sleep_fn(cfg.interval_s)
    rec["finished"] = _now_iso()
    return rec


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# Report writers (md + csv).
# --------------------------------------------------------------------------------------

def gather_metrics(history_path: str = "", socket_route_path: str = "",
                   socket_events_path: str = "", inputs: Optional[list[OwnerInput]] = None,
                   pending_jobs: Optional[list[dict]] = None,
                   res_start: Optional[dict] = None,
                   res_end: Optional[dict] = None) -> dict:
    """Aggregate every metric from the given record paths. Missing files yield empty
    parses and the dependent metrics become None (unmeasured), never a fabricated zero."""
    history = _read_jsonl(history_path) if history_path else []
    worker_done = [r for r in (_read_jsonl(socket_route_path) if socket_route_path else [])
                   if (r.get("event") or r.get("kind") or "") in ("worker_done", "")
                   and r.get("outcome")]
    socket_events = _read_jsonl(socket_events_path) if socket_events_path else []
    m: dict[str, Any] = {}
    m.update(latency_metrics(history))
    m.update(outcome_metrics(worker_done))
    m.update(resend_metrics(socket_events))
    if inputs is not None and pending_jobs is not None:
        m.update(continuation_metrics(inputs, pending_jobs))
    if res_start is not None and res_end is not None:
        m["c_free_gb_start"] = res_start.get("c_free_gb")
        m["c_free_gb_end"] = res_end.get("c_free_gb")
        if res_start.get("c_free_gb") is not None and res_end.get("c_free_gb") is not None:
            m["c_free_gb_delta"] = round(res_end["c_free_gb"] - res_start["c_free_gb"], 3)
        m["commit_gb_start"] = res_start.get("commit_gb")
        m["commit_gb_end"] = res_end.get("commit_gb")
        if res_start.get("commit_gb") is not None and res_end.get("commit_gb") is not None:
            m["commit_gb_delta"] = round(res_end["commit_gb"] - res_start["commit_gb"], 3)
    return m


def write_reports(out_dir: str, plan: dict, metrics: dict, verdict: dict) -> dict:
    """Write report.md and metrics.csv into out_dir. Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "report.md")
    csv_path = os.path.join(out_dir, "metrics.csv")
    lines = []
    lines.append("# New-route stability replay -- report")
    lines.append("")
    lines.append("Mode: **%s**. Inputs: %d (verbatim %d, csv-candidate %d, continuations %d)."
                 % (plan.get("mode"), plan.get("input_count", 0),
                    plan.get("verbatim_count", 0), plan.get("csv_candidate_count", 0),
                    plan.get("continuation_count", 0)))
    gate = plan.get("gate", {})
    lines.append("Gate: max_concurrent=%s, cpu_threshold=%s%%, interval=%ss."
                 % (gate.get("max_concurrent"), gate.get("cpu_threshold"),
                    gate.get("interval_s")))
    lines.append("")
    lines.append("## Verdict: %s" % ("PASS" if verdict.get("passed") else "FAIL"))
    lines.append("")
    lines.append("| criterion | observed | comparator | threshold | status | basis |")
    lines.append("|---|---|---|---|---|---|")
    for r in verdict.get("results", []):
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r["key"], r.get("observed"), r.get("comparator"), r.get("threshold"),
            r.get("status"), r.get("basis", "").replace("|", "/")))
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    for k in sorted(metrics):
        v = metrics[k]
        if isinstance(v, dict):
            lines.append("- **%s**:" % k)
            for kk in sorted(v):
                lines.append("  - %s: %s" % (kk, v[kk]))
        else:
            lines.append("- **%s**: %s" % (k, v))
    lines.append("")
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    # Flat csv: one row per scalar metric and per verdict criterion.
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "key", "value", "extra"])
        for k in sorted(metrics):
            v = metrics[k]
            if isinstance(v, dict):
                for kk in sorted(v):
                    w.writerow(["metric", "%s.%s" % (k, kk), v[kk], ""])
            else:
                w.writerow(["metric", k, v, ""])
        for r in verdict.get("results", []):
            w.writerow(["criterion", r["key"], r.get("observed"),
                        "%s %s %s" % (r.get("status"), r.get("comparator"),
                                      r.get("threshold"))])
    return {"md": md_path, "csv": csv_path}


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Replay the owner's own submissions through the connector-less route "
                    "and measure its stability. DRY RUN by default.")
    ap.add_argument("--execute", action="store_true",
                    help="actually submit (default is a dry run that submits nothing)")
    ap.add_argument("--max-concurrent", type=int, default=2,
                    help="cap on submissions in flight at once (only with --execute)")
    ap.add_argument("--cpu-threshold", type=float, default=85.0,
                    help="refuse to START when system CPU%% is above this (only with --execute)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between submissions (the recorded cadence, may be compressed)")
    ap.add_argument("--include-csv-continuations", action="store_true",
                    help="widen inputs with continuation-shaped CSV heads (NOT proven "
                         "owner-typed; off by default)")
    ap.add_argument("--csv", default="", help="path to submissions_socket_2426.csv")
    ap.add_argument("--verbatim", default=_VERBATIM,
                    help="path to owner_verbatim_submissions.jsonl")
    ap.add_argument("--out", default="", help="output directory for report.md / metrics.csv")
    ap.add_argument("--history", default="", help="history.json(l) for latency metrics")
    ap.add_argument("--socket-route", default="", help="socket_route.jsonl for outcomes")
    ap.add_argument("--socket-events", default="",
                    help="socket events jsonl for resend/retry/fallback")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    cfg = ReplayConfig(
        execute=args.execute,
        max_concurrent=args.max_concurrent,
        cpu_threshold=args.cpu_threshold,
        interval_s=args.interval,
        include_csv_continuations=args.include_csv_continuations,
        csv_path=args.csv,
        verbatim_path=args.verbatim,
        out_dir=args.out,
    )
    res_start = sample_resources() if args.execute else None
    rec = run_replay(cfg)
    res_end = sample_resources() if args.execute else None

    inputs = collect_inputs(cfg.include_csv_continuations, cfg.verbatim_path, cfg.csv_path)
    metrics = gather_metrics(
        history_path=args.history, socket_route_path=args.socket_route,
        socket_events_path=args.socket_events, inputs=inputs, pending_jobs=[],
        res_start=res_start, res_end=res_end)
    verdict = evaluate(metrics)

    if cfg.out_dir:
        paths = write_reports(cfg.out_dir, rec["plan"], metrics, verdict)
        # Also drop the raw run record next to the report.
        from tools import state_store

        def replace_record(previous: dict) -> None:
            previous.clear()
            previous.update(rec)

        state_store.update_json(
            os.path.join(cfg.out_dir, "run_record.json"), replace_record,
            validate=lambda value: isinstance(value, dict), count=len, default=dict,
            allow_shrink=True)
        print("wrote %s and %s" % (paths["md"], paths["csv"]))
    else:
        print(json.dumps({"plan": rec["plan"], "verdict": verdict}, ensure_ascii=False,
                         indent=2))
    return 0 if verdict.get("passed") else 0  # reporting tool: never fail the shell on verdict


if __name__ == "__main__":
    raise SystemExit(main())
