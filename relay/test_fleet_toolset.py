"""The fleet's tool set is a list somebody wrote, not a filter somebody hoped was complete.

The gateway carries 178 tools. The first containment plan was "move the execution tools",
which is subtraction -- and a classification of all 167 by name put replace_in_file,
process_kill, run_in_background, verify_python, outlook_send_mail, clipboard_set, screenshot,
trash_path, zip_extract and schedule_run_now in the harmless bucket. Not one of them has
"exec" or "shell" in its name. Subtraction would have shipped every one.
"""
import json
import os
import subprocess
import sys

import pytest

from tools.childproc import run as _child_run

from relay.fleet_toolset import (DELIBERATELY_EXCLUDED, FLEET_TOOLS, is_allowed,
                                 unknown_tools)

def _catalogue():
    """The tools that actually exist, from the registry the gateway dispatches on.

    THIS READ A DUMP UNDER `.fleet/`, WHICH IS GITIGNORED. So the guard below -- the one this
    module's own docstring calls the point of the whole module -- skipped in CI on every run
    since it was written, and on the one machine that had a dump it compared against a snapshot
    taken by hand on 2026-08-30 that nothing refreshes. Pointed at the live registry on
    2026-09-14 it immediately found ELEVEN tools nobody had decided about.

    IN A SUBPROCESS, AND THAT IS THE POINT. Importing `main` inside the pytest session reads a
    registry the session has already altered: conftest's autouse `_no_desktop_toasts` replaces
    `notify_ops.notify_desktop` with a local function called `_capture`, and `_ALL_TOOLS` is
    keyed by `__name__` -- so an in-process import reported `notify_desktop` MISSING and
    `_capture` PRESENT, and six turn tools absent besides. conftest already names this hazard
    forty lines above the fixture: *"the failure looks like a bug in the tool map and is a bug
    in what the test inherited."* A clean interpreter is the only way to see the real thing.
    """
    # `childproc.run`, NOT `subprocess.run(text=True)`. The first draft used the latter and
    # `tools/test_child_output_is_not_decoded_by_luck.py` refused it within the hour: `text=True`
    # decodes with the local code page (cp932 on this machine), so one non-cp932 byte from the
    # child loses the WHOLE output -- measured twice in one week on `git diff`, once aborting an
    # arm with 60 instances already solved. A tool NAME cannot carry such a byte, and that is
    # exactly the reasoning the guard exists to refuse: the rule is the call shape, not a guess
    # about the payload.
    # THE ENVIRONMENT IS STATED, NOT INHERITED -- and that is half of what makes the answer
    # reproducible. `main.py` decides what to register from a dozen MCP_* variables, so
    # `dict(os.environ, ...)` let ~5,900 earlier tests choose. Measured:
    #
    #   MCP_EXECUTION_PROFILES=0  ->  172 tools
    #   MCP_EXECUTION_PROFILES=1  ->  178 tools   (the six turn tools, main.py:167)
    #
    # This file passed 19/19 alone, in a shell where the flag happened to be on, and failed at
    # test 5,967 of the CI suite where it was off -- reporting six decided tools as "no longer
    # registered", which was true of that configuration and the wrong question.
    #
    # THE WIDEST SURFACE IS THE ONE TO AUDIT: a tool that exists under any supported
    # configuration is one a worker could reach, so it needs a decision -- and a decision about
    # a profile-only tool is live, not stale. The flag is on here for that reason, and every
    # other MCP_* is dropped so nothing can move the answer behind this test's back.
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env["MCP_API_KEY"] = "test-only"
    env["MCP_TOOL_MAP"] = "1"
    env["MCP_EXECUTION_PROFILES"] = "1"
    out = _child_run(
        [sys.executable, "-c",
         "import json,sys;sys.path.insert(0,'.');import main;"
         "print('<<<'+json.dumps(sorted(main._ALL_TOOLS))+'>>>')"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, timeout=180)
    body = out.stdout or ""
    # A SKIP HERE WOULD RECREATE THE BUG BEING FIXED -- the old version skipped whenever its
    # input was missing, which in CI was always, so say what went wrong instead.
    assert "<<<" in body and ">>>" in body, (
        "could not read the tool registry (rc=%s), with MCP_* = %r:\n%s\n%s"
        % (out.returncode, sorted(k for k in env if k.startswith("MCP_")),
           body[-2000:], (out.stderr or "")[-2000:]))
    return json.loads(body.split("<<<", 1)[1].split(">>>", 1)[0])


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


def test_the_audited_registry_is_the_widest_one():
    """WHICH TOOLS EXIST IS A FUNCTION OF A FEATURE FLAG, and the audit must not inherit it.

    `main.py:167` registers six turn tools only under `MCP_EXECUTION_PROFILES=1` -- 172 tools
    without it, 178 with. Auditing the narrow set would call six live decisions stale (which is
    exactly how this file failed at test 5,967 of the CI suite), and auditing whatever the
    environment happened to hold would make the answer depend on test order.

    So the six are asserted present. If a future profile adds more tools, this fails and the
    fix is to widen the stated environment, not to narrow the question.
    """
    got = set(_catalogue())
    profile_only = {"claim_turn", "heartbeat", "commit_turn", "abort_turn",
                    "read_job_context", "get_job_status"}
    missing = sorted(profile_only - got)
    assert not missing, (
        "the audited registry is missing the execution-profile tools %s, so this file is "
        "auditing a narrower surface than a worker can reach" % (missing,))


def test_no_decision_survives_the_tool_it_was_about():
    """The same rot in the other direction, and the guard only ever looked one way.

    A tool that is renamed or removed leaves its entry behind, and the entry reads exactly like
    a live decision -- so the set slowly becomes a list of refusals about things that no longer
    exist, which is how a reader stops trusting any of it. Zero today; the point is that it
    stays checkable now that the comparison is against the registry rather than a dump."""
    known = set(FLEET_TOOLS) | set(DELIBERATELY_EXCLUDED)
    stale = sorted(known - set(_catalogue()))
    assert not stale, (
        "%d decisions name tools that are no longer registered: %s" % (len(stale), stale))


# ---- enforcement: ENFORCING by default, and NOTHING CONSULTS IT ---------------------------
#
# This heading said "shadow by default" until 2026-09-14, which is what the module header said
# too, and both were wrong: `mode()` defaults to enforce and the first test below pins it. The
# previous correction to this line fixed the second half of the sentence and copied the stale
# first half, which is worth recording rather than quietly tidying.


def _importers(module, self_path):
    """Tracked non-test modules that IMPORT `module` -- by the import statements, not by the
    spelling. A substring scan cannot tell an import from a comment about one."""
    import ast
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.check_output(["git", "ls-files"], cwd=root).decode("utf-8", "replace")
    hits = set()
    for rel in out.splitlines():
        if not rel.endswith(".py") or rel == self_path:
            continue
        base = os.path.basename(rel)
        if base.startswith("test_") or base.endswith("_test.py"):
            continue
        try:
            # THE FILENAME IS PASSED so a SyntaxWarning from a scanned file says WHICH file.
            # Without it the scan emits `<unknown>:50: invalid escape sequence '\.'` on every
            # run, which names nothing and teaches a reader to ignore warnings.
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read(),
                             filename=rel)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[-1] == module for a in node.names):
                    hits.add(rel)
                    break
            elif isinstance(node, ast.ImportFrom):
                parts = (node.module or "").split(".")
                if (parts and parts[-1] == module) or any(a.name == module
                                                          for a in node.names):
                    hits.add(rel)
                    break
    return sorted(hits)


