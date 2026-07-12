"""Take an existing `/review` findings report and have the M365 fleet FIX the findings --
safely, for a NON-git-savvy user.

Ties together the pure fix-goal builder (bench/review_build_goals.py:build_fix_goal), the
existing fleet launch contract (bench/review_run.py:fleet_cmd/run_fleet), and this module's
own findings-block parser reuse (bench.review_aggregate.parse_findings_block, for the fleet
worker's "what I changed" block -- same delimiters, no second parser).

SAFETY MODEL (the whole point of this module):
  - Before the fleet touches anything, every file that will be targeted is copied into
    .fleet/review_fix/backup_<stamp>/ (backup_files/write_manifest). A file that does not yet
    exist is recorded as backed_up=False so undo() knows to DELETE it (not "restore" nothing).
  - write_undo_bat() drops a plain double-clickable .bat next to the backup that runs
    `... review_fix.py --undo <stamp>` for a user who has never touched a terminal.
  - git is treated as a pure BONUS, never a requirement: git_bonus_precheck() only says "yes"
    if git is on PATH, the repo is a clean git work tree, and status --porcelain is empty --
    otherwise the fix still runs, just without a branch/commit. The backup+undo path works
    identically whether or not git is available.
  - The post-fix test gate (`python -m relay.selfimprove.run_all_tests`) NEVER triggers an
    automatic revert on failure -- the backup, undo .bat, and any git branch are always left
    in place, and the fix report prominently says "NOT reverted -- inspect manually."

  python bench/review_fix.py --dry-run                      # plan only, fleet NOT launched
  python bench/review_fix.py                                # fix latest report's findings
  python bench/review_fix.py --min-severity high --verified-only
  python bench/review_fix.py --undo 20260711_120000         # restore from that run's backup
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")

# bench/ has no __init__.py (implicit namespace package); see bench/review_run.py's own
# comment on the same fix -- needed so `bench.review_run`/`bench.review_build_goals` resolve
# whether this file is run as `python bench\review_fix.py` or `python -m bench.review_fix`.
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import bench.review_run as review_run  # noqa: E402  (see sys.path fix above)
from bench.review_aggregate import parse_findings_block  # noqa: E402
from bench.review_build_goals import (  # noqa: E402
    build_fix_goal,
    group_files,
    write_goals_jsonl,
)

DEFAULT_OUT_DIR = ".fleet/review_fix"
DEFAULT_MIN_SEVERITY = "medium"
DEFAULT_MAX_FILES_PER_GOAL = 5
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_EFFORT = "auto"

_SEV_RANK = {"low": 0, "medium": 1, "high": 2}


def _stamp():
    """Wall-clock timestamp used in output filenames/branch names/backup dirs. A bare
    module-level function (not inlined) so tests can monkeypatch bench.review_fix._stamp for
    deterministic, collision-free names -- mirrors bench/review_run.py:_stamp."""
    return time.strftime("%Y%m%d_%H%M%S")


def _resolve_out_dir(out_dir, repo_root):
    return out_dir if os.path.isabs(out_dir) else os.path.join(repo_root, out_dir)


# --- report loading -------------------------------------------------------------------------

def find_latest_report(out_dir):
    """Return the path to the lexically-last review_report_*.json under out_dir, or None if
    none exist. review_run.py's stamp format (YYYYMMDD_HHMMSS) sorts lexically == chronologically,
    so lexical-last is also newest. Never raises (a missing out_dir just yields no matches)."""
    pattern = os.path.join(out_dir, "review_report_*.json")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def load_report(path):
    """Tolerantly load a review_report_*.json. Never raises: a missing path, unreadable file,
    corrupt JSON, or a JSON value that isn't an object all yield {"findings": [], "error": ...}
    instead of propagating. A dict that parses fine but is missing/misshapen "findings" gets a
    normalized empty list rather than a KeyError downstream."""
    if not path:
        return {"findings": [], "error": "no report path given"}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"findings": [], "error": "could not read report %s: %s" % (path, e)}
    if not isinstance(data, dict):
        return {"findings": [], "error": "report %s is not a JSON object" % (path,)}
    if not isinstance(data.get("findings"), list):
        data = dict(data)
        data["findings"] = []
    return data


# --- filtering / grouping -------------------------------------------------------------------

def filter_findings(findings, min_severity=DEFAULT_MIN_SEVERITY, verified_only=False):
    """Keep findings at or above min_severity ("low" < "medium" < "high"); ALWAYS drop
    verify_verdict == "false_positive" regardless of severity. If verified_only is requested
    but NO finding in the input carries a "verified" key at all (the report was built without
    `/review --verify`), the verified-only filter is skipped (not silently emptied) and a
    warning is printed so the caller notices the report has no verified data."""
    min_rank = _SEV_RANK.get(str(min_severity).lower(), _SEV_RANK[DEFAULT_MIN_SEVERITY])

    kept = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("verify_verdict") == "false_positive":
            continue
        sev = str(f.get("severity", "low")).lower()
        rank = _SEV_RANK.get(sev, _SEV_RANK["low"])
        if rank < min_rank:
            continue
        kept.append(f)

    if verified_only:
        has_verified_field = any(
            isinstance(f, dict) and "verified" in f for f in findings
        )
        if not has_verified_field:
            print("warning: verified data absent -- no finding in this report carries a "
                  "'verified' field (report was likely built without /review --verify); "
                  "--verified-only NOT applied, keeping severity-filtered findings as-is")
        else:
            kept = [f for f in kept if f.get("verified") is True]

    return kept


def group_findings_by_file(findings, max_files_per_goal=DEFAULT_MAX_FILES_PER_GOAL):
    """Group findings so that ALL findings for one file stay together in the same goal, then
    chunk those per-file groups up to max_files_per_goal files per goal (via group_files, the
    same generic chunker bench.review_build_goals uses for plain filenames). Preserves the
    findings' original relative order within and across files."""
    by_file = {}
    order = []
    for f in findings:
        fp = f.get("file", "") if isinstance(f, dict) else ""
        if fp not in by_file:
            by_file[fp] = []
            order.append(fp)
        by_file[fp].append(f)

    file_chunks = group_files(order, max_files_per_goal)
    result = []
    for chunk in file_chunks:
        group = []
        for fp in chunk:
            group.extend(by_file[fp])
        result.append(group)
    return result


