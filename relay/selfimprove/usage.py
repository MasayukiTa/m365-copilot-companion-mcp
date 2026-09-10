"""Live usage metrics -- the GENERAL-USER lens of the self-improvement dashboard.

A normal user never runs a benchmark, so pass@1 is meaningless to them. What they care about is
whether the companion is getting better AT THEIR ACTUAL WORK. Every metric here is derived purely
from data the product already writes -- the persisted run history (.fleet/history.json) and the live
fleet snapshot (.fleet/status.json) -- so there is NO extra instrumentation and nothing bench-specific.

Metrics (all from real runs):
  completion_rate : done / total            -- did the task actually finish (vs stuck/error/maxturns)
  status_mix      : {status: count}          -- where the non-completions go
  median_turns    : median turns of completed tasks  -- efficiency (fewer = better)
  verify_rate     : verified / done (live)   -- of finished tasks, how many self-verified
  trend           : completion rate per time-ordered segment -- is it improving over time

This is READ-ONLY and defensive: a missing/short history yields an empty-but-valid section.
"""
import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_HISTORY = os.path.join(_REPO_ROOT, ".fleet", "history.json")
_DEFAULT_STATUS = os.path.join(_REPO_ROOT, ".fleet", "status.json")

# A run "completed" iff it reached done. Everything else is a non-completion the user felt as friction.
_DONE = "done"

#: Outcomes that mean "the claim was checked and the record spoke against it". A row carrying
#: one of these reached status "done" -- relay_fleet._settle_done sets the status first and asks
#: for the verdict second -- so status alone cannot tell a completion from a refuted claim.
#:
#: MEASURED, NOT ANTICIPATED. On the live archive (215 rows, 2026-09-11) the done bucket was 94:
#: 91 DONE and 3 EVIDENCE_CONTRADICTED. Three real tasks claimed done, had their tool ledger
#: contradict them, were recorded as contradicted, and were counted as completions anyway.
#:
#: A CLOSED LIST OF POSITIVE FINDINGS, and nothing else. "no acceptance checks", "no ledger",
#: "nothing ran" and an unrecognised outcome all keep counting exactly as they did -- the same
#: rule relay_fleet._claim_verdict applies to itself: only a positive contradiction changes an
#: outcome, because an absence of evidence is not evidence.
CONTRADICTED_OUTCOMES = frozenset({"EVIDENCE_CONTRADICTED", "VERIFY_FAILED"})


def _is_completion(row) -> bool:
    """Whether one archived row counts toward the completion rate.

    Reached done AND its own record does not contradict the claim. This is the whole of the
    change: every other reading of a row is unchanged.
    """
    if (row.get("status") or "").strip() != _DONE:
        return False
    return (row.get("outcome") or "").strip().upper() not in CONTRADICTED_OUTCOMES


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _as_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _history_items(history):
    if isinstance(history, list):
        return [h for h in history if isinstance(h, dict)]
    if isinstance(history, dict):
        for k in ("items", "history", "cards"):
            if isinstance(history.get(k), list):
                return [h for h in history[k] if isinstance(h, dict)]
    return []


