"""HONEST SWE-bench batch scorecard generator (READ-ONLY).

Reusable, side-effect-free reporter. It NEVER runs an eval, NEVER touches docker, and NEVER
spawns/kills a process. It only reads logs/status/spec to print an honest disposition scorecard
for a batch, intended to be run at any time -- including while the fleet is mid-run.

Sources (all read-only):
  * .fleet/swe/batch_30.txt           -- the canonical instance list (the denominator universe)
  * .fleet/swe/run_until_done.log     -- orchestrator log: run-blocks, rounds, chunk launches,
                                         and authoritative 'RESOLVED: <inst>' lines
  * .fleet/status.json                -- the MOST RECENT fleet launch's live worker snapshot
                                         (used to surface in-progress / just-resolved workers
                                         that the log has not yet recorded)
  * .fleet/swe/batch_30_spec.json     -- instance_id -> repo mapping for the repo breakdown

Disposition per instance (intersected with the batch list):
  RESOLVED      -- a 'RESOLVED: <inst>' line in the log, OR a live status.json worker with
                   outcome==DONE and verified==True.
  IN-PROGRESS   -- a live status.json worker that is currently running/verifying (not yet DONE)
                   for an instance that is not already RESOLVED.
  STUCK         -- attempted (appeared in >=1 chunk launch) but never resolved and not currently
                   in-progress (e.g. exhausted its rounds / orchestrator moved on).
  NOT-ATTEMPTED -- in the batch list but never appeared in any chunk launch in the log.

First-pass (pass@1) vs loop-inclusive:
  Each 'launched fleet_runner' line is ONE fleet launch == one verify opportunity for every
  instance in that launch's chunk. We assign each instance the launch index at which it FIRST
  appeared in a chunk (first_launch) and the launch index at which it was RESOLVED
  (resolved_launch). An instance is FIRST-PASS iff resolved_launch == first_launch, i.e. it
  resolved on the very first fleet launch that ever attempted it. If it only resolved on a LATER
  launch (a re-run after a failed verify), it counts toward loop-inclusive but NOT first-pass.
  Denominator for both rates = ATTEMPTED instances (RESOLVED + STUCK + IN-PROGRESS). NOT-ATTEMPTED
  are excluded from the rate and reported separately so the numbers are honest.

Known false-negative adjustment:
  sphinx-doc__sphinx-8595 is a CONFIRMED eval false-negative: in-container it actually passes
  ('1 passed', exit 0) but swebench mis-grades it as failing. The scorecard prints BOTH a RAW
  line (counts it unresolved/stuck, matching the grader) and an ADJUSTED line (counts it
  resolved), each clearly labeled, so the report is honest in both directions.

Usage:
  python bench/swe_scorecard.py
  python bench/swe_scorecard.py --batch batch_30.txt --spec batch_30_spec.json
"""
import argparse
import json
import os
import re
import sys

REPO = r"C:\Users\USER\companion-mcp"
SWEDIR = os.path.join(REPO, ".fleet", "swe")
STATUS = os.path.join(REPO, ".fleet", "status.json")
DEFAULT_LOG = os.path.join(SWEDIR, "run_until_done.log")

# Confirmed eval false-negatives: instance actually passes in-container, swebench mis-grades.
KNOWN_FALSE_NEGATIVES = {
    "sphinx-doc__sphinx-8595": "in-container '1 passed, exit 0'; swebench mis-grades as failing",
}


def _resolve(path):
    """Allow a bare filename (resolved under .fleet/swe) or an absolute/relative path."""
    if os.path.isabs(path):
        return path
    cand = os.path.join(SWEDIR, path)
    if os.path.exists(cand):
        return cand
    return os.path.join(REPO, path)


