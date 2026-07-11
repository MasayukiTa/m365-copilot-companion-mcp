"""Orchestrate a full code/security review run on the free M365 Copilot fleet.

Ties together the pure goal-builder (bench/review_build_goals.py) and the pure aggregator
(bench/review_aggregate.py) with the one impure step in between: launching
relay.fleet_runner and waiting for it to finish.

  python bench/review_run.py --kind review --dry-run          # plan only, no fleet launch
  python bench/review_run.py --kind review                    # full repo, launches the fleet
  python bench/review_run.py --kind security --mode diff       # review only the working diff
  python bench/review_run.py --kind review --verify            # + adversarial 2nd-pass check

FLOW:
  1. enumerate_files -> optional --target-path filter -> group_files -> one build_review_goal
     per group -> write_goals_jsonl to <out-dir>/goals_<stamp>.jsonl.
  2. --dry-run prints the plan (goal count, per-goal files, the exact fleet argv, output
     paths) and returns WITHOUT launching anything.
  3. Otherwise: run_fleet() launches relay.fleet_runner and blocks until it exits, then
     aggregate()/render_markdown()/render_json() turn .fleet/status.json into
     <out-dir>/review_report_<stamp>.{md,json}.
  4. --verify (optional, off by default -- costs a second fleet run): takes the flattened
     findings from step 3 and asks a FRESH batch of workers to adversarially confirm or
     reject each one (verify_goals_from_findings), then merges verdicts back into the report.

Everything that actually launches a subprocess lives in run_fleet() -- the only function
tests must monkeypatch to stay hermetic. Every other step (planning, verify-goal building,
aggregation wiring) is reachable and tested without the fleet.
"""
from __future__ import annotations

import argparse
import json
import os
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

from bench.review_aggregate import aggregate, render_json, render_markdown
from bench.review_build_goals import (
    FINDINGS_BEGIN,
    FINDINGS_END,
    build_review_goal,
    enumerate_files,
    group_files,
    write_goals_jsonl,
)

DEFAULT_OUT_DIR = ".fleet/review"
DEFAULT_ALL_GROUP_SIZE = 20
DEFAULT_VERIFY_BATCH = 10
FLEET_MAX_TURNS = "40"


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
               group_size=None):
    """Pure planning step (aside from enumerate_files' own git subprocess calls): enumerate
    -> filter by target_path -> group -> build one review goal per group.

    Returns (files, groups, goals). group_size=None picks a mode-appropriate default:
    DEFAULT_ALL_GROUP_SIZE for "all", or every changed file in ONE goal for "diff" (a diff
    is usually small, and a reviewer benefits from seeing the whole changeset together)."""
    files = enumerate_files(mode, repo_root, base_ref=base_ref, cached=cached)
    files = _filter_target_path(files, target_path)

    if group_size is None:
        group_size = len(files) if mode == "diff" else DEFAULT_ALL_GROUP_SIZE

    groups = group_files(files, group_size)
    goals = [build_review_goal(g, repo_root, kind) for g in groups]
    return files, groups, goals


def fleet_cmd(goals_path, max_concurrent, effort):
    """The exact, verified relay.fleet_runner launch contract. Do not add, rename, or drop
    flags here -- other bench orchestrators (bench/swe_solve_decoupled.py) rely on this
    same shape and it has been confirmed live against relay/fleet_runner.py's argparse."""
    return [VENVPY, "-m", "relay.fleet_runner",
            "--goals-file", goals_path,
            "--max-concurrent", str(max_concurrent),
            "--max-turns", FLEET_MAX_TURNS,
            "--disk-floor-gb", "0",
            "--effort", effort]


