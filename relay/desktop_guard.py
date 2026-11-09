# -*- coding: utf-8 -*-
"""Refuse to start a job whose goal asks for UI automation on the owner's REAL desktop.

WHY THIS EXISTS. 2026-11-02: an A/B test of a ported UIA tool ("uiaab"/"uiaab2") drove
Notepad and Calculator on the owner's real desktop, taking their screen and keyboard. The
owner cancelled it (twice) and forbade real-desktop UI automation, then it came back after
every coordinator restart. The durable tombstone (relay/fleet_tombstone.py) stops THOSE
specific jids from resurrecting; this guard is the SECOND line the owner asked for -- a
standing rule that refuses ANY job whose goal text asks for real-desktop computer use, so a
new submission of the same kind of work is stopped before a worker opens on the real screen.

THE CLASSIFIER IS A VOCABULARY MATCH, the same shape relay/judge_gate.py / relay/task_kind.py
use: a fixed set of markers, casefolded substring match, first hit wins, deterministic, no
clock and no state. The markers are the tool names and app-automation phrases the real goals
used (screen_uia_act, screen_uia_dump, desktop_act, uia, Notepad/Calculator automation). A
goal that matches none is allowed. A goal that matches but carries the explicit operator flag
allow_real_desktop is allowed -- that is the one deliberate override, off by default.

DEFAULT ON. The switch (settings key ``block_real_desktop_automation`` in settings.txt, read
live like every other each_gate key) ships ON: a matching goal is REFUSED. ``off`` disables
the guard entirely (the pre-guard behaviour). Read fresh on every start so an operator's
change reaches the next worker with no restart.

FAIL OPEN on an error reading settings, but the DEFAULT of a readable-but-absent key is ON:
the owner's rule is a safety floor, so "could not read the switch" must not silently disable
it -- only an explicit ``off`` does.
"""
from __future__ import annotations

import io
import os
import threading

#: Settings key, read from the shared settings.txt (tools/settings_path.py) like every other
#: each_gate control, mtime-cached so an edit reaches the next worker start with no restart.
MODE_KEY = "block_real_desktop_automation"
_MODES = ("off", "on")
DEFAULT_MODE = "on"

#: The operator override that lets ONE goal through: a new boolean field on the goal dict,
#: off (absent/false) by default. A person who truly wants a real-desktop run sets it; an
#: agent handing work to the fleet does not, so the standing refusal holds for ordinary jobs.
ALLOW_FIELD = "allow_real_desktop"

#: The vocabulary. Casefolded substring markers naming the real-desktop UI-automation tools and
#: the app-automation phrasing the cancelled goals actually used (see
#: the per-user ~/.claude/tools/goals/g_uia_ab.txt and g_uia_ab2.txt goal files). Kept narrow so an
#: ordinary coding/data goal that merely MENTIONS a window is not caught: the markers are tool
#: identifiers and explicit "UI automation of <app>" phrases, not the bare app names alone.
#: desktop_act.py is the real-desktop UIA tool file; screen_uia_act / screen_uia_dump are its
#: entry points; bgdesk is the PRIVATE never-displayed desktop and is deliberately NOT a marker.
_MARKERS = (
    "desktop_act.py",
    "desktop_act",
    "screen_uia_act",
    "screen_uia_dump",
    "screen_msg_click",
    "uia tool",
    "uia ツール",
    "ported uia",
    "a/b test of two versions of our uia",
    "uia/notepad",
    "notepad and calculator",
    "notepad / calculator",
    "notepad と calculator",
    "on windows notepad and calculator",
    "the owner's real desktop",
    "owner's screen, taskbar",
    "real desktop",
    "実デスクトップ",
    "実際のデスクトップ",
)

_LOCK = threading.Lock()
_SETTINGS_CACHE: dict = {}


def _settings_value():
    """The MODE_KEY value from settings.txt, mtime-cached. None when absent/unreadable."""
    try:
        from tools.settings_path import settings_file
        path = settings_file()
        if not path:
            return None
        st = os.stat(path)
        ckey = (path, st.st_mtime_ns, st.st_size)
        with _LOCK:
            hit = _SETTINGS_CACHE.get(path)
            if hit is not None and hit[0] == ckey:
                return hit[1]
        val = None
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(MODE_KEY + "="):
                    val = ln.split("=", 1)[1].strip()
        with _LOCK:
            _SETTINGS_CACHE[path] = (ckey, val)
        return val
    except Exception:
        return None


def mode() -> str:
    """'on' (default) or 'off'. Settings decide; the env var MCP_BLOCK_REAL_DESKTOP_AUTOMATION
    beats the file for tests/emergencies. Absent/unreadable/unknown reads as the default ON --
    the owner's safety floor is not disabled by a missing line, only by an explicit 'off'.
    """
    env = (os.environ.get("MCP_BLOCK_REAL_DESKTOP_AUTOMATION") or "").strip().lower()
    if env in _MODES:
        return env
    v = (_settings_value() or "").strip().lower()
    return v if v in _MODES else DEFAULT_MODE


def asks_for_real_desktop(goal_text) -> bool:
    """Does this goal text ask for UI automation / computer use on the real desktop?

    A deterministic vocabulary match (judge_gate / task_kind shape): casefolded substring, any
    marker hits. Non-text input is False.
    """
    if not isinstance(goal_text, str) or not goal_text.strip():
        return False
    text = goal_text.casefold()
    for marker in _MARKERS:
        if marker in text:
            return True
    return False


def _allow_flag(goal) -> bool:
    """The operator override from the goal dict / object: allow_real_desktop truthy."""
    if isinstance(goal, dict):
        return bool(goal.get(ALLOW_FIELD))
    return bool(getattr(goal, ALLOW_FIELD, False))


def _goal_text(goal) -> str:
    if isinstance(goal, dict):
        return str(goal.get("text") or goal.get("goal") or "")
    return str(getattr(goal, "goal", None) or getattr(goal, "text", None) or "")


REFUSAL = (
    "実デスクトップのUI自動化（computer use）は禁止されています。このジョブのゴールは"
    "実画面のNotepad/Calculator等を操作する内容のため、開始を拒否しました。"
    "どうしても実デスクトップで動かす必要があるときは、ゴールに allow_real_desktop を付けて"
    "再投入してください。 / Refused: this goal asks for UI automation on the real desktop "
    "(the owner forbade it). Re-submit with allow_real_desktop set if it truly must run there."
)


def refuse_reason(goal):
    """The refusal string if this goal must NOT start, else None.

    Order: the guard off -> allow; the explicit operator override on the goal -> allow; the
    goal's text asks for real-desktop automation -> refuse. Everything else -> allow. Never
    raises: an error deciding leaves the goal alone (fail open), the one exception being that
    'off' is only reached by an explicit setting, not by a read error (see mode()).
    """
    try:
        if mode() == "off":
            return None
        if _allow_flag(goal):
            return None
        if asks_for_real_desktop(_goal_text(goal)):
            return REFUSAL
        return None
    except Exception:
        return None


def _reset_for_test() -> None:
    """Drop the settings cache. For tests that rewrite settings.txt in place."""
    with _LOCK:
        _SETTINGS_CACHE.clear()
