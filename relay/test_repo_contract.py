"""test_repo_contract.py -- the エージェント契約 block exists AND is wired into every goal.

Hermetic: imports only the two pure-Python modules, calls no browser / M365 / RAM probe,
and asserts on returned strings rather than grepping the source. A unit test that only
checked repo_contract_text() in isolation would leave the wiring unproven -- three
capabilities shipped green-and-wired-to-nothing before -- so the second half of this file
calls the composition helper that relay_fleet.RelayWorker.__init__ uses on line for line.
"""
from __future__ import annotations

from relay.repo_contract import repo_contract_text
from relay import relay_fleet


# The six standing clauses, each identified by a fragment that is unique to it. Kept as a
# list so a dropped clause names itself in the failure rather than collapsing the count.
_CLAUSES = [
    "隠離",                       # (1) isolate until done
    "git add -A",                # (2) explicit add, no add -A / add .
    "main にコミットしない",         # (3) no direct commit to main
    "ci.yml",                    # (4) register a new test in the CI list
    "Windows 専用",               # (5) no Windows-only test code
    "裏を取る",                    # (6) investigate a refutation before acting
]


def test_block_carries_every_clause():
    block = repo_contract_text()
    for fragment in _CLAUSES:
        assert fragment in block, "contract clause missing: %r" % fragment


def test_block_ends_with_a_separator_so_the_goal_starts_clean():
    # A leading section: it ends with a blank-line gap so a prepended goal begins on its
    # own line. (It is prepended, so it must NOT start with a gap.)
    block = repo_contract_text()
    assert block.endswith("\n\n")
    assert not block.startswith("\n")


def test_wrapper_prepends_the_contract_before_a_goal():
    goal = "relay/foo.py の bar() を直す"
    out = relay_fleet._with_repo_contract(goal)
    assert out.endswith(goal)            # the goal stays LAST and intact (suffix invariant)
    assert not out.startswith(goal)      # the contract was prepended, not appended
    assert out != goal                   # something was added
    for fragment in _CLAUSES:
        assert fragment in out


def test_wrapper_is_idempotent():
    goal = "relay/foo.py の bar() を直す"
    once = relay_fleet._with_repo_contract(goal)
    twice = relay_fleet._with_repo_contract(once)
    assert once == twice                 # header guard prevents a second copy


def test_wrapper_never_raises_on_empty():
    assert relay_fleet._with_repo_contract("") == ""
    assert relay_fleet._with_repo_contract(None) is None


def test_the_worker_composes_the_contract_in():
    # The wiring, not just the function: the composed_goal line must run the contract
    # wrapper. Reading the compiled function's source keeps this off a live browser while
    # still failing if the call is removed from __init__.
    import inspect
    src = inspect.getsource(relay_fleet.RelayWorker.__init__)
    assert "_with_repo_contract(" in src, "composed_goal no longer applies the contract"


def test_the_composed_body_still_ends_with_the_goal():
    # The suffix invariant the rest of relay_fleet depends on: _composed_prefix is the
    # composition MINUS the goal, taken by stripping the goal off the end, and the
    # replay / token-limit-recycle branches rebuild from that prefix. If the contract were
    # appended after the goal this would fail -- which is exactly what it is here to catch.
    goal = "MARKER-GOAL-タスク本文"
    w = relay_fleet.RelayWorker(goal, "w0")
    assert w._composed_goal.endswith(goal)
    assert w._composed_prefix + goal == w._composed_goal
    assert goal not in w._composed_prefix


# -- (c) the PROTOCOL prompt must be left alone -----------------------------------------------
#
# The task forbids adding the contract to copilot_autopilot_relay.PROTOCOL: a comment near
# line 314 records that a safety block appended to PROTOCOL's TAIL was measured NOT to be read
# (A/B 1665 vs 1801 chars, neither called skill_match), and a ~1500-char budget is stated.
# These tests pin PROTOCOL byte-for-byte and, at runtime, prove the contract went in as a
# PREFIX ahead of the goal rather than onto PROTOCOL's tail.

# Frozen from the current module; a change to PROTOCOL (intended or not) flips these and
# forces a deliberate re-pin rather than a silent edit.
_PROTOCOL_SHA256 = "a45b126857d377a01ab0478b449c093bd2d3b6a011659a4022b3e79ff1af82cd"
_PROTOCOL_LEN = 1401


def _protocol():
    from relay import copilot_autopilot_relay
    return copilot_autopilot_relay.PROTOCOL


def test_protocol_is_unchanged():
    import hashlib
    p = _protocol()
    assert len(p) == _PROTOCOL_LEN, "PROTOCOL length changed; re-pin only if intended"
    assert hashlib.sha256(p.encode("utf-8")).hexdigest() == _PROTOCOL_SHA256


def test_protocol_stays_within_its_budget():
    # The recorded budget is ~1500 chars; guard the ceiling so a future edit that pushes
    # PROTOCOL past it is caught here rather than by an agent that silently stops reading.
    assert len(_protocol()) <= 1500


def test_the_contract_is_not_in_protocol():
    # The block belongs on the goal body via _with_repo_contract, NOT inside PROTOCOL.
    from relay.repo_contract import CONTRACT_END
    assert CONTRACT_END not in _protocol()
    assert "エージェント契約" not in _protocol()


def test_protocol_still_leads_the_composed_message():
    # The runtime shape (relay_fleet builds PROTOCOL + composed_goal + ...): the contract
    # must sit AFTER PROTOCOL and BEFORE the goal, never on PROTOCOL's unread tail. Rebuild
    # the head of the initial message the way the worker does and check the ordering.
    p = _protocol()
    goal = "MARKER-GOAL-タスク本文"
    body = relay_fleet._with_repo_contract(goal)   # what composed_goal produces (no skill/theme match)
    initial = p + body
    i_proto = initial.index(p[:40])
    i_contract = initial.index("エージェント契約")
    i_goal = initial.index(goal)
    assert i_proto < i_contract < i_goal, "order must be PROTOCOL -> contract -> goal"
