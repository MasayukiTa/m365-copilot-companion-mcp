"""Agent profiles -- drive specialised M365 Copilot agents from the frame.

Spec §5. The relay normally drives one "plain" agent (file ops). For a deep-dive
step it can instead drive the **Researcher** agent ("リサーチ ツール") with its
model switched to **Anthropic / Claude**, run a deep-research query, wait out the
long streaming turn by DOM-state (NOT a sentinel), and bring the report back.

All of this is the *frame* (deterministic plumbing) driving Copilot's web UI over
CDP -- no second AI in the loop. The only intelligence is Copilot itself.

Selectors / URLs captured from the live M365 Copilot DOM (2026-06):
  * Researcher agent URL ends in `.dr_work`.
  * Model picker button:  button[data-testid="researcher-model-picker-button"]
    (its text shows the current model: 自動 / GPT / Claude).
  * Model options are role="menuitemradio" with text like
    "Claude Anthropic の詳細な推論".
  * Composer is the same #m365-chat-editor-target-element, so send() is reused.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv

from .copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver
from tools.gate_ops import stop_check

load_dotenv()

# Agent URLs embed tenant/agent GUIDs, so they are NOT hardcoded here (this file is
# public). Set them in .env (gitignored):
#   MCP_RESEARCHER_AGENT_URL=https://m365.cloud.microsoft/chat/agent/P_....dr_work
#   MCP_ANALYST_AGENT_URL=https://m365.cloud.microsoft/chat/agent/P_....diceberry
RESEARCHER_URL = os.environ.get("MCP_RESEARCHER_AGENT_URL", "")
ANALYST_URL = os.environ.get("MCP_ANALYST_AGENT_URL", "")


@dataclass
class AgentProfile:
    name: str
    url: str
    # CSS for the model dropdown button (None = agent has no model switcher).
    model_picker: str | None = None
    # Deep-research turns stream for minutes; tune completion detection long and
    # patient so an intermediate pause is never mistaken for completion (spec §5).
    end_timeout_s: int = 1800      # 30 min hard cap on one research turn
    dwell_s: float = 12.0          # text must be unchanged this long to count done
    appear_timeout_s: int = 300    # wait up to 5 min for the answer block to appear


PLAIN = AgentProfile(name="plain", url="", model_picker=None,
                     end_timeout_s=1800, dwell_s=4.0, appear_timeout_s=180)

RESEARCHER = AgentProfile(
    name="researcher",
    url=RESEARCHER_URL,   # from MCP_RESEARCHER_AGENT_URL (.env) -- not committed
    model_picker='button[data-testid="researcher-model-picker-button"]',
    end_timeout_s=1800, dwell_s=12.0, appear_timeout_s=300,
)

# Analyst ("アナリスト") analyses an UPLOADED data file. Unlike the Researcher it has
# NO model picker (cannot be switched to Claude) and runs on its default model.
# NOTE on value: the implementation agent already has read_excel / run_python /
# summarize_table locally, so prefer doing analysis with those. The Analyst is only
# worth delegating to for its built-in analysis/visualisation UI on a file. Per spec
# §5 its numeric claims must be ground-verified with the local tools (operator ③).
ANALYST = AgentProfile(
    name="analyst",
    url=ANALYST_URL,      # from MCP_ANALYST_AGENT_URL (.env) -- not committed
    model_picker=None,
    end_timeout_s=900, dwell_s=8.0, appear_timeout_s=180,
)

PROFILES = {p.name: p for p in (PLAIN, RESEARCHER, ANALYST)}


def open_agent(page, profile: AgentProfile) -> bool:
    """Navigate to the agent's bare URL (= a fresh chat) and wait for the composer."""
    if not profile.url:
        raise RuntimeError(
            f"{profile.name} agent URL is empty -- set MCP_RESEARCHER_AGENT_URL in .env"
        )
    page.goto(profile.url, wait_until="domcontentloaded")
    for _ in range(40):
        page.wait_for_timeout(1000)
        if page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
            return True
    return False


