import json

import pytest

from relay import judge_gate
from relay.test_judge_correction_turn import GOAL, _valid_request, _write, worker
from relay.relay_fleet import JUDGE_CORRECTION_BUDGET


@pytest.mark.parametrize("contents, expected", [
    ('{"name": "日本語"}'.encode("cp932"), "not valid UTF-8 at byte"),
    (b'{"claim_id": }', "not valid JSON at line 1, column 14"),
])
def test_bad_request_gets_bounded_correction(tmp_path, monkeypatch, contents, expected):
    monkeypatch.setenv("MCP_JUDGE_STRICT", "1")
    def no_engine(*args, **kwargs):
        pytest.fail("malformed request must never reach the engine")
    monkeypatch.setattr(judge_gate, "run_engine_judge", no_engine)
    (tmp_path / "judge_request.json").write_bytes(contents)
    w = worker(tmp_path)
    for attempt in range(1, JUDGE_CORRECTION_BUDGET + 1):
        w._settle_done()
        assert w.status == "ready" and w._judge_correction_attempt == attempt
        assert expected in w.job
        assert "json.dump(obj, f, ensure_ascii=False)" in w.job
        w.status = "waiting"
    record = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert [c["attempt"] for c in record["corrections"]] == list(
        range(1, JUDGE_CORRECTION_BUDGET + 1))
    assert all(expected in c["messages"][0] for c in record["corrections"])
    w._settle_done()
    assert w.status == "stuck" and w.outcome == "VERIFY_FAILED"
    assert "judge: request invalid (" in w.reason and expected in w.reason


def test_rewritten_utf8_request_reaches_engine_unchanged(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(judge_gate, "run_engine_judge", lambda *a, **kw: (
        calls.append((a, kw)) or {"engine_available": False,
                                  "unavailable_reason": "engine root not found"}))
    (tmp_path / "judge_request.json").write_bytes('{"name":"日本語"}'.encode("cp932"))
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "ready" and not calls
    _write(tmp_path, _valid_request())
    w.status = "waiting"
    w._settle_done()
    assert w.status == "done" and w.outcome == "DONE"
    assert len(calls) == 1 and calls[0][0] == ("SYN1",)
    assert calls[0][1]["registered_claim"] == {"id": "SYN1"}
