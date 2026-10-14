"""Copilot never runs in a browser tab: no goal path may open one (owner rule, 2026-10-14)."""
from __future__ import annotations

import os
import re

import pytest

from relay import relay_fleet as RF
from relay.no_tab import CopilotTabForbidden

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Modules on a goal's path. A `.new_page(` or `_open_fresh(` CALL may appear in none of them,
#: except where the allow-list below says so.
GOAL_PATH_FILES = (
    "relay/relay_fleet.py", "relay/refuter.py", "relay/agent_profiles.py",
    "relay/socket_attachment.py", "relay/socket_route.py", "relay/fleet_runner.py",
    "relay/profile_token.py", "relay/lean_capture.py", "relay/local_loop_controller.py",
)

#: (file, text on the line) pairs that are allowed. Each is a page that carries NO Copilot turn.
ALLOWED = (
    ("relay/profile_token.py", "page = context.new_page()"),                  # token light page
    ("relay/relay_fleet.py", 'context.new_page().goto("about:blank"'),       # blank keeper
    ("relay/fleet_runner.py", 'context.new_page().goto("about:blank"'),      # blank keeper
    # These two call _open_fresh, which raises CopilotTabForbidden (asserted below): they
    # cannot open a tab, they fail loudly if run.
    ("relay/lean_capture.py", "page = _open_fresh(context, agent_url)"),
    ("relay/local_loop_controller.py", "page = _open_fresh(context, agent_url)"),
)


def _code_lines(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            s = line.strip()
            if s.startswith("#") or s.startswith("`"):
                continue
            yield n, s


def test_no_goal_path_module_calls_new_page_or_open_fresh_outside_the_allow_list():
    offenders = []
    for path in GOAL_PATH_FILES:
        for n, s in _code_lines(path):
            if re.search(r"\.new_page\(", s) or re.search(r"(?<![\w.])_open_fresh\(", s):
                if s.startswith("def _open_fresh"):
                    continue
                if any(path == p and frag in s for p, frag in ALLOWED):
                    continue
                if "forbid_copilot_tab" in s:
                    continue
                offenders.append("%s:%d: %s" % (path, n, s[:100]))
    assert not offenders, "a goal path can open a tab:\n" + "\n".join(offenders)


def test_open_fresh_and_capture_via_tab_refuse():
    with pytest.raises(CopilotTabForbidden):
        RF._open_fresh(object(), "https://example.invalid/")
    from relay.socket_route import capture_via_tab
    with pytest.raises(CopilotTabForbidden):
        capture_via_tab(object(), "https://example.invalid/")


def test_capture_never_runs_a_turn_and_fn_is_always_the_no_turn_one(monkeypatch, tmp_path):
    from relay import profile_token as PT
    monkeypatch.setenv("MCP_CAPTURE_LIGHT", "0")
    assert PT.capture_fn() is PT.capture_via_profile
    PT.forget_memo()
    # no cached template -> a refusal, and the context is never touched (no page, no turn)

    class Ctx:
        def new_page(self):
            raise AssertionError("a capture must not open a page without a template")

    with pytest.raises(PT.NoTemplateWithoutTurn):
        PT.capture_via_profile(Ctx(), "https://agent.invalid/", log=lambda _m: None,
                               directory=str(tmp_path))


def test_cached_template_is_not_evicted_by_age(tmp_path):
    from relay import profile_token as PT
    from relay.chathub import RequestTemplate
    tpl = RequestTemplate({"x": "1"}, {"threadLevelGptId": {"id": "T_agent.x"}})
    assert PT.save_template(tpl, "https://agent.invalid/", str(tmp_path))
    old = 10 ** 9      # 2001
    assert PT.load_template("https://agent.invalid/", str(tmp_path), now=old + 10 ** 9) is not None


class _Route:
    def __init__(self, open_, ready, enabled=True, closed=""):
        self._o, self._r, self.enabled, self.closed_reason = open_, ready, enabled, closed

    def open(self):
        return self._o

    def ready(self, _a=None):
        return self._r

    def driver_for(self, *a, **k):
        return None


def _worker():
    w = RF.RelayWorker.__new__(RF.RelayWorker)
    w.name, w.goal, w.resume_conv, w.status, w.reason = "w0", "g", "", RF.PENDING, ""
    w.task_kind = ""
    return w


@pytest.mark.parametrize("open_,expect", [(True, "waiting for token"),
                                          (False, "socket route unavailable")])
def test_attach_without_a_driver_waits_and_never_opens_a_tab(monkeypatch, open_, expect):
    route = _Route(open_, False, closed="3 consecutive failures")
    monkeypatch.setattr(RF, "_socket_route", lambda: route)

    class Ctx:
        def new_page(self):
            raise AssertionError("attach opened a tab")

    w = _worker()
    assert w.attach(Ctx(), "https://agent.invalid/") is False
    assert w.status == RF.PENDING            # not an error: the goal waits
    assert w.reason.startswith(expect)


def test_hold_has_no_ceiling_and_ignores_the_old_tab_split():
    from relay.token_provider import TokenHold
    t = [0.0]
    h = TokenHold(now=lambda: t[0])
    r = _Route(True, False)
    assert h.hold_reason("g", r, "a").startswith("waiting for token")
    t[0] = 10 ** 6                                   # days later: still waiting, never a tab
    assert h.hold_reason("g", r, "a").startswith("waiting for token")
    assert h.hold_reason("g", _Route(False, False, closed="x"), "a").startswith(
        "socket route unavailable")


def test_socket_loss_midgoal_waits_for_a_driver_instead_of_a_tab(monkeypatch):
    route = _Route(False, False, closed="proxy 502")
    monkeypatch.setattr(RF, "_socket_route", lambda: route)
    w = _worker()
    w._needs_socket_rebind, w._socket_conv_id, w._agent_url = True, "cid", "u"
    w._cooldown_until = 0.0
    assert w._rebind_socket() is False
    assert w.reason.startswith("socket route unavailable")
    assert w.page is None if hasattr(w, "page") else True


def test_side_pages_refuse():
    from relay import agent_profiles, refuter
    for mod in (agent_profiles, refuter):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "forbid_copilot_tab" in src
