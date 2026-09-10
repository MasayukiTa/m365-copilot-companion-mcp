"""Unit tests for the general-user usage lens (relay/selfimprove/usage.py).

Run: python -m relay.selfimprove.test_usage

Hermetic: every test builds a synthetic .fleet/history.json (+ optional status.json) and real
temp transcript .jsonl files in a TemporaryDirectory, then calls usage_section(history_path=...,
status_path=...). stdlib only; no real ledger is touched.

Covers both halves of the lens:
  - the existing arithmetic (n_tasks / completion_rate / status_mix / median_turns), and
  - the wired-in persona-leak quality fields (persona_leak_rate / quality_scored / persona_flagged),
    plus the defensive degrade-to-empty path.
"""
import json
import os
import tempfile

from relay.selfimprove import usage as U


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False))


def _write_transcript(path, assistant_text):
    """Write a minimal jsonl transcript: a meta line + one user + one assistant record.

    score_run resolves the body from the LAST role=="assistant" text, so that is what we control.
    """
    rows = [
        {"meta": True, "ts": 0},
        {"role": "user", "text": "question", "ts": 1, "turn": 1},
        {"role": "assistant", "text": assistant_text, "ts": 2, "turn": 1},
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# A clearly dirty-persona body: fires coaching + condescension (>=2 distinct classes -> leak).
_DIRTY = (
    "まずは基礎を完璧に固めろ。今の理解レベルだと初心者の9割は詰む。"
    "言っておくが、ここを飛ばすと後で必ず詰まるぞ。"
)
# Clean bodies: calm, fact-based recommendations (the 2026-06-28 CLEAN-label style). No leak.
_CLEAN_A = (
    "マージ(merge)を基本に使うとよいです。理由は履歴がそのまま残り、共有済みの履歴を壊さないためです。"
    "鉄則: push済み・共有済みの履歴は rebase しない。"
)
_CLEAN_B = (
    "REST と GraphQL の違いはエンドポイント設計にあります。"
    "用途に応じて選ぶのが一般的です。小規模なら REST で十分なことが多いです。"
)


def test_usage_arithmetic_and_persona_fields():
    with tempfile.TemporaryDirectory() as d:
        # three transcripts: one dirty, two clean
        t_dirty = os.path.join(d, "t_dirty.jsonl")
        t_clean1 = os.path.join(d, "t_clean1.jsonl")
        t_clean2 = os.path.join(d, "t_clean2.jsonl")
        _write_transcript(t_dirty, _DIRTY)
        _write_transcript(t_clean1, _CLEAN_A)
        _write_transcript(t_clean2, _CLEAN_B)

        # history.json: 5 items in seq order. 3 done (with turns), 1 stuck, 1 error.
        history = os.path.join(d, "history.json")
        _write_json(history, [
            {"key": "k1", "status": "done", "turn": 4, "seq": 1,
             "goal": "merge vs rebase", "transcript": t_clean1},
            {"key": "k2", "status": "done", "turn": 8, "seq": 2,
             "goal": "rest vs graphql", "transcript": t_clean2},
            {"key": "k3", "status": "done", "turn": 6, "seq": 3,
             "goal": "study plan", "transcript": t_dirty},
            {"key": "k4", "status": "stuck", "turn": 12, "seq": 4,
             "goal": "x", "transcript": None},
            {"key": "k5", "status": "error", "turn": 3, "seq": 5,
             "goal": "y", "outcome": "DONE"},
        ])

        status = os.path.join(d, "status.json")
        # verify_attempts is carried because a verified=False with ZERO attempts cannot come
        # from a gate at all -- _poll_verify increments it before setting False -- and is no
        # longer counted as a verification. Both snapshot builders always write the field, so
        # this is the shape a real worker has; omitting it made this fixture describe a row the
        # fleet cannot produce.
        _write_json(status, {"workers": [{"verified": "True", "verify_attempts": 1},
                                         {"verified": "False", "verify_attempts": 2}]})

        u = U.usage_section(history_path=history, status_path=status)

        # --- existing arithmetic ---
        assert u["n_tasks"] == 5
        assert u["completion_rate"] == round(3 / 5, 4)          # 3 done / 5
        assert u["status_mix"] == {"done": 3, "stuck": 1, "error": 1}
        # median of done turns [4, 8, 6] -> sorted [4,6,8] -> 6
        assert u["median_turns"] == 6
        assert u["verify_rate"] == 0.5                           # 1 True of 2 verifiable

        # --- persona-leak quality fields ---
        # bodies resolvable: 3 transcripts + 1 outcome("DONE") = 4 scored (k4 has no body -> skipped)
        assert u["quality_scored"] == 4
        assert isinstance(u["persona_leak_rate"], float)
        assert u["persona_leak_rate"] > 0                        # the one dirty body leaked
        # exactly 1 leak of 4 scored -> 0.25
        assert u["persona_leak_rate"] == round(1 / 4, 4)
        # persona_flagged: a list of thinned rows (key/signals/excerpt only), the dirty one present
        pf = u["persona_flagged"]
        assert isinstance(pf, list) and len(pf) == 1
        row = pf[0]
        assert set(row.keys()) == {"key", "signals", "excerpt"}
        assert row["key"] == "k3"
        assert isinstance(row["signals"], list) and len(row["signals"]) >= 2
        assert isinstance(row["excerpt"], str) and row["excerpt"]
    print("ok test_usage_arithmetic_and_persona_fields")


def test_all_clean_history_zero_leak():
    with tempfile.TemporaryDirectory() as d:
        t1 = os.path.join(d, "c1.jsonl")
        t2 = os.path.join(d, "c2.jsonl")
        _write_transcript(t1, _CLEAN_A)
        _write_transcript(t2, _CLEAN_B)
        history = os.path.join(d, "history.json")
        _write_json(history, [
            {"key": "k1", "status": "done", "turn": 5, "seq": 1, "transcript": t1},
            {"key": "k2", "status": "done", "turn": 5, "seq": 2, "transcript": t2},
        ])
        u = U.usage_section(history_path=history, status_path=os.path.join(d, "none.json"))
        assert u["quality_scored"] == 2
        assert u["persona_leak_rate"] == 0.0                     # both clean
        assert u["persona_flagged"] == []
    print("ok test_all_clean_history_zero_leak")


def test_missing_history_degrades_to_empty_but_valid():
    # No history file at all -> empty-but-valid section, persona_leak_rate=None, no exception.
    nodir = os.path.join(tempfile.gettempdir(), "no_such_dir_usage_zzz")
    u = U.usage_section(history_path=os.path.join(nodir, "history.json"),
                        status_path=os.path.join(nodir, "status.json"))
    assert u["n_tasks"] == 0
    assert u["completion_rate"] is None
    assert u["status_mix"] == {}
    assert u["median_turns"] is None
    # persona lens degrades cleanly: nothing to score, rate is None (not a misleading 0.0)
    assert u["persona_leak_rate"] is None
    assert u["quality_scored"] == 0
    assert u["persona_flagged"] == []
    print("ok test_missing_history_degrades_to_empty_but_valid")


def test_persona_flagged_capped_at_10():
    # 15 dirty bodies -> leak_rate 1.0, but persona_flagged is thinned to the top <=10.
    with tempfile.TemporaryDirectory() as d:
        items = []
        for i in range(15):
            tp = os.path.join(d, "t%02d.jsonl" % i)
            _write_transcript(tp, _DIRTY)
            items.append({"key": "k%02d" % i, "status": "done", "turn": 3,
                          "seq": i, "transcript": tp})
        history = os.path.join(d, "history.json")
        _write_json(history, items)
        u = U.usage_section(history_path=history, status_path=os.path.join(d, "none.json"))
        assert u["quality_scored"] == 15
        assert u["persona_leak_rate"] == 1.0
        assert len(u["persona_flagged"]) == 10                   # capped at 10
        for row in u["persona_flagged"]:
            assert set(row.keys()) == {"key", "signals", "excerpt"}
    print("ok test_persona_flagged_capped_at_10")


if __name__ == "__main__":
    test_usage_arithmetic_and_persona_fields()
    test_all_clean_history_zero_leak()
    test_missing_history_degrades_to_empty_but_valid()
    test_persona_flagged_capped_at_10()
    print("ALL USAGE TESTS PASSED")


def test_verify_rate_prefers_the_archive_over_the_live_run(tmp_path):
    """A rate over the CURRENT run is a rate over as few as one worker.

    verify_rate read only .fleet/status.json, and said why: "history rows don't carry
    verified". They do now. The difference is not cosmetic -- the live snapshot holds only the
    workers of the run that happens to be in progress, so on 2026-09-08 a single unverified
    worker rendered as 0.0 sitting beside "44 tasks", which reads like an all-time figure and
    was a sample of one.
    """
    import json
    import os
    from relay.selfimprove import usage as U

    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    status = os.path.join(d, "status.json")
    # archive: 3 verifiable, 2 of them true
    with open(history, "w", encoding="utf-8") as fh:
        json.dump([{"status": "done", "verified": True, "verify_attempts": 1},
                   {"status": "done", "verified": True, "verify_attempts": 1},
                   # a REAL failure: the gate ran and did not pass, so it has attempts
                   {"status": "stuck", "verified": False, "verify_attempts": 3},
                   {"status": "done"}], fh)
    # live: a single unverified worker -- the shape that produced the misleading 0.0
    with open(status, "w", encoding="utf-8") as fh:
        json.dump({"workers": [{"verified": "False"}]}, fh)

    u = U.usage_section(history_path=history, status_path=status)
    assert u["verify_rate"] == round(2 / 3, 4), "the live run overrode the archive"
    assert u["verify_n"] == 3
    assert u["verify_source"] == "history"


def test_it_falls_back_to_the_live_run_when_nothing_archived_carries_the_field(tmp_path):
    """A checkout from before the field existed, or a fresh install. The live run is then the
    only evidence there is -- reported as such, so a reader can tell it from an accumulated
    rate rather than having to guess."""
    import json
    import os
    from relay.selfimprove import usage as U

    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    status = os.path.join(d, "status.json")
    with open(history, "w", encoding="utf-8") as fh:
        json.dump([{"status": "done"}, {"status": "stuck"}], fh)      # no `verified` anywhere
    with open(status, "w", encoding="utf-8") as fh:
        json.dump({"workers": [{"verified": "True", "verify_attempts": 1},
                               {"verified": "False", "verify_attempts": 2}]}, fh)

    u = U.usage_section(history_path=history, status_path=status)
    assert u["verify_rate"] == 0.5
    assert u["verify_source"] == "live"
    assert u["verify_n"] == 2


def test_no_evidence_anywhere_is_null_not_zero(tmp_path):
    """Nothing verifiable is not 0% verified. A misleading 0 is worse than an honest null --
    that distinction is the reason the original guarded on `verified in (True, False)`."""
    import json
    import os
    from relay.selfimprove import usage as U

    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    status = os.path.join(d, "status.json")
    with open(history, "w", encoding="utf-8") as fh:
        json.dump([{"status": "done"}], fh)
    with open(status, "w", encoding="utf-8") as fh:
        json.dump({"workers": [{"status": "waiting"}]}, fh)

    u = U.usage_section(history_path=history, status_path=status)
    assert u["verify_rate"] is None
    assert u["verify_source"] == "none"


# ---------------------------------------------------------------------------------------------
# The completion rate used to count claims the record had already refuted (codex-plan item 1).
#
# relay_fleet._settle_done sets status="done" FIRST and asks _claim_verdict() second, so a
# worker whose tool ledger contradicts its own DONE claim keeps status "done" and only its
# OUTCOME carries what was found. This section read status and never outcome.
#
# MEASURED ON THE LIVE ARCHIVE before the fix (215 rows, 2026-09-11):
#     status "done" = 94 = 91 DONE + 3 EVIDENCE_CONTRADICTED
# Three real tasks were refuted by their own record and counted as completions anyway --
# 0.4372 where the recomputable figure is 0.4233.
# ---------------------------------------------------------------------------------------------

def test_a_refuted_claim_is_not_a_completion(tmp_path):
    """The shape measured live: status done, outcome EVIDENCE_CONTRADICTED."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "outcome": "DONE", "turn": 2, "seq": 1},
        {"key": "b", "status": "done", "outcome": "DONE", "turn": 4, "seq": 2},
        {"key": "c", "status": "done", "outcome": "EVIDENCE_CONTRADICTED", "turn": 9, "seq": 3},
        {"key": "d", "status": "stuck", "outcome": "STUCK", "turn": 5, "seq": 4},
    ])

    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))

    assert u["completion_rate"] == round(2 / 4, 4), "the refuted claim was counted"
    assert u["contradicted_done"] == 1
    # status_mix is untouched: it reports what the machinery did, which really was three "done".
    assert u["status_mix"]["done"] == 3
    # ...and the two can be reconciled by hand, which is the whole point of the item-1 bar:
    # "実行履歴と検証結果が対応し、ダッシュボードの値を元ログから再計算できる".
    assert u["status_mix"]["done"] - u["contradicted_done"] == round(
        u["completion_rate"] * u["n_tasks"])
    # A refuted round is not a completion, so its turn count is not "how long a completion takes".
    assert u["median_turns"] == 3


def test_a_verify_failure_is_not_a_completion_either(tmp_path):
    """VERIFY_FAILED means a gate ran and the work did not pass it. relay_fleet pairs it with
    status "stuck" today, but the exclusion must not depend on that pairing holding -- the
    outcome is the positive finding, and a future status change must not silently re-admit it."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "outcome": "DONE", "seq": 1},
        {"key": "b", "status": "done", "outcome": "VERIFY_FAILED", "seq": 2},
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["completion_rate"] == round(1 / 2, 4)
    assert u["contradicted_done"] == 1


def test_only_a_positive_contradiction_excludes(tmp_path):
    """AN ABSENCE OF EVIDENCE IS NOT EVIDENCE -- the same rule _claim_verdict applies to itself.

    A row with no outcome, an empty one, or an outcome nothing here recognises must keep
    counting exactly as it did. Most real work has no mechanical oracle at all, and demoting
    it for that would invent a failure rate out of unconfigured tasks -- the mistake already
    made once here, when 67 of 68 verified=False rows turned out to be workers nothing was
    ever configured to check.
    """
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "seq": 1},                        # no outcome key
        {"key": "b", "status": "done", "outcome": "", "seq": 2},         # empty
        {"key": "c", "status": "done", "outcome": "SOMETHING_NEW", "seq": 3},
        {"key": "d", "status": "done", "outcome": "DONE", "seq": 4},
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["completion_rate"] == 1.0, "an unrecognised or absent outcome was treated as a refusal"
    assert u["contradicted_done"] == 0


