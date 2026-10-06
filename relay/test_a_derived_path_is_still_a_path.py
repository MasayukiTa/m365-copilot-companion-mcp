# -*- coding: utf-8 -*-
"""Eight of the operator's 154 security records were written by this test suite.

`relay/test_live_record_isolation.py` exists so a test can never write into a place where the
operator's records accumulate. It finds those places by reading module-level constants whose own
source mentions `.fleet` or `.companion_runs`. `tools/lock_state.py` declares three files under
`.fleet` and only two of them say so:

    _STATE_FILE     = Path(__file__).resolve().parent.parent / ".fleet" / "lock_state.json"
    _LOG_FILE       = Path(__file__).resolve().parent.parent / ".fleet" / "lock_refusals.jsonl"
    _TOKEN_GAP_FILE = _STATE_FILE.parent / "unlock_token_gap.json"      <- no marker

So conftest redirected the first two and not the third -- and redirecting `_STATE_FILE` does not
move `_TOKEN_GAP_FILE`, which was computed from the real path at import time.

MEASURED 2026-09-13, in the live file rather than inferred:

    ips: {"<the live connector>": 146, "198.51.100.2": 3, "198.51.100.3": 2,
          "203.0.113.77": 1, "198.51.100.77": 2}

198.51.100.0/24 and 203.0.113.0/24 are RFC 5737 documentation ranges. They appear nowhere in
this repository except tools/test_security.py, tests/test_unlock_token.py and
tests/test_unlock_oracle.py. Eight records, in the counter whose total decides whether
MCP_REQUIRE_UNLOCK_TOKEN can be enforced without an outage.

WHY IT IS THE CLASS AND NOT THE INSTANCE. The isolation walk's own docstring already said it was
a tripwire and not a proof -- "a path assembled at call time, or built from a name this does not
know, passes straight through". Adding `_TOKEN_GAP_FILE` to the table by hand would have left
the next derived constant exactly as invisible.
"""
from __future__ import annotations

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import conftest as C  # noqa: E402
from relay import test_live_record_isolation as ISO  # noqa: E402


def _found(src):
    return ISO._record_constants(src, ast.parse(src))


# ── the blind spot ────────────────────────────────────────────────────────────────────────

def test_a_constant_built_from_a_record_constant_is_found():
    """THE DEFECT, in the exact shape it had in tools/lock_state.py."""
    src = ('from pathlib import Path\n'
           'A = Path(__file__).parent / ".fleet" / "state.json"\n'
           'B = A.parent / "derived.json"\n')
    assert _found(src) == {"A", "B"}


def test_it_follows_more_than_one_step():
    """A derived path can itself be derived from; the pass runs to a fixpoint."""
    src = ('from pathlib import Path\n'
           'A = Path(".fleet")\n'
           'B = A / "one.json"\n'
           'C = B.parent / "two.json"\n')
    assert _found(src) == {"A", "B", "C"}


def test_the_declared_form_still_works():
    """THE MARKER IS A PATH SEGMENT, NOT A SUBSTRING. The first version of this test wrote
    `X = "/x/.companion_runs/corrections.jsonl"` and failed -- correctly. The walk looks for the
    directory as its own quoted string, so a name that merely contains it does not count, and a
    fixture that assumes otherwise is testing a rule the code does not have."""
    src = ('from pathlib import Path\n'
           'X = Path("/x") / ".companion_runs" / "corrections.jsonl"\n')
    assert _found(src) == {"X"}


def test_an_unrelated_constant_is_not_swept_in():
    """A guard that flags everything gets exemptions written for it until nobody believes its
    failures any more -- the reason RECORD_DIR_MARKERS is a short list and not "outside the
    repo"."""
    src = ('from pathlib import Path\n'
           'A = Path(".fleet") / "state.json"\n'
           'TIMEOUT = 30\n'
           'OTHER = Path("build") / "out.txt"\n')
    assert _found(src) == {"A"}


def test_a_lowercase_name_is_not_a_declared_path():
    """The module-level-constant convention is what separates a declared location from a
    computed value; the derived pass must not quietly widen it."""
    src = ('from pathlib import Path\n'
           'A = Path(".fleet") / "state.json"\n'
           'b = A.parent / "derived.json"\n')
    assert _found(src) == {"A"}


# ── the instance it was found by ──────────────────────────────────────────────────────────

def test_the_gap_counter_is_seen_now():
    assert ("tools.lock_state", "_TOKEN_GAP_FILE") in ISO.fleet_constants()


def test_the_gap_counter_is_redirected():
    assert C.LIVE_RECORD_REDIRECTS["tools.lock_state"].get("_TOKEN_GAP_FILE"), (
        "見えるようになっただけで、まだ本物のファイルに書いている")


def test_a_test_that_records_a_gap_does_not_touch_the_live_file():
    """THE MEASUREMENT THAT MATTERS. Everything above is about seeing it; this is about the
    eight records. The write goes through the same call security.py makes on the hot path."""
    from tools import lock_state

    live = os.path.join(REPO, ".fleet", "unlock_token_gap.json")
    before = os.path.getmtime(live) if os.path.exists(live) else None
    lock_state.record_token_gap("203.0.113.77")
    after = os.path.getmtime(live) if os.path.exists(live) else None
    assert before == after, "オペレータの実ファイルを書き換えた"
    assert lock_state.token_gap().get("count") == 1, "リダイレクト先にも書けていない"