def parse_args():
    ap = argparse.ArgumentParser(description="Honest read-only SWE-bench batch scorecard.")
    ap.add_argument("--batch", default="batch_30.txt",
                    help="instance-list file (bare name resolved under .fleet/swe)")
    ap.add_argument("--log", default=DEFAULT_LOG, help="orchestrator run_until_done.log")
    ap.add_argument("--spec", default="batch_30_spec.json",
                    help="batch spec JSON for instance->repo mapping (optional)")
    ap.add_argument("--status", default=STATUS, help="live fleet status.json (optional)")
    return ap.parse_args()


def load_batch(path):
    with open(path, encoding="utf-8") as f:
        # preserve order, de-dup
        seen, out = set(), []
        for ln in f:
            t = ln.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def load_repo_map(spec_path, batch):
    """instance_id -> repo. Falls back to deriving 'owner/proj' from the instance id."""
    rmap = {}
    try:
        data = json.load(open(spec_path, encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("instances", data.values())
        for it in items:
            if isinstance(it, dict) and it.get("instance_id"):
                rmap[it["instance_id"]] = it.get("repo", "")
    except Exception:
        pass
    for inst in batch:
        if not rmap.get(inst):
            # 'owner__proj-1234' -> 'owner/proj'
            base = inst.rsplit("-", 1)[0]
            rmap[inst] = base.replace("__", "/", 1)
    return rmap


def parse_log(log_path, batch):
    """Walk the orchestrator log at chunk-launch granularity.

    Returns (first_launch, resolved_launch) dicts keyed by instance, plus the ordered list of
    instances that ever appeared in a chunk (attempted)."""
    bset = set(batch)
    first_launch, resolved_launch = {}, {}
    attempted = []
    launch = 0
    pending = []  # instances named in the most recent 'chunk N:' line, awaiting a launch
    try:
        lines = open(log_path, encoding="utf-8").read().splitlines()
    except Exception:
        lines = []
    for ln in lines:
        cm = re.search(r"chunk \d+:\s*\[(.*)\]", ln)
        if cm:
            pending = [t for t in re.findall(r"'([^']+)'", cm.group(1)) if t in bset]
            continue
        if "launched fleet_runner" in ln:
            launch += 1
            for t in pending:
                if t not in first_launch:
                    first_launch[t] = launch
                    attempted.append(t)
            pending = []
            continue
        rm = re.search(r"RESOLVED:\s*(\S+)", ln)
        if rm and rm.group(1) in bset:
            resolved_launch.setdefault(rm.group(1), launch)
    return first_launch, resolved_launch, attempted


def parse_status(status_path, batch):
    """Live snapshot of the MOST RECENT fleet launch. Returns dicts:
       live_resolved (DONE+verified) and live_inprogress (running/verifying, not done)."""
    bset = set(batch)
    live_resolved, live_inprogress = {}, {}
    meta = {}
    try:
        d = json.load(open(status_path, encoding="utf-8"))
    except Exception:
        return live_resolved, live_inprogress, meta
    meta = {k: d.get(k) for k in ("running", "done_count", "total", "avail_mb", "max_concurrent")}
    for w in d.get("workers", []):
        inst = ""
        m = re.search(r"wt_([A-Za-z0-9_.-]+)", w.get("goal", "") or "")
        if m:
            inst = m.group(1)
        if not inst:
            inst = (w.get("cwd", "") or "").split("wt_")[-1]
        if inst not in bset:
            continue
        status = (w.get("status") or "").lower()
        # any non-terminal worker status the orchestrator uses while actively working an
        # instance in the current fleet launch (pending=queued, generating=LLM turn running,
        # verifying=swe_check running, ready/waiting=between turns)
        active = ("running", "ready", "verifying", "generating", "pending", "waiting")
        if w.get("outcome") == "DONE" and w.get("verified"):
            live_resolved[inst] = w.get("name", "?")
        elif status in active and not w.get("closed"):
            live_inprogress[inst] = "%s/%s" % (w.get("name", "?"), status)
    return live_resolved, live_inprogress, meta


def disposition(batch, first_launch, resolved_launch, live_resolved, live_inprogress):
    """Return ordered list of (instance, disposition, detail)."""
    out = []
    for inst in batch:
        if inst in resolved_launch or inst in live_resolved:
            via = []
            if inst in resolved_launch:
                via.append("log L%d" % resolved_launch[inst])
            if inst in live_resolved:
                via.append("live %s" % live_resolved[inst])
            out.append((inst, "RESOLVED", "+".join(via)))
        elif inst in live_inprogress:
            out.append((inst, "IN-PROGRESS", live_inprogress[inst]))
        elif inst in first_launch:
            out.append((inst, "STUCK", "first attempt L%d, no resolve" % first_launch[inst]))
        else:
            out.append((inst, "NOT-ATTEMPTED", ""))
    return out


def is_first_pass(inst, first_launch, resolved_launch, live_resolved):
    """True iff the instance resolved on the very first fleet launch that attempted it.

    A live-only resolve (status.json DONE+verified, not yet in the log) that occurred on the
    instance's first attempt also counts as first-pass; if the log already records a LATER
    resolve launch than the first attempt, it's loop-inclusive only."""
    fl = first_launch.get(inst)
    rl = resolved_launch.get(inst)
    if rl is not None and fl is not None:
        return rl == fl
    # resolved only in the live snapshot (log lag): first-pass iff this is its first attempt
    # (i.e. it was never seen resolving on a later launch and only just finished its first one).
    if inst in live_resolved and fl is not None:
        return True
    return False


def pct(n, d):
    return "%.1f%%" % (100.0 * n / d) if d else "n/a"


def main():
    a = parse_args()
    batch_path = _resolve(a.batch)
    spec_path = _resolve(a.spec)
    batch = load_batch(batch_path)
    repo_map = load_repo_map(spec_path, batch)
    first_launch, resolved_launch, _attempted = parse_log(a.log, batch)
    live_resolved, live_inprogress, meta = parse_status(a.status, batch)
    disp = disposition(batch, first_launch, resolved_launch, live_resolved, live_inprogress)
    dmap = {i: d for i, d, _ in disp}

    W = 78
    bar = "=" * W

    def line(s=""):
        # ascii-only safe under PYTHONIOENCODING=ascii:replace
        print(s)

    line(bar)
    line("HONEST SWE-bench SCORECARD  --  batch: %s" % os.path.basename(batch_path))
    line("READ-ONLY report (no eval / no docker / no process ops)")
    line(bar)

    counts = {"RESOLVED": 0, "IN-PROGRESS": 0, "STUCK": 0, "NOT-ATTEMPTED": 0}
    for _i, d, _x in disp:
        counts[d] += 1
    total = len(batch)
    attempted_n = counts["RESOLVED"] + counts["IN-PROGRESS"] + counts["STUCK"]

    # ---- per-instance table ----
    line("")
    line("PER-INSTANCE DISPOSITION")
    line("-" * W)
    line("%-42s %-13s %s" % ("instance", "disposition", "detail"))
    line("-" * W)
    for inst, d, detail in disp:
        fn = "  [KNOWN FALSE-NEG]" if inst in KNOWN_FALSE_NEGATIVES else ""
        line("%-42s %-13s %s%s" % (inst, d, detail, fn))
    line("-" * W)

    # ---- disposition summary ----
    line("")
    line("DISPOSITION SUMMARY  (batch total = %d)" % total)
    line("-" * W)
    for k in ("RESOLVED", "IN-PROGRESS", "STUCK", "NOT-ATTEMPTED"):
        line("  %-14s %3d   (%s of batch)" % (k, counts[k], pct(counts[k], total)))
    line("  %-14s %3d" % ("attempted", attempted_n))

    # ---- first-pass vs loop-inclusive (denominator = attempted) ----
    resolved_insts = [i for i, d, _ in disp if d == "RESOLVED"]
    fp_insts = [i for i in resolved_insts
                if is_first_pass(i, first_launch, resolved_launch, live_resolved)]
    loop_insts = [i for i in resolved_insts if i not in fp_insts]

    line("")
    line("PASS RATES  (denominator = ATTEMPTED = %d; NOT-ATTEMPTED %d excluded)"
         % (attempted_n, counts["NOT-ATTEMPTED"]))
    line("-" * W)
    line("  RAW (matches swebench grader):")
    line("    first-pass pass@1 : %2d / %2d  = %s"
         % (len(fp_insts), attempted_n, pct(len(fp_insts), attempted_n)))
    line("    loop-inclusive    : %2d / %2d  = %s"
         % (len(resolved_insts), attempted_n, pct(len(resolved_insts), attempted_n)))

    # ---- known false-negative adjustment ----
    adj_fp = list(fp_insts)
    adj_resolved = list(resolved_insts)
    adj_notes = []
    for inst, reason in KNOWN_FALSE_NEGATIVES.items():
        if inst not in batch:
            continue
        if inst not in adj_resolved:
            adj_resolved.append(inst)
            adj_notes.append("%s -> +resolved (%s)" % (inst, reason))
            # a false-neg that was attempted but mis-graded: treat as first-pass-equivalent
            # only if it would have resolved on its first attempt is unknowable, so we credit
            # it to loop-inclusive AND first-pass adjusted conservatively as a resolve, and
            # note it; we add to adjusted resolved but NOT silently to first-pass.
    line("")
    line("ADJUSTED for KNOWN eval false-negatives:")
    if adj_notes:
        for n in adj_notes:
            line("    " + n)
    else:
        line("    (none applicable to this batch / already counted)")
    line("    loop-inclusive ADJ: %2d / %2d  = %s"
         % (len(adj_resolved), attempted_n, pct(len(adj_resolved), attempted_n)))
    line("    NOTE: first-pass pass@1 is left at the RAW %s -- a false-negative's first-attempt"
         % pct(len(fp_insts), attempted_n))
    line("          status is not knowable from the grader, so it is credited only to the")
    line("          loop-inclusive ADJUSTED line, never silently to pass@1.")

    # ---- repo breakdown ----
    line("")
    line("REPO-LEVEL BREAKDOWN  (resolved / attempted ; in-batch total)")
    line("-" * W)
    repos = {}
    for inst, d, _x in disp:
        r = repo_map.get(inst, "?")
        e = repos.setdefault(r, {"total": 0, "att": 0, "res": 0, "ip": 0, "stuck": 0, "na": 0})
        e["total"] += 1
        if d == "RESOLVED":
            e["res"] += 1; e["att"] += 1
        elif d == "IN-PROGRESS":
            e["ip"] += 1; e["att"] += 1
        elif d == "STUCK":
            e["stuck"] += 1; e["att"] += 1
        else:
            e["na"] += 1
    line("%-26s %8s %6s %8s %6s %6s" % ("repo", "res/att", "rate", "in-prog", "stuck", "n/a"))
    line("-" * W)
    for r in sorted(repos):
        e = repos[r]
        line("%-26s %4d/%-3d %6s %8d %6d %6d"
             % (r, e["res"], e["att"], pct(e["res"], e["att"]), e["ip"], e["stuck"], e["na"]))
    line("-" * W)

    # ---- live fleet context ----
    line("")
    line("LIVE FLEET CONTEXT  (most-recent status.json snapshot)")
    line("-" * W)
    if meta:
        line("  running=%s  done_count=%s  total=%s  max_concurrent=%s  avail_mb=%s"
             % (meta.get("running"), meta.get("done_count"), meta.get("total"),
                meta.get("max_concurrent"), meta.get("avail_mb")))
    else:
        line("  (status.json unavailable or unreadable)")
    if live_inprogress:
        for inst, det in live_inprogress.items():
            line("  IN-PROGRESS now: %-42s (%s)" % (inst, det))
    if live_resolved:
        for inst, nm in live_resolved.items():
            tag = "" if inst in resolved_launch else "  [live-only; not yet in log]"
            line("  live-RESOLVED  : %-42s (worker %s)%s" % (inst, nm, tag))
    if not live_inprogress and not live_resolved:
        line("  (no batch instances in the current live snapshot)")
    line(bar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
