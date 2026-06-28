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
        _write_json(status, {"workers": [{"verified": "True"}, {"verified": "False"}]})

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
