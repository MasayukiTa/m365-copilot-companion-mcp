# -*- coding: utf-8 -*-
"""What .fleet/send_failures.jsonl holds, for a record that has never been read.

WHY THIS EXISTS. `relay/copilot_autopilot_relay.py::_snapshot` has been appending a diagnostic
row on every failed send since 2026-06-13 -- 15,605 of them by 2026-09-19, 8.5 MB. Nothing in
the repository ever opened it. The writer was built to be read: its own comments say a field
was added because "nothing here could test that, because nothing recorded how old the page
was. Cheap to capture and it makes the hypothesis falsifiable from the log alone", and another
because the signal the ack check reads "was the one thing the record of that decision did not
contain". Somebody paid for this measurement three months ago and nobody has looked at it.

THE RATE A PRODUCTION RULE RESTS ON IS IN HERE, AND IT DOES NOT MATCH. `_page_alive` fast-fails
a send when the page looks dead, and four separate comments justify it with "the
TargetClosedError race seen in send_failures.jsonl, 28/72" -- 39%, hand-counted from 72 rows
around 2026-06-13. Measured over the whole file: 29 of 15,605, or 0.19%.

WHAT THAT DOES AND DOES NOT MEAN, because the honest version is narrower than the headline:

  * It does NOT mean the rule is wrong. Fast-failing a genuinely closed page is right whenever
    it happens, and the cost it avoids (three attempts x 12s against a dead target) is real.
  * It does NOT mean the original count was wrong. The 72 were taken during the incident that
    prompted the fix, which is exactly the population where the failure concentrates.
  * It DOES mean the justification as written -- a bare "28/72" with no window -- reads as a
    standing property of send failures, and over three months it is not one. A rate quoted
    without its window is the same defect as a baseline quoted without its date.
  * AND THE FIX ITSELF MOVES THE NUMBER: once a dead page is detected before the send, fewer
    sends reach the point that writes one of these rows. The 0.19% is measured on a file that
    the rule has been shaping since shortly after the 72 were counted. This reader cannot
    separate those, and says so rather than implying a clean before/after.

THE SCHEMA CHANGED MID-FILE and a reader that ignores that will quietly divide by the wrong
number: `is_processing` appears on 13,050 rows and was replaced by `answer_area_busy_or_empty`
(2,555 rows), alongside `is_generating`, `answer_count` and `page_age_s`. Every percentage
below is reported against the count of rows that actually carry the field, never against the
file length.
"""
from __future__ import annotations

import collections
import io
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, ".fleet", "send_failures.jsonl")

#: The value the tab probe writes when the page was already gone. This is the string the
#: "28/72" claim counts, and it is a string rather than an exception type because the writer
#: catches everything and records `"err:" + type(exc).__name__`.
TARGET_CLOSED = "err:TargetClosedError"


def read(path=DEFAULT_LOG) -> list:
    """Every well-formed row. A half-written last line is skipped, not fatal -- this file is
    appended to by a live process, so it is routinely read mid-write."""
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


def _probe_value(row, field, key):
    """One key out of a probe dict, or None when the probe itself failed.

    The probes are recorded as dicts whose values become `"err:<ExceptionName>"` when the
    page could not answer, so "the field is missing", "the probe errored" and "the value was
    falsy" are three different states and are not flattened here.
    """
    d = row.get(field)
    if not isinstance(d, dict):
        return None
    return d.get(key)


def target_closed_rate(rows) -> dict:
    """How often the tab was already gone. The number the `_page_alive` rule cites."""
    have = [r for r in rows if isinstance(r.get("tab"), dict)]
    closed = [r for r in have
              if str(_probe_value(r, "tab", "visibility_state")) == TARGET_CLOSED]
    return {"rows_with_a_tab_probe": len(have), "target_closed": len(closed),
            "rate": round(len(closed) / len(have), 4) if have else None}


