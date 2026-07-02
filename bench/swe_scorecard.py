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

First-pass (pass@1) -- PRIMARY = TRANSCRIPT RECONSTRUCTION (restart-proof):
  First-pass means "the FIRST DONE the agent claimed satisfied the local acceptance gate on the
  FIRST verify attempt." We reconstruct this directly from the per-worker transcripts
  (.fleet/transcripts/<run_id>_<name>.jsonl), NOT from log launch ordering. In each transcript we
  count VERIFY_FIX_JOB user turns -- relay_fleet sends that message (and increments
  verify_attempts) EXACTLY when a DONE claim is rejected by swe_check -- so the count == failed
  verify attempts in that run. A transcript is a first-pass run iff it reached a genuine DONE and
  recorded ZERO verify-fix turns. Because the disk-incident re-starts (slice6 -> KEEP_ENV ->
  no-KEEP_ENV -> continuous) scatter one instance across several run_ids, we aggregate over ALL
  of an instance's transcripts and call it first-pass iff ANY run was a first-pass run ("resolved
  on the first verify attempt in at least one run"). RESOLVED instances whose transcripts never
  reached a parseable DONE (truncated by a restart) are 'first-pass UNDETERMINED': excluded from
  the first-pass numerator AND denominator, but still counted as loop-inclusive resolved. RESOLVED
  is cross-checked against the log 'RESOLVED:' lines (themselves status.json verified==True).

First-pass (pass@1) -- LEGACY launch-index (KNOWN-BROKEN here, kept for comparison only):
  Each 'launched fleet_runner' line is one fleet launch; an instance was deemed first-pass iff its
  resolved_launch == first_launch. This is UNRELIABLE for the holdout run for two independent
  reasons: (1) the holdout log writes 'chunk N (repo=...): [...]' which the 'chunk N:' parser does
  not match, so first_launch is never populated; and (2) the launch counter never resets across the
  four run_until_done re-starts, so first_launch and resolved_launch sit in different epochs.
  Either alone pins legacy first-pass at ~0%, which is an artifact, not a real pass@1.

Loop-inclusive (unchanged, log/status-driven): resolved at all, after any number of verify
  re-tries. Denominator for the loop-inclusive rate = ATTEMPTED (RESOLVED + STUCK + IN-PROGRESS);
  NOT-ATTEMPTED are excluded and reported separately so the numbers are honest.

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
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
STATUS = os.path.join(REPO, ".fleet", "status.json")
TRANSCRIPTS = os.path.join(REPO, ".fleet", "transcripts")
DEFAULT_LOG = os.path.join(SWEDIR, "run_until_done.log")

# Confirmed eval false-negatives: instance actually passes in-container, swebench mis-grades.
KNOWN_FALSE_NEGATIVES = {
    "sphinx-doc__sphinx-8595": "in-container '1 passed, exit 0'; swebench mis-grades as failing",
}

# Network-dependent instances: their canonical fail_to_pass tests reach the live internet
# (httpbin.org etc.). On an OFFLINE eval box these can fail for environmental reasons that have
# nothing to do with the patch, so a non-resolve is NOT necessarily a model miss. We never
# silently credit them; we only break them out with a note so the headline rate stays honest.
# Repo-prefix match keeps it future-proof as new requests instances enter a batch.
NETWORK_DEPENDENT_PREFIXES = ("psf__requests",)

# Unique substring of relay_fleet's VERIFY_FIX_JOB (copilot_autopilot_relay.VERIFY_FIX_JOB).
# It is sent as a USER turn ONLY when a DONE claim was REJECTED by the local acceptance gate
# (swe_check). It never appears in the standard per-turn prompt, so each occurrence in a
# transcript's user turns == exactly one failed verify attempt for that instance in that run.
VERIFY_FIX_MARKER = "あなたは DONE と報告しましたが"


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
    ap.add_argument("--transcripts", default=TRANSCRIPTS,
                    help="per-worker transcript dir for the restart-proof first-pass recon")
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


def _is_done_assistant(text):
    """A genuine DONE claim: the reply contains DONE and its LAST line is not a FAIL/STUCK
    line (mirrors relay_fleet._decide: `"DONE" in up and "FAIL" not in last_line`)."""
    if not text or "DONE" not in text.upper():
        return False
    last = (text.strip().splitlines() or [""])[-1].upper()
    return "FAIL" not in last