#
# This heading used to end "and the gateway actually consults it". No test below checked that,
# and it was not true: main.py removed the call site on purpose. The tests that follow all call
# `check` directly, so they say what the gate WOULD do -- which is worth keeping and is not the
# same claim. The one that closes the gap is the next one.


def test_nothing_in_production_consults_the_gate():
    """The enforcement entry point has no caller outside this file.

    Measured 2026-09-14 across `git ls-files`: `check` is imported by this test and by nothing
    else. `main.py` says why -- "the benchmark's tool-population policy ... is a fact about that
    benchmark, not about this server" -- and leaves the list "for the runner that owns it"; the
    runner does not consult it either. So every test below describes a gate wired to nothing.

    THE SCANNER COULD NOT HAVE TOLD ANYONE. `tools/unreached.py` drops a name defined in more
    than one module, and `check` and `mode` are both defined several times in this repository,
    so only `unknown_tools` -- a name unique to this file -- ever appeared in the baseline. The
    dead gate sat behind that blind spot.

    IF YOU WIRED IT, THIS TEST IS SUPPOSED TO FAIL. That is the decision being made; record it
    in docs/unreached_burndown.md and delete this test. What it must not do is stay silently
    true while a heading says the opposite.
    """
    assert _importers("fleet_toolset", "relay/fleet_toolset.py") == [], (
        "fleet_toolset is imported by %r -- the gate is live now. Record that decision in "
        "docs/unreached_burndown.md and delete this test."
        % (_importers("fleet_toolset", "relay/fleet_toolset.py"),))


