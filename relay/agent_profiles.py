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

# The Researcher agent URL embeds tenant/agent GUIDs, so it is NOT hardcoded here
# (this file is public). Set it in .env (gitignored):
#   MCP_RESEARCHER_AGENT_URL=https://m365.cloud.microsoft/chat/agent/P_....dr_work
RESEARCHER_URL = os.environ.get("MCP_RESEARCHER_AGENT_URL", "")


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

PROFILES = {p.name: p for p in (PLAIN, RESEARCHER)}


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
            if stable_since and (time.time() - stable_since) >= (
                    profile.dwell_s if has_marker else profile.dwell_s * 2):
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

    # Stage 2: if it's a scoping step, approve it and wait out the real research.
    clarification = ""
    if _looks_like_clarification(first) and approval:
        clarification = first
        drv.send(approval)
        ok = _wait_research_done(drv, profile)
    else:
        # already a full answer (e.g. a trivial query that did not need scoping)
        ok = True

    return {
        "ok": ok,
        "model": model_set,
        "result": drv.read_last_response() if ok else "",
        "clarification": clarification,
        "elapsed_s": round(time.time() - t0, 1),
    }
