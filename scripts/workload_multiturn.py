"""A goal set with room to improve, shaped like the operator's actual work.

WHY THE OLD SET HAD TO GO

Across 111 recorded goals, 96.4% finished in ONE turn and 98.2% reached DONE. Turns cannot go
below one and completion cannot exceed all, so every instrument here could only ever detect
harm on that workload -- a candidate that helped had nowhere to show it. That is not a fault in
the instruments and no amount of calibration fixes it.

WHERE THE SHAPE COMES FROM, AND WHAT IS DELIBERATELY NOT COPIED

The operator's recent work is Excel and CSV analysis (a folder of 213 documents, supply-chain
extracts, measurement tables), slide decks, and investigations that read several files and
summarise. Their CSVs run to a median of six columns and twenty-five rows. Those are the SHAPES
the goals below imitate: list a directory, read more than one file, join or compare them,
compute something, write a result, check it.

NONE of the operator's data, filenames, product names, or organisation appear here, and none
ever can: this repository is public and the goals live in a tracked file. The data is generated
by `build()` from a fixed seed, so the numbers are reproducible, the answers are known, and the
acceptance checks can verify them exactly.

WHAT A NULL PASS ON THESE ACTUALLY MEASURED (2026-08-22, 51 min, two identical arms)

    goal                                   arm A   arm B
    join two tables -> write the subset       1       1
    per-lot means -> write a csv              1       1
    largest exceedance -> answer in text     13       4      <- BROKEN, see below
    upcoming calendar entries                 1       1

THREE OF FOUR FINISHED IN ONE TURN IN BOTH ARMS, and the acceptance checks passed with the
exact generated answer (no VERIFY_FAILED in the run). The design argument above -- "each step
needs the previous step's output, so it must take several turns" -- IS WRONG for this harness:
it can run code, so list -> read -> join -> write happens inside a single turn. Intent is not
a measurement, and this set has no more turn headroom than the saturated one it replaced.

The fourth goal took 13 turns against 4 in an identical arm, and that spread was a DEFECT IN
THE GOAL, not a property of the harness. Every goal runs in its OWN worker with no shared
history, and that goal said "the same folder" while naming no path -- so it searched the whole
disk for a folder nobody had told it about. The other file goals only worked because their
output path happened to spell the folder out. Two rules follow, and `test_workload_multiturn`
now enforces both:

  * NO GOAL MAY POINT OUTSIDE ITS OWN TEXT. No "the same folder", "as above", "earlier".
  * EVERY GOAL CARRIES AN ACCEPTANCE CHECK. An ungated goal cannot fail, so a harness that
    cannot find its footing keeps going until the turn cap -- unbounded churn recorded as
    signal. All of the 2.25 turns/goal "gain" in that null run came from this one goal.

WHAT THE SECOND NULL PASS ADDED (2026-08-23, after both rules above were in force)

Completion came off the ceiling for the first time -- 2/4 against 4/4 between two IDENTICAL
arms -- so this set can finally record a failure. That is the property the saturated set never
had. But the arms were not measuring the work: the failing arm's own account was that it had
written under the `~1` short name and the file had not landed, and that `run_python` writes to
an isolated filesystem the acceptance check cannot see. WORKDIR is now resolved, which removes
the first. The second is a real property of the harness rather than a fault in the ruler.

THE SECOND ARM HAS NOW WON THREE RUNS IN A ROW. Memory is isolated per arm and the workspace is
reset between arms, so whatever is left is either position or chance. A mirrored-order null is
the only thing that separates those two, and no threshold on this set means anything until it
has run.

WHAT THE PAIR OF NULLS ON THE FIXED SET MEASURED (2026-08-23, both arm orders)

    order              peak MB          reported "gain"      turns/goal      turns "gain"
    control first      156.7 / 162.0      -5.3 MB            1.00 / 1.00      0.00
    candidate first    354.7 / 157.9    +196.8 MB            1.25 / 1.00      0.25

TURNS: the spread between identical arms is 0.00 and 0.25 -- tighter than the 0.50 the old set
gave, but every arm sits at or beside 1.00 turns per goal, so a candidate has nowhere to show
an improvement and only harm remains detectable. The same ceiling as before, reached by a
shorter route.

MEMORY: the first of these two runs came back 5.3 MB apart, the mirrored one 196.8 MB apart.
The 5.3 was luck, and a floor adopted from the first run alone would have turned ordinary
noise into a finding.

    TWO CORRECTIONS TO WHAT WAS WRITTEN HERE FIRST. Both sets run FOUR goals; the 111 above is
    the number of goal ROWS logged across the whole history, not the size of a campaign, and
    calling the old one "the 111-goal set" was wrong. And the 130-180 MB it was compared
    against comes from nulls taken BEFORE per-arm memory isolation. Every run with the current
    instrument, listed by date:

        saturated set, null      +50.8 MB      -1.3 MB
        this set, null            -5.3 MB    +196.8 MB

    So the current instrument's spread on the OLD set is about 50 MB, not 130-180, and this
    set is the noisier of the two. The 300 MB floor in `route_evaluator` rests on the
    pre-isolation pair and has not been re-derived since the isolation landed.

In both of this set's runs the arm that ran SECOND carried the higher peak, which is the
residue `start_mb` was added to handle and evidently does not.

WHAT THIS SET IS AND IS NOT GOOD FOR. It is a correctness workload: the answers are known, the
checks are exact, and a wrong answer is recorded as a failure instead of a completion. It is
not a power improvement for the memory instrument, and it does not create turn headroom. More
power for a 245 MB effect against a ~160 MB spread comes from REPEATING the comparison, not
from redesigning the ruler again -- every arm here is n=1.

So turns/goal remains an instrument with nothing to measure on the operator's routine file
work: the harness one-shots it correctly. A yardstick with headroom has to come from a measure
that is not pinned at its floor -- correctness on answers that are actually hard, not turns.
"""
from __future__ import annotations