def current_model(page, profile: AgentProfile) -> str:
    if not profile.model_picker:
        return ""
    try:
        return (page.locator(profile.model_picker).first.inner_text() or "").strip()
    except Exception:
        return ""


def set_model(page, profile: AgentProfile, model_name: str = "Claude") -> bool:
    """Switch the agent's model (e.g. to 'Claude' = Anthropic deep research).
    Returns True once the picker button reflects the requested model.

    Patient + retrying: on a freshly opened page the picker button and its menu
    render a beat AFTER the composer, so we wait for each to appear before clicking
    (clicking too early was the silent failure in the relay's side-page path)."""
    if not profile.model_picker:
        return False
    btn = page.locator(profile.model_picker).first
    # 1) wait for the picker button itself to render (up to ~15s).
    for _ in range(30):
        try:
            if btn.count() > 0 and btn.is_visible():
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if model_name.lower() in current_model(page, profile).lower():
        return True  # already selected
    opt = page.locator('[role="menuitemradio"]').filter(has_text=model_name).first
    for _attempt in range(3):
        try:
            btn.click()
        except Exception:
            page.wait_for_timeout(500)
            continue
        # 2) wait for the menu option to render, then click it.
        clicked = False
        for _ in range(12):
            try:
                if opt.count() > 0 and opt.is_visible():
                    opt.click()
                    clicked = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(400)
        page.wait_for_timeout(800)
        if model_name.lower() in current_model(page, profile).lower():
            return True
        # not switched yet -> close any open menu and retry
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


# The Researcher does NOT run on the first message: it first replies with a SCOPING
# step (clarifying questions + an explicit "go ahead" escape hatch) and only runs the
# multi-minute deep research AFTER you approve. So a one-shot send-and-wait stops at
# the clarification, not the report. ask_agent() handles the two stages.
CLARIFY_MARKERS = ("go ahead", "to make sure", "make sure i cover",
                   "問題ないですか", "でよいですか", "含めますか", "限定しますか")
# Elements that appear ONLY once the deep-research report is finished (spec §7).
DONE_MARKERS = ("ステップで完了", "推論が", "に変換:", "PowerPoint", "インフォグラフィック")
DEFAULT_APPROVAL = ("go ahead でお願いします。設定はお任せ（best judgment）で、"
                    "調査を開始して最終レポートまで進めてください。")


def _looks_like_clarification(text: str) -> bool:
    t = (text or "").lower()
    has_clarify = any(m.lower() in t for m in CLARIFY_MARKERS)
    has_done = any(m.lower() in t for m in DONE_MARKERS)
    # short-ish, asks/offers go-ahead, and is NOT already a finished report
    return has_clarify and not has_done and len(text) < 1500


