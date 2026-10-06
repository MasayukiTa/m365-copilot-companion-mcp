# -*- coding: utf-8 -*-
"""The thing that actually runs the self-improvement loop, on a schedule, without being asked.

WHY THIS FILE EXISTS. The loop had every part except the part that starts it.

  * `relay/selfimprove/scheduler.py` has the preconditions an unattended run must pass, and
    `nightly()` to run one.
  * `relay/selfimprove/l2_cron.py` has a single-instance lock, a frozen-set gate and
    `cron_command()` -- "the exact command string an operator registers with Task Scheduler".
    Its own docstring adds: "This module does NOT register any schedule -- that is a separate,
    deliberate operator step."
  * `scripts/run_nightly_real.py` opens with "It has never been run at all."

That step was never taken. Measured 2026-09-14: the machine had exactly one scheduled task and
it was unrelated; the newest file under `.fleet/selfimprove/` was `hypotheses.jsonl`, last
written 2026-08-25. Twenty days. The preconditions evaluated CLEAR at the time -- nothing was
blocking a run, nothing was asking for one.

WHAT IT DOES, AND WHAT IT REFUSES TO DO.

It runs ONE nightly pass in a child process, with a wall-clock limit, and writes a row about it
either way. It does not decide anything about the experiment: which coordinate, which arms,
whether a winner may be installed (it may not -- `run_nightly_real` passes `activate=False`) are
all the entry point's business and stay there.

THE LIMIT IS WALL CLOCK AND IT IS ENFORCED FROM OUTSIDE. `scheduler.preconditions` lists "a
budget -- so a loop that finds a productive-looking direction cannot spend a night on it", and
`l2.SpendCeiling` is the in-process version of that. An in-process ceiling is worth nothing to a
scheduled task: each firing is a new process, so `iters` starts at 0 every time and the ceiling
can never be reached. A timeout on the child is a bound that holds no matter what the loop does
with its own counters.

EVERY FIRING IS RECORDED, INCLUDING THE ONES THAT DO NOTHING. The failure this loop already had
was silence: nothing ran and nothing said so. A scheduled task that dies on an import error
looks exactly like a scheduled task that has not fired yet, and the repository would be back to
where it started with a file saying it is scheduled. So a row goes into
`.fleet/selfimprove/driver.jsonl` before the child starts and another when it ends, and the exit
status is non-zero for anything that is not a clean pass -- Task Scheduler shows the last result
of a task, and a task that always reports success teaches nobody anything.

    python scripts/selfimprove_driver.py                 # one pass, as the schedule runs it
    python scripts/selfimprove_driver.py --status        # when it last ran, and what happened
    python scripts/selfimprove_driver.py --preconditions # what a run would be blocked by, only
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Where a firing is recorded. Append-only: the question this has to answer is "has it run, and
#: what happened", and that is a history rather than a state.
LOG = os.path.join(REPO, ".fleet", "selfimprove", "driver.jsonl")

#: The entry point. `run_nightly_real` rather than `l2_cron`, and the two are not
#: interchangeable: l2_cron drives one SWE-bench iteration, while this is the rung that decides
#: WHAT to try -- it reads recent decisions, checks the harness, selects a replay set and sweeps
#: a coordinate. That is the part the phrase "the system proposes its own experiments" refers to.
ENTRY = os.path.join(REPO, "scripts", "run_nightly_real.py")

#: Which coordinate to sweep. `transport` is the one the route evaluator can actually judge;
#: handing it the others produces confident rows about arms that ran the same program.
COORDINATE = "transport"

#: The wall-clock bound, in seconds. Four hours: long enough for a sweep whose arms each run a
#: goal set, short enough that a firing cannot still be going when the next one is due.
TIMEOUT_S = 4 * 3600


def _record(row):
    """Append one row. Never raises: a driver that dies while writing its own log is worse than
    one that loses a line of it."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def rows(path=None):
    """Every recorded firing, oldest first. A truncated tail is a partial answer, not none."""
    out = []
    try:
        for line in io.open(path or LOG, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    except OSError:
        pass
    return out


def blocked_by():
    """What a run would decline for, or [] if nothing. Reads state; starts nothing."""
    from relay.selfimprove import scheduler as S
    return list(S.preconditions(budget_candidates=1, activate=False) or [])


def run_once(*, run=None, now=None, timeout_s=TIMEOUT_S, coordinate=COORDINATE):
    """One firing. Returns the recorded outcome row.

    `run` is injectable so the tests never start a real sweep -- the default drives the fleet,
    which is the whole point of the file and exactly what a test must not do.
    """
    now = now or time.time
    started = now()
    _record({"event": "start", "ts": started, "entry": os.path.basename(ENTRY),
             "coordinate": coordinate, "timeout_s": timeout_s})

    if run is None:
        run = _spawn
    try:
        result = run([sys.executable, ENTRY, coordinate], timeout_s)
    except Exception as e:
        result = {"status": "error", "reason": "%s: %s" % (type(e).__name__, e)}

    row = dict(result)
    row.update({"event": "end", "ts": now(), "elapsed_s": round(now() - started, 1)})
    _record(row)
    return row


def _spawn(argv, timeout_s):
    """Run the entry point in a child and say plainly how it ended.

    IN A CHILD, so a sweep that wedges can be killed on the timeout -- an in-process call could
    only be bounded by something the loop itself honours, which is the assumption that made
    SpendCeiling useless to a scheduler in the first place.

    `tools.childproc.run`, not `subprocess.run(text=True)`: the latter decodes with the local
    code page, so one non-cp932 byte from a child loses the WHOLE output. This repository has a
    test that refuses the call shape, and a nightly log is exactly where a lost output would go
    unnoticed for weeks.
    """
    from tools import childproc
    try:
        out = childproc.run(argv, cwd=REPO, timeout=timeout_s)
    except Exception as e:
        if "timeout" in type(e).__name__.lower() or "Timeout" in repr(e):
            return {"status": "timeout", "reason": "exceeded %ss" % timeout_s}
        raise
    tail = (out.stdout or "")[-4000:]
    if out.returncode != 0:
        return {"status": "error", "returncode": out.returncode,
                "reason": ((out.stderr or "") or tail)[-1000:]}
    # The entry point prints its own verdict as a json object when it declines.
    blocked = None
    for line in tail.splitlines():
        if line.startswith("[nightly] preconditions:") and "CLEAR" not in line:
            blocked = line.split(":", 1)[1].strip()
    return {"status": "blocked" if blocked else "ran", "reason": blocked or "",
            "tail": tail[-2000:]}


def refresh_skill_proposals(propose=None, write=None):
    """Rebuild the Skill proposals from the ledger, and record what came of it.

    WHY IT RIDES ON THIS FIRING RATHER THAN ITS OWN SCHEDULE. The requirement on the proposal
    path was that it "certainly gets made, certainly runs, and certainly stays up to date". A
    second scheduled task is a second thing that can silently stop -- this repository has one
    loop that spent twenty days doing nothing because nobody looked at its exit code, which is
    the entire reason `_status_text` exists. Attaching to a firing that is already watched costs
    nothing and cannot rot separately.

    IT IS FAILURE-ISOLATED ON PURPOSE. The driver's job is to run the self-improvement sweep;
    drafting proposals is a by-product. A traceback in the by-product must not cost the sweep,
    so everything here is caught and recorded as a row rather than raised -- and recorded
    rather than swallowed, because a by-product that fails invisibly every night is worse than
    one that was never wired up at all.

    WHAT IT WRITES: only `.fleet/skill_proposals/`, which is git-ignored and outside skill
    discovery. Nothing here can make a Skill live; see `tools/skill_draft` for why that step is
    a person's.
    """
    started = time.time()
    try:
        if propose is None or write is None:
            from tools import skill_draft
            propose = propose or skill_draft.propose
            write = write or skill_draft.write
        result = propose()
        written = write(result)
        row = {"event": "skill_proposals", "ts": time.time(),
               "elapsed_s": round(time.time() - started, 1),
               "status": "ok",
               "new": len(result.get("new") or []),
               "amend": len(result.get("amend") or []),
               "refused": len(result.get("refused") or []),
               "lesson_pairs": result.get("lesson_pairs", 0),
               "files": len(written)}
    except Exception as e:
        row = {"event": "skill_proposals", "ts": time.time(),
               "elapsed_s": round(time.time() - started, 1),
               "status": "error", "reason": "%s: %s" % (type(e).__name__, e)}
    _record(row)
    return row


def _status_text():
    got = rows()
    if not got:
        return ("selfimprove driver: HAS NEVER RUN. If a schedule is registered, it has not "
                "fired -- or it died before writing a line, which is the same thing to anyone "
                "reading this.")
    ends = [r for r in got if r.get("event") == "end"]
    if not ends:
        return "selfimprove driver: %d start(s) recorded and no end -- every firing died." % len(got)
    last = ends[-1]
    return ("selfimprove driver: %d firing(s); last %s at %s, status=%s%s"
            % (len(ends), last.get("event"),
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last.get("ts") or 0))),
               last.get("status"),
               (" (%s)" % last.get("reason")) if last.get("reason") else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="when it last ran, and what happened")
    ap.add_argument("--preconditions", action="store_true",
                    help="what a run would be blocked by; starts nothing")
    ap.add_argument("--timeout-s", type=int, default=TIMEOUT_S)
    ap.add_argument("--coordinate", default=COORDINATE)
    args = ap.parse_args(argv)

    if args.status:
        print(_status_text())
        return 0
    if args.preconditions:
        reasons = blocked_by()
        print("blocked by: %s" % (", ".join(reasons) if reasons else "nothing"))
        return 0

    row = run_once(timeout_s=args.timeout_s, coordinate=args.coordinate)
    print("selfimprove driver: status=%s%s"
          % (row.get("status"), (" reason=%s" % row["reason"]) if row.get("reason") else ""))
    # AFTER the sweep, and outside its status. The sweep's exit code is what Task Scheduler
    # shows and what anyone debugging the loop reads; folding a by-product's failure into it
    # would make a green sweep look red for a reason that has nothing to do with the fleet.
    drafts = refresh_skill_proposals()
    if drafts.get("status") == "ok":
        print("skill proposals: %d new, %d amendments, %d refused (from %d lesson pairs)"
              % (drafts["new"], drafts["amend"], drafts["refused"], drafts["lesson_pairs"]))
    else:
        print("skill proposals: FAILED -- %s" % drafts.get("reason"))
    # NON-ZERO FOR ANYTHING THAT IS NOT A CLEAN PASS, including "blocked". Task Scheduler shows
    # the last result, and a task that always reports success is a task nobody ever looks at --
    # which is how this loop spent twenty days doing nothing.
    return 0 if row.get("status") == "ran" else 1


if __name__ == "__main__":
    raise SystemExit(main())
