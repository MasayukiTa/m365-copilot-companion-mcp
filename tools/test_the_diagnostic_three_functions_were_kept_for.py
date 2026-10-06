# -*- coding: utf-8 -*-
"""Three functions said they were kept for the CLI, and the CLI called none of them.

    locked_since     "Kept for the CLI and for diagnostics, where naming the last refusal is
                      exactly the question."
    matching_record  "Kept for callers that only want to name one record (the CLI,
                      diagnostics)."
    locked_recently  "True iff a lock refusal was recorded within `within_sec`."

`_cli` had `show` and `token-gap`. So the justification for keeping all three rested on a
surface that was never built -- which is worse than no justification, because it reads as
settled and stops anyone asking.

NOT `matching_records` (plural). That one is live in three modules and answers a different
question -- "which refusals could have been MINE", which a caller uses to decide. These answer
"was anything refused, when, and which one", which is what a person looking at a stuck worker
asks. Both are wanted; only the plural had a caller.

AND THE FIRST DRAFT OF THE READER PRINTED A CONTRADICTION. Over a 24-hour window it said
`locked_recently: true`, `locked_since: false`, `record: {}` -- which reads as a bug and is the
design: `locked_since` and `matching_record` are ADDITIONALLY capped at DEFAULT_FRESH_SEC so a
clock jump cannot resurrect an ancient record, while `locked_recently` honours the window it is
given. Two windows, labelled, with the age printed beside them.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import lock_state as LS  # noqa: E402


def _run(monkeypatch, argv):
    """Drive `_cli` the way `python -m tools.lock_state` does, and return its JSON."""
    import io as _io
    import contextlib

    monkeypatch.setattr(sys, "argv", ["tools.lock_state"] + list(argv))
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        LS._cli()
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _refusal(monkeypatch, tmp_path, age_s):
    """One recorded refusal, `age_s` old, in a state file of this test's own."""
    import time

    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "lock_state.json", raising=False)
    monkeypatch.setattr(LS, "_LOG_FILE", tmp_path / "lock_refusals.jsonl", raising=False)
    LS.record_locked("203.0.113.9", "refused", ts=time.time() - age_s)


# ── the diagnostic exists and uses all three ──────────────────────────────────────────────

def test_a_fresh_refusal_is_reported_by_every_one_of_them(monkeypatch, tmp_path):
    _refusal(monkeypatch, tmp_path, age_s=5)
    out = _run(monkeypatch, ["recent"])
    assert out["in_asked_window"] is True
    assert out["in_fresh_window"] is True
    assert out["record_if_fresh"], "the record itself is the thing a person wants named"
    assert out["last_refusal_age_s"] < 60


def test_the_two_windows_are_labelled_so_they_do_not_read_as_a_contradiction(
        monkeypatch, tmp_path):
    """THE DEFECT IN THE FIRST READER. A refusal older than the freshness cap but inside the
    asked-for window makes the three disagree, correctly, and the output has to show why."""
    _refusal(monkeypatch, tmp_path, age_s=LS.DEFAULT_FRESH_SEC + 600)
    out = _run(monkeypatch, ["recent", "86400"])
    assert out["in_asked_window"] is True
    assert out["in_fresh_window"] is False
    assert out["record_if_fresh"] == {}
    assert out["fresh_window_s"] == LS.DEFAULT_FRESH_SEC
    assert out["asked_window_s"] == 86400.0
    assert out["last_refusal_age_s"] > LS.DEFAULT_FRESH_SEC


def test_nothing_recorded_says_nothing_rather_than_no(monkeypatch, tmp_path):
    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "lock_state.json", raising=False)
    monkeypatch.setattr(LS, "_LOG_FILE", tmp_path / "lock_refusals.jsonl", raising=False)
    out = _run(monkeypatch, ["recent"])
    assert out["in_asked_window"] is False
    assert out["last_refusal_ts"] is None
    assert out["last_refusal_age_s"] is None


def test_the_window_is_an_argument(monkeypatch, tmp_path):
    _refusal(monkeypatch, tmp_path, age_s=300)
    assert _run(monkeypatch, ["recent", "60"])["in_asked_window"] is False
    assert _run(monkeypatch, ["recent", "3600"])["in_asked_window"] is True


def test_a_window_that_is_not_a_number_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "lock_state.json", raising=False)
    monkeypatch.setattr(sys, "argv", ["tools.lock_state", "recent", "soon"])
    try:
        LS._cli()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("a non-numeric window was accepted")


# ── the surfaces that already existed must not have moved ─────────────────────────────────

def test_show_is_still_the_default(monkeypatch, tmp_path):
    """The cockpit's 詳細設定 panel shells out to it."""
    _refusal(monkeypatch, tmp_path, age_s=5)
    out = _run(monkeypatch, [])
    assert out.get("client_ip") == "203.0.113.9"


def test_token_gap_still_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(LS, "_TOKEN_GAP_FILE", tmp_path / "unlock_token_gap.json",
                        raising=False)
    out = _run(monkeypatch, ["token-gap"])
    assert set(out) >= {"count", "safe_to_enforce", "enforcing"}


def test_an_unknown_subcommand_is_still_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tools.lock_state", "nope"])
    try:
        LS._cli()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("an unknown subcommand was accepted")