def _wait_research_done(drv: CopilotWebDriver, profile: AgentProfile) -> bool:
    """Wait out the long deep-research turn. Completion = a NEW answer block, then
    its text is non-placeholder AND (carries a completion marker OR has been stable
    for the dwell). Polls the kill-switch. Tolerant of multi-minute streaming with
    intermediate pauses (uses a long dwell, so a pause is not mistaken for done)."""
    deadline = time.time() + profile.end_timeout_s
    appear_deadline = time.time() + min(profile.appear_timeout_s, profile.end_timeout_s)
    while time.time() < appear_deadline:
        if stop_check().startswith("STOP"):
            return False
        try:
            if drv._answers().count() > drv._count_before:
                break
        except Exception:
            pass
        time.sleep(1.0)
    last, stable_since = None, None
    from .copilot_autopilot_relay import _is_processing
    while time.time() < deadline:
        if stop_check().startswith("STOP"):
            return False
        t = drv.read_last_response()
        if _is_processing(t):
            last, stable_since = None, None
        elif t == last:
            has_marker = any(m in t for m in DONE_MARKERS)
            # A short, stable status line ("リサーチ ツール web 上の...収集") is an INTERMEDIATE
            # progress update, NOT the report -- the Researcher stalls on one for 30-60s while it
            # works in the background (observed: an identical status held a full 60s). Only treat
            # a stable block as DONE if it actually looks like the finished report: it carries a
            # completion marker OR it is substantial (the real report streams to thousands of
            # chars; intermediates stay <200). This kills the premature-capture-of-garbage bug
            # where a status line was returned and fed back to the implementer as the "result".
            substantial = has_marker or len(t) >= 1000
            # NOTE: the completion marker ("推論が N ステップで完了しました") appears at the START
            # of the report header and THEN the body streams for tens of seconds -- so a marker is
            # NOT a "done" signal and must NOT shorten the dwell (that truncated the report at ~570
            # of ~3300 chars when a brief streaming pause hit right after the header). Require the
            # FULL dwell of stability regardless: only a block that has stopped growing for
            # dwell*2 is the finished report.
            if stable_since and substantial and (time.time() - stable_since) >= profile.dwell_s * 2:
                return True
        else:
            last, stable_since = t, time.time()
        time.sleep(2.0)
    return False


def ask_agent(page, query: str, profile: AgentProfile = RESEARCHER,
              model_name: str | None = "Claude", approval: str = DEFAULT_APPROVAL,
              run_id: str = "research") -> dict:
    """Full deep-dive step: open the agent (fresh chat), switch model (Anthropic /
    Claude), send the query, AUTO-APPROVE the scoping step, then wait out the long
    deep-research turn by DOM state and return the report.

    Returns {ok, model, result, clarification, elapsed_s}. `ok` is False on timeout /
    kill-switch / setup failure. The caller (relay / operator ③) should ground-verify
    any numeric claims with the local tools rather than trusting the prose.
    """
    t0 = time.time()
    drv = CopilotWebDriver(page)
    if not open_agent(page, profile):
        return {"ok": False, "model": "", "result": "", "clarification": "",
                "elapsed_s": 0, "error": "composer never rendered"}
    model_set = ""
    if model_name and profile.model_picker:
        if not set_model(page, profile, model_name):
            return {"ok": False, "model": current_model(page, profile), "result": "",
                    "clarification": "", "elapsed_s": 0,
                    "error": f"could not switch model to {model_name}"}
        model_set = current_model(page, profile)
    if stop_check().startswith("STOP"):
        return {"ok": False, "model": model_set, "result": "", "clarification": "",
                "elapsed_s": 0, "error": "aborted by kill-switch before send"}

    # Stage 1: send the query, wait for the (short) scoping reply.
    drv.send(query)
    if not drv.wait_for_idle(timeout_s=600, dwell_s=4.0,
                             appear_timeout_s=profile.appear_timeout_s):
        return {"ok": False, "model": model_set, "result": "", "clarification": "",
                "elapsed_s": round(time.time() - t0, 1),
                "error": "no scoping reply"}
    first = drv.read_last_response()

    # Stage 2: decide what `first` is and get to the real report.
    clarification = ""
    if _looks_like_clarification(first) and approval:
        # a scoping step -> approve, then wait out the real research.
        clarification = first
        drv.send(approval)
        ok = _wait_research_done(drv, profile)
    elif any(m in first for m in DONE_MARKERS) or len(first) >= 1000:
        # already the finished report (rare: a query that needed no scoping).
        ok = True
    else:
        # `first` is an INTERMEDIATE status line ("リサーチ ツール 処理中です" / "...初期情報を収集"):
        # NOT a clarification and NOT the report. The Researcher streams progress into the SAME
        # block for minutes before the final report lands. Wait it out instead of returning the
        # stub -- THIS was the garbage-research bug (a status line fed back as the result, so the
        # implementer "coded from garbage"). _wait_research_done now ignores short stalled status
        # lines and only returns on a marker/substantial block.
        ok = _wait_research_done(drv, profile)

    return {
        "ok": ok,
        "model": model_set,
        "result": drv.read_last_response() if ok else "",
        "clarification": clarification,
        "elapsed_s": round(time.time() - t0, 1),
    }


