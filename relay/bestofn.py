"""Best-of-N SELECTOR -- pick the best candidate patch among N solves of the SAME task.

This is the testable heart of Bet #1 in bench/AGENT_STRENGTHS.md: cheap fleet parallelism only
pays off if the SELECTOR that picks the winner is principled. The actual N parallel solves are
fleet-heavy and come later; THIS module is a pure, deterministic function over candidate *signals*
-- no fleet, no network, no subprocess, no real diffing. It operates only on the strings/signals
each candidate already carries, so the self-improvement controller can A/B-tune the selector and the
unit tests below can pin its judgment.

Candidate shape (a plain dict):
    {
        "idx":              int,            # stable index of this candidate among the N
        "diff":             str,            # the unified-diff patch this candidate proposes
        "selftest_passed":  bool | None,    # did this candidate's own red->green self-test pass?
                                            #   True = passed, False = failed, None = unknown/not run
        "refuter_refuted":  int,            # how many independent refuters DID refute this patch
        "refuter_total":    int,            # how many refuters ran (0 => no refuter signal)
        "diff_size":        int | None,     # optional; if None, derived from the +/- content lines
    }

The scoring hierarchy (rationale documented at score_candidate) is, strongest first:
  1. HARD: empty/whitespace-only diff is the floor; selftest dominates everything else.
  2. refuter survival: more independent refuters that FAILED to refute => higher.
  3. consensus / self-consistency: more candidates converging on the same change => higher
     (but weighted BELOW selftest -- a consensus of wrong answers is possible).
  4. minimality: a smaller correct-looking patch beats a sprawling one -- a mild tiebreak only.
"""
from __future__ import annotations

from typing import Iterable


# --------------------------------------------------------------------------------------------------
# Weights -- the single tunable surface.
# --------------------------------------------------------------------------------------------------
# These are intended to become a self-improvement *genome* knob the controller A/B-tunes (Bet #3:
# the genome generalizes to "anything in the harness"). Keep them all in one dict so a proposed
# selector change is a single, frozen-A/B-gateable diff. The magnitudes encode the hierarchy:
# selftest >> refuter-survival > consensus > minimality, with the empty-diff floor dominating all.
WEIGHTS: dict[str, float] = {
    "empty_floor": -1000.0,   # empty/whitespace-only diff: the floor, swamps every positive signal
    "selftest_pass": 100.0,   # own red->green self-test passed: strongest positive
    "selftest_fail": -80.0,   # own self-test ran and FAILED: strong negative
    # selftest unknown (None) contributes 0 -- neutral, neither rewarded nor punished
    "refuter": 30.0,          # scaled by survival fraction in [0,1]; 0.5 (neutral) when no refuters
    "consensus": 8.0,         # per *extra* candidate sharing this normalized diff (size-1 => 0)
    "minimality": 2.0,        # mild: rewards smaller diffs, normalized so it never dominates above
}


# --------------------------------------------------------------------------------------------------
# Diff normalization + consensus / self-consistency
# --------------------------------------------------------------------------------------------------
# Header/position noise that two patches making the SAME change can differ on (line numbers, context
# hashes, file timestamps). Dropping these lets such patches group together for consensus.
_HEADER_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "@@",            # hunk-position header: "@@ -a,b +c,d @@ ..."
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
)


def _diff_size(diff: str) -> int:
    """Number of added/removed content lines (lines starting with a single +/-).

    Excludes the +++/--- file header lines so the size reflects real edit volume, not framing.
    """
    n = 0
    for ln in (diff or "").splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+") or ln.startswith("-"):
            n += 1
    return n