import csv
import os
import random

#: Where the generated workbook lives. Regenerated per campaign so a leftover file cannot let a
#: goal pass without the work being done.
#:
#: REALPATH, BECAUSE `TEMP` IS AN 8.3 SHORT NAME HERE. Handing the harness a path with a `~1`
#: component measured PATH HANDLING rather than the work: in the 2026-08-23 null pass one arm
#: spent three turns on a goal the other did in one, and its own account was "I wrote it under
#: the short name and the file did not land in the target folder". Both arms were the same
#: harness, so that entire difference was the ruler, not the thing being measured.
WORKDIR = os.path.join(os.path.realpath(os.environ.get("TEMP", ".")), "multiturn_workload")

#: Fixed, so the same goals always have the same answers and a check can assert them.
SEED = 20260822


def build(workdir: str = WORKDIR) -> dict:
    """Generate the input files and return the answers the checks will assert.

    Returns the expected values so the campaign can embed them in the acceptance checks rather
    than recomputing them at check time -- a check that recomputes the answer the same way the
    goal did agrees with a wrong answer as readily as a right one.
    """
    os.makedirs(workdir, exist_ok=True)
    rng = random.Random(SEED)

    # Two tables that have to be joined: one of readings, one of the limits they are judged
    # against. Neither answers the question alone, which is what makes the goal multi-step.
    lots = ["L-%03d" % i for i in range(1, 13)]
    readings = [(lot, day, round(rng.uniform(1.0, 9.0), 2))
                for lot in lots for day in range(1, 6)]
    # LIMITS CHOSEN SO THE ANSWER IS A REAL SUBSET. The first draft put 11 of 12 lots over and
    # the second put 1: "all of them" and "just that one" are both guesses that score as
    # correct answers, and a goal whose answer can be produced without doing the work measures
    # nothing. Half is where a guess is worth least. Tuned at design time, before any treatment
    # has run, on DISCRIMINATION -- not on which result it would produce.
    limits = {lot: round(rng.uniform(7.6, 8.8), 2) for lot in lots}

    with open(os.path.join(workdir, "readings.csv"), "w", encoding="utf-8",
              newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lot", "day", "value"])
        w.writerows(readings)

    with open(os.path.join(workdir, "limits.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lot", "limit"])
        w.writerows(sorted(limits.items()))

    # A third file that must be IGNORED. A goal that says "the two csv files" against a
    # directory holding three is the ordinary case in real work, and a harness that reads the
    # wrong one produces a confident wrong number rather than an error.
    with open(os.path.join(workdir, "notes.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lot", "comment"])
        w.writerows([(lot, "no comment") for lot in lots])

    over = sorted({lot for lot, _day, value in readings if value > limits[lot]})
    worst = max(readings, key=lambda r: r[2] - limits[r[0]])
    return {"workdir": workdir, "over_limit": over, "n_over": len(over),
            "worst_lot": worst[0], "worst_day": worst[1]}


def clean(workdir: str = WORKDIR) -> None:
    """Remove any answer files a previous campaign left.

    A `file_exists` check passes on yesterday's output, so the arm that "did" the work may have
    done nothing. The inputs are regenerated too, since a goal that reads a stale table answers
    a question nobody asked.
    """
    for name in ("over_limit.txt", "summary.csv", "worst.txt", "agenda.txt",
                 "readings.csv", "limits.csv", "notes.csv"):
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass


#: The files a goal is supposed to PRODUCE, as opposed to the ones it reads.
ANSWERS = ("over_limit.txt", "summary.csv", "worst.txt", "agenda.txt")


def reset_outputs(workdir: str = WORKDIR) -> None:
    """Remove the answers, keep the inputs. Called BETWEEN ARMS.

    Arm 2 runs the same goals in the same folder, so without this it opens on arm 1's finished
    work: `file_exists` passes, and the content checks pass too, because arm 1's answer is the
    right one. Arm 2 then scores a completion for work it did not do, and the bias always
    favours whichever arm ran second -- which is the arm order, not the treatment.

    The inputs stay, because regenerating them is the campaign's job at the start and doing it
    again here would hand arm 2 a folder whose modification times differ from arm 1's.
    """
    for name in ANSWERS:
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass


def goals(workdir: str = WORKDIR) -> list:
    """The goal set. Every goal names its own folder and carries an acceptance check.

    Both properties are load-bearing rather than tidy: a goal that says "the same folder" is
    unanswerable when each goal runs in its own worker, and a goal with no check cannot fail,
    so a lost harness churns to the turn cap and the churn is recorded as a measurement.
    """
    facts = build(workdir)
    d = workdir
    over_txt = os.path.join(d, "over_limit.txt")
    summary_csv = os.path.join(d, "summary.csv")
    worst_txt = os.path.join(d, "worst.txt")
    agenda_txt = os.path.join(d, "agenda.txt")

    return [
        # 1. list -> read two -> join -> write.
        {"text": ("フォルダ %s には CSV が3つある。readings.csv と limits.csv だけを使い"
                  "(notes.csv は無視)、lot ごとに value が limit を一度でも超えた lot を"
                  "昇順で列挙して、1行1lotで %s に書いて。" % (d, over_txt)),
         "checks": [
             {"type": "file_exists", "path": over_txt},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"expected=%r;"
                      "got=[l.strip() for l in open(r'%s',encoding='utf-8') if l.strip()];"
                      "assert got==expected,(got,expected)\""
                      % (facts["over_limit"], over_txt))}]},

        # 2. the same inputs, a different question, and an artefact with structure.
        {"text": ("フォルダ %s の readings.csv を使い、lot ごとに value の平均を小数第2位まで"
                  "求め、lot,mean の2列 CSV を lot 昇順で %s に書いて。ヘッダー行を付けて。"
                  % (d, summary_csv)),
         "checks": [
             {"type": "file_exists", "path": summary_csv},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"import csv;"
                      "r=list(csv.DictReader(open(r'%s',encoding='utf-8')));"
                      "assert len(r)==12,len(r);"
                      "assert [x['lot'] for x in r]==sorted(x['lot'] for x in r);"
                      "assert all(0<float(x['mean'])<10 for x in r)\"" % summary_csv)}]},

        # 3. one fact, but only after the join. Named folder and a checked artefact -- this is
        #    the goal that burned 13 turns when it had neither.
        {"text": ("フォルダ %s の readings.csv と limits.csv を使い、limit からの超過幅"
                  "(value - limit)が最大の行を求め、「lot=<値>, day=<値>」の1行だけを "
                  "%s に書いて。" % (d, worst_txt)),
         "checks": [
             {"type": "file_exists", "path": worst_txt},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"expected='lot=%s, day=%s';"
                      "got=open(r'%s',encoding='utf-8').read().strip();"
                      "assert got==expected,(got,expected)\""
                      % (facts["worst_lot"], facts["worst_day"], worst_txt))}]},

        # 4. Work IQ, kept because the operator's day contains it and a workload of only local
        #    file work would measure a harness nobody runs. The answer depends on a live
        #    calendar, so the check asserts SHAPE -- three dated lines, or a stated reason.
        {"text": ("自分の今日以降の予定を3件、1行1件で「YYYY-MM-DD HH:MM 件名」の形式にして "
                  "%s に書いて。取得できない場合は理由を1行だけ同じファイルに書いて。"
                  % agenda_txt),
         "checks": [
             {"type": "file_exists", "path": agenda_txt},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"import re;"
                      "ls=[l.strip() for l in open(r'%s',encoding='utf-8') if l.strip()];"
                      "assert ls,'empty';"
                      "dated=[l for l in ls if re.match(r'^\d{4}-\d{2}-\d{2} ',l)];"
                      "assert len(dated)==3 or len(ls)==1,(len(dated),len(ls))\"" % agenda_txt)}]},
    ]


if __name__ == "__main__":                                      # pragma: no cover
    import json
    facts = build()
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    for i, g in enumerate(goals(), 1):
        print("%d. checks=%d  %s" % (i, len(g.get("checks") or []), g["text"][:70]))
