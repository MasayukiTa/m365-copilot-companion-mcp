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
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv

from .copilot_autopilot_relay import COPILOT_SELECTORS, CopilotWebDriver
from relay import settle as _settle
from tools.gate_ops import stop_check

load_dotenv()

# Researcher (".dr_work") and Analyst (".diceberry") are Microsoft FIRST-PARTY M365
# Copilot agents. Their deep-link id identifies the AGENT, not the tenant: under one
# tenant the two agents carry DIFFERENT GUIDs (so the GUID is the agent's product id,
# not a per-user id), and they are the same first-party agents for every M365 Copilot
# user -- so these defaults work as-is on most tenants. They are NOT the same URL as
# each other (different agent => different ".suffix" and GUID).
#
# A user can still override either in .env (MCP_RESEARCHER_AGENT_URL /
# MCP_ANALYST_AGENT_URL). And if a default fails to load on some tenant, open_agent()
# pops a dialog (prompt_for_agent_url) so the user pastes their own URL; that value is
# saved to .env and wins from then on. So: default in, dialog fallback if it doesn't
# connect.
DEFAULT_RESEARCHER_URL = "https://m365.cloud.microsoft/chat/agent/P_552e6eda-fc18-7fb9-0ef6-1bf2de3393e4.dr_work"
DEFAULT_ANALYST_URL = "https://m365.cloud.microsoft/chat/agent/P_8cfc4e6f-267e-db15-c6e7-3fc47a54f61e.diceberry"

def _env_agent_url(key: str, default: str) -> tuple[str, bool]:
    """Read an agent URL from .env, returning (url, used_default).

    Previously RESEARCHER_URL/ANALYST_URL were coalesced to their DEFAULT_* silently
    at import time (`os.environ.get(key, "") or DEFAULT`), which meant profile.url was
    NEVER empty for these two profiles even when the user had not set the .env key --
    masking the "not configured" state from the `if not profile.url and allow_prompt`
    self-heal in open_agent() (that path only ever fired for IMPL/FLEET, which have no
    default and hard-fail instead; that hard-fail behaviour is unchanged here).

    Fully unmasking (leaving .url empty until a dialog fills it) was judged too risky
    for the working default case -- every user who has never touched these two keys
    would suddenly hit a prompt/hard-fail on next run. So the minimal, regression-safe
    fix keeps the default fallback applied here (profile.url is still populated, so
    existing callers and runtime behaviour are unaffected), but now ALSO returns
    whether the value is the built-in default or a real user override. That distinction
    is exposed on AgentProfile.url_is_default so a future configure_env/start_all gate
    can tell "unset -> running on default" apart from "user already configured this"
    -- without this module changing what happens at runtime today.
    """
    raw = os.environ.get(key, "").strip()
    if raw:
        return raw, False
    return default, True


RESEARCHER_URL, RESEARCHER_URL_IS_DEFAULT = _env_agent_url(
    "MCP_RESEARCHER_AGENT_URL", DEFAULT_RESEARCHER_URL)
ANALYST_URL, ANALYST_URL_IS_DEFAULT = _env_agent_url(
    "MCP_ANALYST_AGENT_URL", DEFAULT_ANALYST_URL)

# One-time (per process, since module-level code runs once) notice instead of silent
# masking -- so it is at least visible in logs which agents are running on the
# built-in default vs. an explicit .env override.
if RESEARCHER_URL_IS_DEFAULT:
    print("[agent_profiles] using built-in default for RESEARCHER agent; "
          "set MCP_RESEARCHER_AGENT_URL in .env to override", file=sys.stderr)
if ANALYST_URL_IS_DEFAULT:
    print("[agent_profiles] using built-in default for ANALYST agent; "
          "set MCP_ANALYST_AGENT_URL in .env to override", file=sys.stderr)


@dataclass
class AgentProfile:
    name: str
    url: str
    # .env key this profile's URL comes from (drives the dialog fallback below).
    env_key: str = ""
    # CSS for the model dropdown button (None = agent has no model switcher).
    model_picker: str | None = None
    # Deep-research turns stream for minutes; tune completion detection long and
    # patient so an intermediate pause is never mistaken for completion (spec §5).
    end_timeout_s: int = 1800      # 30 min hard cap on one research turn
    dwell_s: float = 12.0          # text must be unchanged this long to count done
    appear_timeout_s: int = 300    # wait up to 5 min for the answer block to appear
    # True when .url came from DEFAULT_*_URL (the .env key is unset/blank), False when
    # it is a real user override. Lets external gates (configure_env/start_all) tell
    # "not configured yet" apart from "configured" even though .url is never empty for
    # RESEARCHER/ANALYST (see _env_agent_url above). Not consumed at runtime by
    # open_agent()/ask_agent() themselves -- purely informational metadata.
    url_is_default: bool = False


