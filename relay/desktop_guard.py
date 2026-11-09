# -*- coding: utf-8 -*-
"""Classify whether a job's goal asks for UI automation on the owner's REAL desktop.

WHY THIS EXISTS. 2026-11-02: an A/B test of a ported UIA tool ("uiaab"/"uiaab2") drove
Notepad and Calculator on the owner's real desktop, taking their screen and keyboard. The
owner cancelled it (twice). The durable tombstone (relay/fleet_tombstone.py) stops THOSE
specific jids from resurrecting. This guard is a standing CLASSIFIER for the same KIND of
work: a fixed vocabulary that recognises a goal asking for real-desktop computer use.

DEFAULT FLIPPED (owner instruction 2026-11-09). The guard now ALLOWS a matching goal by
default. The owner asked to stop refusing these goals outright and instead keep an explicit
OPT-OUT that restores the old refuse-by-default behaviour. So:

  * DEFAULT (nothing set)            -> a matching goal is ALLOWED (born as a normal worker).
  * OPT-OUT ``desktop_guard=deny``   -> the old behaviour: a matching goal is REFUSED unless
    in settings.txt, OR env var           it carries the explicit operator field
    MCP_DESKTOP_GUARD=deny                 allow_real_desktop.

The setting is read live from the shared settings.txt (tools/settings_path.py) like every
other each_gate key, mtime-cached, so an operator's change reaches the next worker start with
no restart. The env var MCP_DESKTOP_GUARD beats the file for tests/emergencies.

BACK-COMPAT. The previous switch (``block_real_desktop_automation`` in settings.txt, env
MCP_BLOCK_REAL_DESKTOP_AUTOMATION) is still honoured as a deny trigger: an explicit ``on``
there also restores the refuse-by-default behaviour. But its ABSENCE no longer means deny --
the default is allow now, so only an explicit opt-out refuses.

FAIL OPEN. Any error deciding leaves the goal alone (allow). With the flipped default that is
also the fail-open direction, so a settings read error can never silently start refusing work.
"""
from __future__ import annotations

import io
import os
import threading

#: The opt-out settings key, read from the shared settings.txt (tools/settings_path.py) like
#: every other each_gate control, mtime-cached so an edit reaches the next worker with no
#: restart. ``allow`` (default) lets matching goals through; ``deny`` restores the old refusal.
MODE_KEY = "desktop_guard"
_MODES = ("allow", "deny")
DEFAULT_MODE = "allow"

#: Back-compat: the previous settings key / env var. An explicit ``on`` there still means deny.
LEGACY_MODE_KEY = "block_real_desktop_automation"
LEGACY_ENV = "MCP_BLOCK_REAL_DESKTOP_AUTOMATION"

#: The operator override that forces ONE goal through even under the deny opt-out: a boolean
#: field on the goal dict / object, off (absent/false) by default. Under the default allow mode
#: it is a no-op (everything is allowed); it only matters when the deny opt-out is in effect.
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
    "uia \u30c4\u30fc\u30eb",
    "ported uia",
    "a/b test of two versions of our uia",
    "uia/notepad",
    "notepad and calculator",
    "notepad / calculator",
    "notepad \u3068 calculator",
    "on windows notepad and calculator",
    "the owner's real desktop",
    "owner's screen, taskbar",
    "real desktop",
    "\u5b9f\u30c7\u30b9\u30af\u30c8\u30c3\u30d7",
    "\u5b9f\u969b\u306e\u30c7\u30b9\u30af\u30c8\u30c3\u30d7",
)

_LOCK = threading.Lock()
_SETTINGS_CACHE: dict = {}


def _settings_value(key):
    """The value of ``key`` from settings.txt, mtime-cached. None when absent/unreadable."""
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
                return hit[1].get(key)
        values: dict = {}
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                for k in (MODE_KEY, LEGACY_MODE_KEY):
                    if ln.startswith(k + "="):
                        values[k] = ln.split("=", 1)[1].strip()
        with _LOCK:
            _SETTINGS_CACHE[path] = (ckey, values)
        return values.get(key)
    except Exception:
        return None


def mode() -> str:
    """'allow' (default) or 'deny'.

    Resolution order, first decisive wins:
      1. env MCP_DESKTOP_GUARD        -> allow|deny
      2. env MCP_BLOCK_REAL_DESKTOP_AUTOMATION (legacy) -> on means deny, off means allow
      3. settings.txt desktop_guard   -> allow|deny
      4. settings.txt block_real_desktop_automation (legacy) -> on means deny, off means allow
      5. DEFAULT_MODE = allow

    Absent/unreadable/unknown falls through to the default ALLOW -- the flipped default, which
    is also the fail-open direction, so a missing or garbled setting never starts refusing work.
    """
    env = (os.environ.get("MCP_DESKTOP_GUARD") or "").strip().lower()
    if env in _MODES:
        return env
    legacy_env = (os.environ.get(LEGACY_ENV) or "").strip().lower()
    if legacy_env == "on":
        return "deny"
    if legacy_env == "off":
        return "allow"
    v = (_settings_value(MODE_KEY) or "").strip().lower()
    if v in _MODES:
        return v
    legacy = (_settings_value(LEGACY_MODE_KEY) or "").strip().lower()
    if legacy == "on":
        return "deny"
    if legacy == "off":
        return "allow"
    return DEFAULT_MODE


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
    "\u5b9f\u30c7\u30b9\u30af\u30c8\u30c3\u30d7\u306eUI\u81ea\u52d5\u5316\uff08computer use\uff09\u306f"
    "\u3053\u306e\u74b0\u5883\u3067\u306f desktop_guard=deny \u306b\u3088\u308a\u7981\u6b62\u3055\u308c\u3066\u3044\u307e\u3059\u3002"
    "\u3053\u306e\u30b8\u30e7\u30d6\u306e\u30b4\u30fc\u30eb\u306f\u5b9f\u753b\u9762\u306eNotepad/Calculator\u7b49\u3092"
    "\u64cd\u4f5c\u3059\u308b\u5185\u5bb9\u306e\u305f\u3081\u3001\u958b\u59cb\u3092\u62d2\u5426\u3057\u307e\u3057\u305f\u3002"
    "\u5b9f\u30c7\u30b9\u30af\u30c8\u30c3\u30d7\u3067\u52d5\u304b\u3059\u5fc5\u8981\u304c\u3042\u308b\u5834\u5408\u306f\u3001"
    "\u30b4\u30fc\u30eb\u306b allow_real_desktop \u3092\u4ed8\u3051\u3066\u518d\u6295\u5165\u3059\u308b\u304b\u3001"
    "desktop_guard \u306e\u30aa\u30d7\u30c8\u30a2\u30a6\u30c8\u3092\u89e3\u9664\u3057\u3066\u304f\u3060\u3055\u3044\u3002 / "
    "Refused: desktop_guard=deny is set and this goal asks for UI automation on the real "
    "desktop. Re-submit with allow_real_desktop set, or remove the desktop_guard=deny opt-out."
)


def refuse_reason(goal):
    """The refusal string if this goal must NOT start, else None.

    Default is ALLOW (owner instruction 2026-11-09). A goal is only refused when the operator
    has opted back in to the old behaviour with desktop_guard=deny (settings.txt or env), AND
    the goal asks for real-desktop automation, AND it does not carry the allow_real_desktop
    override. Everything else -> allow. Never raises: an error deciding leaves the goal alone
    (fail open = allow, matching the flipped default).
    """
    try:
        if mode() != "deny":
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
