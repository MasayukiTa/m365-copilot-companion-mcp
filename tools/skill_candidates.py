# -*- coding: utf-8 -*-
"""Which past work has earned a Skill, decided from the ledger rather than from memory.

WHY THIS IS THE FIRST PIECE. Every Skill in this repository was typed by a person:
`relay/skills.py::create_local` writes the name, description and instructions verbatim from
what somebody entered at `/skill-create`, and nothing anywhere reads a transcript, a fleet run
or a document to produce one. Measured 2026-09-14 -- an exhaustive search for an authoring path
found none, and no document claims one is planned. So "past work becomes a Skill" begins by
answering which past work qualifies, and that answer has to come from the record.

WHAT THE RECORD ACTUALLY SUPPORTS, AND WHAT IT DOES NOT. `.fleet/socket_route.jsonl` holds 3,917
`worker_done` rows carrying the goal, a status and an outcome. It does NOT hold a verdict on the
ANSWER: no row has a `verified` field, and `outcome=DONE` (2,129 of them) is the worker saying
DONE. The only externally-judged signal in the file is negative -- `EVIDENCE_CONTRADICTED`, 221
rows. So this module can say "this work was done repeatedly and did not end badly". It cannot
say "this output was good", and it must never be read as saying so: the step that fixes an
IDEAL output is a human approving one, and it is the only step here that cannot be automated.

THE GROUPING IS THE HARD PART AND IT IS STATED, NOT HIDDEN. Two runs are "the same work" when
their goals normalise to the same text -- paths, dates, GUIDs and bare numbers replaced by
placeholders. Get that wrong and every count below is wrong, so `normalise` is small, explicit
and tested, and `--show-groups` prints what was merged so a person can disagree with it.

IT WRITES NOTHING, AND THAT IS NOT AN ACCIDENT. Goal text is business content: customer names,
telephone numbers, internal paths. It lives under .fleet, which is git-ignored, and this tool
reads it and prints to stdout. Nothing here may copy a goal into a tracked file -- the rule this
repository has broken three times.

    python -m tools.skill_candidates                # the ranked candidates
    python -m tools.skill_candidates --show-groups  # what was merged into each, to disagree with
    python -m tools.skill_candidates --all          # include benchmark traffic
    python -m tools.skill_candidates --json         # for a driver
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

LEDGER = os.path.join(REPO, ".fleet", "socket_route.jsonl")
SKILLS_DIR = os.path.join(REPO, "skills")

#: How many times the same work has to have been done before a Skill is worth writing. Two is
#: not enough: a goal run twice is as likely to be a retry of a failure as a habit. Measured on
#: this machine, 3+ leaves 157 non-benchmark groups out of 419 distinct goals.
MIN_RUNS = 3

#: And at least this many of them have to have finished. A goal attempted five times and stuck
#: five times is a candidate for a FIX, not for a Skill -- writing down a procedure that has
#: never worked is how a store fills with instructions nobody can follow.
MIN_DONE = 2

#: Work whose goal names one of these is benchmark traffic, not the operator's own work. It
#: dominates the ledger (405 of 824 distinct goals) and would bury everything else. `--all`
#: turns the filter off, and the count it removed is always printed -- a filter that hides how
#: much it hid is the shape of scan this repository keeps being bitten by.
BENCHMARK_MARKS = ("django", "ansible", "nodebb", "sympy", "scikit", "matplotlib",
                   "open-source project", "mcp_loadtest", "swe-bench", "swebench")

_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+|/(?:home|tmp|usr|var)/[^\s\"'<>|]+")
_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_DATE = re.compile(r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?\b|\b\d{8}-\d{6}\b")
_NUM = re.compile(r"\b\d[\d,._-]{2,}\b")
_WS = re.compile(r"\s+")


def normalise(goal):
    """The text two runs share when they are the same work.

    ORDER MATTERS: paths first, because a path contains digits and dates that the later rules
    would otherwise chew out of the middle of it and leave a stump that no longer matches.
    """
    text = str(goal or "")
    text = _PATH.sub("<path>", text)
    text = _GUID.sub("<id>", text)
    text = _DATE.sub("<date>", text)
    text = _NUM.sub("<n>", text)
    text = _WS.sub(" ", text).strip().lower()
    return text


def is_benchmark(goal):
    low = str(goal or "").lower()
    return any(m in low for m in BENCHMARK_MARKS)


#: The ledger keeps only the first 600 characters of a goal -- `relay_fleet.py` writes
#: `goal=(self.goal or "").strip()[:600]` on every `worker_done`, and that cap is load-bearing
#: elsewhere (conversation matching compares the same 600-character prefix on both sides), so it
#: is not something to widen from here.
GOAL_LEDGER_CAP = 600

#: Where the WHOLE goal survives. `bridge/session_store.py` interns every goal exactly once in
#: `fleet_goals`, so the full text is on disk even though the ledger's copy is cut.
SESSIONS_DB = os.path.join(REPO, ".fleet", "sessions", "sessions.sqlite3")


def _full_goals(db_path=None):
    """{first 600 chars: whole goal} for every goal the session store has interned.

    Returns {} when the database is absent or unreadable -- every caller then goes on with the
    truncated text, which is worse but not wrong.
    """
    import sqlite3
    path = db_path or SESSIONS_DB
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            for (goal,) in conn.execute("SELECT goal FROM fleet_goals"):
                if goal and len(goal) > GOAL_LEDGER_CAP:
                    out[goal.strip()[:GOAL_LEDGER_CAP]] = goal
        finally:
            conn.close()
    except Exception:
        return {}
    return out


def rehydrate(goal, table=None):
    """The whole goal behind a ledger row's truncated copy, or the truncation unchanged.

    WHY THIS EXISTS, AND WHAT IT CHANGED. Every measurement this module and `skill_lessons` make
    about what a goal CONTAINS was being made on the first 600 characters, because that is all
    the ledger holds. Measured 2026-09-14 over the 1,977 interned goals: **71% are longer than
    the cap** (median 2,126 characters, longest 14,052), and the difference is not cosmetic --
    goals declaring an output contract go from 19% to 37%, and goals carrying BOTH a path and a
    contract from 15 to 287. The conclusion drawn from the truncated text, that the record holds
    almost no material a Skill could be grounded in, was an artefact of the cap.

    Keyed on the prefix rather than joined through `fleet_turns` on purpose: the outcome
    (DONE / STUCK) lives in the ledger and the full text lives in the database, and the prefix
    is the only thing both are guaranteed to agree on. A prefix collision would attach the wrong
    tail to a goal, so only goals that ACTUALLY exceed the cap are put in the table -- a short
    goal is already whole and has nothing to gain.
    """
    text = str(goal or "")
    if len(text) < GOAL_LEDGER_CAP:
        return text                       # never truncated; nothing to look up
    if table is None:
        table = _full_goals()
    return table.get(text.strip()[:GOAL_LEDGER_CAP], text)


def _rows(path):
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row
    except OSError:
        return


def existing_skill_names(directory=SKILLS_DIR):
    try:
        return sorted(d for d in os.listdir(directory)
                      if os.path.isdir(os.path.join(directory, d)))
    except OSError:
        return []


def collect(ledger=LEDGER, include_benchmarks=False):
    """{normalised goal: group} for finished fleet work, with the counts a decision needs.

    Returns (groups, excluded) so the caller can print how much the filter removed. A scan that
    reports only what survived it is the one that reads as thorough and is not.
    """
    groups, excluded = {}, 0
    _goal_table = _full_goals()          # one query, then thousands of lookups
    for row in _rows(ledger):
        if row.get("event") != "worker_done":
            continue
        # REHYDRATED FIRST. What a group's example goal CONTAINS -- the paths, the output
        # contract -- is the whole reason these groups are collected, and the ledger's copy of
        # it stops at 600 characters. See `rehydrate` for what that cap was hiding.
        goal = rehydrate(str(row.get("goal") or "").strip(), _goal_table)
        if not goal:
            continue
        if not include_benchmarks and is_benchmark(goal):
            excluded += 1
            continue
        key = normalise(goal)
        g = groups.setdefault(key, {
            "key": key, "runs": 0, "done": 0, "contradicted": 0, "stuck": 0,
            "refused": 0, "turns": [], "first_ts": None, "last_ts": None,
            "examples": [],
        })
        g["runs"] += 1
        outcome = str(row.get("outcome") or "").upper()
        if outcome == "DONE":
            g["done"] += 1
        elif outcome == "EVIDENCE_CONTRADICTED":
            g["contradicted"] += 1
        elif outcome == "STUCK":
            g["stuck"] += 1
        elif outcome == "REFUSED":
            g["refused"] += 1
        try:
            g["turns"].append(int(row.get("turns") or 0))
        except (TypeError, ValueError):
            pass
        ts = float(row.get("ts") or 0.0)
        if ts:
            g["first_ts"] = ts if g["first_ts"] is None else min(g["first_ts"], ts)
            g["last_ts"] = ts if g["last_ts"] is None else max(g["last_ts"], ts)
        if len(g["examples"]) < 3 and goal not in g["examples"]:
            g["examples"].append(goal)
    return groups, excluded


def qualifies(group, min_runs=MIN_RUNS, min_done=MIN_DONE):
    """(ok, reason). The reason is returned for the ones that FAIL too -- "these 260 did not
    qualify" with no reason is a number nobody can act on."""
    if group["runs"] < min_runs:
        return False, "run %d time(s), needs %d" % (group["runs"], min_runs)
    if group["done"] < min_done:
        return False, "finished %d time(s), needs %d" % (group["done"], min_done)
    if group["contradicted"] > group["done"]:
        return False, ("contradicted %d times against %d finished -- fix the work before "
                       "writing it down" % (group["contradicted"], group["done"]))
    return True, ""


