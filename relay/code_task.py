"""code_task.py -- the natural-language front door for autonomous coding (Claude-Code style).

You do NOT write a goals file, pick a mode, or spell out --check-cmd. You say what you
want in plain language and point at a folder; this turns it into ONE self-verifying task:

    python -m relay.code_task --folder C:\\proj --instruction "落ちてるテストを直して"

It auto-detects how to verify the folder (project_introspect: pytest if there is a test
suite, else compileall; npm test for Node), attaches those as the acceptance gate, and
runs the autonomous loop -- the agent explores and edits whatever it needs, and the frame
only accepts DONE once the project's own tests/compile actually pass. That is the
Claude-Code experience: natural language in, verified work out, no ceremony.

Contrast with folder_coder --mode per-file, which fans one instruction across every file
(useful for mechanical per-file sweeps, wrong for a single cross-cutting task like "fix
the bug" -- that is ONE task the agent should scope itself, which is what this does).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.project_introspect import detect_checks

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def build_goal(instruction, folder, extra_check=None, no_verify=False,
               with_map=True, map_chars=4000, with_memory=True, state_dir=".fleet"):
    """Turn a natural-language instruction + folder into one self-verifying goal dict.
    Verification is auto-detected unless no_verify; extra_check (a shell command string)
    is appended on top of whatever was detected. When with_map, a compact repo map is
    prepended so the agent starts oriented (like Claude Code/aider) instead of blind."""
    folder = os.path.abspath(folder)
    checks, notes = [], []
    if not no_verify:
        det = detect_checks(folder)
        checks, notes = list(det["checks"]), list(det["notes"])
    if extra_check:
        checks.append({"type": "shell", "cmd": extra_check, "cwd": folder})
        notes.append("extra check: %s" % extra_check)
    prefix = ""
    if with_memory:
        try:
            from relay.project_memory import load_notes
            mem = load_notes(folder, state_dir=state_dir)
            if mem:
                prefix += ("--- このリポジトリでの過去の作業メモ ---\n%s\n"
                           "--- メモここまで ---\n\n" % mem)
                notes.append("project memory primed")
        except Exception:
            pass
    if with_map:
        try:
            from relay.repo_map import build_map
            m = build_map(folder, max_chars=map_chars)
            if m:
                prefix += ("まず下記のリポジトリ地図で全体構成を把握してから着手してください"
                           "（詳細は read_file で確認）。\n--- リポジトリ地図 ---\n%s\n"
                           "--- 地図ここまで ---\n\n" % m)
                notes.append("repo map primed (%d chars)" % len(m))
        except Exception:
            pass
    text = (prefix
            + "対象リポジトリ %s に対して、次の作業を行ってください: %s\n"
              "必要なファイルを list_directory / grep / read_file で調べ、"
              "replace_in_file / write_file で編集してください。"
              % (folder, instruction))
    goal = {"text": text, "cwd": folder}
    if checks:
        goal["checks"] = checks
    return goal, notes


def main():
    ap = argparse.ArgumentParser(
        description="Natural-language autonomous coding with auto-verification "
                    "(Claude-Code style): say what you want, point at a folder.")
    ap.add_argument("--instruction", "-i", required=True,
                    help="what to do, in plain language (e.g. '落ちてるテストを直して')")
    ap.add_argument("--folder", "-f", default=os.getcwd(),
                    help="target repository folder (default: current directory)")
    ap.add_argument("--agent-url", default=(os.environ.get("MCP_FLEET_AGENT_URL")
                                            or os.environ.get("MCP_IMPL_AGENT_URL", "")))
    ap.add_argument("--max-turns", type=int, default=1000)
    ap.add_argument("--extra-check", default=None,
                    help="an extra shell command that must exit 0 for DONE "
                         '(on top of auto-detected verification), e.g. "ruff check ."')
    ap.add_argument("--no-verify", action="store_true",
                    help="skip auto-detected verification (accept a self-reported DONE)")
    ap.add_argument("--effort", choices=["min", "max", "ultra", "auto"], default=None,
                    help="scaffold effort for this single run (min/max/ultra/auto). Passed through "
                         "to fleet_runner (a single run IS a 1-goal fleet), so it gets the SAME "
                         "effort presets as a fleet -- incl. auto's minimality refuter. Omitted -> "
                         "fleet_runner follows the cockpit's settings.txt effort= (default auto).")
    ap.add_argument("--refuter", action="store_true",
                    help="after a candidate DONE, an independent reviewer tries to refute "
                         "it before accepting (operator B; doubles oracle cost)")
    ap.add_argument("--panel", action="store_true",
                    help="review with a perspective-diverse PANEL (correctness/edge/"
                         "security, majority vote) instead of one reviewer (~3x cost)")
    ap.add_argument("--plan", action="store_true",
                    help="plan-first: propose a numbered plan and pause for your approval "
                         "(approve or edit via a steer in the cockpit) before executing")
    ap.add_argument("--no-map", action="store_true",
                    help="do not prepend a repo map to the goal (saves prompt size on a "
                         "huge tree; default is to prime the agent with the map)")
    ap.add_argument("--no-memory", action="store_true",
                    help="do not prime prior project notes / record this task "
                         "(default: accumulate per-folder memory across tasks)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the detected verification + goal and exit (do not run)")
    ap.add_argument("--state-dir", default=None,
                    help="status.json dir for the cockpit (default: repo .fleet)")
    args = ap.parse_args()

    if not os.path.isdir(args.folder):
        ap.error("no such folder: %s" % args.folder)
    if not args.agent_url and not args.dry_run:
        ap.error("no agent URL -- pass --agent-url or set MCP_FLEET_AGENT_URL in .env")

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = args.state_dir or os.path.join(repo, ".fleet")
    os.makedirs(state_dir, exist_ok=True)

    goal, notes = build_goal(args.instruction, args.folder,
                             extra_check=args.extra_check, no_verify=args.no_verify,
                             with_map=not args.no_map,
                             with_memory=not args.no_memory, state_dir=state_dir)

    print("code_task: %s" % args.instruction)
    print("  folder: %s" % os.path.abspath(args.folder))
    print("  verification:")
    for n in notes:
        print("    - " + n)

    # pre-flight RAM check: under memory pressure the heavy M365 SPA is slow and sends /
    # turns get flaky. Warn loudly so a failed run isn't a mystery (the companion Edge tab
    # alone wants ~0.3-0.6 GB).
    try:
        from relay.relay_fleet import avail_phys_mb
        free_mb = avail_phys_mb()
        if free_mb < 1500 and not args.dry_run:
            print("  WARNING: only %d MB free RAM. The M365 Copilot tab is heavy (~0.5 GB) "
                  "and runs get flaky below ~1.5 GB free. Consider closing apps, or "
                  "recreating the companion Edge profile, for reliable runs."
                  % round(free_mb))
    except Exception:
        pass

    goals_file = os.path.join(state_dir, "code_task.goals.jsonl")
    with open(goals_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(goal, ensure_ascii=False) + "\n")

    if args.dry_run:
        print("  goal: %s" % goal["text"].replace("\n", " "))
        print("  (dry-run: not launching)")
        return 0

    # one task, one tab: reuse the full fleet machinery (status.json, watchdog, recovery)
    cmd = [sys.executable, "-m", "relay.fleet_runner", "--goals-file", goals_file,
           "--max-concurrent", "1", "--max-turns", str(args.max_turns),
           "--agent-url", args.agent_url, "--state-dir", state_dir]
    if args.effort:
        cmd += ["--effort", args.effort]
    if args.refuter:
        cmd.append("--refuter")
    if args.panel:
        cmd.append("--panel")
    if args.plan:
        cmd.append("--plan")
    print("  launching: fleet_runner (1 tab, auto-verified)\n")
    rc = subprocess.call(cmd, cwd=repo)

    # accumulate per-folder project memory: record what this task did so the NEXT task on
    # this folder is primed with it (read the outcome from the run's final status.json).
    if not args.no_memory:
        try:
            from relay.project_memory import record_task
            d = json.load(open(os.path.join(state_dir, "status.json"), encoding="utf-8"))
            w = (d.get("workers") or [{}])[0]
            record_task(args.folder, args.instruction, w.get("outcome") or "",
                        note=(w.get("reason") or w.get("last") or "")[:200],
                        state_dir=state_dir)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