class ResearchSession:
    """NON-BLOCKING deep-research for the FLEET. A blocking ask_agent() would freeze every other
    worker for the minutes the Researcher takes (it streams status lines for a long time before
    the report lands). start() opens the side chat, switches to Claude and sends the query; poll()
    then checks for the finished report WITHOUT blocking -- returning None while the Researcher is
    still streaming status/report, or the report string ('' on failure/timeout) once it stabilises.
    Mirrors refuter.RefuterSession's send/wait state machine. Never raises."""

    def __init__(self, context, query, model_name="Claude", approval=DEFAULT_APPROVAL,
                 dwell_s=4.0, timeout_s=600, tx_dir=None, parent_key="", parent_turn=0, sub_index=1):
        self.context = context
        self.query = query
        self.model_name = model_name
        self.approval = approval
        self.dwell_s = dwell_s
        self.timeout_s = timeout_s
        self.page = None
        self.drv = None
        self._count_before = 0
        self._t_send = None
        self._last = None
        self._stable_since = None
        self._approved = False
        self._pending_open = True  # side-page not opened yet -- deferred until there's free RAM
        self._done = None          # report string once finished ('' on failure)
        # Sub-conversation capture: where to persist this deep-dive (query + full report) so it is
        # linked to the spawning worker and visible afterwards instead of vanishing on close().
        self.tx_dir = tx_dir
        self.parent_key = parent_key
        self.parent_turn = parent_turn
        self.sub_index = sub_index
        self._report_full = ""     # untruncated report (the main convo only gets the [:3500] head)

    def start(self):
        # Defer the side-page open until poll() sees enough free RAM (ram_room_for_tab). On a
        # low-RAM box this stops a single task's ultra pipeline (main + research + refuter tabs)
        # from opening a tab it can't afford -> the overload that wedged the sweep before.
        self._pending_open = True
        self._t_send = time.time()
        return self

    def _do_open(self):
        from .copilot_autopilot_relay import CopilotWebDriver
        try:
            if self.context is None:
                self._finish(""); return
            self.page = self.context.new_page()
            if not open_agent(self.page, RESEARCHER):
                self._finish(""); return
            if self.model_name and RESEARCHER.model_picker:
                set_model(self.page, RESEARCHER, self.model_name)
            self.drv = CopilotWebDriver(self.page)
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(self.query)
            self._pending_open = False
            self._t_send = time.time()   # reset the clock to when the query actually went out
        except Exception:
            self._finish("")

    def poll(self):
        """None while the Researcher is still working; else the report string ('' on failure)."""
        from .copilot_autopilot_relay import _is_processing
        if self._done is not None:
            return self._done
        # RAM-gated lazy open: hold off opening the side-page until there's room (bounded by the
        # timeout). Returns quickly each sweep, so the fleet stays responsive while it waits.
        if self._pending_open:
            from .relay_fleet import ram_room_for_tab
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                self._finish(""); return self._done
            if not ram_room_for_tab():
                return None
            self._do_open()
            return None
        if self.drv is None:
            return self._done
        try:
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                self._finish(""); return self._done
            if self.drv._answers().count() <= self._count_before:
                return None
            t = self.drv.read_last_response()
            if _is_processing(t):
                self._last, self._stable_since = None, None
                return None
            # one-time scoping/clarification approval (the Researcher may ask before it researches)
            if not self._approved and _looks_like_clarification(t) and self.approval:
                self._approved = True
                self.drv.send(self.approval)
                self._last, self._stable_since = None, None
                return None
            # a stable, SUBSTANTIAL block (completion marker OR >=1000 chars) is the finished
            # report; a short stalled status line is not (same gate as _wait_research_done). The
            # marker appears at the report header then the body streams, so require the full dwell.
            substantial = any(m in t for m in DONE_MARKERS) or len(t) >= 1000
            if t == self._last:
                if self._stable_since and substantial and (
                        time.time() - self._stable_since) >= self.dwell_s * 2:
                    self._report_full = t          # keep the WHOLE report for the sub-transcript
                    self._finish(t[:3500]); return self._done
                return None
            self._last, self._stable_since = t, time.time()
            return None
        except Exception:
            self._finish(""); return self._done

    def _finish(self, report):
        self._done = report or ""
        self._persist()
        self.close()

    def _persist(self):
        """Save the research sub-conversation (query + full report) to a transcript file LINKED to
        the spawning worker, so the cockpit/chat can show what the deep-dive actually did instead
        of it vanishing when the side page closes. The key prefix `<parent_key>__sub_research_`
        lets a reader glob the children of a parent conversation. Best-effort; never raises."""
        if not self.tx_dir or not self.parent_key:
            return
        try:
            from .relay_fleet import _Transcript
            subkey = "%s__sub_research_t%s_%s" % (self.parent_key, self.parent_turn, self.sub_index)
            tx = _Transcript(self.tx_dir, subkey, "research", self.query)
            tx._append({"meta_link": True, "parent_key": self.parent_key,
                        "parent_turn": self.parent_turn, "kind": "research", "ts": time.time()})
            tx.user(1, self.query)
            tx.assistant(1, self._report_full or self._done or "(no report captured)")
        except Exception:
            pass

    def close(self):
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
        self.drv = None


