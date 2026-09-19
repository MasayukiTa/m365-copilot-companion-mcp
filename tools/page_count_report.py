# -*- coding: utf-8 -*-
"""Date the onset of a page leak — the job .fleet/page_counts.jsonl was created to make possible.

WHY THIS EXISTS, IN THE WRITER'S OWN WORDS. `bridge/copilot_bridge.py` samples the browser's
page count every 60s, and the comment above that sampler says why:

    THE INSTRUMENT THIS INCIDENT WAS MISSING. On 2026-09-10 the bridge's headless Edge was
    found holding 71 pages, 69 of them orphans on one URL. Nothing noticed for hours, and
    afterwards THE ONSET COULD NOT EVEN BE DATED, because no log on this machine had ever
    recorded a page count over time. Process counts had been sampled repeatedly and were
    useless by construction: the pages were same-origin, so Chromium shared ~7 renderers
    between all 70 and the process count sat flat at 17 the whole time. Only a PAGE count can
    see this class, and ONLY A PERSISTED ONE CAN DATE IT.

The log has been written ever since. **Nothing has ever read it** — the only code that opens it
is the sampler's own trim, which reads the file solely to rewrite it shorter. So the log that
exists to date an onset could not be used to date one.

COVERAGE IS REPORTED BESIDE THE VERDICT, AND THAT IS THE POINT OF THIS MODULE. "No onset" and
"nobody was looking" produce the same silence, and the difference is the only thing that makes
the negative worth anything. Measured 2026-09-20: 12,254 samples over 210.2 hours, 97% of the
expected rate, two gaps past five minutes totalling 3.6 h (3.4 h of it on 09-11). Peak 4 pages
against a warn threshold of 8 -- so the leak has not recurred since the fix, and that sentence
is now backed by a number instead of by nobody having noticed.

THE LOG BEGINS 2026-09-11 07:52, the day AFTER the incident. It has never seen the thing it
was built for, which is the correct outcome and is not the same as the instrument working --
an instrument that has only ever observed the quiet case has not been shown to catch the loud
one. `onsets()` is therefore written against the threshold, not against the data.
"""
from __future__ import annotations

import io
import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, ".fleet", "page_counts.jsonl")

#: Mirrors bridge.copilot_bridge.PAGE_COUNT_WARN_AT / PAGE_COUNT_SAMPLE_SEC as DEFAULTS, taken
#: as arguments rather than imported: this is a stdlib-only reader and importing the bridge to
#: read a log would pull Playwright in to answer a question about a text file.
DEFAULT_WARN_AT = 8
DEFAULT_SAMPLE_SEC = 60.0

#: A gap is only interesting when it is several samples wide -- one late tick is scheduling
#: noise, not a hole in the record.
GAP_FACTOR = 5.0


def read(path=DEFAULT_LOG) -> list:
    """Usable samples, oldest first. A row without both `ts` and `pages` is not a sample."""
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
                if (isinstance(row, dict) and isinstance(row.get("pages"), int)
                        and isinstance(row.get("ts"), (int, float))):
                    rows.append(row)
    except OSError:
        return []
    rows.sort(key=lambda r: r["ts"])
    return rows


def observed_coverage(rows, sample_sec: float = DEFAULT_SAMPLE_SEC) -> dict:
    """How much of the window was actually observed, and where the holes are.

    WITHOUT THIS, "no onset" IS UNFALSIFIABLE. A sampler that stopped for a day and a browser
    that stayed healthy for a day leave the same log, and the incident this instrument exists
    for took hours. The gaps are returned, not just counted, so a reader can check whether a
    suspicious window is one of them.
    """
    if len(rows) < 2:
        return {"samples": len(rows), "span_s": 0.0, "expected": len(rows),
                "coverage": 1.0 if rows else 0.0, "gaps": [], "unobserved_s": 0.0}
    span = rows[-1]["ts"] - rows[0]["ts"]
    gaps = []
    for a, b in zip(rows, rows[1:]):
        d = b["ts"] - a["ts"]
        if d > sample_sec * GAP_FACTOR:
            gaps.append({"from": a["ts"], "to": b["ts"], "seconds": round(d, 1)})
    expected = int(span / sample_sec) if sample_sec > 0 else len(rows)
    return {"samples": len(rows), "span_s": round(span, 1), "expected": expected,
            "coverage": round(len(rows) / expected, 4) if expected else 1.0,
            "gaps": gaps, "unobserved_s": round(sum(g["seconds"] for g in gaps), 1)}


