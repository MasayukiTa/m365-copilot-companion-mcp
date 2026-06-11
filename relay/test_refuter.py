"""Tests for relay/refuter.py (operator B) -- pure verdict logic + the run_relay loop
integration, with the live side-page call stubbed out.

Proves: REFUTED:/UPHELD/UNCLEAR parsing, agent-base-url derivation, prompt assembly,
and that run_relay feeds a refutation back (keeps working) then accepts an upheld DONE,
and that the refute budget is honoured.

Run:  .venv\\Scripts\\python.exe relay\\test_refuter.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import relay.refuter as refuter
from relay.copilot_autopilot_relay import REFUTE_FIX_JOB, run_relay

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class MockDriver:
    def __init__(self, responses):
        self.responses = list(responses)
        self.i = -1
        self.sent = []

    def send(self, text):
        self.i += 1
        self.sent.append(text)

    def wait_for_idle(self, timeout_s=0):
        return True

    def read_last_response(self):
        return self.responses[self.i] if self.i < len(self.responses) else "CONTINUE"


def run(responses, verdicts, max_refute=2, max_turns=8, review_lenses=None):
    """Drive run_relay with the refuter stubbed to return scripted verdicts in order."""
    seq = list(verdicts)
    calls = {"n": 0}

    def fake_run_refuter(context, conv_url, goal, final_response, **kw):
        v = seq[calls["n"]] if calls["n"] < len(seq) else ("UPHELD", "")
        calls["n"] += 1
        return v

    orig = refuter.run_refuter
    refuter.run_refuter = fake_run_refuter
    try:
        drv = MockDriver(responses)
        outcome = run_relay(drv, goal="g", run_id="test_refuter", max_turns=max_turns,
                            notify=lambda *a: None, sleep_s=0, browser_context=object(),
                            refuter=True, max_refute=max_refute, review_lenses=review_lenses)
        return outcome, drv, calls["n"]
    finally:
        refuter.run_refuter = orig


def main():
    # --- pure functions ---
    check("parse_refuted", refuter.parse_verdict("text\nREFUTED: 例外処理が抜けている")
          == ("REFUTED", "例外処理が抜けている"))
    check("parse_upheld", refuter.parse_verdict("looks fine\nUPHELD")[0] == "UPHELD")
    check("parse_bare_refuted_unclear", refuter.parse_verdict("REFUTED")[0] == "UNCLEAR")
    check("parse_empty_unclear", refuter.parse_verdict("")[0] == "UNCLEAR")
    check("base_url", refuter.agent_base_url(
        "https://m365.cloud.microsoft/chat/agent/T_x.y/conversation/abc")
        == "https://m365.cloud.microsoft/chat/agent/T_x.y")
    p = refuter.build_refuter_prompt("ゴールX", "報告Y")
    check("prompt_has_goal_and_report", "ゴールX" in p and "報告Y" in p and "REFUTED" in p)

    # --- run_relay integration ---
    # 1. upheld on first review -> DONE accepted (1 refuter call)
    outcome, drv, n = run(["all set DONE"], [("UPHELD", "")])
    check("upheld_done", outcome == "DONE" and n == 1)

    # 2. refuted then upheld -> the refutation is fed back, then DONE
    outcome, drv, n = run(["claim DONE", "fixed it DONE"], [("REFUTED", "境界値が未対応"), ("UPHELD", "")])
    fed_back = any("境界値が未対応" in s for s in drv.sent)
    check("refuted_then_done", outcome == "DONE" and fed_back and n == 2)
    check("refute_reinjects_fix_job", any(REFUTE_FIX_JOB.split("%s")[0][:20] in s for s in drv.sent))

    # 3. budget cap: only max_refute reviews run, then DONE stands even if it would refute
    outcome, drv, n = run(["DONE", "DONE", "DONE", "DONE"],
                          [("REFUTED", "a"), ("REFUTED", "b"), ("REFUTED", "c")],
                          max_refute=1)
    check("refute_budget_capped", outcome == "DONE" and n == 1)

    # --- review panel (perspective-diverse, majority vote) ---
    check("aggregate_majority_refute", refuter.aggregate_panel(
        [("correctness", "REFUTED", "a"), ("edge", "REFUTED", "b"), ("security", "UPHELD", "")])
        == ("REFUTED", "[correctness] a / [edge] b"))
    check("aggregate_minority_upheld", refuter.aggregate_panel(
        [("correctness", "REFUTED", "a"), ("edge", "UPHELD", ""), ("security", "UPHELD", "")])[0]
        == "UPHELD")
    check("aggregate_empty_unclear", refuter.aggregate_panel([])[0] == "UNCLEAR")
    check("lens_in_prompt", "境界値" in refuter.build_refuter_prompt("g", "f", lens="edge"))

    # panel integration: round1 = 2/3 refuted -> reinject; round2 = all upheld -> DONE
    lenses = list(refuter.PANEL_LENSES)
    outcome, drv, n = run(
        ["claim DONE", "fixed DONE"],
        [("REFUTED", "境界値"), ("REFUTED", "型不一致"), ("UPHELD", ""),
         ("UPHELD", ""), ("UPHELD", ""), ("UPHELD", "")],
        review_lenses=lenses)
    panel_reinjected = any("境界値" in s and "型不一致" in s for s in drv.sent)
    check("panel_majority_reinjects", outcome == "DONE" and panel_reinjected and n == 6)

    print("\n=== %d/%d refuter checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
