"""Convert a grader's verdict map into the JSON-LINES ledger calibration reads.

TWO SHAPES, AND THEY ARE NOT THE SAME. The SWE-bench-Pro grader writes eval_results.json as
one object of instance_id -> bool. relay/selfimprove/calibration reads a LEDGER: one JSON
object per line carrying instance_id and a verdict string, where anything other than
"RESOLVED" counts as unresolved and "EVALERR" is excluded from the denominator entirely
because an eval-host fault is infrastructure, not competence.

Handing the first to the second printed "no grade history yet" -- which reads as "nothing has
ever been graded" and was actually "this file is not that format".
"""
from __future__ import annotations

import io
import json


def to_ledger_rows(verdicts, run_id="", ts=None):
    """[{instance_id, verdict, run_id, ts}] from {instance_id: bool}.

    A bool has no way to say EVALERR, so nothing is ever emitted as one here: an instance the
    grader could not evaluate is absent from its map, and inventing a verdict for it would put
    an infrastructure fault into a competence number.
    """
    rows = []
    for inst, ok in sorted(verdicts.items()):
        row = {"instance_id": inst, "verdict": "RESOLVED" if ok else "UNRESOLVED"}
        if run_id:
            row["run_id"] = run_id
        if ts is not None:
            row["ts"] = ts
        rows.append(row)
    return rows


def convert(eval_path, out_path, run_id="", ts=None):
    verdicts = json.load(io.open(eval_path, encoding="utf-8-sig"))
    if not isinstance(verdicts, dict):
        raise ValueError("expected an object of instance_id -> verdict")
    rows = to_ledger_rows({k: bool(v) for k, v in verdicts.items()}, run_id, ts)
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--ts", type=float, default=None)
    a = ap.parse_args()
    n = convert(a.eval, a.out, a.run_id, a.ts)
    print("wrote %d ledger rows to %s" % (n, a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