def test_the_outcome_is_matched_case_insensitively_and_untrimmed(tmp_path):
    """The archive is written by a second program; a stray space or case must not decide this."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "outcome": " evidence_contradicted ", "seq": 1},
        {"key": "b", "status": "done", "outcome": "DONE", "seq": 2},
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["contradicted_done"] == 1
    assert u["completion_rate"] == round(1 / 2, 4)


def test_the_trend_and_the_recent_rate_use_the_same_rule_as_the_headline(tmp_path):
    """A sparkline that counts refuted claims while the headline does not is two answers to one
    question, and the reader cannot tell which is which."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    rows = []
    for i in range(1, 13):
        # every third row is a refuted claim
        oc = "EVIDENCE_CONTRADICTED" if i % 3 == 0 else "DONE"
        rows.append({"key": "k%d" % i, "status": "done", "outcome": oc, "seq": i})
    _write_json(history, rows)

    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"),
                        segments=4)
    assert u["completion_rate"] == round(8 / 12, 4)
    assert u["contradicted_done"] == 4
    # four buckets of three, each holding exactly one refuted row
    assert u["trend"] == [round(2 / 3, 4)] * 4
    # The recent window is min(50, max(1, n//3)) = 4, so it covers seq 9..12 -- which holds two
    # refuted rows (9 and 12), not one. Asserting the headline here would have been asserting
    # the window size, and the window is deliberately a different question from the all-time rate.
    assert u["recent_window"] == 4
    assert u["recent_completion_rate"] == round(2 / 4, 4)