def run_fleet(goals_path, max_concurrent, effort):
    """The ONE function that touches the fleet subprocess -- isolated so tests can
    monkeypatch it out entirely without a real Popen. Blocks until the fleet run exits
    (fleet_runner drives the free M365 Copilot fleet on companion Edge :9222); returns its
    return code. Writes .fleet/status.json and .fleet/transcripts/* as a side effect."""
    cmd = fleet_cmd(goals_path, max_concurrent, effort)
    print("fleet: " + " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, cwd=REPO, env=dict(os.environ))
    proc.wait()
    return proc.returncode


VERIFY_RUBRIC = (
    "以下はコードレビューで報告された指摘事項です。それぞれについて該当ファイルを\n"
    "ファイルツール（read_file / grep 等）で実際に読み、指摘が本当に正しいか、誤検知でないかを\n"
    "判定してください。\n"
    "対象リポジトリ（このPCのローカル、git チェックアウト済み）:\n"
    "  {REPO}\n\n"
    "指摘一覧:\n"
    "{FINDINGS_TEXT}\n\n"
    "作業の最後に、次の形式で各指摘の判定をJSON配列として出力してください（この行より前にも\n"
    "後にも文章を書いて構いませんが、必ず以下の2行のデリミタで囲んでください）:\n\n"
    + FINDINGS_BEGIN + "\n"
    "[\n"
    '  {"file": "path/to/file.py", "line": 123, "title": "...", '
    '"verdict": "confirmed", "reason": "..."}\n'
    "]\n"
    + FINDINGS_END + "\n\n"
    "verdict は \"confirmed\" / \"false_positive\" / \"unclear\" のいずれか。\n"
    "このブロックを出力した後、最後に DONE と書いて終了してください。"
)


def verify_goals_from_findings(findings, repo_root, batch_size=DEFAULT_VERIFY_BATCH):
    """PURE (no fleet, no I/O): build second-round adversarial-verification goals from a
    flattened findings list (as produced by bench.review_aggregate.aggregate()["findings"]).

    Chunks findings into batches of `batch_size` using group_files -- it is a generic list
    chunker, not git-specific, so it is safe to reuse here on findings dicts instead of
    filenames. Each finding's file/line/title appears verbatim in its goal's text, followed
    by a <<<FINDINGS>>>...<<<END_FINDINGS>>> verdict-block instruction: the SAME delimiters
    the first pass uses, so bench.review_aggregate.parse_findings_block can parse a
    worker's verdict list exactly like it parses findings, with no separate parser needed."""
    batches = group_files(findings, batch_size)
    goals = []
    for batch in batches:
        lines = []
        for i, f in enumerate(batch):
            lines.append(
                "%d. file=%s line=%s severity=%s title=%s\n   detail: %s" % (
                    i + 1, f.get("file", "?"), f.get("line"), f.get("severity", "?"),
                    f.get("title", ""), f.get("detail", ""),
                )
            )
        text = VERIFY_RUBRIC.replace("{REPO}", repo_root).replace(
            "{FINDINGS_TEXT}", "\n".join(lines))
        goals.append({"text": text, "cwd": repo_root})
    return goals


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
    return agg


def _resolve_out_dir(out_dir, repo_root):
    return out_dir if os.path.isabs(out_dir) else os.path.join(repo_root, out_dir)


def build_arg_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["review", "security"], required=True)
    ap.add_argument("--mode", choices=["all", "diff"], default="all")
    ap.add_argument("--base-ref", default=None)
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("--target-path", default=None,
                     help="restrict enumeration to files under this subdir")
    ap.add_argument("--group-size", type=int, default=None,
                     help="files per goal (default: 20 for --mode all, all-in-one for diff)")
    ap.add_argument("--max-concurrent", type=int, default=4)
    ap.add_argument("--effort", choices=["min", "auto"], default="auto")
    ap.add_argument("--verify", action="store_true",
                     help="run an optional 2nd adversarial verification pass (costs a "
                          "second fleet run)")
    ap.add_argument("--dry-run", action="store_true",
                     help="build goals and print the plan without launching the fleet")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    repo_root = REPO
    out_dir = _resolve_out_dir(args.out_dir, repo_root)
    stamp = _stamp()

    files, groups, goals = plan_goals(
        args.kind, args.mode, repo_root, base_ref=args.base_ref, cached=args.cached,
        target_path=args.target_path, group_size=args.group_size,
    )
    goals_path = os.path.join(out_dir, "goals_%s.jsonl" % stamp)
    write_goals_jsonl(goals, goals_path)
    cmd = fleet_cmd(goals_path, args.max_concurrent, args.effort)

    if args.dry_run:
        print("DRY RUN -- plan only, fleet NOT launched")
        print("kind=%s mode=%s target-path=%s" %
              (args.kind, args.mode, args.target_path or "(none)"))
        print("files matched: %d" % len(files))
        print("goals: %d" % len(goals))
        for i, g in enumerate(groups):
            print("  goal %d/%d: %d file(s): %s" % (i + 1, len(groups), len(g), ", ".join(g)))
        print("goals file: %s" % goals_path)
        print("fleet cmd: %s" % cmd)
        print("report would be written under: %s" % out_dir)
        return 0

    if not os.path.isfile(VENVPY):
        print("ERROR: .venv python not found at %s -- run quickstart.bat first." % VENVPY)
        return 1

    if not goals:
        print("no files matched --mode %s --target-path %s -- nothing to review" %
              (args.mode, args.target_path or "(none)"))
        return 0

    print("launching %d review goal(s) on the free M365 fleet..." % len(goals))
    run_fleet(goals_path, args.max_concurrent, args.effort)

    status_path = os.path.join(repo_root, ".fleet", "status.json")
    transcripts_dir = os.path.join(repo_root, ".fleet", "transcripts")
    if not os.path.isfile(status_path):
        print("ERROR: fleet run finished but %s was not found -- the fleet run likely "
              "failed to start or was killed before writing a snapshot; check the .fleet "
              "logs." % status_path)
        return 1

    agg = aggregate(status_path, transcripts_dir, now=time.time())

    if args.verify:
        findings = agg.get("findings", [])
        if not findings:
            print("--verify requested but there are no findings to verify; skipping")
        else:
            vgoals = verify_goals_from_findings(findings, repo_root)
            vgoals_path = os.path.join(out_dir, "verify_goals_%s.jsonl" % stamp)
            write_goals_jsonl(vgoals, vgoals_path)
            print("launching verify pass: %d goal(s)..." % len(vgoals))
            run_fleet(vgoals_path, args.max_concurrent, args.effort)
            if os.path.isfile(status_path):
                vagg = aggregate(status_path, transcripts_dir, now=time.time())
                merge_verdicts(agg, vagg.get("findings", []))
            else:
                print("WARNING: verify pass produced no status.json; skipping verdict merge")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
