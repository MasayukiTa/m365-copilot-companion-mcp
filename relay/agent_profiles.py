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
    Returns True once the picker button reflects the requested model."""
    if not profile.model_picker:
        return False
    btn = page.locator(profile.model_picker).first
    if model_name.lower() in current_model(page, profile).lower():
        return True  # already selected
    try:
        btn.click()
    except Exception:
        return False
    page.wait_for_timeout(600)
    # The options are menuitemradio rows; pick the one mentioning the model name.
    opt = page.locator('[role="menuitemradio"]').filter(has_text=model_name).first
    try:
        opt.click()
    except Exception:
        # close the menu and report failure rather than leaving it half-open
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    page.wait_for_timeout(800)
    return model_name.lower() in current_model(page, profile).lower()


def ask_agent(page, query: str, profile: AgentProfile = RESEARCHER,
              model_name: str | None = "Claude", run_id: str = "research") -> dict:
    """Full deep-dive step: open the agent (fresh chat), optionally switch model,
    send the query, wait out the long streaming turn by DOM state, return the report.

    Returns {ok, model, result, elapsed_s}. `ok` is False on timeout / kill-switch /
    setup failure -- the caller (the relay / operator ③) should ground-verify any
    numeric claims with the local tools rather than trusting the prose.
    """
    t0 = time.time()
    drv = CopilotWebDriver(page)
    if not open_agent(page, profile):
        return {"ok": False, "model": "", "result": "", "elapsed_s": 0,
                "error": "composer never rendered"}
    model_set = ""
    if model_name and profile.model_picker:
        ok_model = set_model(page, profile, model_name)
        model_set = current_model(page, profile)
        if not ok_model:
            return {"ok": False, "model": model_set, "result": "", "elapsed_s": 0,
                    "error": f"could not switch model to {model_name}"}
    if stop_check().startswith("STOP"):
        return {"ok": False, "model": model_set, "result": "", "elapsed_s": 0,
                "error": "aborted by kill-switch before send"}
    drv.send(query)
    ok = drv.wait_for_idle(timeout_s=profile.end_timeout_s,
                           dwell_s=profile.dwell_s,
                           appear_timeout_s=profile.appear_timeout_s)
    return {
        "ok": ok,
        "model": model_set,
        "result": drv.read_last_response() if ok else "",
        "elapsed_s": round(time.time() - t0, 1),
    }