def scan_transcripts(batch, tx_dir=TRANSCRIPTS):
    """Reconstruct, per instance, the first-pass evidence directly from the per-worker
    transcripts (one .jsonl per run+worker). This is RESTART-PROOF: it keys off the actual
    verify feedback recorded in each conversation, not the orchestrator log's launch ordering
    (which is corrupted by the slice6 -> KEEP_ENV -> no-KEEP_ENV -> continuous re-starts).

    For each transcript we count:
      * done_claims    -- assistant turns that genuinely claim DONE
      * verify_fails   -- user turns carrying VERIFY_FIX_MARKER == a DONE that the LOCAL
                          acceptance gate (swe_check) rejected. relay_fleet increments
                          verify_attempts and re-injects VERIFY_FIX_JOB exactly here, so the
                          number of these turns == the number of failed verify attempts in
                          that run.

    A transcript represents a FIRST-PASS run for its instance iff it reached >=1 genuine DONE
    claim AND recorded ZERO verify-fail turns (i.e. the very first DONE the agent emitted
    satisfied the gate on the first try). We aggregate per instance over ALL its transcripts
    (the disk-incident re-runs scatter one instance across several run_ids) and take the BEST
    run: an instance is transcript-first-pass iff ANY of its transcripts is a first-pass run
    (== "resolved on the first verify attempt in at least one run", per the agreed definition).

    Returns dict: instance -> {
        'n_tx', 'min_verify_fails' (over DONE-bearing tx; None if none reached DONE),
        'any_first_pass' (bool), 'total_done', 'total_verify_fails' }.
    Instances with no DONE-bearing transcript get min_verify_fails=None -> caller treats them
    as 'first-pass UNDETERMINED' (transcript missing/ambiguous), never as a first-pass nor a
    loop-only miss. This does NOT touch loop-inclusive, which stays log/status-driven."""
    bset = set(batch)
    agg = {}
    try:
        files = sorted(glob.glob(os.path.join(tx_dir, "*.jsonl")))
    except Exception:
        files = []
    for path in files:
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            continue
        meta = next((r for r in rows if r.get("meta")), None)
        if not meta:
            continue
        m = re.search(r"wt_([A-Za-z0-9_.\-]+)", meta.get("goal", "") or "")
        if not m:
            continue
        inst = m.group(1)
        if inst not in bset:
            continue
        n_done = sum(1 for r in rows
                     if r.get("role") == "assistant" and _is_done_assistant(r.get("text")))
        n_vfail = sum(1 for r in rows
                      if r.get("role") == "user"
                      and VERIFY_FIX_MARKER in (r.get("text") or ""))
        e = agg.setdefault(inst, {"n_tx": 0, "done_bearing": [], "total_done": 0,
                                  "total_verify_fails": 0})
        e["n_tx"] += 1
        e["total_done"] += n_done
        e["total_verify_fails"] += n_vfail
        if n_done >= 1:
            e["done_bearing"].append(n_vfail)
    for inst, e in agg.items():
        db = e.pop("done_bearing")
        e["min_verify_fails"] = min(db) if db else None
        e["any_first_pass"] = bool(db) and min(db) == 0
    return agg