def candidates(ledger=LEDGER, include_benchmarks=False, min_runs=MIN_RUNS, min_done=MIN_DONE):
    groups, excluded = collect(ledger, include_benchmarks)
    out = {"qualified": [], "rejected": [], "excluded_benchmark_runs": excluded,
           "groups_seen": len(groups)}
    for g in groups.values():
        ok, why = qualifies(g, min_runs, min_done)
        g = dict(g)
        g["median_turns"] = (sorted(g["turns"])[len(g["turns"]) // 2] if g["turns"] else 0)
        if ok:
            out["qualified"].append(g)
        else:
            g["why_not"] = why
            out["rejected"].append(g)
    out["qualified"].sort(key=lambda g: (-g["runs"], -g["done"]))
    out["rejected"].sort(key=lambda g: -g["runs"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="include benchmark traffic")
    ap.add_argument("--show-groups", action="store_true",
                    help="print the goals merged into each candidate, to disagree with")
    ap.add_argument("--min-runs", type=int, default=MIN_RUNS)
    ap.add_argument("--min-done", type=int, default=MIN_DONE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    got = candidates(include_benchmarks=args.all, min_runs=args.min_runs,
                     min_done=args.min_done)
    if args.json:
        print(json.dumps(got, ensure_ascii=False, default=str))
        return 0

    have = existing_skill_names()
    print("groups seen: %d   qualified: %d   rejected: %d   benchmark runs excluded: %d"
          % (got["groups_seen"], len(got["qualified"]), len(got["rejected"]),
             got["excluded_benchmark_runs"]))
    print("skills already on disk: %d (%s)" % (len(have), ", ".join(have) or "none"))
    print()
    print("%-5s %-5s %-6s %-6s %s" % ("runs", "done", "contra", "turns", "work"))
    for g in got["qualified"][:40]:
        print("%-5d %-5d %-6d %-6d %s"
              % (g["runs"], g["done"], g["contradicted"], g["median_turns"],
                 g["examples"][0][:70].replace("\n", " ")))
        if args.show_groups and len(g["examples"]) > 1:
            for ex in g["examples"][1:]:
                print("      also: %s" % ex[:70].replace("\n", " "))
    print()
    print("NOT a verdict on the ANSWERS. `done` is the worker saying DONE; no row in the ledger "
          "carries an external check. Fixing an IDEAL output is a human approving one, once per "
          "skill -- see docs/skill_generation.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
