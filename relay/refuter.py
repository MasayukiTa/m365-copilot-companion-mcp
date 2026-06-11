"""refuter.py -- operator B: adversarial refutation of a claimed DONE (spec 4B).

A self-reported -- and even machine-checked -- DONE can still be semantically wrong in
ways no local check covers: the goal was misread, an edge case ignored, the "fix" merely
masks the bug, the test that "passes" doesn't actually exercise the requirement. The
refuter runs a SECOND, INDEPENDENT Copilot conversation as a skeptic: given the goal and
the implementer's claimed-done summary, it must find a concrete reason the goal is NOT
met, or uphold it. A genuine refutation is fed back to the implementer so it keeps
working; an upheld verdict lets the DONE stand.

Per spec 4B this doubles oracle cost, so callers keep it OFF by default and budget-capped
(use it for high-stakes / not-machine-verifiable goals). This module holds the pure
prompt/verdict logic (unit-tested) plus run_refuter(), which drives the live side page
the same way the research/analyst delegation does.
"""
from __future__ import annotations

import re

REFUTER_INSTRUCTION = (
    "あなたは厳格なレビュアーです。別のエージェントが下記のゴールに対して『完了(DONE)』と"
    "報告しました。あなたの仕事は、その完了を鵜呑みにせず、ゴールが本当には達成されていない"
    "具体的な理由を全力で探すことです（ゴールの読み違い、見落とされたエッジケース、症状だけ"
    "を隠す修正、要件を実際には検証していないテスト等）。\n"
    "判定を最後の行に必ず次の形式で書いてください:\n"
    "・本当に達成されていない具体的欠陥が一つでもあれば: REFUTED: <その欠陥を1〜2文で>\n"
    "・全力で探しても具体的欠陥が見つからなければ: UPHELD\n"
    "推測や些末な好みではなく、ゴール未達と言える具体的根拠のみを REFUTED の理由にしてください。"
)


def build_refuter_prompt(goal: str, final_response: str) -> str:
    """Compose the adversarial reviewer prompt from the goal and the implementer's
    claimed-done summary."""
    return (
        REFUTER_INSTRUCTION
        + "\n\n--- ゴール ---\n" + (goal or "").strip()
        + "\n\n--- 実装エージェントの最終報告 ---\n" + (final_response or "").strip()
        + "\n--- ここまで ---"
    )


def parse_verdict(text: str):
    """Parse a refuter reply into (kind, reason).

    kind is "REFUTED" (with a non-empty reason), "UPHELD", or "UNCLEAR". We look for an
    explicit REFUTED:/UPHELD marker (the prompt demands one on the last line); a bare
    "REFUTED" with no reason, or no marker at all, is UNCLEAR -- callers treat UNCLEAR as
    "do not block" so ambiguous output can never trap the loop forever.
    """
    if not text:
        return ("UNCLEAR", "")
    # an explicit "REFUTED: <reason>" anywhere wins
    for line in text.splitlines():
        m = re.search(r"REFUTED\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            return ("REFUTED", m.group(1).strip())
    up = text.upper()
    if "UPHELD" in up:
        return ("UPHELD", "")
    if "REFUTED" in up:               # marker present but no concrete reason given
        return ("UNCLEAR", "")
    return ("UNCLEAR", "")


def agent_base_url(conversation_url: str) -> str:
    """The bare agent URL (a fresh chat) from a conversation URL -- navigating here starts
    an INDEPENDENT conversation, which is what makes the refuter a separate skeptic rather
    than a continuation of the implementer's own chat."""
    if not conversation_url:
        return ""
    return conversation_url.split("/conversation/", 1)[0]


def run_refuter(context, conversation_url: str, goal: str, final_response: str,
                notify=None, runlog=None, run_id: str = "relay", turn: int = 0,
                timeout_s: int = 600):
    """Open an independent side-page Copilot chat and ask it to refute the claimed DONE.
    Returns (kind, reason). Never raises into the control loop -- any failure yields
    ("UNCLEAR", "") so the loop falls back to accepting the DONE. Mirrors the research/
    analyst side-page delegation."""
    from .copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver
    base = agent_base_url(conversation_url)
    if context is None or not base:
        return ("UNCLEAR", "")
    page = None
    try:
        page = context.new_page()
        page.goto(base, wait_until="domcontentloaded", timeout=45000)
        appeared = False
        for _ in range(40):
            page.wait_for_timeout(1000)
            if page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                appeared = True
                break
        if not appeared:
            return ("UNCLEAR", "")
        drv = CopilotWebDriver(page)
        drv.send(build_refuter_prompt(goal, final_response))
        ok = drv.wait_for_idle(timeout_s=timeout_s)
        if not ok:
            return ("UNCLEAR", "")
        verdict = parse_verdict(drv.read_last_response())
    except Exception:
        verdict = ("UNCLEAR", "")
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
    if runlog is not None:
        try:
            runlog(run_id, {"turn": turn, "event": "refute",
                            "verdict": verdict[0], "reason": (verdict[1] or "")[:200]})
        except Exception:
            pass
    return verdict
