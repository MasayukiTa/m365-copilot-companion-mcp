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
import re
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
    "外部の深い調査が必要なときは、その行に `RESEARCH: <調べてほしいこと>` と書いて止まってください。"
    "こちらが深い調査(Claude)を行い、結果を渡すので、それを使って続行できます。"
    "ローカルのデータファイルを専用ツールで分析させたいときは、その行に "
    "`ANALYZE: <ファイルの絶対パス> | <分析指示>` と書いてください"
    "（ただし単純な集計は自分の run_python/read_excel の方が速く確実です）。"
    "各ターンの最後の行に必ず次のいずれかを書いてください: "
    "まだ続きがある場合は CONTINUE、深い調査を依頼する場合は RESEARCH: と内容、"
    "データ分析を依頼する場合は ANALYZE: と内容、"
    "ゴール全体が完了したら DONE、行き詰まって人手が要る場合は STUCK: と理由。"
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


def extract_research(resp: str) -> str:
    """Pull the query out of a `RESEARCH: <...>` line if the agent asked for a
    deep-dive. Returns '' if no research was requested."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*RESEARCH\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def extract_analyze(resp: str):
    """Pull (file_path, instruction) out of an `ANALYZE: <path> | <instruction>`
    line. Returns None if no analysis was requested."""
    for line in (resp or "").splitlines():
        m = re.match(r"\s*ANALYZE\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).strip():
            body = m.group(1).strip()
            if "|" in body:
                path, instr = body.split("|", 1)
                path, instr = path.strip(), instr.strip()
            else:
                path, instr = body, "添付データを分析し、要点を短くまとめてください。"
            if path:
                return path, instr
    return None


def _adjust_backoff(ok, turn_elapsed, backoff_s, base_elapsed,
                    backoff_step_s, backoff_max_s, slow_factor):
    """Pure adaptive-throttle step (spec §6). Returns (new_backoff, new_base, reason).

      * a turn that timed out         -> raise backoff hard (2 steps)
      * a turn >slow_factor x the fastest healthy turn (and >20s) -> raise 1 step
      * an otherwise healthy turn      -> decay backoff by half a step
    `base_elapsed` tracks the fastest healthy turn = the "not throttled" baseline.
    """
    if not ok:
        return min(backoff_max_s, backoff_s + backoff_step_s * 2), base_elapsed, "turn_timeout"
    if base_elapsed is None or turn_elapsed < base_elapsed:
        base_elapsed = turn_elapsed
    if base_elapsed and turn_elapsed > base_elapsed * slow_factor and turn_elapsed > 20:
        return min(backoff_max_s, backoff_s + backoff_step_s), base_elapsed, "slow_turn"
    return max(0.0, backoff_s - backoff_step_s * 0.5), base_elapsed, "healthy"


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

    def _send_button(self):
        return self.page.locator(COPILOT_SELECTORS["send_button"]).first

    def _composer_text(self) -> str:
        """Current composer text, minus zero-width junk."""
        try:
            t = self.page.locator(COPILOT_SELECTORS["composer"]).first.inner_text() or ""
        except Exception:
            t = ""
        return t.replace("​", "").replace("‌", "").strip()

    def _wait_send_armed(self, timeout_s: float = 12.0) -> bool:
        """Wait until the Send button is present AND enabled.

        Two facts this guards against, both learned the hard way:
          * The Send button only ARMS a beat after real text is typed -- clicking
            immediately after typing finds nothing and silently no-ops.
          * WHILE the agent turn is running, the Send button is REPLACED by the
            Stop (square) button, so `送信` is simply absent. Its (re)appearance is
            therefore the reliable "the turn is idle and ready" signal (spec §7:
            judge by an element that only exists after completion).
        """
        deadline = time.time() + timeout_s
        btn = self._send_button()
        while time.time() < deadline:
            try:
                if btn.count() > 0 and btn.is_enabled():
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(400)
        return False

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

        # Type -> wait for Send to ARM -> force-click -> verify composer emptied.
        # Retry a few times; if it never empties, RAISE so run_relay records a real
        # STUCK instead of pretending the turn was submitted.
        for attempt in range(3):
            composer.click()
            self.page.keyboard.press("Control+a")   # clear via keyboard, not fill("")
            self.page.keyboard.press("Delete")       # -- fill("") leaves the editor
            self.page.wait_for_timeout(150)          #    in a state where Send won't arm
            # insert_text = ONE atomic Input.insertText (paste-like). Unlike type(), it
            # sends no per-key events, so the OS Japanese IME never intercepts it and a
            # long Japanese goal lands intact -> the Send button arms reliably. type()
            # was the cause of "Send button never submitted" on long JP goals.
            self.page.keyboard.insert_text(one_line)
            if self._wait_send_armed(timeout_s=12.0):
                try:
                    self._send_button().click(force=True, timeout=4000)
                except Exception:
                    pass
            else:
                # Send never armed (rare): last-ditch Enter.
                try:
                    self.page.keyboard.press("Enter")
                except Exception:
                    pass
            # The submit + composer-clear is async and can take >1s (esp. under load /
            # right after a fresh page). POLL for the composer to empty instead of one
            # fixed 800ms check -- the short check was the real cause of the false
            # "Send button never submitted" failures (and the retry then double-typed).
            for _ in range(24):                  # up to ~6s
                self.page.wait_for_timeout(250)
                if not self._composer_text():
                    return  # composer emptied => message was submitted
        raise RuntimeError(
            "send failed: composer still holds text after 3 attempts "
            "(Send button never submitted the message)"
        )

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
    browser_context=None,
    research_model: str = "Claude",
    max_research: int = 3,
    throttle: bool = True,
    backoff_step_s: float = 20.0,
    backoff_max_s: float = 300.0,
    slow_factor: float = 2.5,
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

    Deep-dive delegation (spec §5): if `browser_context` is given and the agent
    writes a `RESEARCH: <query>` line, the relay opens the M365 Researcher agent in
    a side page, runs a Claude/Anthropic deep research, and feeds the report back
    into the implementation agent's next turn. Capped at `max_research` per run.
    """
    prior = memory_load(f"relay.{run_id}.context", scope="relay")
    context = "" if prior.startswith("[memory_load") else f"\n(前回までの文脈: {prior})\n"

    job = PROTOCOL + goal + context
    turn = 0
    no_progress = 0
    timeouts = 0
    research_count = 0
    analyze_count = 0
    backoff_s = 0.0          # adaptive throttle: extra cool-down added between turns
    base_elapsed = None      # fastest healthy turn so far -> the "not throttled" baseline
    last_norm = None
    outcome: str | None = None
    reason = ""

    while turn < max_turns:
        if stop_check().startswith("STOP"):
            outcome, reason = "ABORTED", "kill-switch"
            break
        turn += 1
        t_send = time.time()
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
        turn_elapsed = time.time() - t_send

        # Adaptive throttle (spec §6): the laptop cannot see Microsoft's fair-use
        # ceiling directly, so infer "being throttled" from the agent's own
        # responsiveness -- a turn that times out, or runs much slower than the
        # fastest healthy turn, raises a cool-down added between turns; healthy
        # turns decay it. Every change is logged so "when did it start being
        # throttled" is visible in the run-log (operator D).
        if throttle:
            prev = backoff_s
            backoff_s, base_elapsed, t_reason = _adjust_backoff(
                ok, turn_elapsed, backoff_s, base_elapsed,
                backoff_step_s, backoff_max_s, slow_factor)
            if abs(backoff_s - prev) > 0.1:
                runlog_append(run_id, {"turn": turn, "event": "throttle", "reason": t_reason,
                                       "turn_elapsed_s": round(turn_elapsed, 1),
                                       "base_s": round(base_elapsed or 0, 1),
                                       "backoff_s": round(backoff_s, 1)})

        if not ok:
            timeouts += 1
            runlog_append(run_id, {"turn": turn, "event": "turn_timeout", "count": timeouts})
            if timeouts >= max_timeouts:
                outcome, reason = "STUCK", "turn did not finish (repeated timeout)"
                break
            job = NUDGE_JOB
            time.sleep(sleep_s + backoff_s)
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

        # ---- deep-dive delegation (spec §5: researcher node) ----
        rq = extract_research(resp)
        if rq and browser_context is not None:
            research_count += 1
            if research_count > max_research:
                job = ("これ以上は調査を依頼できません（上限到達）。今ある情報で進めるか、"
                       "無理なら最後の行に STUCK: 理由 と書いてください。")
                time.sleep(sleep_s)
                continue
            notify("🔎 Relay 調査開始", rq[:80])
            runlog_append(run_id, {"turn": turn, "event": "research_start", "query": rq[:200]})
            print(f"[relay turn {turn}] -> RESEARCH delegated: {rq[:80]}")
            rres = {"ok": False, "result": "", "error": "not run"}
            rpage = None
            try:
                from .agent_profiles import RESEARCHER, ask_agent
                rpage = browser_context.new_page()
                rres = ask_agent(rpage, rq, RESEARCHER, model_name=research_model)
            except Exception as e:
                rres = {"ok": False, "result": "", "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    if rpage is not None:
                        rpage.close()
                except Exception:
                    pass
                try:
                    driver.page.bring_to_front()
                except Exception:
                    pass
            report = (rres.get("result") or "")[:3500] if rres.get("ok") else ""
            runlog_append(run_id, {"turn": turn, "event": "research_done",
                                   "ok": bool(rres.get("ok")), "len": len(report),
                                   "elapsed_s": rres.get("elapsed_s"),
                                   "error": rres.get("error", "")})
            print(f"[relay turn {turn}] <- RESEARCH done ok={rres.get('ok')} "
                  f"len={len(report)} elapsed={rres.get('elapsed_s')}s")
            if report:
                job = ("依頼された調査が完了しました。以下が結果です。これを踏まえて作業を続けてください。\n"
                       f"--- 調査結果 ---\n{report}\n--- 調査結果ここまで ---\n" + CONTINUE_JOB)
            else:
                job = (f"調査を試みましたが結果を取得できませんでした（{rres.get('error', 'timeout/empty')}）。"
                       "調査結果なしで可能な範囲で進めるか、無理なら最後の行に STUCK: 理由 と書いてください。")
            time.sleep(sleep_s + backoff_s)
            continue
        # ---- end deep-dive delegation ----

        # ---- data-analysis delegation (spec §5: analyst node) ----
        az = extract_analyze(resp)
        if az and browser_context is not None:
            apath, ainstr = az
            analyze_count += 1
            if analyze_count > max_research:
                job = ("これ以上は分析を依頼できません（上限到達）。自前ツールで分析するか、"
                       "無理なら最後の行に STUCK: 理由 と書いてください。")
                time.sleep(sleep_s)
                continue
            notify("📊 Relay 分析開始", apath[:80])
            runlog_append(run_id, {"turn": turn, "event": "analyze_start", "file": apath[:200]})
            print(f"[relay turn {turn}] -> ANALYZE delegated: {apath[:80]}")
            ares = {"ok": False, "result": "", "error": "not run"}
            apage = None
            try:
                from .agent_profiles import ANALYST, analyze
                apage = browser_context.new_page()
                ares = analyze(apage, apath, ainstr, ANALYST)
            except Exception as e:
                ares = {"ok": False, "result": "", "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    if apage is not None:
                        apage.close()
                except Exception:
                    pass
                try:
                    driver.page.bring_to_front()
                except Exception:
                    pass
            rep = (ares.get("result") or "")[:3000] if ares.get("ok") else ""
            runlog_append(run_id, {"turn": turn, "event": "analyze_done",
                                   "ok": bool(ares.get("ok")), "len": len(rep),
                                   "elapsed_s": ares.get("elapsed_s"),
                                   "error": ares.get("error", "")})
            print(f"[relay turn {turn}] <- ANALYZE done ok={ares.get('ok')} len={len(rep)}")
            if rep:
                job = ("依頼した分析が完了しました。以下が結果です。**数値は鵜呑みにせず、必ず "
                       "run_python / read_excel などの自前ツールで再計算して地上検証してから**使ってください。\n"
                       f"--- 分析結果 ---\n{rep}\n--- 分析結果ここまで ---\n" + CONTINUE_JOB)
            else:
                job = (f"分析を試みましたが結果を取得できませんでした（{ares.get('error', 'timeout/empty')}）。"
                       "自前ツールで分析するか、無理なら最後の行に STUCK: 理由 と書いてください。")
            time.sleep(sleep_s + backoff_s)
            continue
        # ---- end data-analysis delegation ----

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
        time.sleep(sleep_s + backoff_s)

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
    ap.add_argument("--no-research", action="store_true",
                    help="Disable RESEARCH: delegation to the Researcher agent")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = find_conversation_page(context, args.conversation_url)
        page.bring_to_front()
        driver = CopilotWebDriver(page)
        run_relay(driver, args.goal, args.run_id, args.max_turns,
                  per_turn_timeout_s=args.per_turn_timeout,
                  browser_context=None if args.no_research else context)


if __name__ == "__main__":
    main()
