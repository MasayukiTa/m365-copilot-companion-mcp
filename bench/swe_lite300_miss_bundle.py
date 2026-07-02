#!/usr/bin/env python3
"""Build and classify miss bundles for the SWE-bench Lite 300 strong-scaffold run.

Inputs are the two official the eval host batch result files:

    .fleet/swe/_grade_batch/b0620191201.batchresult.json
    .fleet/swe/_grade_batch/b0620220832.batchresult.json

For every unresolved/error/empty instance, this script writes:

    .fleet/swe/_miss300/<instance>.md
    .fleet/swe/_miss300/miss_index.jsonl
    .fleet/swe/_miss300/summary.md

When `--pull-logs` is set, it also pulls `report.json` and `test_output.txt` from the eval host.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swe_check_remote as R


REPO = Path(__file__).resolve().parents[1]
SWEDIR = REPO / ".fleet" / "swe"
GRADE_DIR = SWEDIR / "_grade_batch"
PREDS = SWEDIR / "preds_solve"
OUT = SWEDIR / "_miss300"
PARQUET = SWEDIR / "SWE-bench_Lite_test.parquet"
RUNS = ("b0620191201", "b0620220832")


def safe_name(instance_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff_files(diff_text):
    out = []
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                b = parts[3]
                out.append(b[2:] if b.startswith("b/") else b)
    return out


def diff_stats(diff_text):
    plus = minus = hunks = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return plus, minus, hunks


def load_misses():
    rows = []
    seen = set()
    for run_id in RUNS:
        data = read_json(GRADE_DIR / (run_id + ".batchresult.json"))
        for verdict in ("unresolved", "error", "empty"):
            for instance_id in data.get(verdict, []) or []:
                if instance_id in seen:
                    raise SystemExit("duplicate miss: %s" % instance_id)
                seen.add(instance_id)
                rows.append({"instance_id": instance_id, "bucket": verdict, "run_id": run_id})
    return rows


def pull_logs(misses):
    remote_tar = "/mnt/c/wsl-setup/miss300_logs.tgz"
    tar_members = [
        "gb_%s/logs/run_evaluation/%s/companion" % (run_id, run_id)
        for run_id in RUNS
    ]
    cmd = "cd /tmp && tar -czf %s %s && ls -lh %s" % (
        remote_tar, " ".join(tar_members), remote_tar)
    ps = ("$j = Start-Job { (wsl.exe -d " + R.DISTRO + " -u root -- bash -lc '"
          + cmd + "' 2>$null) -join \"`n\" }; "
          "if(Wait-Job $j -Timeout 180){ Receive-Job $j } else { 'HUNG' }; Remove-Job $j -Force")
    out = R._ssh_ps(ps, timeout=240, tries=1)
    if "miss300_logs.tgz" not in out:
        raise SystemExit("failed to create remote log tar: %s" % out)

    remote_dir = OUT / "_remote"
    remote_dir.mkdir(parents=True, exist_ok=True)
    local_tar = remote_dir / "miss300_logs.tgz"
    r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=30",
                        "eval-host:/C:/wsl-setup/miss300_logs.tgz", str(local_tar)],
                       cwd=str(REPO), capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        raise SystemExit("scp log tar failed: %s" % (r.stderr or r.stdout))
    subprocess.run(["tar", "-xzf", str(local_tar), "-C", str(remote_dir)],
                   cwd=str(REPO), check=True)

    logs = OUT / "_logs"
    logs.mkdir(parents=True, exist_ok=True)
    copied = 0
    for m in misses:
        inst = m["instance_id"]
        run_id = m["run_id"]
        src = (remote_dir / ("gb_" + run_id) / "logs" / "run_evaluation" /
               run_id / "companion" / inst)
        s = safe_name(inst)
        for suffix, name in ((".to", "test_output.txt"), (".rep", "report.json")):
            p = src / name
            if p.exists():
                (logs / (s + suffix)).write_bytes(p.read_bytes())
                copied += 1
    return str(copied)


def report_breakdown(rep_text):
    try:
        data = json.loads(rep_text)
        inner = next(iter(data.values())) if data else {}
        status = inner.get("tests_status", {}) or {}
        f2p = status.get("FAIL_TO_PASS", {}) or {}
        p2p = status.get("PASS_TO_PASS", {}) or {}
        return {
            "f2p_success": f2p.get("success", []) or [],
            "f2p_failure": f2p.get("failure", []) or [],
            "p2p_success": p2p.get("success", []) or [],
            "p2p_failure": p2p.get("failure", []) or [],
        }
    except Exception:
        return {"f2p_success": [], "f2p_failure": [], "p2p_success": [], "p2p_failure": []}


def extract_error_signal(test_output):
    text = test_output or ""
    pats = [
        r"(AssertionError[^\n]*)",
        r"(ImportError[^\n]*)",
        r"(ModuleNotFoundError[^\n]*)",
        r"(TypeError[^\n]*)",
        r"(ValueError[^\n]*)",
        r"(AttributeError[^\n]*)",
        r"(KeyError[^\n]*)",
        r"(FAILED \(.*?\))",
        r"(ERROR: .*?)(?:\n|$)",
    ]
    for pat in pats:
        m = re.search(pat, text)
        if m:
            return " ".join(m.group(1).split())[:220]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return (lines[-1] if lines else "")[:220]


def classify(bucket, agent_patch, gold_patch, agent_files, gold_files, breakdown):
    if bucket == "empty" or not (agent_patch or "").strip():
        return "empty_or_capture_failure"
    if bucket == "error":
        return "official_eval_error"
    if breakdown["p2p_failure"]:
        return "regression"
    gold_set = set(gold_files)
    agent_set = set(agent_files)
    if gold_set and agent_set and not (gold_set & agent_set):
        return "wrong_file_or_layer"
    if gold_set and not gold_set <= agent_set:
        return "partial_multifile_fix"
    a_plus, a_minus, a_hunks = diff_stats(agent_patch)
    g_plus, g_minus, g_hunks = diff_stats(gold_patch)
    if (a_plus + a_minus) > max(80, 4 * max(1, g_plus + g_minus)):
        return "overbroad_patch"
    if (a_plus + a_minus) < max(3, (g_plus + g_minus) // 4):
        return "underfit_patch"
    return "same_file_precision_miss"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-logs", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    misses = load_misses()
    if args.pull_logs:
        copied = pull_logs(misses)
        print("pulled log files:", copied)

    df = pd.read_parquet(PARQUET)
    rows_by_id = {r["instance_id"]: r for _, r in df.iterrows()}
    index_rows = []
    for m in misses:
        inst = m["instance_id"]
        row = rows_by_id[inst]
        pred_path = PREDS / (inst + ".json")
        agent_patch = ""
        if pred_path.exists():
            try:
                agent_patch = (read_json(pred_path)[0].get("model_patch") or "")
            except Exception:
                agent_patch = ""
        gold_patch = str(row["patch"] or "")
        logs = OUT / "_logs"
        test_output_path = logs / (safe_name(inst) + ".to")
        report_path = logs / (safe_name(inst) + ".rep")
        test_output = test_output_path.read_text(encoding="utf-8", errors="replace") if test_output_path.exists() else ""
        report_text = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
        breakdown = report_breakdown(report_text)
        agent_files = diff_files(agent_patch)
        gold_files = diff_files(gold_patch)
        category = classify(m["bucket"], agent_patch, gold_patch, agent_files, gold_files, breakdown)
        a_plus, a_minus, a_hunks = diff_stats(agent_patch)
        g_plus, g_minus, g_hunks = diff_stats(gold_patch)
        rec = {
            "instance_id": inst,
            "repo": row["repo"],
            "bucket": m["bucket"],
            "run_id": m["run_id"],
            "category": category,
            "agent_files": agent_files,
            "gold_files": gold_files,
            "file_overlap": sorted(set(agent_files) & set(gold_files)),
            "agent_added": a_plus,
            "agent_deleted": a_minus,
            "agent_hunks": a_hunks,
            "gold_added": g_plus,
            "gold_deleted": g_minus,
            "gold_hunks": g_hunks,
            "f2p_failure_count": len(breakdown["f2p_failure"]),
            "p2p_failure_count": len(breakdown["p2p_failure"]),
            "f2p_failures": breakdown["f2p_failure"][:8],
            "p2p_failures": breakdown["p2p_failure"][:8],
            "error_signal": extract_error_signal(test_output),
        }
        index_rows.append(rec)

        body = []
        body.append("# MISS: %s\n" % inst)
        body.append("- repo: `%s`" % row["repo"])
        body.append("- official bucket: `%s`" % m["bucket"])
        body.append("- primary category: `%s`" % category)
        body.append("- agent files: `%s`" % "`, `".join(agent_files))
        body.append("- gold files: `%s`" % "`, `".join(gold_files))
        body.append("- remaining FAIL_TO_PASS: %d" % len(breakdown["f2p_failure"]))
        body.append("- PASS_TO_PASS regressions: %d" % len(breakdown["p2p_failure"]))
        body.append("- error signal: `%s`\n" % rec["error_signal"])
        body.append("## Problem\n%s\n" % str(row["problem_statement"])[:4000])
        body.append("## Official Remaining Failures\n```json\n%s\n```\n" %
                    json.dumps({"FAIL_TO_PASS": rec["f2p_failures"], "PASS_TO_PASS": rec["p2p_failures"]},
                               indent=2, ensure_ascii=False))
        body.append("## Gold Patch\n```diff\n%s\n```\n" % gold_patch[:5000])
        body.append("## Agent Patch\n```diff\n%s\n```\n" % (agent_patch[:5000] or "(empty)"))
        body.append("## Test Output Tail\n```\n%s\n```\n" % (test_output[-4000:] if test_output else "(not pulled)"))
        (OUT / (safe_name(inst) + ".md")).write_text("\n".join(body), encoding="utf-8")

    with open(OUT / "miss_index.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for rec in index_rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_cat = collections.Counter(r["category"] for r in index_rows)
    by_repo = collections.Counter(r["repo"].split("/", 1)[0] for r in index_rows)
    lines = ["# SWE-bench Lite 300 Miss Analysis Index", ""]
    lines.append("Total misses: **%d**" % len(index_rows))
    lines.append("")
    lines.append("## Category Counts")
    lines.append("")
    lines.append("| category | count |")
    lines.append("|---|---:|")
    for k, v in by_cat.most_common():
        lines.append("| %s | %d |" % (k, v))
    lines.append("")
    lines.append("## Repo Counts")
    lines.append("")
    lines.append("| repo owner | count |")
    lines.append("|---|---:|")
    for k, v in by_repo.most_common():
        lines.append("| %s | %d |" % (k, v))
    lines.append("")
    lines.append("## Miss Table")
    lines.append("")
    lines.append("| instance | category | bucket | remaining F2P | P2P regressions | file overlap |")
    lines.append("|---|---|---|---:|---:|---|")
    for r in sorted(index_rows, key=lambda x: (x["category"], x["instance_id"])):
        lines.append("| `%s` | %s | %s | %d | %d | %s |" %
                     (r["instance_id"], r["category"], r["bucket"], r["f2p_failure_count"],
                      r["p2p_failure_count"], ", ".join(r["file_overlap"]) or "-"))
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", len(index_rows), "miss bundles to", OUT)
    print("categories:", dict(by_cat))


if __name__ == "__main__":
    main()