PLAIN = AgentProfile(name="plain", url="", model_picker=None,
                     end_timeout_s=1800, dwell_s=4.0, appear_timeout_s=180)

RESEARCHER = AgentProfile(
    name="researcher",
    url=RESEARCHER_URL,   # MCP_RESEARCHER_AGENT_URL (.env) or DEFAULT_RESEARCHER_URL
    env_key="MCP_RESEARCHER_AGENT_URL",
    model_picker='button[data-testid="researcher-model-picker-button"]',
    end_timeout_s=1800, dwell_s=12.0, appear_timeout_s=300,
    url_is_default=RESEARCHER_URL_IS_DEFAULT,
)

# Analyst ("アナリスト") analyses an UPLOADED data file. Unlike the Researcher it has
# NO model picker (cannot be switched to Claude) and runs on its default model.
# NOTE on value: the implementation agent already has read_excel / run_python /
# summarize_table locally, so prefer doing analysis with those. The Analyst is only
# worth delegating to for its built-in analysis/visualisation UI on a file. Per spec
# §5 its numeric claims must be ground-verified with the local tools (operator ③).
ANALYST = AgentProfile(
    name="analyst",
    url=ANALYST_URL,      # MCP_ANALYST_AGENT_URL (.env) or DEFAULT_ANALYST_URL
    env_key="MCP_ANALYST_AGENT_URL",
    model_picker=None,
    end_timeout_s=900, dwell_s=8.0, appear_timeout_s=180,
    url_is_default=ANALYST_URL_IS_DEFAULT,
)

PROFILES = {p.name: p for p in (PLAIN, RESEARCHER, ANALYST)}


def prompt_for_agent_url(env_key: str, reason: str = "") -> str:
    """Pop the configure_env dialog focused on ONE agent-URL field so the user can
    paste the correct URL when the default/recorded one fails to load, then return the
    saved value from .env. Returns "" if there is nothing to prompt with, the user
    cancels, or GUI prompting is disabled (MCP_AGENT_URL_PROMPT=0 -- set it for
    unattended/batch runs so a hidden dialog can't block them).

    This is the "dialog fallback" half of: default URL in, dialog if it won't connect.
    """
    if not env_key or os.environ.get("MCP_AGENT_URL_PROMPT", "1") != "1":
        return ""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ps1 = os.path.join(repo, "scripts", "configure_env.ps1")
    if not os.path.isfile(ps1):
        return ""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
             "-Only", env_key, "-Reason", (reason or "")],
            timeout=600, check=False,
        )
    except Exception:
        return ""
    # Re-read .env so the freshly-saved value is visible to this process.
    try:
        load_dotenv(os.path.join(repo, ".env"), override=True)
    except Exception:
        pass
    return os.environ.get(env_key, "").strip()


