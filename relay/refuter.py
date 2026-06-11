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
    "を隠す修正、要件を実際には検証していないテスト等）。必要ならツール"
    "(read_file / grep / run_python など)で実物のファイルやテストを今すぐ確認してください。\n"
    "次の観点を順に当ててください: ①正しさ(ゴールの全要件を満たすか) "
    "②境界値・エラー処理・例外ケース ③セキュリティ・安全性。\n"
    "重要: 『確認します』『調べます』等の前置きだけで終わらせないこと。**このターン内で確認まで"
    "済ませ、必ず回答の最後の行に判定を書く**こと。形式は次のどちらか:\n"
    "・本当に達成されていない具体的欠陥が一つでもあれば: REFUTED: <その欠陥を1〜2文で>\n"
    "・全力で探しても具体的欠陥が見つからなければ: UPHELD\n"
    "推測や些末な好みではなく、ゴール未達と言える具体的根拠のみを REFUTED の理由にしてください。"
)

# Nudge sent when the reviewer answered without a clear verdict (e.g. it only said
# "I'll check the files"). Asks it to finish and emit the marker now.
REFUTER_NUDGE = (
    "確認は済みましたか。前置きは不要です。今の判定を、最後の行に "
    "REFUTED: <理由> もしくは UPHELD の形式で必ず1行で書いてください。"
)


# Review panel (operator B, perspective-diverse): N INDEPENDENT reviewers, each with a
# distinct lens, aggregated by majority. Catches failure modes a single redundant pass
# misses -- a quality mechanism Claude Code does not have built in.
LENS_PROMPTS = {
    "correctness": "このレビューでは特に『正しさ』に集中: ゴールの全要件を実際に満たしているか。",
    "edge": "このレビューでは特に『境界値・エラー処理・例外・想定外入力』に集中。",
    "security": "このレビューでは特に『セキュリティ・安全性』に集中: インジェクション、"
                "権限、破壊的操作、機微情報の漏えい等。",
}
PANEL_LENSES = ("correctness", "edge", "security")


def build_refuter_prompt(goal: str, final_response: str, lens: str = "") -> str:
    """Compose the adversarial reviewer prompt from the goal and the implementer's
    claimed-done summary. `lens` (one of LENS_PROMPTS) focuses a panel reviewer."""
    base = REFUTER_INSTRUCTION
    if lens and lens in LENS_PROMPTS:
        base = LENS_PROMPTS[lens] + "\n" + base
    return (
        base
        + "\n\n--- ゴール ---\n" + (goal or "").strip()
        + "\n\n--- 実装エージェントの最終報告 ---\n" + (final_response or "").strip()
        + "\n--- ここまで ---"
    )


def aggregate_panel(results, min_refute=None):
    """Aggregate a panel of (lens, kind, reason) verdicts into one (kind, reason).

    REFUTED only when at least `min_refute` reviewers refute (default: strict majority),
    so a lone over-eager reviewer can't block, but a real defect that several lenses see
    does. The combined reason names which lenses objected. Anything short of the threshold
    is UPHELD (we never trap the loop on a minority/ambiguous objection).
    """
    n = len(results)
    if n == 0:
        return ("UNCLEAR", "")
    refuted = [(l, r) for (l, k, r) in results if k == "REFUTED"]
    if min_refute is None:
        min_refute = (n // 2) + 1
    if len(refuted) >= min_refute:
        reason = " / ".join("[%s] %s" % (l, r) for (l, r) in refuted)
        return ("REFUTED", reason)
    return ("UPHELD", "")


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
                timeout_s: int = 600, max_nudges: int = 2, lens: str = ""):
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
        drv.send(build_refuter_prompt(goal, final_response, lens=lens))
        ok = drv.wait_for_idle(timeout_s=timeout_s)
        verdict = parse_verdict(drv.read_last_response()) if ok else ("UNCLEAR", "")
        # the reviewer often answers a preamble first ("I'll check the files") -- nudge it
        # to actually emit the verdict, like the implementer needs a CONTINUE.
        nudges = 0
        while verdict[0] == "UNCLEAR" and nudges < max_nudges:
            nudges += 1
            drv.send(REFUTER_NUDGE)
            if not drv.wait_for_idle(timeout_s=timeout_s):
                break
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


class RefuterSession:
    """Non-blocking refuter for the FLEET (single-thread round-robin): a blocking
    side-page wait would freeze every other worker for minutes. start() opens the side
    chat and sends the refuter prompt (a brief one-off, like opening any tab); poll()
    then checks for the verdict without blocking -- returning None while the reviewer is
    still thinking, or (kind, reason) when its answer stabilises. Mirrors the worker's own
    send/wait state machine. Never raises; any failure yields ("UNCLEAR", "")."""

    def __init__(self, context, base_url, goal, final_response,
                 dwell_s=4.0, timeout_s=600, max_nudges=2, lens=""):
        self.context = context
        self.base_url = base_url
        self.goal = goal
        self.final = final_response
        self.lens = lens               # panel reviewer focus (correctness/edge/security)
        self.dwell_s = dwell_s
        self.timeout_s = timeout_s
        self.max_nudges = max_nudges
        self.page = None
        self.drv = None
        self._count_before = 0
        self._t_send = None
        self._last = None
        self._stable_since = None
        self._nudges_used = 0
        self._done = None          # verdict tuple once finished

    def start(self):
        import time
        from .copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver
        try:
            if self.context is None or not self.base_url:
                self._finish(("UNCLEAR", ""))
                return self
            self.page = self.context.new_page()
            self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
            appeared = False
            for _ in range(40):
                self.page.wait_for_timeout(1000)
                if self.page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                    appeared = True
                    break
            if not appeared:
                self._finish(("UNCLEAR", ""))
                return self
            self.drv = CopilotWebDriver(self.page)
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(build_refuter_prompt(self.goal, self.final, lens=self.lens))
            self._t_send = time.time()
        except Exception:
            self._finish(("UNCLEAR", ""))
        return self

    def poll(self):
        """None while the reviewer is still answering; else (kind, reason)."""
        import time
        from .copilot_autopilot_relay import _is_processing
        if self._done is not None:
            return self._done
        if self.drv is None:
            return self._done
        try:
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                self._finish(("UNCLEAR", ""))
                return self._done
            if self.drv._answers().count() <= self._count_before:
                return None
            t = self.drv.read_last_response()
            if _is_processing(t):
                self._last, self._stable_since = None, None
                return None
            if t == self._last:
                if self._stable_since and (time.time() - self._stable_since) >= self.dwell_s:
                    verdict = parse_verdict(t)
                    # preamble-only answer ("I'll check...") -> nudge for the verdict
                    if verdict[0] == "UNCLEAR" and self._nudges_used < self.max_nudges:
                        self._nudge()
                        return None
                    self._finish(verdict)
                    return self._done
                return None
            self._last, self._stable_since = t, time.time()
            return None
        except Exception:
            self._finish(("UNCLEAR", ""))
            return self._done

    def _nudge(self):
        import time
        self._nudges_used += 1
        try:
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(REFUTER_NUDGE)
            self._t_send = time.time()
            self._last, self._stable_since = None, None
        except Exception:
            self._finish(("UNCLEAR", ""))

    def _finish(self, verdict):
        self._done = verdict
        self.close()

    def close(self):
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
