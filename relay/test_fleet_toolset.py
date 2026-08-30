"""The fleet's tool set is a list somebody wrote, not a filter somebody hoped was complete.

The gateway carries 167 tools. The first containment plan was "move the execution tools",
which is subtraction -- and a classification of all 167 by name put replace_in_file,
process_kill, run_in_background, verify_python, outlook_send_mail, clipboard_set, screenshot,
trash_path, zip_extract and schedule_run_now in the harmless bucket. Not one of them has
"exec" or "shell" in its name. Subtraction would have shipped every one.
"""
import io
import os
import re

import pytest

from relay.fleet_toolset import (DELIBERATELY_EXCLUDED, FLEET_TOOLS, is_allowed,
                                 unknown_tools)

CATALOGUE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".fleet", "tool_catalogue.txt")


def _catalogue():
    if not os.path.exists(CATALOGUE):
        pytest.skip("no catalogue dump on this machine")
    names = []
    for line in io.open(CATALOGUE, encoding="utf-8"):
        m = re.match(r"^([a-z][a-z0-9_]{2,44})\s+--\s", line)
        if m:
            names.append(m.group(1))
    if not names:
        pytest.skip("catalogue dump parsed to nothing")
    return sorted(set(names))


def test_the_allowed_set_is_small_enough_to_have_been_read():
    """A list of a hundred is a filter wearing a list's clothes."""
    assert len(FLEET_TOOLS) <= 25, "the allowed set has grown past what anybody audits"


def test_every_allowed_tool_carries_a_reason():
    """A tool nobody can justify in one line is a tool that should not be here."""
    for name, why in FLEET_TOOLS.items():
        assert isinstance(why, str) and len(why.split()) >= 4, (
            "%s has no real justification: %r" % (name, why))


def test_nothing_is_both_allowed_and_refused():
    overlap = set(FLEET_TOOLS) & set(DELIBERATELY_EXCLUDED)
    assert not overlap, "contradictory entries: %s" % sorted(overlap)


def test_the_tools_that_fooled_a_name_based_classifier_are_refused():
    """The concrete counter-examples, named, so this cannot regress quietly.

    Each of these was sorted into 'OTHER' by a regex over tool names -- the exact reasoning a
    subtractive denylist would use."""
    for name in ("replace_in_file",):
        assert is_allowed(name), "%s is needed to make a fix and must stay allowed" % name
    for name in ("process_kill", "run_in_background", "verify_python", "outlook_send_mail",
                 "clipboard_set", "screenshot", "trash_path", "zip_extract",
                 "schedule_run_now", "python_check"):
        assert not is_allowed(name), "%s is reachable by a worker" % name
        assert name in DELIBERATELY_EXCLUDED, (
            "%s is merely absent; absence must be a recorded decision" % name)


def test_a_worker_cannot_widen_its_own_permissions():
    """The category that matters most: nothing that changes what the worker may do next."""
    for name in ("unlock", "gate_ask", "gate_poll", "stop_request", "stop_clear",
                 "forge_tool", "skill_request_approval"):
        assert not is_allowed(name)


def test_capture_still_works_under_this_set():
    """The run's own mechanics must survive the restriction, or it will be turned off.

    Capture reads `git diff HEAD` from the worktree, so a worker that cannot commit or stage
    loses nothing -- and git_commit would actively HIDE the change from the capture step."""
    assert is_allowed("git_diff") and is_allowed("git_status")
    assert not is_allowed("git_commit") and not is_allowed("git_add")
    assert not is_allowed("git_checkout")


def test_the_worker_can_still_do_the_job():
    """A containment that stops the benchmark is a containment that gets reverted."""
    for name in ("read_file", "grep", "glob", "write_file", "replace_in_file",
                 "shell_exec", "run_python"):
        assert is_allowed(name), "%s is needed to solve an instance" % name


def test_every_catalogue_tool_has_been_decided_about():
    """Neither allowed nor refused means nobody looked.

    Safe by default -- an unlisted tool is not allowed -- but silent, and silence is how a
    list stops being a decision and becomes a leftover."""
    missing = unknown_tools(_catalogue())
    assert not missing, (
        "%d tools in the catalogue are neither allowed nor explicitly refused:\n  %s"
        % (len(missing), ", ".join(missing)))


# ---- enforcement: shadow by default, and the gateway actually consults it -----------------

def test_the_default_mode_blocks_nothing():
    """Switching a gate from permissive to closed without measuring first is the mistake this
    repository has already been corrected for. Shadow records; it does not refuse."""
    import relay.fleet_toolset as FT
    assert FT.mode() in ("off", "shadow", "enforce")
    old = os.environ.pop(FT.MODE_ENV, None)
    try:
        assert FT.mode() == "shadow"
        ok, _note = FT.check("process_kill")
        assert ok, "shadow mode must not block"
    finally:
        if old is not None:
            os.environ[FT.MODE_ENV] = old


def test_enforce_refuses_only_while_a_fleet_run_is_active(monkeypatch):
    """The gateway cannot tell a worker from the operator -- authentication carries an API key
    and no user identity. The only signal is WHEN, so the restriction is scoped to a run."""
    import relay.fleet_toolset as FT
    monkeypatch.setenv(FT.MODE_ENV, "enforce")
    monkeypatch.setattr(FT, "_fleet_run_active", lambda: False)
    ok, _ = FT.check("process_kill")
    assert ok, "outside a run the operator's own tools must keep working"
    monkeypatch.setattr(FT, "_fleet_run_active", lambda: True)
    ok, note = FT.check("process_kill")
    assert not ok and "outside the fleet's allowed set" in note


def test_an_allowed_tool_is_never_refused(monkeypatch):
    import relay.fleet_toolset as FT
    monkeypatch.setenv(FT.MODE_ENV, "enforce")
    monkeypatch.setattr(FT, "_fleet_run_active", lambda: True)
    for name in ("read_file", "write_file", "shell_exec", "git_diff"):
        ok, _ = FT.check(name)
        assert ok, "%s is in the allowed set and must pass" % name


def test_the_policy_can_never_crash_the_gateway(monkeypatch):
    """A tool call must not fail over bookkeeping. If the policy raises, the call proceeds."""
    import relay.fleet_toolset as FT
    def boom():
        raise RuntimeError("policy is broken")
    monkeypatch.setattr(FT, "_fleet_run_active", boom)
    monkeypatch.setenv(FT.MODE_ENV, "enforce")
    ok, _ = FT.check("process_kill")
    assert ok


def test_the_gateway_consults_the_policy_after_the_help_branch():
    """Asserted against the SOURCE FILE, not by importing main.

    Importing it needs MCP_API_KEY in the environment, which is a fact about deployment and
    not about this ordering. Anchored on executable lines rather than on comment text, because
    a comment can be moved without moving the code it describes.

    Order matters: reading a tool's signature is not running it, and a gate that refuses to
    describe a tool teaches nothing except that the tool exists.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "from relay.fleet_toolset import check as _fleet_check" in src
    i_help = src.index("sig = str(_inspect.signature(fn))")
    i_gate = src.index("_fleet_check(name)")
    assert i_help < i_gate, "the policy is consulted before the help branch"
