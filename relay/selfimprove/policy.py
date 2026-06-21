"""L3 campaign policy: held-out rotation + plateau stop + tripwires.

This is the L3 layer of the autonomy ladder (bench/SELF_GROWTH_L4_DESIGN.md sec 1): L2 (the gated,
auto-committing iteration driver) plus the discipline that decides *which held-out data to feed next*,
*when to stop chasing noise*, and *when to halt-and-page-a-human* because something looks like a leak
or a confound rather than real capability.

Three concerns, kept small/pure/testable like guards.py so the campaign runner (and L2, task #24) can
compose them without re-deriving the judgment:

  1. DatasetRotation     -- ordered curriculum (sec 5): Verified-fresh -> full -> other; hand out a
                            deterministic slice of NON-burned ids, advancing past exhausted datasets.
  2. plateaued           -- K consecutive iterations with no gate-passing genome -> stop (sec 8).
  3. tripwires           -- frozen-set changed / implausible pass@1 jump / infra-fault spike /
                            sentinel regressed; each FIRED == halt + page a human (sec 8).
  4. run_campaign        -- the loop that wires them around an INJECTED iterate_fn (the L2 driver).

CRITICAL: this module never imports the L2 driver at top level (it is being written in parallel) and
never reads the wall clock directly. The campaign runner takes an injected `iterate_fn` and `now_fn`
so it is fully deterministic under test. guards is imported READ-ONLY only for typing/reference; the
runner depends only on the duck-typed `.filter_fresh` of whatever `burned` object it is handed.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Iterable

# READ-ONLY reference import; we only rely on the duck-typed `.filter_fresh(ids) -> list` so tests may
# pass a real guards.BurnedRegistry or any stand-in. Do NOT import relay.selfimprove.l2 here.
from relay.selfimprove import guards as _guards  # noqa: F401  (kept for reference/typing)

_DEFAULT_SEED = 20260621


# --------------------------------------------------------------------------------------------------
# 1. Dataset rotation (held-out curriculum)
# --------------------------------------------------------------------------------------------------

def _default_available_ids(spec_path: str) -> list[str]:
    """Read a spec json (a list of {"instance_id": ...} dicts) and return its instance_ids.

    A missing/unreadable spec yields [] -- a dataset whose spec is absent is simply treated as
    exhausted, never an error, so the curriculum can advance past it.
    """
    if not spec_path or not os.path.isfile(spec_path):
        return []
    try:
        with open(spec_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out: list[str] = []
    for row in data or []:
        if isinstance(row, dict) and "instance_id" in row:
            out.append(row["instance_id"])
    return out


class DatasetRotation:
    """An ordered curriculum of held-out datasets.

    Construct from a list of dicts, e.g.
        [{"key": "Verified", "spec_path": "verified.json"},
         {"key": "Full",     "spec_path": "full.json"}, ...]
    Order is the curriculum order: Verified-fresh first, expand to broader pools only when the
    earlier ones run out of NON-burned ids (sec 5). An instance is graded at most once per genome and
    then burned, so "fresh" shrinks monotonically across a campaign.
    """

    def __init__(self, datasets: Iterable[dict], seed: int = _DEFAULT_SEED):
        self.datasets = [dict(d) for d in datasets]
        self.seed = seed

    def next_slice(self, n: int, burned, available_ids_fn: Callable[[str], list[str]] | None = None
                   ) -> tuple[str, list[str]] | None:
        """Return (dataset_key, up-to-n fresh ids) for the first non-exhausted dataset, else None.

        - `burned` is a guards.BurnedRegistry (or anything with `.filter_fresh(ids) -> list`).
        - `available_ids_fn(spec_path) -> list[str]` is injected; defaults to reading the spec json.
        - walks datasets in curriculum order; the first one with >= 1 fresh id wins, and we hand back
          a DETERMINISTIC slice: the fresh ids sorted, truncated to n. (Sorted, not sampled, so a
          replay is byte-identical; seed is retained for a future sampling policy but unused here.)
        - returns None iff every dataset is exhausted (no fresh ids anywhere).
        """
        get_ids = available_ids_fn or _default_available_ids
        for ds in self.datasets:
            ids = get_ids(ds.get("spec_path", ""))
            fresh = burned.filter_fresh(ids)
            if fresh:
                chosen = sorted(fresh)[: max(0, int(n))]
                return ds["key"], chosen
        return None


# --------------------------------------------------------------------------------------------------
# 2. Plateau detector
# --------------------------------------------------------------------------------------------------

def _keep_flag(result: dict) -> bool:
    """Truthy keep flag of an iteration result: prefer 'final_keep', fall back to 'kept'."""
    if "final_keep" in result:
        return bool(result.get("final_keep"))
    return bool(result.get("kept"))


def plateaued(history: Iterable[dict], k: int) -> bool:
    """True iff there are >= k results and the last k all have a falsey keep flag.

    "No gate-passing genome for k straight iterations" (sec 8). A single kept genome anywhere in the
    last-k window resets the plateau -> False. k <= 0 never plateaus.
    """
    hist = list(history)
    if k <= 0 or len(hist) < k:
        return False
    window = hist[-k:]
    return all(not _keep_flag(r) for r in window)


# --------------------------------------------------------------------------------------------------
# 3. Tripwires (each True == FIRED == halt + page a human)
# --------------------------------------------------------------------------------------------------

def tw_frozen_changed(frozen_ok: bool) -> bool:
    """Fired iff the frozen constitution is NOT intact (a frozen file changed -> possible hack)."""
    return not bool(frozen_ok)


def tw_implausible_jump(prev_pass, new_pass, max_pp: float = 25.0) -> bool:
    """Fired iff pass@1 jumped by more than max_pp percentage points in a single step.

    A one-step jump that large is almost always a leak / grader hack, not real capability (sec 8).
    prev_pass None (no prior measurement) -> never fires.
    """
    if prev_pass is None or new_pass is None:
        return False
    return (float(new_pass) - float(prev_pass)) > float(max_pp)


def tw_infra_spike(infra_rate: float, threshold: float = 0.30) -> bool:
    """Fired iff the infra-fault rate exceeds threshold (harness is sick; gains are untrustworthy)."""
    return float(infra_rate) > float(threshold)


def tw_sentinel_regressed(sentinel_regressed: bool) -> bool:
    """Fired iff the cross-dataset sentinel regressed (gain is likely dataset/grader-specific)."""
    return bool(sentinel_regressed)


def evaluate_tripwires(state: dict) -> list[str]:
    """Return the NAMES of all fired tripwires, evaluating ONLY those whose inputs are present.

    state may contain any subset of:
      frozen_ok, prev_pass, new_pass, infra_rate, sentinel_regressed.
    A tripwire whose required input is absent from state is simply not evaluated (so a partial state
    cannot falsely fire). Returned names match the predicate names (sans the tw_ prefix is NOT used;
    we return the human-facing tripwire id).
    """
    fired: list[str] = []
    if "frozen_ok" in state:
        if tw_frozen_changed(state.get("frozen_ok")):
            fired.append("frozen_changed")
    if "new_pass" in state:
        # implausible jump needs a current measurement; prev_pass may legitimately be None (first run)
        if tw_implausible_jump(state.get("prev_pass"), state.get("new_pass")):
            fired.append("implausible_jump")
    if "infra_rate" in state:
        if tw_infra_spike(state.get("infra_rate")):
            fired.append("infra_spike")
    if "sentinel_regressed" in state:
        if tw_sentinel_regressed(state.get("sentinel_regressed")):
            fired.append("sentinel_regressed")
    return fired


# --------------------------------------------------------------------------------------------------
# 4. Campaign runner
# --------------------------------------------------------------------------------------------------

def run_campaign(iterate_fn: Callable[[str, list[str]], dict], rotation: DatasetRotation, burned,
                 available_ids_fn: Callable[[str], list[str]] | None = None, *, n: int,
                 max_iters: int, plateau_k: int = 3, max_hours: float | None = None,
                 now_fn: Callable[[], float] | None = None, seed: int = _DEFAULT_SEED) -> dict:
    """Drive held-out iterations until a stop condition is reached.

    Each loop:
      1. slice = rotation.next_slice(n, burned, available_ids_fn); None -> stop_reason "exhausted".
      2. result = iterate_fn(dataset_key, slice_ids)  (the INJECTED L2 driver; returns a dict).
      3. tripwires: any fired (result['tripwires'] or evaluate_tripwires(result)) ->
         stop_reason "tripwire:<comma-names>" and break BEFORE recording the result as progress
         (page-human semantics: the caller sees the reason).
      4. append result to history; if plateaued(history, plateau_k) -> stop_reason "plateau".
      5. ceilings: len(history) >= max_iters -> "ceiling"; if max_hours and now_fn and elapsed >=
         max_hours -> "time_ceiling".

    `now_fn` is injected (pass time.time normally) so tests are deterministic; this function NEVER
    calls the wall clock itself. kept_ids accumulates slice ids of iterations whose keep flag is set.

    Returns {"iterations": len(history), "stop_reason": ..., "kept_ids": [...], "history": history}.
    """
    history: list[dict] = []
    kept_ids: list[str] = []
    stop_reason = "ceiling"
    start = now_fn() if (max_hours is not None and now_fn is not None) else None

    while True:
        sl = rotation.next_slice(n, burned, available_ids_fn)
        if sl is None:
            stop_reason = "exhausted"
            break
        dataset_key, slice_ids = sl

        result = iterate_fn(dataset_key, slice_ids)
        result = dict(result) if result else {}
        result.setdefault("dataset_key", dataset_key)
        result.setdefault("slice_ids", list(slice_ids))

        fired = list(result.get("tripwires") or []) + evaluate_tripwires(result)
        # de-duplicate, preserve order
        seen, fired_uniq = set(), []
        for name in fired:
            if name not in seen:
                seen.add(name)
                fired_uniq.append(name)
        if fired_uniq:
            stop_reason = "tripwire:" + ",".join(fired_uniq)
            break

        history.append(result)
        if _keep_flag(result):
            kept_ids.extend(slice_ids)

        if plateaued(history, plateau_k):
            stop_reason = "plateau"
            break

        if len(history) >= max_iters:
            stop_reason = "ceiling"
            break

        if max_hours is not None and now_fn is not None:
            elapsed_hours = (now_fn() - start) / 3600.0
            if elapsed_hours >= max_hours:
                stop_reason = "time_ceiling"
                break

    return {"iterations": len(history), "stop_reason": stop_reason,
            "kept_ids": kept_ids, "history": history}