def probe_failure_rate(rows) -> dict:
    """Rows where EVERY probe errored -- the page answered nothing at all.

    Counted separately from target-closed because they are different findings: a closed target
    is a conversation that ended, and an all-errored probe is a page that is there and not
    answering. Lumping them together is how "the page died" absorbs "the page is wedged".

    THE FIRST VERSION OF THIS COUNTED ZERO, AND ZERO WAS WRONG. It asked whether
    `send_button.match_count` started with "err:" alongside the tab and composer probes -- and
    `match_count` is the one value in that dict that does NOT error when the page is
    unreachable: the locator matches nothing and returns a perfectly ordinary 0, while every
    other key in the same dict reads "err:AttributeError". So the AND was never true and the
    answer was a confident 0 while hundreds of such rows sat in the file.

    It was caught only by comparing the reader's output against a count made by hand first,
    and that is the check worth keeping: a new reader's FIRST output is a claim about data
    nobody has ever looked at, so there is nothing for it to be wrong against except a
    measurement taken another way.

    THE TWO COUNTS ARE NOT THE SAME QUESTION AND ARE NOT REPORTED AS ONE. The hand count was
    581 rows carrying one specific fully-errored `send_button` signature; this function counts
    610 rows whose `tab` AND `composer` probes errored in every key. Neither is wrong -- the
    probe you key on decides the number, which is the same lesson one level up.
    """
    n = 0
    for r in rows:
        probes = [r.get("tab"), r.get("composer")]
        if not all(isinstance(p, dict) and p for p in probes):
            continue
        if all(str(v).startswith("err:") for p in probes for v in p.values()):
            n += 1
    return {"all_probes_errored": n,
            "rate": round(n / len(rows), 4) if rows else None}


def page_age(rows) -> dict:
    """The distribution of page age at failure, for the hypothesis the field was added for.

    STATED LIMIT, AND IT IS THE WHOLE POINT. The hypothesis is "younger pages fail more"
    (an editor-attach race). This file records FAILURES ONLY, so it has no denominator: a pile
    of failures at low ages is equally consistent with "young pages fail more" and with "most
    sends happen soon after a page opens". Answering it needs the age of SUCCESSFUL sends too,
    which nothing records. Reporting the distribution without that caveat would be inventing a
    conclusion out of a one-sided sample.
    """
    ages = sorted(v for v in (r.get("page_age_s") for r in rows)
                  if isinstance(v, (int, float)))
    if not ages:
        return {"rows_with_an_age": 0}

    def q(p):
        return ages[min(len(ages) - 1, int(len(ages) * p))]

    return {"rows_with_an_age": len(ages), "min": ages[0], "p25": q(0.25), "p50": q(0.50),
            "p75": q(0.75), "p90": q(0.90), "max": ages[-1],
            "under_5s": sum(1 for a in ages if a <= 5),
            "under_30s": sum(1 for a in ages if a <= 30)}


def report(path=DEFAULT_LOG) -> str:
    """The whole summary, as a person would want it in front of the file."""
    rows = read(path)
    if not rows:
        return "send failures: no records at %s" % path
    stamps = sorted(str(r.get("ts") or "") for r in rows if r.get("ts"))
    tc, pf, age = target_closed_rate(rows), probe_failure_rate(rows), page_age(rows)
    out = ["send failures: %d records%s" % (
        len(rows), (", %s -> %s" % (stamps[0][:10], stamps[-1][:10])) if stamps else "")]
    phases = collections.Counter(str(r.get("phase")) for r in rows)
    out.append("  by phase: " + ", ".join("%s=%d" % kv for kv in phases.most_common()))
    if tc["rate"] is not None:
        out.append("  tab already closed: %d of %d (%.2f%%)  -- the rule in "
                   "copilot_autopilot_relay._page_alive cites 28/72 = 38.9%%, hand-counted "
                   "on the first day; see this module's header for why both can be true"
                   % (tc["target_closed"], tc["rows_with_a_tab_probe"], 100 * tc["rate"]))
    if pf["rate"] is not None:
        out.append("  every probe errored (page there, answering nothing): %d (%.2f%%)"
                   % (pf["all_probes_errored"], 100 * pf["rate"]))
    if age.get("rows_with_an_age"):
        out.append("  page age at failure (%d rows carry it): p25 %.0fs  p50 %.0fs  "
                   "p90 %.0fs  max %.0fs; %d were under 5s"
                   % (age["rows_with_an_age"], age["p25"], age["p50"], age["p90"],
                      age["max"], age["under_5s"]))
        out.append("    NOT evidence for the editor-attach race on its own: this file records "
                   "failures only, so there is no denominator. See page_age().")
    else:
        out.append("  no row carries page_age_s -- the field post-dates most of this file")
    return "\n".join(out)


if __name__ == "__main__":                                      # pragma: no cover
    print(report())
