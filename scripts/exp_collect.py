# -*- coding: utf-8 -*-
"""Plans the paired runs, and measures them: duplicate cost per campaign and per run, information loss
at the merge, and the fan-out on/off pairs. Reads only; writes only under the experiment root.

WHY A COLLECTOR AND NOT scripts/sibling_dup_report.py ALONE. Three facts of the isolated runs
(measured in the earlier real-fleet comparisons) make the stock report miss everything:
  * the connector-less route labels a ledger row with the worker NAME only (attr "explicit", no task,
    no campaign), and worker names (w0, w1 ...) repeat in every run, so the report's join cannot place
    the row in a campaign;
  * status.json keeps only the LAST run's workers and, after a fan-out run ends, a single root worker;
    turns and the final answer must come from the transcripts;
  * each run has its own state directory (so a run can be stopped, resumed or thrown away alone) while
    the tool ledger lives in the checkout's .fleet, shared by every run of the private server.
This script puts the pieces together per run: it slices the ledger by the run's time window, maps each
worker to its fan-out child through the transcript header (the child's goal text is the campaign
row's text), writes the enriched rows to <root>/analysis, and then runs the UNCHANGED
scripts/sibling_dup_report.py and scripts/ab_compare.py code on them.

DEFINITIONS (the same as sibling_dup_report.py; restated so a reader of a result does not need the code)
  duplicate call   a sibling call whose tool+argument fingerprint was already issued by a DIFFERENT
                   sibling of the same campaign. The first issuer is not a duplicate; later copies are.
  duplicate cost   the summed duration of the duplicate calls, per campaign and per run. Also shown
                   as a share of (a) the campaign's wall-clock span and (b) all sibling-call time.
                   Siblings run in parallel, so the sum can exceed the span; it is work, not elapsed time.
  information loss an item the FINAL answer got wrong or left out while a child's report (the 1200
                   characters the merge sees) carries the right value. A wrong child is not a loss.

  python scripts/exp_collect.py plan    --root X --goals X/data/goals.json [--pairs 20] [--exp-id ID]
  python scripts/exp_collect.py collect --root X [--ledger PATH]
  python scripts/exp_collect.py report  --root X
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402
from scripts import exp_public_data as D  # noqa: E402
from scripts import sibling_dup_report as S  # noqa: E402

#: A call at least this long counts as "expensive" in the run table.
EXPENSIVE_S = 5.0
LEDGER_SLACK_S = 180.0          # outcomes of calls started inside the window may land after it
METRICS = ("turns", "wall_work_s", "tool_calls", "tool_time_s", "items_ok", "expensive_time_s")
#: Pairs whose condition B (fan-out on) did not split are kept but counted apart: nothing differed.
ARM_A, ARM_B = "off", "on"      # A = a single worker (--no-fanout), B = fan-out. delta = B - A.


# --------------------------------------------------------------------------- plan

def schedule(goal_ids, n_pairs):
    """Goal order for the pairs: families interleaved, then a second pass over the first goals of each
    family (a replicate, with the arm order reversed by the caller's alternation)."""
    fam_s = [g for g in goal_ids if g.startswith("s")]
    fam_t = [g for g in goal_ids if g.startswith("t")]
    first = [g for pair in zip(fam_s, fam_t) for g in pair]
    first += [g for g in goal_ids if g not in first]
    order = [(g, 1) for g in first]
    k = 0
    while len(order) < n_pairs and first:
        order.append((first[k % len(first)], 2))
        k += 1
    return order[:n_pairs]


def make_plan(goal_ids, n_pairs, exp_id, root):
    runs = []
    for k, (gid, rep) in enumerate(schedule(goal_ids, n_pairs)):
        pair_id = "%s#%d" % (gid, rep)
        order = (ARM_B, ARM_A) if k % 2 == 0 else (ARM_A, ARM_B)
        for pos, arm in enumerate(order):
            label = "%02d%s_%s_r%d_%s" % (k + 1, "ab"[pos], gid, rep, arm)
            runs.append({"label": label, "pair_id": pair_id, "goal_id": gid, "arm": arm,
                         "condition": "B" if arm == ARM_B else "A", "pair_no": k + 1,
                         "experiment_id": "%s-%s-r%d" % (exp_id, gid, rep),
                         "state_dir": os.path.join(root, "runs", label)})
    return runs


# --------------------------------------------------------------------------- reading one run

def _open_text(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8-sig", errors="replace")


def load_transcripts(state_dir):
    """[{name, key, goal, ts, assistant, waits, last_ts}] from <state_dir>/transcripts."""
    tdir = os.path.join(state_dir, "transcripts")
    out = []
    try:
        names = sorted(os.listdir(tdir))
    except OSError:
        return out
    for fn in names:
        if ".jsonl" not in fn:
            continue
        meta, assistant, waits, last = {}, [], 0.0, None
        try:
            with _open_text(os.path.join(tdir, fn)) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if row.get("meta"):
                        meta = row
                    elif row.get("role") == "assistant":
                        assistant.append(str(row.get("text") or ""))
                    elif row.get("role") == "metric":
                        if row.get("name") == "turn_wait_s" and isinstance(row.get("value"), (int, float)):
                            waits += float(row["value"])
                        if isinstance(row.get("ts"), (int, float)):
                            last = row["ts"] if last is None else max(last, row["ts"])
        except OSError:
            continue
        key = str(meta.get("key") or fn.split(".jsonl")[0])
        out.append({"name": str(meta.get("name") or key.rsplit("_", 1)[-1]), "key": key,
                    "goal": str(meta.get("goal") or ""), "ts": meta.get("ts"),
                    "assistant": assistant, "waits": waits, "last_ts": last})
    return out


def map_workers(transcripts, camp_rows):
    """{worker name: (campaign_id, subtask_id)} for transcripts whose goal carries a campaign child's text.
    The child's text is the parent goal plus the child's scope block, so the ROOT goal never contains it
    (the other way round would pull the root worker in). An exact match wins; else the longest text."""
    kids = [(str(r.get("campaign_id")), str(r.get("task_id")), str(r.get("text") or ""))
            for r in camp_rows if r.get("task_id") and r.get("text") and r.get("kind") is None]
    mapped = {}
    for t in transcripts:
        g = t["goal"]
        best = None
        for cid, tid, text in kids:
            if g == text:
                best = (cid, tid, len(text) + 1)
                break
            if len(text) > 50 and text in g and (best is None or len(text) > best[2]):
                best = (cid, tid, len(text))
        if best:
            mapped[t["name"]] = (best[0], best[1])
    return mapped


def ledger_slice(ledger_path, t0, t1):
    """Call rows with t0 <= ts <= t1 and the outcome rows of exactly those calls (rotated copy first)."""
    rows = S._read_jsonl(ledger_path + ".1") + S._read_jsonl(ledger_path)
    calls = [r for r in rows if r.get("event") == "call" and isinstance(r.get("ts"), (int, float))
             and t0 <= r["ts"] <= t1]
    ids = {r.get("id") for r in calls}
    outs = [r for r in rows if r.get("event") == "outcome" and r.get("id") in ids]
    return calls + outs


def enrich(rows, mapped):
    """Copies of the rows; a call by a mapped worker gets campaign_id / subtask_id / role=subtask, so the
    UNCHANGED sibling report can place it. Other rows (root, merge, unknown) are left as they are."""
    out = []
    for r in rows:
        r = dict(r)
        hit = mapped.get(str(r.get("worker") or "")) if r.get("event") == "call" else None
        if hit:
            r["campaign_id"], r["subtask_id"], r["role"] = hit[0], hit[1], S.CHILD_ROLE
        out.append(r)
    return out


def final_and_children(transcripts, mapped, camp_rows):
    """(final answer text, its source worker, [child result texts the merge saw]).
    Heuristic, and said so in the output: the final answer is the last assistant message of the latest
    transcript that is not a campaign child (the merge worker after a split, the only worker otherwise)."""
    rest = [t for t in transcripts if t["name"] not in mapped and t["assistant"]]
    rest.sort(key=lambda t: (t["ts"] if isinstance(t["ts"], (int, float)) else 0.0))
    final, src = ("", "")
    if rest:
        final, src = rest[-1]["assistant"][-1], rest[-1]["name"]
    kids = [str(r.get("result") or "") for r in camp_rows if r.get("kind") == "child_result"]
    return final, src, kids


# --------------------------------------------------------------------------- one run's record

def run_record(run, goal, finished, ledger_path, stop_at=None):
    sd = run["state_dir"]
    camp = S._read_jsonl(os.path.join(sd, "campaigns.jsonl"))
    trs = load_transcripts(sd)
    mapped = map_workers(trs, camp)
    t0, t1 = float(finished["ts_start"]), float(finished["ts_end"]) + LEDGER_SLACK_S
    if stop_at is not None:     # the next run's workers reuse the names w0, w1 ...: never reach into it
        t1 = min(t1, float(stop_at) - 1e-6)
    rows = enrich(ledger_slice(ledger_path, t0, t1), mapped)
    pairs = [(c, o) for c, o in S.pair_events(rows) if not str(c.get("tool") or "").startswith(S.DISCOVERY_PREFIX)]
    durs = [S._duration(o) for _c, o in pairs]
    timed = [d for d in durs if d is not None]
    res = S.analyse(rows, [], camp)
    qual = [r for r in res["rows"] if r["qualifies"]]
    final, src, kids = final_and_children(trs, mapped, camp)
    graded = D.grade(goal["expected"], final, kids)
    headers = [r for r in camp if r.get("kind") == "campaign"]
    work_ts = [t["ts"] for t in trs if isinstance(t["ts"], (int, float))] + \
              [t["last_ts"] for t in trs if t["last_ts"] is not None]
    wall_work = (max(work_ts) - min(work_ts)) if len(work_ts) >= 2 else None
    dup_t = sum(r["dup_time_s"] for r in qual)
    sib_t = sum(d for (c, o), d in zip(pairs, durs) if d is not None and c.get("role") == S.CHILD_ROLE)
    span = sum(r["wall_s"] for r in qual if r["wall_s"])
    return {
        "label": run["label"], "pair_id": run["pair_id"], "goal_id": run["goal_id"], "family": goal["family"],
        "arm": run["arm"], "exit_code": finished.get("exit_code"), "data_intact": finished.get("data_intact"),
        "wall_total_s": float(finished["ts_end"]) - float(finished["ts_start"]),
        "wall_work_s": wall_work, "workers": len(trs),
        "turns": sum(len(t["assistant"]) for t in trs), "turn_wait_s": sum(t["waits"] for t in trs),
        "split": bool(headers), "children": sum(int(h.get("n") or 0) for h in headers),
        "campaign_ids": [h.get("campaign_id") for h in headers],
        "tool_calls": len(pairs), "tool_time_s": sum(timed),
        "expensive_calls": sum(1 for d in timed if d >= EXPENSIVE_S),
        "expensive_time_s": sum(d for d in timed if d >= EXPENSIVE_S),
        "sibling_calls": sum(r["calls"] for r in qual), "comparable": sum(r["comparable"] for r in qual),
        "dup_calls": sum(r["dup"] for r in qual), "dup_time_s": dup_t, "sibling_time_s": sib_t,
        "campaign_span_s": span,
        "dup_share_of_span": (dup_t / span) if span else None,
        "dup_share_of_sibling_time": (dup_t / sib_t) if sib_t else None,
        "merge_failed_structural": sum(1 for r in qual if r["merge_failed"]),
        "merge_lost_info_structural": sum(1 for r in qual if r["merge_failed"] and r["merge_missing"] > 0),
        "items": len(graded), "items_ok": sum(1 for v in graded.values() if v == "ok"),
        "items_merge_loss": sum(1 for v in graded.values() if v == "merge_loss"),
        "items_wrong": sum(1 for v in graded.values() if v == "wrong"),
        "final_source": src, "graded": graded,
        "_enriched": rows, "_camp": camp,
    }


# --------------------------------------------------------------------------- paired statistics

def sign_test_p(deltas):
    """Exact two-sided sign test over the non-zero deltas. 1.0 when there are none."""
    d = [x for x in deltas if x]
    n = len(d)
    if not n:
        return 1.0
    k = min(sum(1 for x in d if x > 0), sum(1 for x in d if x < 0))
    p = 2.0 * sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return min(1.0, p)


def bootstrap_ci(deltas, reps=10000, seed=20261020):
    """Percentile 95% interval of the mean of the paired deltas (seeded, so a re-run gives the same)."""
    if not deltas:
        return (None, None)
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return (means[int(0.025 * reps)], means[int(0.975 * reps) - 1])


def pair_table(records):
    """[{pair_id, A: rec, B: rec}] for pairs whose two runs both finished with exit code 0."""
    by = {}
    for r in records:
        by.setdefault(r["pair_id"], {})[r["arm"]] = r
    out = []
    for pid, arms in sorted(by.items()):
        a, b = arms.get(ARM_A), arms.get(ARM_B)
        if a and b and a.get("exit_code") == 0 and b.get("exit_code") == 0:
            out.append({"pair_id": pid, "A": a, "B": b})
    return out


def paired_summary(pairs, metric):
    deltas = []
    for p in pairs:
        a, b = p["A"].get(metric), p["B"].get(metric)
        if a is not None and b is not None:
            deltas.append(b - a)
    lo, hi = bootstrap_ci(deltas)
    return {"metric": metric, "n": len(deltas),
            "mean_delta_on_minus_off": (sum(deltas) / len(deltas)) if deltas else None,
            "median_delta": sorted(deltas)[len(deltas) // 2] if deltas else None,
            "ci95": [lo, hi], "sign_test_p": sign_test_p(deltas)}


def pooled(events, camp):
    """(analysis, summary) of the stock sibling report over the pooled, enriched fan-out-on records."""
    res = S.analyse(events, [], camp)
    return res, S.summarise(res)


# --------------------------------------------------------------------------- commands

def _jsonl_write(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_plan(a):
    root = os.path.abspath(a.root)
    with open(a.goals, encoding="utf-8") as fh:
        goal_ids = [g["goal_id"] for g in json.load(fh)]
    exp_id = a.exp_id
    if not exp_id:
        from relay.selfimprove.experiment import new_experiment_id
        exp_id = new_experiment_id("exp")
    runs = make_plan(goal_ids, a.pairs, exp_id, root)
    with open(os.path.join(root, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"experiment": exp_id, "pairs": a.pairs, "runs": runs}, fh, ensure_ascii=False, indent=1)
    print("planned %d runs (%d pairs) under experiment %s" % (len(runs), a.pairs, exp_id))
    return 0


def _finished_runs(root):
    out = {}
    for r in S._read_jsonl(os.path.join(root, "runs.jsonl")):
        if r.get("label") and r.get("ts_start") is not None and r.get("ts_end") is not None:
            out[r["label"]] = r
    return out


def cmd_collect(a):
    root = os.path.abspath(a.root)
    with open(os.path.join(root, "plan.json"), encoding="utf-8") as fh:
        plan = json.load(fh)
    with open(os.path.join(root, "data", "goals.json"), encoding="utf-8") as fh:
        goals = {g["goal_id"]: g for g in json.load(fh)}
    ledger = a.ledger or os.path.join(REPO, ".fleet", "tool_events.jsonl")
    finished = _finished_runs(root)
    records = []
    starts = sorted(float(f["ts_start"]) for f in finished.values())
    for run in plan["runs"]:
        fin = finished.get(run["label"])
        if fin:
            nxt = [t for t in starts if t > float(fin["ts_start"])]
            records.append(run_record(run, goals[run["goal_id"]], fin, ledger, stop_at=nxt[0] if nxt else None))
    adir = os.path.join(root, "analysis")
    os.makedirs(adir, exist_ok=True)
    # the pooled view the stock reports can read: enriched ledger, concatenated campaign rows
    _jsonl_write(os.path.join(adir, "tool_events.jsonl"), [x for r in records for x in r["_enriched"]])
    _jsonl_write(os.path.join(adir, "campaigns.jsonl"), [x for r in records for x in r["_camp"]])
    workers = []
    for r in records:
        st = os.path.join(root, "runs", r["label"], "status.json")
        workers += S._read_status_workers(st)
    with open(os.path.join(adir, "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"workers": workers}, fh)
    manifest = []
    for run in plan["runs"]:
        rec = next((r for r in records if r["label"] == run["label"]), None)
        if rec is None:
            continue
        ent = {"goal_id": run["pair_id"], "condition": run["condition"], "experiment_id": run["experiment_id"]}
        if run["arm"] == ARM_B and rec["campaign_ids"]:
            ent["campaign_id"] = rec["campaign_ids"][0]
        else:
            tr = load_transcripts(run["state_dir"])
            ent["run_id"] = tr[0]["key"].rsplit("_w", 1)[0] if tr else ""
        manifest.append(ent)
    with open(os.path.join(adir, "ab_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    for r in records:
        r.pop("_enriched", None)
        r.pop("_camp", None)
    with open(os.path.join(adir, "runs.json"), "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)
    print("collected %d of %d planned runs into %s" % (len(records), len(plan["runs"]), adir))
    return 0


def _f(v, nd=2):
    return "n/a" if v is None else "%.*f" % (nd, v)


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def cmd_report(a):
    root = os.path.abspath(a.root)
    adir = os.path.join(root, "analysis")
    with open(os.path.join(adir, "runs.json"), encoding="utf-8") as fh:
        records = json.load(fh)
    # the pooled files already carry the enrichment, so the stock report code reads them as they are
    events = S._read_jsonl(os.path.join(adir, "tool_events.jsonl"))
    camp = S._read_jsonl(os.path.join(adir, "campaigns.jsonl"))
    _res, summ = pooled(events, camp)
    lines = ["# Experiment report", "",
             "## Sibling duplication (stock K1/K2 rule over the fan-out-on runs)", "",
             "- verdict: **%s**" % S.verdict(summ),
             "- campaigns that qualify (M4): %d (K2 needs %d)" % (summ["m4"], S.MIN_CAMPAIGNS),
             "- M1 duplicate-call rate: %s (%d of %d comparable sibling calls)" % (
                 _pct(summ["dup_rate"]), summ["dup"], summ["comparable"]),
             "- M2 duplicate time / campaign span: %s (%.1f s of %.1f s)" % (
                 _pct(summ["dup_share"]), summ["dup_time_s"], summ["wall_s"]),
             "- M3 merges lost to missing child information (structural proxy): %d of %d" % (
                 summ["merge_lost_info"], summ["merges"]), "",
             "## Per run", "",
             "| run | arm | split | turns | work s | tool calls | exp. s | dup calls | dup s | dup/span | items ok | merge loss |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append("| %s | %s | %s | %d | %s | %d | %s | %d | %s | %s | %d/%d | %d |" % (
            r["label"], r["arm"], "yes" if r["split"] else "no", r["turns"], _f(r["wall_work_s"], 0),
            r["tool_calls"], _f(r["expensive_time_s"], 0), r["dup_calls"], _f(r["dup_time_s"], 0),
            _pct(r["dup_share_of_span"]),
            r["items_ok"], r["items"], r["items_merge_loss"]))
    pairs = pair_table(records)
    split_pairs = [p for p in pairs if p["B"]["split"]]
    lines += ["", "## Pairs (delta = fan-out on minus single worker)", "",
              "- complete pairs: %d, of which the fan-out arm really split: %d" % (len(pairs), len(split_pairs)), ""]
    for label, ps in (("all complete pairs", pairs), ("pairs where fan-out split", split_pairs)):
        lines += ["### " + label, "", "| metric | n | mean delta | median | 95% CI (bootstrap) | sign test p |",
                  "|---|---|---|---|---|---|"]
        for m in METRICS:
            s = paired_summary(ps, m)
            lines.append("| %s | %d | %s | %s | %s .. %s | %s |" % (
                m, s["n"], _f(s["mean_delta_on_minus_off"]), _f(s["median_delta"]),
                _f(s["ci95"][0]), _f(s["ci95"][1]), _f(s["sign_test_p"], 3)))
        lines.append("")
    mp = os.path.join(adir, "ab_manifest.json")
    ab = AB.compare(*_ab_inputs(mp, adir))
    lines += ["## ab_compare (stock) cross-check", "", "- verdict %s, complete pairs %d" % (
        ab["verdict"], ab["complete_pairs"]), "- " + ab["note"], ""]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(adir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


def _ab_inputs(manifest_path, adir):
    entries, _problems = AB.load_manifest(manifest_path)
    events, workers, camps, _n = S.load_fleet(adir)
    return entries, events, workers, camps


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--root", required=True)
    p.add_argument("--goals", required=True)
    p.add_argument("--pairs", type=int, default=20)
    p.add_argument("--exp-id", default="")
    c = sub.add_parser("collect")
    c.add_argument("--root", required=True)
    c.add_argument("--ledger", default="")
    r = sub.add_parser("report")
    r.add_argument("--root", required=True)
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "collect": cmd_collect, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
