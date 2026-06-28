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
    done_turns = []
    completed = 0
    for h in items:
        st = (h.get("status") or "").strip() or "unknown"
        status_mix[st] = status_mix.get(st, 0) + 1
        if st == _DONE:
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
                c = sum(1 for h in chunk if (h.get("status") or "") == _DONE)
                trend.append(round(c / len(chunk), 4))

    # verify_rate from the LIVE snapshot (history rows don't carry verified); best-effort. Only count
    # workers where verification ACTUALLY ran (verified in True/False) -- otherwise the metric is null,
    # not a misleading 0% (SWE-bench workers, for instance, never set verified).
    verify_rate = None
    if isinstance(status, dict):
        workers = status.get("workers") or []
        verifiable = [w for w in workers if isinstance(w, dict) and str(w.get("verified")) in ("True", "False")]
        if verifiable:
            ver = sum(1 for w in verifiable if str(w.get("verified")) == "True")
            verify_rate = round(ver / len(verifiable), 4)

    # recent window = last min(50, n//3) tasks, so "lately" is visible vs the all-time rate
    win = min(50, max(1, n // 3)) if n else 0
    recent = items[-win:] if win else []
    recent_rate = round(sum(1 for h in recent if (h.get("status") or "") == _DONE) / len(recent), 4) if recent else None

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
        "status_mix": status_mix,
        "trend": trend,
        "persona_leak_rate": persona_leak_rate,
        "quality_scored": quality_scored,
        "persona_flagged": persona_flagged,
    }


if __name__ == "__main__":
    print(json.dumps(usage_section(), indent=2, ensure_ascii=False))
