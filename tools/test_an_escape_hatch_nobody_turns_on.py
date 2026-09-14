# -*- coding: utf-8 -*-
"""An override that redirects a production store away from the operator, which nothing sets.

WHAT HAPPENED. relay/skills.py writes skill-approval questions into the operator's approval
queue. Its own docstring records the discovery and the repair:

    THE ONE OF THE THREE WITH NO ESCAPE HATCH, AND IT LEAKED FOR MONTHS. ... Measured
    2026-09-07: 378 pending questions in ~/.companion_gates, every single one naming a pytest
    temp directory, none naming a skill that exists, 202 already past their 24h TTL.
    ... MCP_SKILLS_GATE_DIR mirrors MCP_SKILLS_STATE_DB so the isolation a test already asks
    for actually covers the third thing this object writes.

The knob was added. **Nothing ever turned it on.** conftest set MCP_GATE_DIR -- a different
door to the same queue -- and not this one. Measured 2026-09-14, a week later: 2,174 files in
the live queue, 187 written that day, 11 per run across 17 runs, every one still naming a pytest
temp directory. The owner was getting desktop notifications for approvals that did not exist.

WHY A TEST AND NOT A LINE IN A CHECKLIST. The failure is not that somebody forgot; it is that
forgetting was invisible. A knob and its switch live in different files, the knob's own tests
pass either way, and the evidence of the leak is in a directory outside the repository. Nothing
in the ordinary loop of writing code and running tests could have shown it.

WHY IT SCANS RATHER THAN LISTS. A hand-maintained list of variables would fail open exactly the
way the redirect table did before relay/test_live_record_isolation.py walked the source for it:
the next override added is the one nobody adds to the list. So this reads the production source
for the overrides that exist, and requires each to be answered.

WHAT IT DOES NOT CLAIM. A variable set here is not proof that the isolation works -- only that
somebody decided. Whether a test can still reach the live store is the other file's question,
and the two together are what the evidence for either one rests on.
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: The shape of a name that selects WHERE something is stored. An override called
#: MCP_CAPTURE_LEAN or MCP_FLEET_AGENT_URL changes behaviour, not location, and is not this
#: file's business.
_LOCATION = re.compile(r"(?:_DIR|_PATH|_FILE|_DB|_LEDGER|_STORE)$")
_OURS = ("MCP_", "FLEET_", "COMPANION_")

#: Overrides that select a location and are deliberately NOT set for the suite. Each line is a
#: claim that a test run cannot reach the operator's copy through it, and each was measured.
#:
#: MCP_EDGE_PROFILE IS NOT HERE, and the reason is a caught mistake worth keeping. The first
#: draft listed it, because the first draft's pattern was `(DIR|PATH|FILE|...)$` -- and
#: "PROFILE" ends in "FILE". It is a profile NAME, not a location, and requiring the underscore
#: (`_FILE$`) drops it. The stale-entry test below is what said so.
ANSWERED_ELSEWHERE = {
    "MCP_SETTLE_TRACE_PATH":
        "the constant it defaults into (copilot_autopilot_relay._SETTLE_TRACE_PATH) is already "
        "redirected by conftest.LIVE_RECORD_REDIRECTS, and settle_collect refuses to run at all "
        "without the variable (SystemExit), so neither reader can reach the live file",
}


def _tracked_python():
    out = subprocess.check_output(["git", "ls-files"], cwd=REPO).decode("utf-8", "replace")
    for rel in out.splitlines():
        if not rel.endswith(".py"):
            continue
        base = os.path.basename(rel)
        if base.startswith("test_") or base.endswith("_test.py") or rel == "conftest.py":
            continue
        yield rel


def location_overrides():
    """{VARIABLE: {files that read it}} for every location-selecting override in production.

    By the AST, not by the spelling: a substring scan finds the variable named in a comment
    explaining why it was added, which is precisely the kind of evidence that reads as proof
    and is not.
    """
    found = {}
    for rel in _tracked_python():
        try:
            tree = ast.parse(io.open(os.path.join(REPO, rel), encoding="utf-8").read(),
                             filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if getattr(func, "attr", "") != "get":
                continue
            if getattr(getattr(func, "value", None), "attr", "") != "environ":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            name = node.args[0].value
            if not isinstance(name, str) or not name.startswith(_OURS):
                continue
            if _LOCATION.search(name):
                found.setdefault(name, set()).add(rel)
    return found


def _conftest_sets():
    """The variables conftest hands a value to, read from its source rather than from os.environ
    -- the environment already carries them by the time this test runs, which would make the
    check pass by observing its own side effect."""
    src = io.open(os.path.join(REPO, "conftest.py"), encoding="utf-8").read()
    tree = ast.parse(src, filename="conftest.py")
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("setdefault",
                                                                             "__setitem__"):
            if node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)):
                    names.add(t.slice.value)
    return names


# ── the property ──────────────────────────────────────────────────────────────────────────

def test_every_location_override_is_either_set_or_answered():
    found = location_overrides()
    conftest = _conftest_sets()
    unanswered = sorted(n for n in found
                        if n not in conftest and n not in ANSWERED_ELSEWHERE)
    assert not unanswered, (
        "these redirect a production store away from the operator and NOTHING turns them on. "
        "An escape hatch nobody sets is not an escape hatch -- MCP_SKILLS_GATE_DIR sat like "
        "this for a week and put 11 approval questions per test run into the owner's live "
        "queue. Set each in conftest, or add it to ANSWERED_ELSEWHERE with the measurement "
        "that says a test cannot reach the live copy through it:\n  "
        + "\n  ".join("%s  (read by %s)" % (n, ", ".join(sorted(found[n]))) for n in unanswered))


def test_the_one_that_caused_this_is_set():
    """PINNED BY NAME. The scan above would go quiet if relay/skills.py stopped reading the
    variable -- which is one of the ways the leak could come back."""
    assert "MCP_SKILLS_GATE_DIR" in location_overrides(), (
        "relay/skills.py no longer honours MCP_SKILLS_GATE_DIR; check where its approval "
        "questions go now")
    assert "MCP_SKILLS_GATE_DIR" in _conftest_sets()


def test_the_scan_can_see_a_known_override():
    """A scan that finds nothing passes every assertion above. Three that certainly exist."""
    found = location_overrides()
    for name in ("MCP_GATE_DIR", "MCP_SKILLS_GATE_DIR", "FLEET_STATE_DIR"):
        assert name in found, "the override scan is broken: it cannot see %s" % name


def test_a_behaviour_flag_is_not_mistaken_for_a_location():
    """The other half: a scan that reported every MCP_* variable would collect dozens of flags,
    and the list would be maintained by adding exemptions until it meant nothing."""
    found = location_overrides()
    for flag in ("MCP_CAPTURE_LEAN", "MCP_TOOL_MAP", "MCP_EXECUTION_PROFILES",
                 "MCP_FLEET_AGENT_URL", "MCP_API_KEY",
                 # A NAME THAT ENDS IN A LOCATION WORD AND IS NOT ONE. MCP_EDGE_PROFILE selects
                 # a profile name; it was collected by a first draft whose pattern ended
                 # `FILE$`, because PROFILE does. The underscore is what tells them apart.
                 "MCP_EDGE_PROFILE"):
        assert flag not in found, "%s is a flag, not a location" % flag


def test_every_answered_entry_still_exists():
    """A stale exemption is an answer to a question nobody is asking, and it reads as coverage."""
    found = location_overrides()
    stale = sorted(n for n in ANSWERED_ELSEWHERE if n not in found)
    assert not stale, "listed as answered but no longer read anywhere: %s" % (stale,)