def usage_section(history_path=None, status_path=None, segments=6):
    """Aggregate live usage metrics into one JSON-safe dict. All args optional (repo-root defaults)."""
    history = _read_json(_DEFAULT_HISTORY if history_path is None else history_path)
    status = _read_json(_DEFAULT_STATUS if status_path is None else status_path)

    items = _history_items(history)
    # keep time order: prefer an explicit seq, else file order
    def seq_key(h):
        s = _as_int(h.get("seq"))
        return s if s is not None else 0
    items = sorted(items, key=seq_key)

    n = len(items)
    status_mix = {}
    # BESIDE THE STATUS MIX, NOT INSTEAD OF IT. status_mix is what the run's machinery did;
    # outcome_mix is what was concluded about the claim, and the two disagree exactly where this
    # section used to be blind. Both are emitted so any rate here can be recomputed from the
    # archive by hand -- which is codex-plan item 1's evidence bar, not a convenience.
    outcome_mix = {}
    done_turns = []
    completed = 0
    contradicted_done = 0
    for h in items:
        st = (h.get("status") or "").strip() or "unknown"
        status_mix[st] = status_mix.get(st, 0) + 1
        oc = (h.get("outcome") or "").strip() or "unknown"
        outcome_mix[oc] = outcome_mix.get(oc, 0) + 1
        if st == _DONE and not _is_completion(h):
            contradicted_done += 1
        if _is_completion(h):
            completed += 1
            t = _as_int(h.get("turn"))
            if t is not None:
                done_turns.append(t)

    completion_rate = round(completed / n, 4) if n else None
    median_turns = _median(done_turns)

    # Trend: completion rate over `segments` equal, time-ordered buckets (a sparkline of improvement).
    trend = []
    if n >= segments:
        size = n / float(segments)
        for i in range(segments):
            lo = int(round(i * size))
            hi = int(round((i + 1) * size))
            chunk = items[lo:hi]
            if chunk:
                c = sum(1 for h in chunk if _is_completion(h))
                trend.append(round(c / len(chunk), 4))

    # verify_rate over the ARCHIVE, falling back to the live snapshot.
    #
    # This read only the live snapshot, and said why: "history rows don't carry verified". They
    # do now -- the cockpit archives the field as of 2026-09-08 -- and the difference is not
    # cosmetic. The live snapshot holds the workers of the CURRENT run and nothing else, so the
    # number beside "44 tasks" was computed over as few as one worker: a single unverified
    # worker rendered as 0.0, which reads like a measured all-time rate and is a sample of one.
    #
    # Older rows predate the field and simply do not count -- they are not verifiable and never
    # were, which is exactly what `verified in (True, False)` already means. So the rate starts
    # thin and thickens as runs land, rather than being wrong on a full-looking denominator.
    #
    # Only workers where verification ACTUALLY ran are counted; otherwise the metric is null,
    # not a misleading 0% (SWE-bench workers, for instance, never set verified).
    def _rate(rows):
        # A `False` FROM BEFORE THE TRI-STATE FIX IS NOT A FAILED VERIFICATION.
        #
        # relay_fleet's _on_done_claimed used to set verified=False when a task had NO
        # acceptance checks configured, which is "nobody asked", not "it was asked and failed".
        # That was corrected on 2026-09-09 (relay_fleet.py:4168 now sets None) -- but the fix
        # only changes NEW rows, and this rate is computed over an archive full of old ones.
        #
        # MEASURED (215-row archive, 2026-09-11): the denominator was 72 -- 4 True and 68 False
        # -- and 67 of those 68 carried verify_attempts == 0. So the published "verify_rate
        # 0.0556" was 4 passes against 1 real failure and 67 workers nothing was ever
        # configured to check, and it reads as "5.6% of gated tasks passed".
        #
        # verify_attempts IS THE DISCRIMINATOR, and it is exact rather than a heuristic:
        # _poll_verify increments it on the failing branch BEFORE verified is set to False
        # (relay_fleet.py:4500), so a genuine failure can never carry zero. A False with no
        # attempts can only have come from the old no-checks branch.
        #
        # Excluded rather than counted as a pass: it belongs with the None rows, which already
        # mean "not verifiable, and never was".
        def _gate_actually_ran(w):
            if not isinstance(w, dict):
                return False
            v = str(w.get("verified"))
            if v == "True":
                return True
            if v != "False":
                return False
            return bool(_as_int(w.get("verify_attempts")))

        verifiable = [w for w in rows if _gate_actually_ran(w)]
        if not verifiable:
            return None, 0
        ver = sum(1 for w in verifiable if str(w.get("verified")) == "True")
        return round(ver / len(verifiable), 4), len(verifiable)

    verify_rate, verify_n = _rate(items)
    verify_source = "history"
    if verify_rate is None:
        # Nothing archived carries it yet (a fresh install, or a checkout from before the field
        # existed). The live run is then the only evidence there is -- reported as such, so a
        # reader can tell a one-run sample from an accumulated rate.
        verify_rate, verify_n = _rate((status or {}).get("workers") or []
                                      if isinstance(status, dict) else [])
        verify_source = "live" if verify_rate is not None else "none"

    # recent window = last min(50, n//3) tasks, so "lately" is visible vs the all-time rate
    win = min(50, max(1, n // 3)) if n else 0
    recent = items[-win:] if win else []
    recent_rate = round(sum(1 for h in recent if _is_completion(h)) / len(recent), 4) \
        if recent else None

    # Persona-leak lens (the QUALITY half of the general-user lens): of the runs whose body we can
    # resolve, how many leaked an unsolicited advisor/lecture/ego persona. Reuses the SAME time-ordered
    # `items` list (each carries a transcript path, so score_history can resolve the real body).
    # DEFENSIVE: the quality scorer is a soft dependency -- if its import OR its call fails for any
    # reason we degrade to (None, 0, []) and leave EVERY existing metric above untouched.
    persona_leak_rate = None
    quality_scored = 0
    persona_flagged = []
    try:
        from relay.selfimprove import quality
        r = quality.score_history(items)  # offline heuristic only (judge_fn=None)
        persona_leak_rate = r.get("leak_rate")
        quality_scored = r.get("n_scored") or 0
        # thin each flagged row down to the display-only fields (key/signals/excerpt), top <=10
        for f in (r.get("flagged") or [])[:10]:
            if isinstance(f, dict):
                persona_flagged.append({
                    "key": f.get("key"),
                    "signals": f.get("signals", []),
                    "excerpt": f.get("excerpt"),
                })
    except Exception:
        persona_leak_rate = None
        quality_scored = 0
        persona_flagged = []

    return {
        "n_tasks": n,
        "completion_rate": completion_rate,
        "recent_completion_rate": recent_rate,
        "recent_window": win,
        "median_turns": median_turns,
        "verify_rate": verify_rate,
        # WHAT THE RATE WAS COMPUTED OVER. A rate with no denominator beside it cannot be told
        # apart from a rate over one worker, and that is precisely how 0.0 came to sit next to
        # "44 tasks" and read as an all-time figure.
        "verify_n": verify_n,
        "verify_source": verify_source,
        "status_mix": status_mix,
        "outcome_mix": outcome_mix,
        # HOW MANY COMPLETIONS THE RECORD TOOK BACK. Without this the corrected rate is simply
        # a different number from the one printed yesterday, with nothing to explain the gap;
        # with it, status_mix["done"] - contradicted_done == completed, and a reader can check
        # the arithmetic against the archive.
        "contradicted_done": contradicted_done,
        "trend": trend,
        "persona_leak_rate": persona_leak_rate,
        "quality_scored": quality_scored,
        "persona_flagged": persona_flagged,
    }


if __name__ == "__main__":
    print(json.dumps(usage_section(), indent=2, ensure_ascii=False))