def open_agent(page, profile: AgentProfile, allow_prompt: bool = True) -> bool:
    """Navigate to the agent's bare URL (= a fresh chat) and wait for the composer.

    Self-healing: if the composer never renders (the default or recorded URL did not
    resolve to a usable agent) and this profile is bound to an .env key, pop a dialog
    for the user to paste the correct URL, save it, and retry ONCE. So a wrong default
    on some tenant degrades to a one-time prompt instead of a hard failure.
    """
    if not profile.url and allow_prompt and profile.env_key:
        new = prompt_for_agent_url(profile.env_key, reason=f"{profile.name} agent URL is not set yet.")
        if new:
            profile.url = new
    if not profile.url:
        raise RuntimeError(
            f"{profile.name} agent URL is empty -- set {profile.env_key or 'the agent URL'} in .env"
        )

    def _go_and_wait() -> bool:
        page.goto(profile.url, wait_until="domcontentloaded")
        for _ in range(40):
            page.wait_for_timeout(1000)
            if page.locator(COPILOT_SELECTORS["composer"]).count() > 0:
                return True
        return False

    if _go_and_wait():
        return True
    # Composer never rendered -> the URL likely points at no usable agent on this tenant.
    if allow_prompt and profile.env_key:
        new = prompt_for_agent_url(
            profile.env_key,
            reason=(f"The {profile.name} agent did not load from:\n{profile.url}\n\n"
                    f"Open the correct agent in M365 Copilot and paste its address-bar URL."))
        if new and new != profile.url:
            profile.url = new
            return _go_and_wait()
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
    (clicking too early was the silent failure in the relay's side-page path).

    THIS CHOICE IS NOT PAGE STATE. Measured 2026-08-21 by capturing the client's own chat
    frame with the picker at Default and at Claude and diffing them: exactly one non-volatile
    field moved,

        gpts[0].clientOverrides.deepResearchModels[0]:  "Default" -> "Claude"

    so the model travels in the request. A socket-driven Researcher can therefore select the
    model by setting that field and does not need a tab for the picker -- which matters,
    because the side-pages are what a worker still pays tabs for. (The only other difference
    was `feature.EnableExplicitWarmup` appearing in variants, which is unrelated to the model,
    and clientSessionId, which differs in every capture.)

    The Analyst is the opposite case and no experiment will move it: it needs a local file in
    a real <input type=file>, and a socket has nowhere to put one."""
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
#
# KEPT FOR CALLERS THAT STILL READ IT, but `_report_marker` below is what decides. Three of
# these five are words a finished report's BODY contains all the time -- a research report
# about slide decks says "PowerPoint" in its second paragraph -- and the completion test was
# `marker anywhere OR len >= 1000`, so a short status line that happened to mention one of
# them was captured as the final report.
DONE_MARKERS = ("ステップで完了", "推論が", "に変換:", "PowerPoint", "インフォグラフィック")

#: The completion HEADER, anchored to the top of the block and matched as a shape rather than
#: a word. "推論が" on its own appears in prose; "推論が 12 ステップで完了" is the header the
#: UI writes when the run finishes.
_REPORT_MARKERS = (re.compile(r"推論が\s*\d+\s*ステップで完了"),)

#: Buttons the UI offers BELOW a finished report. Anchored to the tail for the same reason:
#: the report's body mentions these formats; the affordance row is at the end.
_UI_AFFORDANCE = ("に変換:", "PowerPoint", "インフォグラフィック")

#: How much of each end to look at. Generous enough for a header that follows a title line,
#: tight enough that the middle of a long report cannot trip either test.
_MARKER_HEAD, _MARKER_TAIL = 400, 600

#: How long a block must be to count as the report rather than a status line,
#: when no completion marker is present. Named because it is one half of a pair:
#: `_looks_like_clarification` caps at 900, and the gap between them is the
#: whole point -- see the note there.
SUBSTANTIAL_CHARS = 1000


def _report_marker(text: str) -> bool:
    """Whether this block carries evidence that the report FINISHED.

    Position is half the signal. The old test asked only "does this string appear anywhere",
    which cannot distinguish the UI announcing completion from the report discussing the same
    words -- and the consequence was capturing a status line as the deliverable.
    """
    t = text or ""
    if any(p.search(t[:_MARKER_HEAD]) for p in _REPORT_MARKERS):
        return True
    # THE TAIL HAS TO BE A TAIL. `t[-600:]` on a 200-character status line is the whole line,
    # so anchoring bought nothing and "進捗: PowerPoint への変換を検討中です" still read as a
    # finished report -- the exact failure this function was written to stop, surviving the
    # rewrite intended to fix it. The affordance row only means anything below a body.
    if len(t) <= _MARKER_TAIL:
        return False
    return any(a in t[-_MARKER_TAIL:] for a in _UI_AFFORDANCE)
DEFAULT_APPROVAL = ("go ahead でお願いします。設定はお任せ（best judgment）で、"
                    "調査を開始して最終レポートまで進めてください。")


def _looks_like_clarification(text: str) -> bool:
    t = (text or "").lower()
    has_clarify = any(m.lower() in t for m in CLARIFY_MARKERS)
    # THE FINISHED-REPORT TEST IS THE STRICT ONE NOW. Using the loose word list here meant a
    # report that merely mentioned PowerPoint was exempted from the clarification check for
    # the wrong reason -- right answer, wrong evidence, and it stopped working the moment the
    # word list changed.
    has_done = _report_marker(text or "")
    # UNDER 900, NOT 1500. `substantial` (below and in _wait_research_done) starts at 1000, so
    # 1000-1500 belonged to BOTH: a finished report of that length containing an approval word
    # was read as a question, answered with the approval text, and the real report discarded --
    # then the turn timed out. The 100-char gap is deliberate slack between the two bands so
    # neither can creep into the other unnoticed.
    return has_clarify and not has_done and len(text) < 900


#: How many scoping questions the auto-approval will answer before giving up on being asked.
#: More than one because the Researcher genuinely asks twice sometimes; bounded because an
#: agent that only ever asks would otherwise consume a whole research budget in questions.
MAX_APPROVALS = 3


def _still_generating(drv) -> bool:
    """Whether the agent is visibly still working, per the Stop button.

    Defensive on purpose: a driver without the probe (a stub, or a socket driver whose turn
    ends by protocol) degrades to the old text-only behaviour rather than raising. A failed
    probe answers False, because "I could not tell" must not freeze a research that has in
    fact finished -- the deadline is the backstop, not this.
    """
    probe = getattr(drv, "_is_generating", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return False


#: WHY A DOM SIGNAL DECIDES THIS AND TEXT CANNOT. Measured on two full researches,
#: 2026-08-21:
#:   * the completion header (`推論が N ステップで完了`) NEVER appeared -- not once, on a
#:     finished 11,507-character report that then sat unchanged for two minutes. Requiring it
#:     would hang every research until its deadline.
#:   * length is useless: the block passes 1,000 characters at 30-45s, roughly ten minutes
#:     before the report exists.
#:   * stability is useless: while still working, the block held byte-identical for 169
#:     seconds. Sixteen separate samples satisfied "substantial and stable for 24s" while the
#:     research was still running, the earliest at 6,060 characters -- 52% of the final report.
#:   * the block SHRINKS. It went 6,060 -> 929 -> 1,475 -> 11,507. Nothing monotonic can be
#:     concluded from its size.
#: The Stop button separated the two perfectly: True for the whole run, False from 673s, with
#: the final text already in place. It is also not text, so it cannot be reworded.
#:
#: A SOCKET WOULD NOT NEED ANY OF THIS. Over ChatHub the backend sends a completion frame and
#: the turn is over by protocol, which is a stronger signal than any button -- one more reason
#: the side agents belong on the socket route.


def _wait_research_done(drv: CopilotWebDriver, profile: AgentProfile,
                        approval: str = "", approvals_left: int = 0) -> bool:
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
        # A SECOND SCOPING QUESTION IS STILL A SCOPING QUESTION. This loop used to have no
        # notion of one at all: ask_agent answered the first and then waited here, so a
        # Researcher that asked again was met with silence until the deadline.
        if approval and approvals_left > 0 and _looks_like_clarification(t):
            approvals_left -= 1
            drv.send(approval)
            last, stable_since = None, None
            time.sleep(1.0)
            continue
        if _still_generating(drv):
            # Still working. Reset the clock: stability gathered mid-run is not evidence.
            last, stable_since = None, None
            time.sleep(2.0)
            continue
        if _is_processing(t):
            last, stable_since = None, None
        elif t == last:
            has_marker = _report_marker(t)
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
                # This loop bypasses CopilotWebDriver.wait_for_idle -- apply the same
                # cross-turn correspondence guard directly (see its docstring): a
                # settled, substantial text byte-identical to the PREVIOUS turn's
                # accepted answer on this driver is the stale-capture signature, not a
                # freshly finished report. Keep waiting; still bounded by `deadline`.
                if getattr(drv, "_is_stale_repeat", lambda _t: False)(t):
                    last, stable_since = t, stable_since  # keep sampling, do not reset
                else:
                    accept = getattr(drv, "_accept_new_reply", None)
                    if callable(accept):
                        accept(t)
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
        ok = _wait_research_done(drv, profile, approval=approval,
                                 approvals_left=MAX_APPROVALS - 1)
    elif _report_marker(first) or len(first) >= SUBSTANTIAL_CHARS:
        # already the finished report (rare: a query that needed no scoping).
        ok = True
    else:
        # `first` is an INTERMEDIATE status line ("リサーチ ツール 処理中です" / "...初期情報を収集"):
        # NOT a clarification and NOT the report. The Researcher streams progress into the SAME
        # block for minutes before the final report lands. Wait it out instead of returning the
        # stub -- THIS was the garbage-research bug (a status line fed back as the result, so the
        # implementer "coded from garbage"). _wait_research_done now ignores short stalled status
        # lines and only returns on a marker/substantial block.
        ok = _wait_research_done(drv, profile, approval=approval,
                                 approvals_left=MAX_APPROVALS)

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
                 dwell_s=4.0, timeout_s=600, tx_dir=None, parent_key="", parent_turn=0,
                 sub_index=1, profile=None, upload_path="", max_approvals=MAX_APPROVALS):
        # WHICH SIDE AGENT, AND WHETHER IT NEEDS A FILE FIRST. Parameters rather than a second
        # copy of this class: the fleet needed an ANALYZE: path as well as RESEARCH:, and the
        # two differ only in which agent opens and whether a local file is uploaded before the
        # instruction is sent. Duplicating a state machine this fiddly -- lazy RAM-gated open,
        # dwell, stabilisation, timeout, sub-conversation capture -- is how one copy quietly
        # stops matching the other.
        self.profile = profile or RESEARCHER
        self.upload_path = upload_path
        self.context = context
        self.query = query
        self.model_name = model_name
        self.approval = approval
        self.max_approvals = max(0, int(max_approvals))
        self.dwell_s = dwell_s
        self.timeout_s = timeout_s
        self.page = None
        self.drv = None
        self._count_before = 0
        self._t_send = None
        self._last = None
        self._stable_since = None
        self._settle_state = _settle.SettleState()
        self._approved = False        # kept: other readers ask "was one ever sent"
        #: HOW MANY have been sent. `_approved` was a one-shot flag, so a Researcher that asks
        #: a SECOND scoping question was never answered -- the session then sat out its whole
        #: budget and returned empty, which is a 25-minute way to produce nothing. Bounded,
        #: because an agent that only ever asks must not spend the budget on being asked.
        self._approvals = 0
        self._pending_open = True  # side-page not opened yet -- deferred until there's free RAM
        #: True while this deep-dive is running over a socket instead of a side page. A socket
        #: passes no RAM gate, which is the point: measured today, ram_room_for_tab() was False
        #: and the fleet would have solved WITHOUT its research rather than waiting for one.
        self.socket = False
        #: Asked once. A capture costs a real turn on a real tab, so retrying it every poll
        #: would spend more than the tab it is trying to avoid.
        self._socket_tried = False
        self._done = None          # report string once finished ('' on failure)
        #: WHY it finished empty, when it did. Every path to an empty report used to look
        #: identical from outside -- a swallowed exception, a timeout and a RAM skip all
        #: produced "" -- so a research that CRASHED was indistinguishable from one that found
        #: nothing, and the worker carried on as if the deep dive had simply come back quiet.
        self.error = ""
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

    @staticmethod
    def _socket_share(budget_s) -> float:
        from relay.refuter import _socket_share as share
        return share(budget_s)

    def _socket_retry(self, reason):
        """Reconnect after a transport fault instead of dropping to a side page.

        Bounded by the same budget a worker uses, and reported with the token life at the
        moment of the fault so the next argument about WHY a socket died is settled from a
        record rather than from a hypothesis. Returns True when the caller should simply wait
        for the lazy-open path to take a socket again.
        """
        from relay.relay_fleet import (DEFAULT_SOCKET_RETRIES, _socket_route,
                                       socket_fault_is_transport)
        if not socket_fault_is_transport(reason):
            return False
        tries = getattr(self, "_socket_retries", 0)
        route = _socket_route()
        if tries >= DEFAULT_SOCKET_RETRIES or not route.open():
            return False
        self._socket_retries = tries + 1
        try:
            route.record("socket_retry", worker="research", attempt=self._socket_retries,
                         token_seconds_left=int(route.token_life()), reason=reason[:300])
        except Exception:
            pass
        try:
            self.drv.close()
        except Exception:
            pass
        self.socket, self.drv = False, None
        self._socket_tried = False
        self._pending_open = True
        self._t_send = time.time()
        return True

    def _try_socket(self) -> bool:
        """Run this deep-dive over a socket if one can be had. Never raises, never blocks long.

        MEASURED END TO END, 2026-08-21: a Researcher turn over a socket finished in 255
        seconds against 673-809 for the same work in a tab, returned a 5,906-character report
        with citations, streamed 29 progress messages, and reported itself running as Claude --
        the model chosen through the frame rather than through a picker only a tab has. No tab
        was open at any point.

        And the completion problem disappears. Over a tab, "is it finished" had to be inferred
        from a Stop button because the text lies -- the block passes 1,000 characters ten
        minutes early, holds still for 169 seconds mid-run, and SHRINKS. Over a socket the
        backend sends a completion frame and the turn is over by protocol.
        """
        self._socket_tried = True
        if self.upload_path:
            # The Analyst reads a local file from a real <input type=file>. See
            # relay/transport_policy.py -- the one property measured to force a tab.
            return False
        try:
            from relay.relay_fleet import _socket_route

            route = _socket_route()
            if not route.open() or self.context is None:
                return False
            url = getattr(self.profile, "url", "") or ""
            if not url:
                return False
            if route.needs_refresh(url) and not route.refresh(self.context, url):
                return False
            drv = route.driver_for(
                "research", agent_url=url,
                model=(self.model_name if getattr(self.profile, "model_picker", None) else ""),
                turn_timeout_s=self._socket_share(self.timeout_s),
                frame_timeout_s=300.0)
            if drv is None:
                return False
            self.page, self.drv, self.socket = None, drv, True
            self._count_before = 0
            self.drv._count_before = 0
            self.drv.send(self.query)
            self._pending_open = False
            self._t_send = time.time()
            return True
        except Exception:
            # A route that cannot be had is not a failure of the deep-dive: the tab path is
            # right there and is what ran before this existed.
            self.socket = False
            self.page, self.drv = None, None
            return False

    def _do_open(self):
        from .copilot_autopilot_relay import CopilotWebDriver
        try:
            if self.context is None:
                self._fail("no browser context"); return
            self.page = self.context.new_page()
            if not open_agent(self.page, self.profile):
                self._fail("the %s surface did not open" % self.profile.name); return
            if self.upload_path:
                # AND THIS IS WHY THE ANALYST CANNOT USE A SOCKET: the file goes into a real
                # <input type=file>, and a socket has nowhere to put one. Measured across
                # twenty socket turns and eight request classes, it is the ONLY property found
                # so far that structurally forces a tab -- and it is knowable here, from a
                # parameter the caller already set, rather than predicted from request text.
                # relay/transport_policy.py owns the transport decision; this belongs in it.
                # THE ANALYST NEEDS THE DATA BEFORE THE QUESTION. A failed upload must end the
                # session rather than send an instruction about a file that is not there --
                # which would come back as a confident answer about nothing.
                if not upload_file(self.page, self.upload_path):
                    self._fail("the upload failed; an instruction about a file that is not "
                               "there comes back as a confident answer about nothing")
                    return
            if self.model_name and self.profile.model_picker:
                set_model(self.page, self.profile, self.model_name)
            self.drv = CopilotWebDriver(self.page)
            self._count_before = self.drv._answers().count()
            self.drv._count_before = self._count_before
            self.drv.send(self.query)
            self._pending_open = False
            self._t_send = time.time()   # reset the clock to when the query actually went out
        except Exception as exc:
            self._fail("open failed: %s: %s" % (type(exc).__name__, str(exc)[:160]))

    def poll(self):
        """None while the Researcher is still working; else the report string ('' on failure)."""
        from .copilot_autopilot_relay import _is_processing
        if self._done is not None:
            return self._done
        # RAM-gated lazy open: hold off opening the side-page until there's room (bounded by the
        # timeout). Returns quickly each sweep, so the fleet stays responsive while it waits.
        if self.socket and getattr(self.drv, "failed", ""):
            # THE ROUTE FAILING IS NOT THE DEEP-DIVE FAILING. Drop to a tab and let the
            # RAM-gated open below do exactly what it did before sockets existed.
            reason = getattr(self.drv, "failed", "") or "unknown"
            # RECONNECT FIRST, exactly as a worker does -- see refuter.py for why all three
            # call sites had to change together rather than one at a time.
            if self._socket_retry(reason):
                return None
            try:
                from relay.relay_fleet import _socket_route
                _socket_route().note_failure("research: %s" % reason)
                _socket_route().record("fallback", worker="research",
                                       goal=(self.query or "")[:600], reason=reason[:300])
            except Exception:
                pass
            try:
                self.drv.close()
            except Exception:
                pass
            self.socket, self.drv = False, None
            self._pending_open = True
            self._t_send = time.time()
            return None
        if self._pending_open:
            if not self._socket_tried and self._try_socket():
                return None
            from .relay_fleet import ram_room_for_tab
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                # RAM-STARVED SKIP (see refuter.py): research never got a tab within timeout_s,
                # so the worker proceeds WITHOUT the requested deep-dive. Distinct marker so a
                # benchmark can count tab-pressure degradations and re-run those instances cooler.
                import sys
                sys.stderr.write("[research] RAM_SKIP: no tab within %ds, deep-dive SKIPPED "
                                 "(instance solved without research)\n" % int(self.timeout_s))
                self._fail("no tab within %ds: the box had no RAM for a side page"
                           % int(self.timeout_s))
                return self._done
            if not ram_room_for_tab():
                return None
            self._do_open()
            return None
        if self.drv is None:
            return self._done
        try:
            if self._t_send and time.time() - self._t_send > self.timeout_s:
                self._fail("timeout: %ds without a finished report" % int(self.timeout_s))
                return self._done
            if self.drv._answers().count() <= self._count_before:
                return None
            t = self.drv.read_last_response()
            if _is_processing(t):
                self._last, self._stable_since = None, None
                self._settle_state = _settle.SettleState()
                return None
            # one-time scoping/clarification approval (the Researcher may ask before it researches)
            if (self._approvals < self.max_approvals and _looks_like_clarification(t)
                    and self.approval):
                self._approvals += 1
                self._approved = True
                self.drv.send(self.approval)
                self._last, self._stable_since = None, None
                # The approval starts a NEW turn; carrying settle across it would let
                # stability gathered on the question count toward accepting the answer.
                self._settle_state = _settle.SettleState()
                return None
            if _still_generating(self.drv):
                # Still working -- see the note on _wait_research_done. Everything below this
                # line asks "has it settled", and settling while it runs means nothing.
                self._last, self._stable_since = None, None
                self._settle_state = _settle.SettleState()
                return None
            # a stable, SUBSTANTIAL block (completion marker OR >=1000 chars) is the finished
            # report; a short stalled status line is not (same gate as _wait_research_done). The
            # marker appears at the report header then the body streams, so require the full dwell.
            substantial = _report_marker(t) or len(t) >= SUBSTANTIAL_CHARS
            if _settle.unified():
                # THE ONE RULE. This site had no sample requirement either -- only a dwell,
                # doubled unconditionally -- so a deep-research report that paused mid-stream
                # for longer than 2x dwell was captured at the pause. The marker here is
                # "the report finished", which is a stronger statement than the other three
                # sites can make, so an unmarked block genuinely deserves the longer settle.
                state = getattr(self, "_settle_state", None) or _settle.SettleState()
                state, outcome = _settle.settle_step(
                    state, t, now=time.time(), dwell_s=self.dwell_s, generating=False,
                    is_processing=_is_processing(t), has_marker=_report_marker)
                self._settle_state = state
                self._last, self._stable_since = state.last, state.stable_since
                if outcome != _settle.ACCEPT or not substantial:
                    return None
                if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                    return None
                accept = getattr(self.drv, "_accept_new_reply", None)
                if callable(accept):
                    accept(t)
                self._report_full = t
                self._finish(t[:3500]); return self._done
            if t == self._last:
                if self._stable_since and substantial and (
                        time.time() - self._stable_since) >= self.dwell_s * 2:
                    # This poll loop bypasses CopilotWebDriver.wait_for_idle -- apply the
                    # same cross-turn correspondence guard directly (see its docstring): a
                    # settled, substantial text byte-identical to the PREVIOUS turn's
                    # accepted answer on this driver is the stale-capture signature, not
                    # a fresh report. Keep waiting; still bounded by self.timeout_s above.
                    if getattr(self.drv, "_is_stale_repeat", lambda _t: False)(t):
                        return None
                    accept = getattr(self.drv, "_accept_new_reply", None)
                    if callable(accept):
                        accept(t)
                    self._report_full = t          # keep the WHOLE report for the sub-transcript
                    self._finish(t[:3500]); return self._done
                return None
            self._last, self._stable_since = t, time.time()
            return None
        except Exception as exc:
            self._fail("%s: %s" % (type(exc).__name__, str(exc)[:200]))
            return self._done

    def _fail(self, reason):
        """Finish empty, but say why. An empty report is a fact; an empty reason is a bug."""
        self.error = str(reason)
        import sys as _sys
        _sys.stderr.write("[research] gave up: %s" % self.error + chr(10))
        self._finish("")

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
            if self.socket and self.drv is not None:
                self.drv.close()          # a socket is cheap, but it is not free
        except Exception:
            pass
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        self.page = None
        self.drv = None
        self.socket = False


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
