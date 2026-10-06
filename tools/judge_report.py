# -*- coding: utf-8 -*-
"""What the pre-execution review has actually decided — and how often it decided nothing.

THE LEDGER NOBODY COUNTED. `tools/code_exec.py::_record_judgement` has written a row for every
judged command since 2026-08-31: 11,732 of them by 2026-09-20, 11.4 MB, 10,371 distinct
commands. The only code that reads the file is `tools/ledger_health.py`, which checks whether
particular COLUMNS are ever filled -- not what the rows say. So nothing has ever answered the
question the layer exists for: is it assessing commands, and what does it conclude.

THE ANSWER IS THAT IT MOSTLY IS NOT, AND THE RAW COUNTS HIDE IT. Measured 2026-09-20:

    would_block, shadow mode                    11,263 of 11,263   (100%)
    decision = REQUIRE_HUMAN                    11,265 of 11,732   ( 96%)

Read alone, that is a review layer refusing everything. It is not. Splitting by REASON:

    "the judge could not be reached"            10,361   (88.3%)
    "no judge is configured"                       904
    an actual verdict about the command            350

So 10,361 rows are the layer recording ITS OWN UNAVAILABILITY, and failing closed while it
does -- which is correct, and is what `code_exec`'s own comment insists on: "FAILURE IS NOT
PERMISSION, INCLUDING MY OWN FAILURE." What is wrong is only that nobody could see it: a
transport error and a considered refusal are the same row shape, and counting them together
turns "the judge is unreachable" into "the judge is strict".

THIS IS WHY THE REPORT SEPARATES THEM AND WILL NOT TOTAL THEM. `assessed()` counts rows where
a judge actually answered; `unavailable()` counts rows where one could not be asked. Any rate
computed over their sum is a statement about two different things at once.

WHAT IT SAYS ABOUT SWITCHING `enforce` ON, which is a standing question elsewhere in this
repository: in the 469 rows already recorded under enforce, 352 blocked, and NONE of those 352
carries a human approval -- `_ask_operator` never once produced a yes. Turning enforce on more
widely today would refuse commands at the rate the transport fails, for reasons that say
nothing about the commands. Same shape as the unlock-token gap measurement, which found that
enforcement "would have refused the live integration".
"""
from __future__ import annotations

import collections
import io
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, ".fleet", "judge.jsonl")

#: Reason prefixes that mean NO JUDGEMENT WAS OBTAINED. Matched on the reason text because that
#: is where the distinction lives -- the decision field says REQUIRE_HUMAN either way, which is
#: exactly why the raw decision counts mislead.
#:
#: SUBSTRING, NOT EQUALITY: the transport error carries the exception's own message on the end
#: ("...JudgeTransportError: the connection...", "...: no MCP request context..."), so matching
#: the whole string would count each distinct failure separately and each as a verdict.
UNAVAILABLE_MARKERS = (
    "the judge could not be reached",
    "no judge is configured",
)


def read(path=DEFAULT_LOG) -> list:
    """Every well-formed row. A half-written last line is skipped -- this is a live append."""
    rows = []
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def was_assessed(row) -> bool:
    """Whether a judge actually answered about this command.

    A row whose reason says the judge could not be reached is NOT an assessment, however
    decisive its `decision` field looks. This predicate is the whole point of the module: it is
    the difference between "the review layer refused this" and "the review layer was not there".
    """
    reason = str((row or {}).get("reason") or "")
    return not any(m in reason for m in UNAVAILABLE_MARKERS)


def assessed(rows) -> list:
    return [r for r in rows if was_assessed(r)]


def unavailable(rows) -> list:
    return [r for r in rows if not was_assessed(r)]


def by_decision(rows) -> dict:
    return dict(collections.Counter(str(r.get("decision")) for r in rows))


def enforce_blocks(rows) -> dict:
    """The rows that actually refused a command, and whether a person released any of them.

    `human_approved` is None in shadow BY CONSTRUCTION -- `_ask_operator` is only called in
    enforce, so counting Nones across the whole file says nothing. This looks only where the
    question was really put.
    """
    enf = [r for r in rows if r.get("mode") == "enforce"]
    blocked = [r for r in enf if r.get("would_block") is True]
    approved = [r for r in blocked if r.get("human_approved") is True]
    return {"enforce_rows": len(enf), "blocked": len(blocked),
            "released_by_a_person": len(approved),
            "blocked_without_an_assessment": sum(1 for r in blocked if not was_assessed(r))}


def report(path=DEFAULT_LOG) -> str:
    rows = read(path)
    if not rows:
        return "judge: no records at %s" % path
    ok, bad = assessed(rows), unavailable(rows)
    out = ["judge: %d records, %d distinct commands"
           % (len(rows), len({r.get("cmd_sha16") for r in rows}))]
    # THE TWO NUMBERS ARE NEVER ADDED. A rate over their sum answers "how often did the layer
    # say no", which is a question about two different things wearing one name.
    out.append("  a judge answered: %d" % len(ok))
    if ok:
        out.append("    " + ", ".join("%s=%d" % kv for kv in
                                      sorted(by_decision(ok).items(), key=lambda kv: -kv[1])))
    out.append("  no judge could be asked: %d (%.1f%% of all rows)"
               % (len(bad), 100.0 * len(bad) / len(rows)))
    if bad:
        why = collections.Counter(
            next((m for m in UNAVAILABLE_MARKERS if m in str(r.get("reason") or "")), "?")
            for r in bad)
        out.append("    " + ", ".join("%s: %d" % kv for kv in why.most_common()))
        out.append("    these fail CLOSED, which is correct -- but they are not verdicts "
                   "about the commands, and a count that mixes them in says the layer is "
                   "strict when it is absent")
    e = enforce_blocks(rows)
    out.append("  under enforce: %d rows, %d blocked, %d released by a person"
               % (e["enforce_rows"], e["blocked"], e["released_by_a_person"]))
    if e["blocked"]:
        out.append("    of those blocks, %d had no assessment behind them"
                   % e["blocked_without_an_assessment"])
    return "\n".join(out)


if __name__ == "__main__":                                      # pragma: no cover
    print(report())