def _normalize_diff(diff: str) -> str:
    """Normalize a diff for *grouping*: keep only the +/- content, drop header/position noise.

    Two patches that make the same change but differ only in line numbers / context hashes / file
    timestamps must normalize to the same string. We keep the +/- content lines (the actual edit),
    strip whitespace on each, and drop the noise headers listed in _HEADER_PREFIXES. The result is
    used only as a cluster key -- it is intentionally lossy.
    """
    kept: list[str] = []
    for raw in (diff or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if any(ln.startswith(p) for p in _HEADER_PREFIXES):
            continue
        # Keep only the meaningful edit content: added/removed lines. A bare "+"/"-" with nothing
        # after it carries no content, so skip it too.
        if ln[0] in "+-" and len(ln) > 1:
            kept.append(ln)
    return "\n".join(kept)


def _is_empty(diff: str) -> bool:
    """A candidate is 'empty' when it proposes no actual +/- edit content."""
    return _normalize_diff(diff) == ""


def consensus(candidates: Iterable[dict]) -> dict:
    """Cluster candidates by normalized-diff equality; return {idx: cluster_size}.

    cluster_size counts how many candidates (including this one) share the same normalized diff --
    the self-consistency signal best-of-N uniquely enables. Empty/whitespace-only diffs are each
    their own singleton (we do NOT cluster empties together: agreeing on "no change" is not
    convergence on a fix).
    """
    cands = list(candidates)
    groups: dict[str, list[int]] = {}
    singletons: dict[int, int] = {}
    for c in cands:
        idx = c["idx"]
        norm = _normalize_diff(c.get("diff", ""))
        if norm == "":
            singletons[idx] = 1            # each empty diff stands alone
        else:
            groups.setdefault(norm, []).append(idx)
    out: dict[int, int] = {}
    for members in groups.values():
        size = len(members)
        for idx in members:
            out[idx] = size
    out.update(singletons)
    return out


# --------------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------------

def _refuter_survival(cand: dict) -> float:
    """Fraction of refuters that FAILED to refute this patch, in [0, 1]; 0.5 (neutral) if none ran.

    survival = (total - refuted) / total. More independent refuters that could NOT break the patch
    => higher confidence it is correct.
    """
    total = int(cand.get("refuter_total", 0) or 0)
    if total <= 0:
        return 0.5
    refuted = int(cand.get("refuter_refuted", 0) or 0)
    refuted = max(0, min(refuted, total))
    return (total - refuted) / total


def score_candidate(cand: dict, consensus_size: int, *, weights: dict | None = None) -> float:
    """Score a single candidate. Pure + deterministic.

    Scoring hierarchy (strongest -> weakest), encoded by WEIGHTS magnitudes:

      HARD signals (dominant):
        * empty/whitespace-only diff -> empty_floor (large negative). A candidate that proposes no
          edit cannot be the winner while any real candidate exists. This floor swamps every
          positive below it.
        * selftest_passed is the strongest *positive*: a candidate whose own red->green self-test
          passed is far preferred. selftest_passed False is a strong *negative*. None (unknown) is
          neutral (contributes 0) -- we neither reward nor punish an unrun test.

      refuter survival:
        * (refuter_total - refuter_refuted) / refuter_total, scaled by `refuter`. Neutral 0.5 when
          no refuters ran. Ranked below selftest but above consensus.

      consensus_size (self-consistency):
        * (consensus_size - 1) * `consensus` -- more candidates converging on this same change ==
          higher confidence, but deliberately weighted BELOW selftest: a consensus of *wrong*
          answers is entirely possible, so it must never overrule a passing self-test.

      minimality (mild tiebreak):
        * prefer smaller diffs: `minimality` * 1/(1 + diff_size). Bounded and small so it only ever
          breaks ties among otherwise-equal candidates; it never dominates the signals above.
    """
    w = WEIGHTS if weights is None else weights
    diff = cand.get("diff", "")

    # HARD floor: empty diff cannot win against any real candidate.
    if _is_empty(diff):
        return float(w["empty_floor"])

    score = 0.0

    # HARD: selftest dominates.
    st = cand.get("selftest_passed", None)
    if st is True:
        score += w["selftest_pass"]
    elif st is False:
        score += w["selftest_fail"]
    # st is None -> neutral, no contribution.

    # refuter survival in [0,1].
    score += w["refuter"] * _refuter_survival(cand)

    # consensus: only EXTRA agreeing candidates add confidence (a singleton adds nothing).
    extra = max(0, int(consensus_size) - 1)
    score += w["consensus"] * extra

    # minimality: smaller diff -> larger bounded reward; never dominant.
    size = cand.get("diff_size", None)
    if size is None:
        size = _diff_size(diff)
    size = max(0, int(size))
    score += w["minimality"] * (1.0 / (1.0 + size))

    return score


# --------------------------------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------------------------------

def _effective_size(cand: dict) -> int:
    size = cand.get("diff_size", None)
    if size is None:
        size = _diff_size(cand.get("diff", ""))
    return max(0, int(size))


def _rationale(winner: dict, consensus_size: int) -> str:
    """Short string naming the signals that decided the winner."""
    parts: list[str] = []
    st = winner.get("selftest_passed", None)
    if st is True:
        parts.append("selftest passed")
    elif st is False:
        parts.append("selftest failed (least-bad)")
    else:
        parts.append("selftest unknown")
    total = int(winner.get("refuter_total", 0) or 0)
    if total > 0:
        survived = total - max(0, min(int(winner.get("refuter_refuted", 0) or 0), total))
        parts.append("refuters survived %d/%d" % (survived, total))
    if consensus_size > 1:
        parts.append("consensus cluster of %d" % consensus_size)
    else:
        parts.append("no consensus (singleton)")
    parts.append("diff_size %d" % _effective_size(winner))
    return "; ".join(parts)


def select_best(candidates: Iterable[dict], *, weights: dict | None = None) -> dict:
    """Pick the best candidate. Pure + deterministic.

    Returns:
        {
            "winner":   <the chosen candidate dict, or None if no candidates>,
            "ranking":  [ {"idx","score","consensus_size","selftest_passed"} ... best -> worst ],
            "rationale": "<short string naming the deciding signals>",
        }

    Deterministic tie-break (applied in order): higher score, then larger consensus, then smaller
    diff_size, then lower idx.
    """
    cands = list(candidates)
    if not cands:
        return {"winner": None, "ranking": [], "rationale": "no candidates"}

    # THIS SELECTOR ONLY WORKS ON DIFFS, AND IT FAILS SILENTLY ON ANYTHING ELSE.
    #
    # Every signal here is read from a unified diff. `_normalize_diff` keeps only lines
    # beginning with + or -, so prose normalises to the empty string: every candidate is then
    # "empty", every empty is its own singleton by design (agreeing on "no change" is not
    # convergence), and the self-consistency signal -- the one thing best-of-N uniquely buys --
    # is gone. What survives is minimality, so the shortest answer wins.
    #
    # MEASURED, on three text answers where two were identical and one disagreed: the selector
    # returned the ODD ONE OUT, because it was the shortest. Two candidates agreeing was
    # invisible to it. A mechanism whose whole purpose is to prefer the answer several attempts
    # converged on had inverted itself, and returned a confident rationale while doing it.
    #
    # So it refuses. An exception is recoverable; a plausible wrong winner is not, and the
    # caller cannot tell the difference from the return value.
    if len(cands) > 1 and all(_is_empty(c.get("diff", "")) for c in cands):
        raise ValueError(
            "best-of-N received %d candidates and none of them contains diff content. This "
            "selector reads every signal from a unified diff; on prose it loses consensus "
            "entirely and ranks by length alone, which picks the shortest answer rather than "
            "the agreed one. Use a selector built for the answer shape you have." % len(cands))

    csizes = consensus(cands)
    scored = []
    for c in cands:
        cs = csizes.get(c["idx"], 1)
        scored.append({
            "cand": c,
            "idx": c["idx"],
            "score": score_candidate(c, cs, weights=weights),
            "consensus_size": cs,
            "selftest_passed": c.get("selftest_passed", None),
            "_size": _effective_size(c),
        })

    # Sort best -> worst. Tie-break: higher score, larger consensus, smaller diff, lower idx.
    scored.sort(key=lambda e: (-e["score"], -e["consensus_size"], e["_size"], e["idx"]))

    winner_entry = scored[0]
    winner = winner_entry["cand"]
    ranking = [
        {
            "idx": e["idx"],
            "score": e["score"],
            "consensus_size": e["consensus_size"],
            "selftest_passed": e["selftest_passed"],
        }
        for e in scored
    ]
    return {
        "winner": winner,
        "ranking": ranking,
        "rationale": _rationale(winner, winner_entry["consensus_size"]),
    }