# --- backup / manifest / undo (the critical safety path) ------------------------------------

def backup_files(finding_groups, repo_root, backup_dir, stamp=None, created_at=None):
    """Back up every unique file referenced across `finding_groups` (a list of lists of
    finding dicts, as produced by group_findings_by_file) into backup_dir, preserving its
    repo-relative path. Pure filesystem only -- no git, no fleet.

    For each file: if it exists on disk, shutil.copy2 it into backup_dir/<relpath> (creating
    parent dirs) and record backed_up=True; if it does not exist (the fix is expected to
    CREATE it), record backed_up=False with no copy -- undo() uses this to know whether to
    restore or delete. `stamp`/`created_at` are accepted, not computed here (mirrors
    review_aggregate.aggregate()'s `now=` pattern) so this stays deterministically testable;
    callers (main()) stamp them."""
    by_file_findings = {}
    order = []
    for group in finding_groups:
        for f in group:
            if not isinstance(f, dict):
                continue
            fp = f.get("file", "")
            if not fp:
                continue
            if fp not in by_file_findings:
                by_file_findings[fp] = []
                order.append(fp)
            by_file_findings[fp].append({
                "file": fp, "line": f.get("line"), "title": f.get("title", ""),
            })

    files_manifest = []
    for fp in order:
        full = os.path.join(repo_root, fp)
        backed_up = False
        if os.path.isfile(full):
            dest = os.path.join(backup_dir, fp)
            dest_parent = os.path.dirname(dest)
            os.makedirs(dest_parent if dest_parent else backup_dir, exist_ok=True)
            shutil.copy2(full, dest)
            backed_up = True
        files_manifest.append({
            "path": fp,
            "backed_up": backed_up,
            "findings_applied": by_file_findings[fp],
        })

    return {
        "stamp": stamp,
        "repo_root": repo_root,
        "created_at": created_at,
        "files": files_manifest,
    }


def write_manifest(manifest, backup_dir):
    """Write manifest.json into backup_dir (creating it if needed). Returns the path."""
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path


