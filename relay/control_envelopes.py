"""Control-envelope classifiers shared by intake and execution boundaries.

These strings are protocol messages for the durable LOCAL_LOOP runtime, not user work.  If one
re-enters ordinary Fleet execution it can recursively spawn workers that all try to claim the
same durable job.  Keep the classifier tiny and dependency-free so every ingress can reuse it.
"""
from __future__ import annotations

import re

_LOCAL_LOOP_SOURCE_PREFIXES = (
    "local_loop run ",
    "local_loop bootstrap ",
    "local_loop protocol ",
)

_LOCAL_LOOP_GOAL_PREFIXES = _LOCAL_LOOP_SOURCE_PREFIXES + (
    "execute local_loop job ",
    "run local_loop job ",
)

# Exact durable-controller wire / wrapper shapes observed in production. Keep these structural,
# not broad prefix matches: ordinary prose like "Run companion analysis" or
# "LOCAL_LOOP job scheduling is slow" must remain legitimate user work.
_CONTROLLER_RUN_RE = re.compile(r"^run\s+\S+\s+seq=\d+\s+worker=[^\s:]+\s*$", re.IGNORECASE)
_LOCAL_LOOP_JOB_RE = re.compile(
    r"^local_loop\s+job\s+\S+\s+seq=\d+\s+worker=[^\s:]+(?:\s*:|\s+を(?:、|\s)|\s*$)",
    re.IGNORECASE,
)
_CLAIM_LOCAL_LOOP_JOB_RE = re.compile(
    r"^claim\s+and\s+execute\s+local_loop\s+job\s+\S+\s+seq=\d+\s+worker=[^\s:]+(?:\s*:|\s|$)",
    re.IGNORECASE,
)


def _norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def is_local_loop_control_submission(goal, source="") -> bool:
    """Return True only for a durable-runtime control envelope, never ordinary discussion.

    Provenance is strongest when present.  Goal prefixes are the fail-closed fallback for paths
    such as Cockpit retry/goals-file that do not carry provenance.  A sentence like
    ``Inspect the LOCAL_LOOP implementation`` intentionally does not match.
    """
    src = _norm(source)
    text = _norm(goal)
    return (
        src.startswith(_LOCAL_LOOP_SOURCE_PREFIXES)
        or text.startswith(_LOCAL_LOOP_GOAL_PREFIXES)
        or bool(_CONTROLLER_RUN_RE.match(text))
        or bool(_LOCAL_LOOP_JOB_RE.match(text))
        or bool(_CLAIM_LOCAL_LOOP_JOB_RE.match(text))
    )
