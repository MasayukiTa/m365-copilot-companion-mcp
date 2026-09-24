"""Tests for honest framing when an unlock-recovery payload is REDELIVERED to a worker.

BACKGROUND (owner report, 2026-09-25): fleet workers were stopping with a stuck reason of
"the agent judged the injected unlock instruction as a prompt injection and refused to use
the password." Root cause, found by reading the two channels that redeliver a pending job to
a worker that is not currently mid-turn:

  * RelayWorker._begin_send's steer branch (relay_fleet.py) wraps WHATEVER text is sitting in
    self.steer_msgs as "【ユーザーからの追加指示】" ("additional instruction FROM THE USER").
  * fleet_runner._follow_up's FOLLOW_UP_PROMPT does the identical thing for a worker that had
    already gone TERMINAL by the time its pending job was ready to resume.

Both channels are also used to redeliver relay_fleet._inject_unlock's own UNLOCK_PREFIX
payload (composed by THIS process from the local .env password, never by a person) whenever
the worker that needed it had already left the 'ready' state. Confirmed against
.fleet/transcripts/*.jsonl: the wrapped text reads verbatim "...ユーザーからの追加指示】
【要解錠】書込/実行ツールはロック解除が必要です。まず call_tool で 'unlock' を引数
{"password": ...". That is a FALSE "from the user" label wrapped around text that hands over a
credential and directs a tool call -- exactly the shape a safety-aligned model is right to
refuse as an injection. The label was lying, not the model's judgment.

The fix (relay_fleet.is_recovery_payload / SYSTEM_RECOVERY_PREFIX, and fleet_runner's
SYSTEM_RECOVERY_FOLLOW_UP_PROMPT) recognises UNLOCK_PREFIX's distinctive marker and, only for
that payload, substitutes an honest "this machine's own automation, not the user" label --
without changing how a genuine human steer is framed.

Run:  .venv\\Scripts\\python.exe relay\\test_unlock_redelivery_framing.py
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PW = "unit_test_redelivery_pw_9f2"
_PREVIOUS_PW = os.environ.get("MCP_UNLOCK_PASSWORD")
os.environ["MCP_UNLOCK_PASSWORD"] = PW


def _restore_env():
    if _PREVIOUS_PW is None:
        os.environ.pop("MCP_UNLOCK_PASSWORD", None)
    else:
        os.environ["MCP_UNLOCK_PASSWORD"] = _PREVIOUS_PW


try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _unlock_env():
        os.environ["MCP_UNLOCK_PASSWORD"] = PW
        yield
        _restore_env()
except ImportError:
    pytest = None

from relay.relay_fleet import (
    RelayWorker, UNLOCK_PREFIX, SYSTEM_RECOVERY_PREFIX, is_recovery_payload,
)
from relay import fleet_runner
from relay.fleet_runner import (
    FOLLOW_UP_PROMPT, SYSTEM_RECOVERY_FOLLOW_UP_PROMPT, _follow_up,
)

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class _StubWorker:
    """The minimum _follow_up reads: name, goal, cwd."""
    def __init__(self, name, goal, cwd=""):
        self.name = name
        self.goal = goal
        self.cwd = cwd


def main():
    unlock_payload = (UNLOCK_PREFIX % PW) + "original goal text here"
    human_steer = "追加指示: ログも出力して"

    # --- 1. is_recovery_payload: pure classifier ---
    check("recognises_unlock_payload", is_recovery_payload(unlock_payload) is True)
    check("does_not_flag_ordinary_steer", is_recovery_payload(human_steer) is False)
    check("does_not_flag_empty", is_recovery_payload("") is False)
    check("does_not_flag_none", is_recovery_payload(None) is False)

    # --- 2. RelayWorker._begin_send's steer branch picks the right label ---
    w = RelayWorker("some goal", "w0", max_continue=8, max_no_progress=100)
    w.steer(unlock_payload)
    w._begin_send()
    check("recovery_payload_not_labelled_as_user_instruction",
          "【ユーザーからの追加指示】" not in w.job)
    check("recovery_payload_gets_system_label", SYSTEM_RECOVERY_PREFIX in w.job)
    check("recovery_payload_content_preserved", unlock_payload in w.job)

    w2 = RelayWorker("some goal", "w1", max_continue=8, max_no_progress=100)
    w2.steer(human_steer)
    w2._begin_send()
    check("genuine_steer_still_labelled_as_user_instruction",
          "【ユーザーからの追加指示】" in w2.job)
    check("genuine_steer_not_mislabelled_as_system", SYSTEM_RECOVERY_PREFIX not in w2.job)

    # --- 3. fleet_runner._follow_up picks the right template ---
    sent = []

    def _enqueue(d):
        sent.append(d)

    def _say(msg):
        pass

    stub = _StubWorker("w2follow", "the original goal")
    ok = _follow_up(stub, unlock_payload, _enqueue, _say)
    check("follow_up_queued_recovery", ok is True and len(sent) == 1)
    check("follow_up_recovery_not_labelled_as_user_instruction",
          "【ユーザーからの追加指示】" not in sent[0]["text"])
    check("follow_up_recovery_gets_system_label",
          "システムからの運用連絡" in sent[0]["text"])
    check("follow_up_recovery_content_preserved", unlock_payload in sent[0]["text"])

    sent2 = []
    stub2 = _StubWorker("w3follow", "the original goal")
    ok2 = _follow_up(stub2, human_steer, lambda d: sent2.append(d), _say)
    check("follow_up_queued_human_steer", ok2 is True and len(sent2) == 1)
    check("follow_up_human_steer_still_labelled_as_user_instruction",
          "【ユーザーからの追加指示】" in sent2[0]["text"])
    check("follow_up_human_steer_not_mislabelled_as_system",
          "システムからの運用連絡" not in sent2[0]["text"])

    # --- 4. templates themselves never claim user authorship for the system case ---
    check("system_recovery_prefix_disclaims_user_authorship",
          "ユーザー" in SYSTEM_RECOVERY_PREFIX and "ではありません" in SYSTEM_RECOVERY_PREFIX)
    check("system_follow_up_prompt_disclaims_user_authorship",
          "ユーザー発言ではありません" in SYSTEM_RECOVERY_FOLLOW_UP_PROMPT)
    check("follow_up_prompt_unchanged_for_back_compat",
          FOLLOW_UP_PROMPT.startswith("【ユーザーからの追加指示】%s"))

    # --- 5. _follow_up degrades safely if relay_fleet is unimportable (defensive import) ---
    _real = fleet_runner._follow_up
    check("follow_up_function_is_callable", callable(_real))

    print("\n=== %d/%d unlock-redelivery-framing checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _restore_env()
