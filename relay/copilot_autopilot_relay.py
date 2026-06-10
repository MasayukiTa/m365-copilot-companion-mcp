"""copilot_autopilot_relay.py -- autonomous, hands-off relay for an M365 Copilot agent.

WHAT IT IS
  A standalone controller (the "frame") that drives a Microsoft 365 Copilot agent
  (the one wired to m365-copilot-companion-mcp) toward a goal, completely on its
  own -- no human and no second AI in the loop. Given a goal it:

      send the goal + a turn protocol  ->  loop:
          wait until the agent turn finishes   (DOM-state completion detection)
          read the agent's answer
          record it to memory + the run-log     (operators D and memory_ops)
          decide deterministically:
              "DONE"  in answer -> stop
              "FAIL"  in answer -> send a fix instruction
              else              -> send "continue"
      until DONE / max_turns / kill-switch.

WHY IT DOES NOT INTERFERE WITH YOUR OTHER WORK  (the important property)
  It drives the page through the Chrome DevTools Protocol (Playwright
  connect_over_cdp). Keystrokes and clicks are dispatched into the target tab via
  CDP -- they do NOT move your OS mouse cursor and do NOT steal your keyboard
  focus. So while the relay pumps a Copilot conversation in one tab, you can keep
  typing in other apps / windows. This is the whole point versus screen-scraping.

ONE-TIME SETUP  (no re-login, no Playwright browser download -- it attaches to
                 the Edge you already use and are already signed into)
  1. Close Edge, then relaunch it with the debug port from a terminal:
         & "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222
     (Chrome works too: chrome.exe --remote-debugging-port=9222)
  2. In that Edge, open M365 Copilot, pick your MCP agent, start a NEW chat with
     it, and copy the conversation URL from the address bar.
  3. Make sure the MCP server + tunnel are up (start.ps1 + supervisor.ps1) and the
     Copilot backend IP is unlocked once (see the project README).

RUN
    .venv\\Scripts\\python.exe relay\\copilot_autopilot_relay.py ^
        --conversation-url "https://m365.cloud.microsoft/chat/agent/.../conversation/..." ^
        --goal "copilot_loop_demo に data.csv(10行) を作り、合計と平均を出す stats.py を書き、self-test を足して PASS させ、SUMMARY.txt にまとめる" ^
        --max-turns 12

NOTES
  * Selectors below were captured from the live M365 Copilot DOM. Microsoft may
    change them; they are isolated in COPILOT_SELECTORS for easy patching.
  * The frame makes NO model calls. The only intelligence is the Copilot agent
    itself (the fixed oracle). The frame is deterministic plumbing.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.gate_ops import stop_check                     # operator E: kill-switch
from tools.memory_ops import memory_load, memory_save     # cross-session history
from tools.runlog_ops import runlog_append, runlog_summarize  # operator D: audit

# --- Selectors captured from the live M365 Copilot DOM (2026-06) ------------
COPILOT_SELECTORS = {
    "composer": "#m365-chat-editor-target-element",          # contenteditable, role=textbox
    # The agent's reply lives in .fai-CopilotMessage (one per AGENT turn). This is
    # the reliable signal: its count rises only when the agent answers, and its
    # inner_text is the answer. NOTE: data-testid="chatOutput" was NOT reliable --
    # it can read back the user's own message, which broke STUCK detection.
    "assistant_msg": ".fai-CopilotMessage",
    "assistant_msg_fallback": '[data-testid="copilot-message-reply-div"]',
    # The Send button. Pressing Enter in this rich editor does NOT reliably submit
    # (the text just sits in the composer) -- clicking this button does. aria-label
    # is locale-specific; cover JP + EN.
    "send_button": 'button[aria-label="送信"], button[aria-label="Send"]',
}

PROTOCOL = (
    "あなたはこのゴールに向けて、ツールを使いながら自律的に作業します。"
    "重いゴールは一発で終わらせようとせず、自分で小さなステップに分割し、"
    "1ターンに1〜数ステップずつ着実に進めてください。"
    "各ターンの最後の行に必ず次のいずれかを書いてください: "
    "まだ続きがある場合は CONTINUE、ゴール全体が完了したら DONE、"
    "行き詰まって人手が要る場合は STUCK: と理由。"
    "まず全体を小ステップに分割し、最初のステップを実行してください。\nGoal: "
)

CONTINUE_JOB = (
    "次のステップを実行してください。ゴール全体が完了したら最後の行に DONE、"
    "まだ続きがあれば CONTINUE、行き詰まったら STUCK: 理由 と書いてください。"
)
FIX_JOB = (
    "直前の失敗の原因を分析し、ツールで修正してから続けてください。"
    "どうしても無理なら最後の行に STUCK: 理由 と書いてください。"
)
NUDGE_JOB = (
    "前のステップがまだ完了していないようです。今の状況を1行で報告し、"
    "可能なら次に進んでください。"
)


# Placeholder text Copilot shows in the answer block WHILE it is still working.
# Treat these as "not finished" so completion detection never stabilizes on them.
PROCESSING_MARKERS = ("処理中", "生成しています", "考えています", "working on it",
                      "thinking", "...")


def _is_processing(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m.lower() in t for m in PROCESSING_MARKERS) and len(t) < 40


def default_notify(title: str, body: str) -> None:
    """Best-effort Windows toast; never raises into the control loop."""
    try:
        from tools.notify_ops import notify_desktop
        notify_desktop(title, body[:240])
    except Exception:
        pass


class CopilotWebDriver:
    """Drives one M365 Copilot conversation tab over CDP. No OS input is used."""

    def __init__(self, page):
        self.page = page
        self._count_before = 0  # number of answer blocks before the current send

    def _answers(self):
        return self.page.locator(COPILOT_SELECTORS["assistant_msg"])

    def send(self, text: str) -> None:
        # CRITICAL: a newline in the Copilot composer SUBMITS the message. Collapse
        # all whitespace (incl. newlines) to single spaces so the whole job is sent
        # as ONE message with a single trailing Enter.
        one_line = " ".join(str(text).split())
        # remember how many answer blocks exist now, so wait_for_idle can detect a
        # genuinely NEW one (rather than re-reading the previous turn's answer).
        try:
            self._count_before = self._answers().count()
        except Exception:
            self._count_before = 0
        composer = self.page.locator(COPILOT_SELECTORS["composer"]).first
        composer.click()
        composer.fill("")  # contenteditable: clear any leftover text
        self.page.keyboard.type(one_line)   # via CDP -> lands in the page, not the OS
        self.page.wait_for_timeout(300)
        # SUBMIT via the Send button -- Enter is unreliable in this editor and
        # frequently leaves the text sitting in the composer unsent.
        submitted = False
        btn = self.page.locator(COPILOT_SELECTORS["send_button"]).first
        try:
            if btn.count() > 0 and btn.is_enabled():
                btn.click()
                submitted = True
        except Exception:
            submitted = False
        if not submitted:
            self.page.keyboard.press("Enter")
        # verify the composer actually cleared; if not, retry once.
        self.page.wait_for_timeout(600)
        try:
            leftover = (composer.inner_text() or "").strip()
        except Exception:
            leftover = ""
        if leftover:
            try:
                if btn.count() > 0 and btn.is_enabled():
                    btn.click()
                else:
                    self.page.keyboard.press("Enter")
            except Exception:
                self.page.keyboard.press("Enter")

    def wait_for_idle(self, timeout_s: int = 1800, dwell_s: float = 4.0,
                      appear_timeout_s: int = 180) -> bool:
        """Completion = a NEW answer block appears, then its text stops changing
        for `dwell_s`. We do NOT rely on the loading indicator: that element stays
        present (and even 'visible') in the DOM while idle, so it is useless as a
        signal. Polls the kill-switch so STOP aborts promptly."""
        deadline = time.time() + timeout_s
        # 1) wait for a brand-new answer block to appear.
        appear_deadline = time.time() + min(appear_timeout_s, timeout_s)
        appeared = False
        while time.time() < appear_deadline:
            if stop_check().startswith("STOP"):
                return False
            try:
                if self._answers().count() > self._count_before:
                    appeared = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not appeared:
            return False
        # 2) wait for the last answer's REAL text to stabilize. While the block
        # still shows a processing placeholder ("処理中です" etc.), keep waiting --
        # otherwise we would lock onto the placeholder as the final answer.
        last, stable_since = None, None
        while time.time() < deadline:
            if stop_check().startswith("STOP"):
                return False
            t = self.read_last_response()
            if _is_processing(t):
                last, stable_since = None, None
            elif t == last:
                if stable_since and (time.time() - stable_since) >= dwell_s:
                    return True
            else:
                last, stable_since = t, time.time()
            time.sleep(1.0)
        return False

    def read_last_response(self) -> str:
        loc = self._answers()
        if loc.count() == 0:
            loc = self.page.locator(COPILOT_SELECTORS["assistant_msg_fallback"])
        if loc.count() == 0:
            return ""
        try:
            txt = loc.last.inner_text() or ""
        except Exception:
            return ""
        # strip the "<agent> said:" prefix Copilot prepends, then a duplicated
        # agent-name line if present.
        if " said:" in txt:
            txt = txt.split(" said:", 1)[1]
        lines = txt.splitlines()
        while lines and not lines[0].strip():
            lines = lines[1:]
        if len(lines) >= 2 and lines[0].strip() and lines[0].strip() == lines[1].strip():
            lines = lines[1:]
        return "\n".join(lines).strip()


def run_relay(
    driver,
    goal: str,
    run_id: str = "relay",
    max_turns: int = 20,
    per_turn_timeout_s: int = 1800,
    max_no_progress: int = 3,
    max_timeouts: int = 2,
    notify=default_notify,
    sleep_s: float = 1.0,
) -> str:
    """Run the autonomous loop unattended. Returns one of:
    DONE | STUCK | MAXTURNS | ABORTED. Notifies on every terminal outcome.

    Reliability guards (so a hands-off run never spins forever or dies silently):
      * per-turn completion timeout (max_timeouts consecutive -> STUCK)
      * no-progress detection: identical answer for max_no_progress turns -> STUCK
      * agent self-reported STUCK: -> STUCK
      * hard max_turns cap -> MAXTURNS
      * kill-switch (stop_check) every turn -> ABORTED
      * send/read exceptions -> STUCK (never crash unattended)
    Every turn is written to the run-log (operator D) and cross-session memory.
    """
    prior = memory_load(f"relay.{run_id}.context", scope="relay")
    context = "" if prior.startswith("[memory_load") else f"\n(前回までの文脈: {prior})\n"

    job = PROTOCOL + goal + context
    turn = 0
    no_progress = 0
    timeouts = 0
    last_norm = None
    outcome: str | None = None
    reason = ""

    while turn < max_turns:
        if stop_check().startswith("STOP"):
            outcome, reason = "ABORTED", "kill-switch"
            break
        turn += 1
        try:
            driver.send(job)
        except Exception as e:
            outcome, reason = "STUCK", f"send failed: {type(e).__name__}: {e}"
            break

        try:
            ok = driver.wait_for_idle(timeout_s=per_turn_timeout_s)
        except Exception as e:
            outcome, reason = "STUCK", f"wait failed: {type(e).__name__}: {e}"
            break

        if not ok:
            timeouts += 1
            runlog_append(run_id, {"turn": turn, "event": "turn_timeout", "count": timeouts})
            if timeouts >= max_timeouts:
                outcome, reason = "STUCK", "turn did not finish (repeated timeout)"
                break
            job = NUDGE_JOB
            continue
        timeouts = 0

        try:
            resp = driver.read_last_response()
        except Exception as e:
            outcome, reason = "STUCK", f"read failed: {type(e).__name__}: {e}"
            break

        runlog_append(run_id, {"turn": turn, "job_excerpt": job[:160],
                               "response_excerpt": resp[:500]})
        memory_save(f"relay.{run_id}.turn{turn}", resp[:4000], scope="relay",
                    tags=["relay", run_id])
        print(f"[relay turn {turn}] {resp[:160].replace(chr(10), ' ')}")

        norm = " ".join(resp.lower().split())[:300]
        no_progress = no_progress + 1 if norm and norm == last_norm else 0
        last_norm = norm

        up = resp.upper()
        last_line = (resp.strip().splitlines() or [""])[-1].upper()

        if "STUCK" in up:
            outcome, reason = "STUCK", "agent reported STUCK"
            break
        if "DONE" in up and "FAIL" not in last_line:
            outcome = "DONE"
            break
        if no_progress >= max_no_progress:
            outcome, reason = "STUCK", f"no progress for {no_progress + 1} turns"
            break
        if "FAIL" in last_line:
            job = FIX_JOB
        else:
            job = CONTINUE_JOB
        time.sleep(sleep_s)

    if outcome is None:
        outcome, reason = "MAXTURNS", f"reached max_turns={max_turns} without DONE"

    memory_save(f"relay.{run_id}.context",
                f"last_status={outcome} turns={turn} reason={reason}",
                scope="relay", tags=["relay", run_id])

    titles = {
        "DONE":     ("✅ Relay 完了", f"ゴール達成 ({turn} ターン): {goal[:120]}"),
        "STUCK":    ("⚠ Relay 停止 (要確認)", f"{reason} / {turn} ターンで停止"),
        "MAXTURNS": ("⏹ Relay 上限到達", f"{turn} ターンで DONE に至らず"),
        "ABORTED":  ("⏹ Relay 中止", "kill-switch により停止"),
    }
    title, body = titles.get(outcome, ("Relay", outcome))
    notify(title, body)

    print("\n--- run-log (operator D) ---")
    print(runlog_summarize(run_id))
    print(f"\nrelay finished: {outcome} ({reason}) in {turn} turn(s). "
          f"History in memory scope 'relay'. Notification sent.")
    return outcome


def find_conversation_page(context, conversation_url: str):
    """Always load the target URL fresh (a bare agent URL starts a NEW chat) and
    wait for the composer to render before returning."""
    pg = context.pages[0] if context.pages else context.new_page()
    pg.goto(conversation_url, wait_until="domcontentloaded")
    for _ in range(30):
        pg.wait_for_timeout(1000)
        if pg.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            break
    return pg


def main():
    ap = argparse.ArgumentParser(description="Autonomous M365 Copilot relay (hands-off, non-interfering).")
    ap.add_argument("--cdp-url", default="http://localhost:9222",
                    help="CDP endpoint of an Edge/Chrome started with --remote-debugging-port")
    ap.add_argument("--conversation-url", required=True,
                    help="URL of the (new) Copilot agent conversation to drive")
    ap.add_argument("--goal", required=True, help="The goal to pursue autonomously")
    ap.add_argument("--run-id", default="relay", help="Identifier for run-log + memory keys")
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--per-turn-timeout", type=int, default=1800,
                    help="Max seconds to wait for a single agent turn to finish")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = find_conversation_page(context, args.conversation_url)
        page.bring_to_front()
        driver = CopilotWebDriver(page)
        run_relay(driver, args.goal, args.run_id, args.max_turns,
                  per_turn_timeout_s=args.per_turn_timeout)


if __name__ == "__main__":
    main()