def load_manifest(backup_dir):
    """Read manifest.json from backup_dir. Raises on missing/corrupt input -- undo() is the
    one caller, and it catches this itself to produce a graceful message instead of a
    traceback."""
    path = os.path.join(backup_dir, "manifest.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def undo(stamp, repo_root, out_dir):
    """Restore repo_root to its pre-fix state using the backup taken under
    out_dir/backup_<stamp>/manifest.json. THE CRITICAL PATH for a non-git user: this is the
    only safety net if a fleet fix goes wrong.

    For each manifest entry: backed_up=True -> byte-exact copy2 back over repo_root/<path>
    (overwriting whatever the fix left there); backed_up=False -> delete repo_root/<path> if
    it still exists (the fix created it; there is nothing to "restore" to, only to remove).
    A missing or corrupt manifest (bad stamp, deleted backup dir, hand-edited JSON) yields a
    plain-text explanation -- this function NEVER raises, so a user's undo .bat always prints
    something readable instead of a traceback."""
    backup_dir = os.path.join(out_dir, "backup_%s" % stamp)
    try:
        manifest = load_manifest(backup_dir)
    except Exception as e:
        return ("undo failed: could not read backup manifest for stamp %r under %s (%s). "
                "No files were changed." % (stamp, backup_dir, e))

    restored = []
    deleted = []
    errors = []
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path", "")
        if not rel:
            continue
        dest = os.path.join(repo_root, rel)
        if entry.get("backed_up"):
            src = os.path.join(backup_dir, rel)
            try:
                dest_parent = os.path.dirname(dest)
                if dest_parent:
                    os.makedirs(dest_parent, exist_ok=True)
                shutil.copy2(src, dest)
                restored.append(rel)
            except Exception as e:
                errors.append("restore failed for %s: %s" % (rel, e))
        else:
            if os.path.isfile(dest):
                try:
                    os.remove(dest)
                    deleted.append(rel)
                except Exception as e:
                    errors.append("delete failed for %s: %s" % (rel, e))

    lines = ["undo %s: restored %d file(s), deleted %d newly-created file(s)" %
             (stamp, len(restored), len(deleted))]
    for r in restored:
        lines.append("  restored: %s" % r)
    for d in deleted:
        lines.append("  deleted (was created by the fix): %s" % d)
    if errors:
        lines.append("errors:")
        for e in errors:
            lines.append("  %s" % e)
    return "\n".join(lines)


def write_undo_bat(stamp, repo_root, out_dir):
    """Drop a plain, ASCII-only, double-clickable .bat next to the backup that runs the undo
    for a user who has never opened a terminal. ASCII-only per repo convention (cp932 console
    garbles non-ASCII .bat content)."""
    venvpy = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
    script = os.path.join(repo_root, "bench", "review_fix.py")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "undo_%s.bat" % stamp)
    content = (
        "@echo off\r\n"
        "REM Restores files changed by review-fix run %s.\r\n" % stamp +
        '"%s" "%s" --undo %s\r\n' % (venvpy, script, stamp) +
        "pause\r\n"
    )
    with open(path, "w", encoding="ascii", newline="") as f:
        f.write(content)
    return path


# --- git bonus (strictly optional) -----------------------------------------------------------

def git_bonus_precheck(repo_root):
    """True only if: git is on PATH, repo_root is inside a git work tree, AND the tree is
    clean (git status --porcelain is empty). ANY failure (git missing, not a repo, dirty tree,
    subprocess error) returns False -- callers must treat the fix as running WITHOUT git in
    that case, never as an error. Mirrors bench.review_build_goals.enumerate_files' own git
    subprocess style (capture_output, text, utf-8); this is plain git, not the MCP git tools."""
    if not shutil.which("git"):
        return False
    try:
        r1 = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r1.returncode != 0 or r1.stdout.strip() != "true":
            return False
        r2 = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r2.returncode != 0:
            return False
        return r2.stdout.strip() == ""
    except Exception:
        return False