def upload_file(page, file_path: str, timeout_s: float = 30.0) -> bool:
    """Attach a local data file to the composer via the hidden <input type=file>
    (bypasses the OS file dialog). Returns True once set_input_files succeeds."""
    inp = page.locator('input[type="file"][accept*="csv"]').first
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if inp.count() > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    try:
        inp.set_input_files(file_path)
    except Exception:
        try:
            page.locator('input[type="file"]').first.set_input_files(file_path)
        except Exception:
            return False
    # give the attachment chip a moment to register before we send the instruction
    page.wait_for_timeout(2500)
    return True


def analyze(page, file_path: str, instruction: str, profile: AgentProfile = ANALYST,
            run_id: str = "analyze") -> dict:
    """Delegate a data-analysis step to the Analyst agent: open it, upload the local
    data file, send the instruction, wait out the turn, return the analysis.

    Returns {ok, result, elapsed_s}. Per spec §5 the caller MUST ground-verify any
    numeric claims with the local tools (run_python etc.) -- the Analyst's prose is
    not the source of truth.
    """
    t0 = time.time()
    if not profile.url:
        return {"ok": False, "result": "", "elapsed_s": 0,
                "error": "MCP_ANALYST_AGENT_URL not set in .env"}
    if not os.path.isfile(file_path):
        return {"ok": False, "result": "", "elapsed_s": 0,
                "error": f"file not found: {file_path}"}
    drv = CopilotWebDriver(page)
    if not open_agent(page, profile):
        return {"ok": False, "result": "", "elapsed_s": 0,
                "error": "composer never rendered"}
    if not upload_file(page, file_path):
        return {"ok": False, "result": "", "elapsed_s": round(time.time() - t0, 1),
                "error": "file upload failed"}
    if stop_check().startswith("STOP"):
        return {"ok": False, "result": "", "elapsed_s": 0,
                "error": "aborted by kill-switch before send"}
    drv.send(instruction)
    ok = drv.wait_for_idle(timeout_s=profile.end_timeout_s, dwell_s=profile.dwell_s,
                           appear_timeout_s=profile.appear_timeout_s)
    return {
        "ok": ok,
        "result": drv.read_last_response() if ok else "",
        "elapsed_s": round(time.time() - t0, 1),
    }
