"""解錠パスワードを、送る文には残し、書く先からは消すこと。

伏字を入れると解錠が通らなくなるのでは、という懸念に対する答え。掛けているのは
「ファイルに書く瞬間」だけで、エージェントへ送る文には掛けていない。両立している
ことを確かめる。片方だけ確かめても意味がない:

  ・送る文まで伏せてしまうと、解錠が通らない（動かない）
  ・書く先を伏せ忘れると、台帳に平文が残る（露出する）

読み戻して再送する経路が無いことも前提。あれば伏字がそのまま送られて壊れる。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / "relay" / "relay_fleet.py"
BRIDGE = ROOT / "bridge" / "copilot_bridge.py"
PW = "S3cret-Unlock-Value"


def _src(path):
    return path.read_text(encoding="utf-8")


# ── 送る側：本物が入っていること ────────────────────────────────

def test_fleet_first_turn_carries_the_real_password(monkeypatch):
    """CHANGED 2026-09-25: the NORMAL (non-plan_mode) first turn no longer injects the
    password proactively at all -- M365 Copilot's own safety filter refused that exact shape
    deterministically (see relay/relay_fleet.py's _initial_job_with_unlock docstring). The
    password is only injected reactively now, after a genuine lock refusal.

    plan_mode (operator-set, plan-then-WAIT) is the one remaining path that still composes the
    password into the initial job at construction time, so it is what this test -- whose whole
    point is "the real password must reach the agent, not just survive redaction" -- now
    exercises."""
    import relay.relay_fleet as rf

    monkeypatch.setattr(rf, "_unlock_password", lambda: PW)
    body, did = rf._initial_job_with_unlock("ゴール", plan_mode=True)
    assert did is True
    assert PW in body, "送る文から消してしまうと解錠が通らない"
    assert "ゴール" in body


def test_fleet_first_turn_no_longer_proactively_injects(monkeypatch):
    """The normal (non-plan_mode) path: even with a password configured, turn 1 is the plain
    goal, and `did` (whether this call itself injected anything) is False."""
    import relay.relay_fleet as rf

    monkeypatch.setattr(rf, "_unlock_password", lambda: PW)
    body, did = rf._initial_job_with_unlock("ゴール")
    assert did is False
    assert PW not in body
    assert "ゴール" in body


def test_fleet_first_turn_without_a_password_is_unchanged(monkeypatch):
    import relay.relay_fleet as rf

    monkeypatch.setattr(rf, "_unlock_password", lambda: "")
    body, did = rf._initial_job_with_unlock("ゴール")
    assert did is False
    assert "ゴール" in body


# ── 書く側：本物が残らないこと ──────────────────────────────────

def test_fleet_redacts_before_writing(monkeypatch):
    import relay.relay_fleet as rf

    import tools.secret_store as ss
    monkeypatch.setattr(ss, "secret_values", lambda environ=None: [PW])
    out = rf._redact_unlock_password("解錠します password=%s 続けます" % PW)
    assert PW not in out
    assert "<redacted>" in out
    assert "続けます" in out, "伏せるのは秘密だけで、前後の文は残す"


def test_fleet_redaction_is_a_no_op_without_a_password(monkeypatch):
    import relay.relay_fleet as rf

    import tools.secret_store as ss
    monkeypatch.setattr(ss, "secret_values", lambda environ=None: [])
    text = "ふつうの返事"
    assert rf._redact_unlock_password(text) == text


def test_both_sides_of_the_transcript_are_redacted():
    """相手が復唱した場合も残さないこと。

    こちらが送った文だけ伏せても、相手が復唱すれば同じ台帳に平文で残る。
    プロンプトでは「二度と出力するな」と頼んでいるが、頼みごとであって保証ではない。
    """
    src = _src(RELAY)
    user_line = re.search(r'def user\(self, turn, text\):(.{0,300})', src, re.S).group(1)
    asst_line = re.search(r'def assistant\(self, turn, text\):(.{0,400})', src, re.S).group(1)
    assert "_redact_unlock_password" in user_line
    assert "_redact_unlock_password" in asst_line


def test_bridge_redacts_both_sides_of_the_ledger():
    src = _src(BRIDGE)
    body = re.search(r'def _persist_exchange\(.{0,1400}', src, re.S).group(0)
    appends = re.findall(r'S\.append_turn\([^)]*\)', body)
    assert len(appends) >= 2
    for call in appends[:2]:
        assert "_redact_unlock_password" in call, call


def test_bridge_redactor_exists_where_it_is_used():
    """使う場所より前に定義があること。

    定義せずに呼ぶと、台帳を書く一行目で落ちて会話ごと壊れる。
    """
    src = _src(BRIDGE)
    defined = src.index("def _redact_unlock_password")
    used = src.index("_redact_unlock_password(user_msg)")
    assert defined < used


# ── 送る文そのものは伏せていないこと（動作を壊していない） ────────

def test_the_bridge_sends_the_original_text_not_the_redacted_one(monkeypatch, tmp_path):
    """Runtime check, not a source-string match (the shape of _run_one_turn changed under
    commit 6b11ca3, "Bridge unlock budget per conversation instead of per process" -- a test
    that greps for a literal `turn_payload = msg` breaks on any such refactor even when the
    behaviour it cares about is untouched).

    Drives the real Handler._run_one_turn (browser layer stubbed, as in
    bridge/test_bridge_unlock_budget_per_conversation.py) and the real _persist_exchange
    against a throwaway session store, then checks both halves at once: the text handed to
    the stub (what would reach Copilot) still carries the secret, while the text landing in
    the session store does not.
    """
    import importlib
    import tempfile

    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, tempfile.mkdtemp())
    importlib.reload(S)

    import bridge.copilot_bridge as B
    import tools.lock_state as LS
    import tools.secret_store as ss
    monkeypatch.setattr(B, "S", S, raising=False)
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_BY_CONV", {})
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_TIMES", [])
    monkeypatch.setattr(B, "_prepare_capture_baseline", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_bridge_unlock_password", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(ss, "secret_values", lambda environ=None: [PW])
    # _run_one_turn ends by asking tools.lock_state whether THIS turn was refused for lock
    # (_bridge_should_auto_unlock -> lock_state.matching_records). Unredirected, that reads
    # the real ~/.companion_gates lock log the live supervisor on this machine writes to
    # concurrently with every test run (see conftest.py's LIVE-STATE CANARY) -- the same
    # live-file hazard bridge/test_bridge_unlock_budget_per_conversation.py's `rig` fixture
    # redirects for exactly this reason. Redirected here too, to tmp_path.
    monkeypatch.setattr(LS, "_LOG_FILE", Path(str(tmp_path / "refusals.jsonl")))
    monkeypatch.setattr(LS, "_STATE_FILE", Path(str(tmp_path / "state.json")))

    class _H(object):
        def __init__(self):
            self.sent = []

        def _send_and_stream_once(self, payload, stream_out=True):
            self.sent.append(payload)
            return "ok, %s received" % PW

    h = _H()
    h._run_one_turn = B.Handler._run_one_turn.__get__(h, _H)
    sid = S.new_session("c")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", sid, raising=False)

    msg = "解錠します password=%s 続けます" % PW
    final = h._run_one_turn(sid, msg, stream_out=False)

    assert PW in h.sent[-1], "送る文を伏せると解錠が通らない"
    B._persist_exchange(sid, msg, final)
    stored = S.all_turns(sid)
    for row in stored:
        assert PW not in row["text"], row
    assert any("<redacted>" in row["text"] for row in stored if row["role"] == "user")