def git_bonus_branch(repo_root, branch):
    """Best-effort `git checkout -b branch`. Swallows and logs (prints) any failure instead of
    raising -- the fix must proceed with or without the branch. Returns True iff the branch
    was actually created."""
    try:
        r = subprocess.run(
            ["git", "-C", repo_root, "checkout", "-b", branch],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            print("git_bonus_branch: failed to create branch %r: %s" % (branch, r.stderr.strip()))
            return False
        return True
    except Exception as e:
        print("git_bonus_branch: exception creating branch %r: %s" % (branch, e))
        return False


def git_bonus_commit(repo_root, branch, touched_files):
    """Best-effort `git add -- <touched_files>` + `git commit`. NEVER `git add -A` (repo
    convention -- must not sweep up unrelated working-tree state). Returns `branch` on success,
    None on any failure or if there is nothing to commit."""
    if not touched_files:
        return None
    try:
        cmd_add = ["git", "-C", repo_root, "add", "--"] + list(touched_files)
        r1 = subprocess.run(cmd_add, capture_output=True, text=True, encoding="utf-8")
        if r1.returncode != 0:
            print("git_bonus_commit: git add failed: %s" % r1.stderr.strip())
            return None
        msg = "review-fix: apply %d finding-targeted file change(s)" % len(touched_files)
        r2 = subprocess.run(
            ["git", "-C", repo_root, "commit", "-m", msg],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r2.returncode != 0:
            print("git_bonus_commit: git commit failed: %s" % r2.stderr.strip())
            return None
        return branch
    except Exception as e:
        print("git_bonus_commit: exception: %s" % e)
        return None


# --- fleet (the one impure launch step, isolated for hermetic tests) ------------------------

def run_fix_fleet(goals_path, max_concurrent, effort):
    """The ONE function that touches the fleet subprocess for review-fix -- thin wrapper over
    bench.review_run.run_fleet (the verified launch contract) so tests can monkeypatch this
    name without touching review_run itself."""
    return review_run.run_fleet(goals_path, max_concurrent, effort)


def run_test_gate(repo_root):
    """Run the post-fix regression gate (`python -m relay.selfimprove.run_all_tests`) as its
    own subprocess. Returns (result, tail): result is "PASSED" or "FAILED"; tail is the last
    ~20 non-empty output lines, kept for the fix report. Isolated in its own function (rather
    than inlined in main()) so tests can monkeypatch a canned pass/fail without touching
    subprocess or a real .venv."""
    try:
        r = subprocess.run(
            [VENVPY, "-m", "relay.selfimprove.run_all_tests"],
            cwd=repo_root, capture_output=True, text=True, timeout=600,
        )
        result = "PASSED" if r.returncode == 0 else "FAILED"
        tail_lines = [ln for ln in ((r.stdout or "") + (r.stderr or "")).splitlines()
                      if ln.strip()]
        return result, "\n".join(tail_lines[-20:])
    except Exception as e:
        return "FAILED", "could not run test gate: %s" % e


# --- CLI --------------------------------------------------------------------------------------

def build_arg_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default=None,
                     help="review_report_*.json path (default: latest under .fleet/review)")
    ap.add_argument("--min-severity", choices=["low", "medium", "high"],
                     default=DEFAULT_MIN_SEVERITY)
    ap.add_argument("--verified-only", action="store_true")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT)
    ap.add_argument("--group-size", type=int, default=DEFAULT_MAX_FILES_PER_GOAL,
                     help="max files per fix goal (a file's findings always stay together)")
    ap.add_argument("--effort", choices=["min", "auto"], default=DEFAULT_EFFORT)
    ap.add_argument("--dry-run", action="store_true",
                     help="build goals and print the plan; no backup, no git, no fleet")
    ap.add_argument("--branch", default=None,
                     help="git branch name if the git bonus precheck passes "
                          "(default: review-fix-<stamp>)")
    ap.add_argument("--skip-tests", action="store_true",
                     help="skip the post-fix `run_all_tests` gate")
    ap.add_argument("--undo", default=None, metavar="STAMP",
                     help="restore files from a prior run's backup (mutually exclusive with "
                          "everything else) and exit")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    repo_root = REPO
    out_dir = _resolve_out_dir(args.out_dir, repo_root)

    if args.undo:
        print(undo(args.undo, repo_root, out_dir))
        return 0

    review_report_dir = _resolve_out_dir(review_run.DEFAULT_OUT_DIR, repo_root)
    report_path = args.report or find_latest_report(review_report_dir)
    if not report_path:
        print("no report found, run /review first")
        return 0

    report = load_report(report_path)
    if report.get("error"):
        print("could not use report %s: %s" % (report_path, report["error"]))
        return 0

    findings = filter_findings(report.get("findings", []),
                                min_severity=args.min_severity,
                                verified_only=args.verified_only)
    if not findings:
        print("0 findings match, nothing to fix")
        return 0

    groups = group_findings_by_file(findings, max_files_per_goal=args.group_size)
    goals = [build_fix_goal(g, repo_root) for g in groups]

    stamp = _stamp()
    goals_path = os.path.join(out_dir, "goals_%s.jsonl" % stamp)
    write_goals_jsonl(goals, goals_path)
    cmd = review_run.fleet_cmd(goals_path, args.max_concurrent, args.effort)
    branch_name = args.branch or ("review-fix-%s" % stamp)

    if args.dry_run:
        print("DRY RUN -- plan only, no backup / no git / fleet NOT launched")
        print("report: %s" % report_path)
        print("findings matched: %d (min-severity=%s verified-only=%s)" %
              (len(findings), args.min_severity, args.verified_only))
        print("goals: %d" % len(goals))
        for i, g in enumerate(groups):
            files_in_group = []
            for f in g:
                fp = f.get("file", "?")
                if fp not in files_in_group:
                    files_in_group.append(fp)
            print("  goal %d/%d: %d finding(s) across %d file(s): %s" %
                  (i + 1, len(groups), len(g), len(files_in_group), ", ".join(files_in_group)))
        print("goals file: %s" % goals_path)
        print("fleet cmd: %s" % cmd)
        would_git = git_bonus_precheck(repo_root)
        print("git bonus branch %r would be used: %s" % (branch_name, would_git))
        return 0

    # --- backup BEFORE anything touches the repo ---------------------------------------------
    backup_dir = os.path.join(out_dir, "backup_%s" % stamp)
    manifest = backup_files(groups, repo_root, backup_dir, stamp=stamp, created_at=time.time())
    write_manifest(manifest, backup_dir)
    undo_bat = write_undo_bat(stamp, repo_root, out_dir)
    print("backed up %d file(s) to %s" % (len(manifest["files"]), backup_dir))

    branch_created = False
    if git_bonus_precheck(repo_root):
        branch_created = git_bonus_branch(repo_root, branch_name)
    else:
        print("git bonus precheck failed (git missing / not a repo / dirty tree) -- "
              "proceeding without a branch; backup/undo is the safety net")

    print("launching %d fix goal(s)..." % len(goals))
    run_fix_fleet(goals_path, args.max_concurrent, args.effort)

    status_path = os.path.join(repo_root, ".fleet", "status.json")
    applied = []
    skipped = []
    touched_files = []
    if os.path.isfile(status_path):
        try:
            with open(status_path, encoding="utf-8") as f:
                status = json.load(f)
            workers = status.get("workers", []) if isinstance(status, dict) else []
            for w in workers:
                if not isinstance(w, dict):
                    continue
                text = w.get("display_result") or w.get("last") or ""
                fixes, _perr = parse_findings_block(text)
                for fx in fixes:
                    if not isinstance(fx, dict):
                        continue
                    if fx.get("applied"):
                        applied.append(fx)
                        fp = fx.get("file")
                        if fp and fp not in touched_files:
                            touched_files.append(fp)
                    else:
                        skipped.append(fx)
        except Exception as e:
            print("WARNING: could not parse fleet status.json: %s" % e)
    else:
        print("WARNING: fleet run finished but %s was not found -- the fleet run likely "
              "failed to start; the backup and undo .bat are still in place." % status_path)

    if branch_created:
        git_bonus_commit(repo_root, branch_name, touched_files)

    if not args.skip_tests:
        test_result, test_tail = run_test_gate(repo_root)
    else:
        test_result, test_tail = "skipped", ""

    os.makedirs(out_dir, exist_ok=True)
    fix_report = {
        "stamp": stamp,
        "report_used": report_path,
        "findings_attempted": len(findings),
        "findings_applied": applied,
        "findings_skipped": skipped,
        "backup_dir": backup_dir,
        "undo_bat": undo_bat,
        "undo_cmd": '"%s" "%s" --undo %s' % (VENVPY, os.path.join(repo_root, "bench",
                                                                    "review_fix.py"), stamp),
        "branch": branch_name if branch_created else None,
        "test_result": test_result,
        "test_output_tail": test_tail,
    }
    json_path = os.path.join(out_dir, "fix_report_%s.json" % stamp)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(fix_report, f, ensure_ascii=False, indent=2)

    md_lines = ["# Review-fix report %s" % stamp, ""]
    md_lines.append("- report used: %s" % report_path)
    md_lines.append("- findings attempted: %d" % len(findings))
    md_lines.append("- findings applied: %d" % len(applied))
    md_lines.append("- findings skipped (worker chose not to fix): %d" % len(skipped))
    md_lines.append("- backup: %s" % backup_dir)
    md_lines.append("- undo (double-click): %s" % undo_bat)
    md_lines.append("- undo (command line): %s" % fix_report["undo_cmd"])
    md_lines.append("- git branch: %s" %
                     (branch_name if branch_created else "(none -- git bonus not used)"))
    md_lines.append("- test gate (`relay.selfimprove.run_all_tests`): %s" % test_result)
    if test_result == "FAILED":
        md_lines.append("")
        md_lines.append("**NOT reverted -- inspect manually.** The backup and (if created) "
                         "the git branch are preserved. Run the undo command above if you "
                         "want to roll back these changes.")
        if test_tail:
            md_lines.append("")
            md_lines.append("```")
            md_lines.append(test_tail)
            md_lines.append("```")
    md_path = os.path.join(out_dir, "fix_report_%s.md" % stamp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")

    print("fix report: %s" % md_path)
    print("applied=%d skipped=%d test_gate=%s" % (len(applied), len(skipped), test_result))
    print("backup: %s" % backup_dir)
    print("undo: %s (or `%s`)" % (undo_bat, fix_report["undo_cmd"]))
    if branch_created:
        print("branch: %s" % branch_name)
    if test_result == "FAILED":
        print("TEST GATE FAILED -- NOT reverted. Inspect manually; undo available above.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