def test_the_outcome_mix_is_emitted_so_the_rate_can_be_rebuilt_from_the_archive(tmp_path):
    """The bar is not "the number is right", it is "a reader can rebuild it". status_mix alone
    could not: it cannot see the outcome the exclusion turns on."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "outcome": "DONE", "seq": 1},
        {"key": "b", "status": "done", "outcome": "EVIDENCE_CONTRADICTED", "seq": 2},
        {"key": "c", "status": "stuck", "outcome": "REFUSED", "seq": 3},
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["outcome_mix"] == {"DONE": 1, "EVIDENCE_CONTRADICTED": 1, "REFUSED": 1}
    rebuilt = (u["status_mix"]["done"] - u["contradicted_done"]) / u["n_tasks"]
    assert round(rebuilt, 4) == u["completion_rate"]


# ---------------------------------------------------------------------------------------------
# verify_rate counted 67 workers nobody had configured a check for (codex-plan item 1).
#
# The tri-state fix (relay_fleet.py:4168, 2026-09-09) stopped NEW rows recording verified=False
# for "no checks configured". It could not touch the archive, and this rate is computed over the
# archive: denominator 72 = 4 True + 68 False, of which 67 carried verify_attempts == 0.
# ---------------------------------------------------------------------------------------------

def test_a_false_with_no_attempts_is_not_a_failed_verification(tmp_path):
    """_poll_verify increments verify_attempts on the failing branch BEFORE setting
    verified=False, so a real failure can never carry zero. A False with no attempts is the old
    no-checks branch, and it belongs with the None rows rather than in the denominator."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "verified": True, "verify_attempts": 1, "seq": 1},
        {"key": "b", "status": "stuck", "verified": False, "verify_attempts": 3, "seq": 2},
        # the shape that polluted the live denominator, 67 times over
        {"key": "c", "status": "done", "verified": False, "verify_attempts": 0, "seq": 3},
        {"key": "d", "status": "done", "verified": False, "seq": 4},   # field absent entirely
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["verify_n"] == 2, "an unconfigured worker was counted as a verification"
    assert u["verify_rate"] == 0.5


def test_a_none_row_is_still_excluded(tmp_path):
    """The tri-state's whole point: None means no gate ran, and it never enters the rate."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [
        {"key": "a", "status": "done", "verified": None, "seq": 1},
        {"key": "b", "status": "done", "verified": True, "verify_attempts": 2, "seq": 2},
    ])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["verify_n"] == 1
    assert u["verify_rate"] == 1.0


def test_nothing_gated_at_all_is_still_null_not_zero(tmp_path):
    """Tightening the denominator must not turn 'nothing to measure' into 0%."""
    d = str(tmp_path)
    history = os.path.join(d, "history.json")
    _write_json(history, [{"key": "a", "status": "done", "verified": False,
                           "verify_attempts": 0, "seq": 1}])
    u = U.usage_section(history_path=history, status_path=os.path.join(d, "absent.json"))
    assert u["verify_rate"] is None
    assert u["verify_source"] == "none"
