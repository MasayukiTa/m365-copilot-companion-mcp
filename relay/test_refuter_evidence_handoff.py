import json
import time
from types import SimpleNamespace

from relay import refuter, relay_fleet


def test_session_attributed_odbc_without_saved_script(tmp_path, monkeypatch):
    now = time.time()
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in [
        {"event": "call", "ts": now - 5, "session": "s", "id": "abc", "tool": "odbc_query", "task": "", "turn": None},
        {"event": "outcome", "ts": now - 4, "id": "abc", "ok": True, "result": {"text": "rows", "truncated": False}},
    ]), encoding="utf-8")
    binding = {"s": {"worker": "w", "conversation": "c", "how": "id", "ts": now - 10}}
    route = SimpleNamespace(ledger_path=str(ledger), mcp_bindings=SimpleNamespace(snapshot=lambda: binding))
    monkeypatch.setattr(relay_fleet, "_socket_route", lambda: route)
    worker = SimpleNamespace(name="w", drv=SimpleNamespace(_turn_started_ts=now - 6),
                             _t_send=now - 6, _conversation_ids_for_binding=lambda: {"client": "c"})
    evidence = relay_fleet.RelayWorker._review_tool_provenance(worker)
    assert evidence["events"] == [{"tool": "odbc_query", "id": "abc", "outcome": "success", "truncated": False}]
    prompt = refuter.build_refuter_prompt("g", "no saved SQL", case_context={}, tool_provenance=evidence)
    assert "odbc_query" in prompt and "abc" in prompt


def test_panel_handoff(monkeypatch):
    captured = {}
    class Session:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
        def start(self):
            return self
    monkeypatch.setattr(refuter, "RefuterSession", Session)
    worker = SimpleNamespace(_panel_queue=["edge"], _context=None, _agent_url="", last_response="done",
                             checks=[], _review_case={"case_dir": "case"},
                             goal_as_amended=lambda: "goal", _procedure_note=lambda: "",
                             _review_tool_provenance=lambda: {"events": ["event"]},
                             _refuter_recorder=lambda lens: None)
    relay_fleet.RelayWorker._start_next_lens(worker)
    assert captured["case_context"]["case_dir"] == "case"
    assert captured["tool_provenance"]["events"] == ["event"]
