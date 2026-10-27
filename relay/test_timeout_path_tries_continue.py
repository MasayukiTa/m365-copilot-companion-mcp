"""A timed-out acting goal tries the continue turn before it is refused.

Measured 2026-10-27 (.fleet/socket_route.jsonl): 993 `resend_refused` records, 260 of them
"turn timeout without a reply", and only 43 `continue_plan` records, none for a timeout. The
reconnect path planned a continue (same conversation, relay/continue_recovery.py) before it
refused; the turn-timeout path refused outright, so a worker whose reply was merely late ended STUCK.

The fix routes the timeout through `_retry_socket(..., assume_landed=True)`: an acting goal then
takes the same continue-or-refuse decision as a dropped socket. These checks read the source, as
the neighbouring relay tests do for wiring, so a later edit cannot quietly drop the call.

Run:  python -m pytest relay/test_timeout_path_tries_continue.py
"""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "relay_fleet.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _method(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("method not found: " + name)


def test_retry_socket_accepts_assume_landed():
    fn = _method("_retry_socket")
    names = [a.arg for a in fn.args.args]
    assert "assume_landed" in names
    assert fn.args.defaults, "assume_landed must default to False so every other caller is unchanged"
    seg = ast.get_source_segment(SRC, fn)
    assert "if assume_landed and self._goal_may_act():" in seg
    assert "landed = True" in seg


def test_timeout_path_continues_before_it_refuses():
    start = SRC.index('_timeout_policy = getattr(self, "_timeout_resend_decision", None)')
    block = SRC[start:start + 5500]
    call = block.index('self._retry_socket("turn timeout without a reply",')
    call_end = block.index("))", call)
    assert "assume_landed=True" in block[call:call_end]
    delivery = block.index("if _DV.verify_on() and self._saw_output_for_turn():")
    refuse = block.index('self._refuse_resend("turn timeout without a reply", _delivery)')
    assert call < delivery < refuse, "continue and delivery verification must precede refusal"
    # a worker the continue already ended STUCK is not refused a second time
    assert 'if self.status != "stuck":' in block[call:refuse]


def test_read_only_goals_keep_the_plain_retry():
    # the refusal (and so the continue attempt) sits under `_timeout_resend != "resend"`; a goal that
    # may resend still falls through to `_retry_transient`, unchanged.
    start = SRC.index('if _timeout_resend != "resend":')
    after = SRC[start:start + 5500]
    assert "if self._retry_transient():" in after
    assert after.index('if _timeout_resend != "resend":') < after.index("if self._retry_transient():")