def transcript_first_pass(resolved_insts, tx_agg):
    """Partition the (loop-inclusive) RESOLVED instances using the transcript reconstruction.

    Returns (first_pass, loop_only, undetermined):
      first_pass   -- resolved AND has a transcript run with a DONE and zero verify-fails
      loop_only    -- resolved AND every DONE-bearing transcript needed >=1 verify-fix
      undetermined -- resolved but NO transcript reached a parseable DONE (transcript missing
                      or truncated by the disk-incident restart) -> first-pass not knowable.
                      Excluded from the first-pass numerator AND denominator; still counts as
                      loop-inclusive resolved."""
    fp, loop, undet = [], [], []
    for inst in resolved_insts:
        e = tx_agg.get(inst)
        if not e or e.get("min_verify_fails") is None:
            undet.append(inst)
        elif e["any_first_pass"]:
            fp.append(inst)
        else:
            loop.append(inst)
    return fp, loop, undet


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

    # Restart-proof first-pass reconstruction from per-worker transcripts (computed once;
    # used both in the per-instance table and the PASS RATES section below).
    resolved_insts = [i for i, d, _ in disp if d == "RESOLVED"]
    tx_agg = scan_transcripts(batch, a.transcripts)
    tfp_insts, tloop_insts, tundet_insts = transcript_first_pass(resolved_insts, tx_agg)
    tfp_set, tloop_set, tundet_set = set(tfp_insts), set(tloop_insts), set(tundet_insts)

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
        tx = ""
        if d == "RESOLVED":
            if inst in tfp_set:
                tx = "  {tx:first-pass}"
            elif inst in tloop_set:
                e = tx_agg.get(inst, {})
                tx = "  {tx:loop-only, +%s verify-fix}" % e.get("min_verify_fails", "?")
            elif inst in tundet_set:
                tx = "  {tx:first-pass UNDETERMINED}"
        line("%-42s %-13s %s%s%s" % (inst, d, detail, fn, tx))
    line("-" * W)

    # ---- disposition summary ----
    line("")
    line("DISPOSITION SUMMARY  (batch total = %d)" % total)
    line("-" * W)
    for k in ("RESOLVED", "IN-PROGRESS", "STUCK", "NOT-ATTEMPTED"):
        line("  %-14s %3d   (%s of batch)" % (k, counts[k], pct(counts[k], total)))
    line("  %-14s %3d" % ("attempted", attempted_n))

    # ---- first-pass vs loop-inclusive (denominator = attempted) ----
    # PRIMARY first-pass = transcript reconstruction (resolved_insts / tx_agg / tfp_insts /
    # tloop_insts / tundet_insts were computed up top, restart-proof). The launch-index version
    # below is kept only as a corroborating diagnostic and is known-broken across re-starts.
    # first-pass denominator excludes UNDETERMINED (transcript missing/ambiguous), so the rate
    # only divides by instances whose first-attempt outcome is actually knowable.
    tfp_denom = len(tfp_insts) + len(tloop_insts)

    # LEGACY first-pass: orchestrator-log launch-index. Retained for comparison only.
    fp_insts = [i for i in resolved_insts
                if is_first_pass(i, first_launch, resolved_launch, live_resolved)]
    loop_insts = [i for i in resolved_insts if i not in fp_insts]

    line("")
    line("PASS RATES  (denominator = ATTEMPTED = %d; NOT-ATTEMPTED %d excluded)"
         % (attempted_n, counts["NOT-ATTEMPTED"]))
    line("-" * W)
    line("  PRIMARY -- first-pass reconstructed from TRANSCRIPTS (restart-proof):")
    line("    first-pass pass@1 : %2d / %2d  = %s   (of %d loop-resolved)"
         % (len(tfp_insts), tfp_denom, pct(len(tfp_insts), tfp_denom), len(resolved_insts)))
    line("        denominator = RESOLVED with a determinable first-attempt outcome")
    line("        (%d resolved - %d first-pass-UNDETERMINED). loop-only(needed a re-verify)=%d"
         % (len(resolved_insts), len(tundet_insts), len(tloop_insts)))
    if tundet_insts:
        line("        first-pass UNDETERMINED (no parseable DONE transcript; loop-resolved only):")
        for inst in tundet_insts:
            e = tx_agg.get(inst, {})
            line("          - %-42s (tx=%s, no DONE-bearing transcript)"
                 % (inst, e.get("n_tx", 0)))
    line("")
    line("  loop-inclusive (resolved at all, any # of verify re-tries):")
    line("    loop-inclusive    : %2d / %2d  = %s"
         % (len(resolved_insts), attempted_n, pct(len(resolved_insts), attempted_n)))

    line("")
    # The launch-index value is only trustworthy when it agrees with the transcript value AND
    # the log was parseable / single-epoch. Flag it as BROKEN when it disagrees materially with
    # the transcript reconstruction (the holdout case), else mark it a corroborating diagnostic.
    legacy_broken = abs(len(fp_insts) - len(tfp_insts)) > max(2, len(tfp_insts) // 4)
    tag = "KNOWN-BROKEN for this run" if legacy_broken else "corroborating diagnostic"
    line("  LEGACY first-pass (orchestrator-log launch-index) -- %s:" % tag)
    line("    first-pass pass@1 : %2d / %2d  = %s"
         % (len(fp_insts), attempted_n, pct(len(fp_insts), attempted_n)))
    if legacy_broken:
        line("    NOTE: the launch-index method is unreliable here and disagrees with the")
        line("          transcript reconstruction. Two independent failure modes hit this run:")
        line("          (1) the holdout log writes 'chunk N (repo=...): [...]' which the legacy")
        line("          'chunk N:' parser does not match, so first_launch is never populated; and")
        line("          (2) the launch counter never resets across the slice6 -> KEEP_ENV ->")
        line("          no-KEEP_ENV -> continuous re-starts, so first_launch and resolved_launch")
        line("          land in different launch epochs. Either alone pins it near 0%. Trust the")
        line("          TRANSCRIPT line above; the launch-index value is an artifact.")
    else:
        line("    NOTE: agrees with the transcript reconstruction (log was parseable and")
        line("          single-epoch). The TRANSCRIPT line remains the source of truth.")

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
    line("    NOTE: the TRANSCRIPT first-pass pass@1 (%s) is left unchanged -- a false-negative's"
         % pct(len(tfp_insts), tfp_denom))
    line("          first-attempt status is not knowable from the grader, so it is credited only")
    line("          to the loop-inclusive ADJUSTED line, never silently to pass@1.")

    # ---- network-dependent (environmental false-negative) breakout ----
    net_insts = [i for i in batch
                 if i.startswith(NETWORK_DEPENDENT_PREFIXES)]
    if net_insts:
        line("")
        line("NETWORK-DEPENDENT instances (env false-negative risk; broken out, NOT adjusted):")
        line("-" * W)
        line("  These reach the live internet in their fail_to_pass tests, so a non-resolve on")
        line("  an OFFLINE eval box may be environmental, not a model miss. Listed for honesty;")
        line("  never silently credited.")
        for inst in net_insts:
            d = dmap.get(inst, "?")
            e = tx_agg.get(inst, {})
            note = ""
            if d != "RESOLVED" and e.get("total_done", 0) >= 1:
                note = ("  (agent reached DONE %dx in transcripts but eval never resolved"
                        " -- likely offline-network fail)" % e["total_done"])
            line("  %-42s %-13s%s" % (inst, d, note))

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