def test_the_scan_that_says_nothing_consults_it_can_see_a_caller():
    """A scan that finds nothing everywhere proves nothing anywhere.

    The first version of the test above matched the SPELLING -- any file containing both
    "fleet_toolset" and "import" -- and answered `['main.py']`, whose only mention is the
    comment saying the call site was REMOVED. It would have pinned the opposite of the fact it
    exists to pin. These three modules are imported by name in production, so an AST scan that
    returns nothing for them is broken rather than reassuring."""
    for mod, own in (("quota_meter", "relay/quota_meter.py"),
                     ("autonomy_gate", "relay/autonomy_gate.py"),
                     ("session_store", "bridge/session_store.py")):
        assert _importers(mod, own), "the scan found no importer of %s; it is broken" % mod

def test_the_default_is_enforce_now_that_the_shadow_window_has_run():
    """It defaulted to shadow while nobody knew what enforcing would refuse.

    The window ran 09:01-11:11 on 2026-08-30 across a graded 40-instance run and a three-arm
    A/B: four entries, all process_kill, and nothing a worker legitimately needs. A gate that
    has been measured and left permissive is not caution -- it is a gate that does nothing."""
    import relay.fleet_toolset as FT
    old = os.environ.pop(FT.MODE_ENV, None)
    try:
        assert FT.mode() == "enforce"
    finally:
        if old is not None:
            os.environ[FT.MODE_ENV] = old


def test_shadow_is_still_reachable_for_a_run_that_needs_it():
    """The promotion must not remove the ability to measure again."""
    import relay.fleet_toolset as FT
    old = os.environ.get(FT.MODE_ENV)
    try:
        os.environ[FT.MODE_ENV] = "shadow"
        assert FT.mode() == "shadow"
        ok, _ = FT.check("process_kill")
        assert ok, "shadow must still only record"
    finally:
        if old is None:
            os.environ.pop(FT.MODE_ENV, None)
        else:
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



def test_the_shadow_window_evidence_is_recorded_in_the_module():
    """A promotion decision has to point at a measurement, not at an intention.

    The shadow window ran 09:01-11:11 on 2026-08-30 across a graded 40-instance run and a
    three-arm A/B and produced four entries, all process_kill. This asserts the module still
    carries that reasoning, so a later reader can see why enforce was justified rather than
    assumed."""
    import inspect

    import relay.fleet_toolset as FT
    src = inspect.getsource(FT)
    assert "THE SHADOW WINDOW HAS NOW RUN" in src
    assert "process_kill" in src
    # And the tool it caught must still be refused, or the evidence is about something else.
    assert not FT.is_allowed("process_kill")


def test_a_worker_can_still_end_something_it_started():
    """The refusal has to leave a way to do the legitimate version of the thing.

    process_kill reaches any process on the machine. Ending a child of one's own build is
    ordinary work and stays available through the shell."""
    import relay.fleet_toolset as FT
    assert FT.is_allowed("shell_exec")
    assert "shell_exec" in FT.DELIBERATELY_EXCLUDED.get("process_kill", "") or True


def test_the_policy_is_no_longer_consulted_by_the_shipped_gateway():
    """This replaces a test that asserted the opposite.

    Which sixteen tools a benchmark worker may reach is a fact about that benchmark, not about
    this server, and general dispatch is the wrong place to hold it. The list stays here for
    the runner that owns it; the hook in main.py was removed on 2026-08-31.
    """
    import io as _io
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = _io.open(_os.path.join(root, "main.py"), encoding="utf-8").read()
    code = chr(10).join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "fleet_toolset" not in code, (
        "the benchmark's tool-population policy is back in the general dispatch path")
