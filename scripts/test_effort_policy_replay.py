"""The replay reads a ledger and never writes to it."""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import effort_policy_replay as rp  # noqa: E402


_clock = [0.0]


def _row(inst, turn, verdict, run="r1"):
    _clock[0] += 1
    ts = _clock[0]
    return {"mechanism": "refuter", "run_id": run, "instance": inst, "turn": turn, "ts": ts,
            "executed": True, "decision_after": verdict}


def _ledger(tmp_path):
    rows = [_row("a", 2, "REFUTED"), _row("a", 4, "UPHELD"),
            _row("b", 1, "UPHELD"), _row("b", 2, "UPHELD"), _row("b", 3, "UPHELD"),
            {"mechanism": "retry", "run_id": "r1", "turn": 3},
            _row("", 5, "UPHELD")]
    p = tmp_path / "mechanisms.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    return str(p)


def test_replay_reports_and_leaves_the_ledger_byte_identical(tmp_path, capsys):
    p = _ledger(tmp_path)
    before = hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert rp.main(["x", p]) == 0
    out = capsys.readouterr().out
    assert hashlib.sha256(open(p, "rb").read()).hexdigest() == before
    assert "would have been escalated:   1" in out       # worker a: REFUTED
    assert "de-escalated (never escalated): 0" in out    # structural within one worker
    assert "refuted with retries left" in out
    assert "verdict rows without instance            1" in out
    assert "CANNOT BE SHOWN" in out


def test_empty_run_id_sequences_split_when_the_turn_goes_backwards():
    rows = [_row("w", 3, "UPHELD", run=""), _row("w", 1, "UPHELD", run="")]
    seqs, _ = rp.sequences(rows)
    assert len(seqs) == 2