def onsets(rows, warn_at: int = DEFAULT_WARN_AT) -> list:
    """Every contiguous run of samples above `warn_at`, with when it started and its peak.

    A RUN, NOT A SAMPLE. One sample over the line is a tab that was being opened; the incident
    was 69 orphans persisting for hours, and what a reader needs is the START of that -- the
    number nobody could produce on 2026-09-10.

    A GAP DOES NOT END A RUN silently: an onset that spans a hole in the record is still one
    onset, and it is reported with `spans_a_gap` so a reader knows its interior is partly
    unobserved rather than measured.
    """
    out, cur = [], None
    for r in rows:
        if r["pages"] > warn_at:
            if cur is None:
                cur = {"started": r["ts"], "ended": r["ts"], "peak": r["pages"],
                       "samples": 0, "spans_a_gap": False}
            elif r["ts"] - cur["ended"] > DEFAULT_SAMPLE_SEC * GAP_FACTOR:
                cur["spans_a_gap"] = True
            cur["ended"] = r["ts"]
            cur["peak"] = max(cur["peak"], r["pages"])
            cur["samples"] += 1
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def report(path=DEFAULT_LOG, warn_at: int = DEFAULT_WARN_AT,
           sample_sec: float = DEFAULT_SAMPLE_SEC) -> str:
    """The summary a person wants when asking "is the browser leaking pages, and since when"."""
    rows = read(path)
    if not rows:
        return "page counts: no samples at %s" % path
    cov = observed_coverage(rows, sample_sec)
    peak = max(rows, key=lambda r: r["pages"])
    out = ["page counts: %d samples, %s -> %s (%.1f h)"
           % (len(rows), _when(rows[0]["ts"]), _when(rows[-1]["ts"]), cov["span_s"] / 3600.0)]
    out.append("  peak %d pages at %s (agent surface: %s); warn threshold is %d"
               % (peak["pages"], _when(peak["ts"]), peak.get("agent", "?"), warn_at))
    found = onsets(rows, warn_at)
    if found:
        for o in found:
            out.append("  ONSET %s -> %s, peak %d over %d samples%s"
                       % (_when(o["started"]), _when(o["ended"]), o["peak"], o["samples"],
                          " (spans a gap in the record)" if o["spans_a_gap"] else ""))
    else:
        out.append("  no sample ever exceeded the threshold")
    # THE COVERAGE LINE IS NOT OPTIONAL WHEN THE ANSWER IS "NOTHING HAPPENED". A clean report
    # over a log with a day-wide hole is a statement about the sampler, not about the browser.
    out.append("  coverage %.0f%% of the expected rate; %.1f h unobserved across %d gap(s)%s"
               % (100 * cov["coverage"], cov["unobserved_s"] / 3600.0, len(cov["gaps"]),
                  (", largest %.1f h from %s"
                   % (max(g["seconds"] for g in cov["gaps"]) / 3600.0,
                      _when(max(cov["gaps"], key=lambda g: g["seconds"])["from"])))
                  if cov["gaps"] else ""))
    if not found:
        out.append("    so \"it has not recurred\" holds only for the observed %.0f%%."
                   % (100 * cov["coverage"]))
    return "\n".join(out)


if __name__ == "__main__":                                      # pragma: no cover
    print(report())
