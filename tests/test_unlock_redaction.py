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
    import relay.relay_fleet as rf

    monkeypatch.setattr(rf, "_unlock_password", lambda: PW)
    body, did = rf._initial_job_with_unlock("ゴール")
    assert did is True
    assert PW in body, "送る文から消してしまうと解錠が通らない"
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

def test_the_bridge_sends_the_original_text_not_the_redacted_one():
    src = _src(BRIDGE)
    turn = src[src.index("turn_payload = msg"):]
    turn = turn[:turn.index("def ", 200)] if "def " in turn[200:] else turn[:4000]
    assert "_send_and_stream_once(turn_payload" in turn
    assert "_send_and_stream_once(_redact" not in turn, \
        "送る文を伏せると解錠が通らない"
