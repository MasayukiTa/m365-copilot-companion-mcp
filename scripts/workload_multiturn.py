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

WHY THESE TAKE MORE THAN ONE TURN

Not because they are padded. Each needs a step whose input is the previous step's output: you
cannot compute the summary before reading the files, and you cannot know which file to read
before listing the directory. That is the same reason the operator's real tasks take several
turns, and it is what gives a better-primed or better-planned harness something to save.

THE HEADROOM IS A CLAIM UNTIL IT IS MEASURED. These goals are BUILT to need several turns; how
many they actually take is a question for a null pass on them, which also has to re-measure the
noise floor -- the current 0.75 describes the old, saturated set and says nothing about this one.
"""
from __future__ import annotations

import csv
import os
import random

#: Where the generated workbook lives. Regenerated per campaign so a leftover file cannot let a
#: goal pass without the work being done.
WORKDIR = os.path.join(os.environ.get("TEMP", "."), "multiturn_workload")

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
    for name in ("over_limit.txt", "summary.csv", "readings.csv", "limits.csv", "notes.csv"):
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass


def goals(workdir: str = WORKDIR) -> list:
    """The goal set, with acceptance checks that assert the generated answers."""
    facts = build(workdir)
    d = workdir
    py = ("python -c \"import csv,sys;"
          "rows=list(csv.DictReader(open(r'%s',encoding='utf-8')));")

    return [
        # 1. list -> read two -> join -> write. Four steps, each needing the last.
        {"text": ("フォルダ %s には CSV が3つある。readings.csv と limits.csv だけを使い"
                  "(notes.csv は無視)、lot ごとに value が limit を一度でも超えた lot を"
                  "昇順で列挙して、1行1lotで %s に書いて。"
                  % (d, os.path.join(d, "over_limit.txt"))),
         "checks": [
             {"type": "file_exists", "path": os.path.join(d, "over_limit.txt")},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"expected=%r;"
                      "got=[l.strip() for l in open(r'%s',encoding='utf-8') if l.strip()];"
                      "assert got==expected,(got,expected)\""
                      % (facts["over_limit"], os.path.join(d, "over_limit.txt")))}]},

        # 2. the same inputs, a different question, and an artefact with structure.
        {"text": ("同じフォルダで、lot ごとに value の平均を小数第2位まで求め、"
                  "lot,mean の2列 CSV を lot 昇順で %s に書いて。ヘッダー行を付けて。"
                  % os.path.join(d, "summary.csv")),
         "checks": [
             {"type": "file_exists", "path": os.path.join(d, "summary.csv")},
             {"type": "shell", "expect_code": 0,
              "cmd": ("python -c \"import csv;"
                      "r=list(csv.DictReader(open(r'%s',encoding='utf-8')));"
                      "assert len(r)==12,len(r);"
                      "assert [x['lot'] for x in r]==sorted(x['lot'] for x in r);"
                      "assert all(0<float(x['mean'])<10 for x in r)\""
                      % os.path.join(d, "summary.csv"))}]},

        # 3. a question whose answer is a single fact, but only after the join.
        {"text": ("同じフォルダで、limit からの超過幅(value - limit)が最大の行の lot と day を"
                  "「lot=..., day=...」の1行だけで答えて。ファイルは作らなくてよい。")},

        # 4. Work IQ, kept because the operator's day contains it and because a workload of
        #    only local file work would measure a harness nobody runs.
        {"text": ("自分の今日以降の予定を3件、開始日時と件名だけの箇条書きで挙げて。"
                  "取得できない場合はその理由を1行で書いて FAIL と出力して。")},
    ]


if __name__ == "__main__":                                      # pragma: no cover
    import json
    facts = build()
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    for i, g in enumerate(goals(), 1):
        print("%d. checks=%d  %s" % (i, len(g.get("checks") or []), g["text"][:70]))
