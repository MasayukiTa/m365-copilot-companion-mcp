# -*- coding: utf-8 -*-
"""The Approval Center's "Recent decisions" list was one card per ANSWERED gate file, capped
at 20, newest first. Measured against the real gate directory on one machine
(ResolveGateDirectory's default, `%USERPROFILE%\\.companion_gates`): 2175 gate files existed, of
which 2169 were Skill approvals (`gate_skill_*`), and of THOSE, 2167 shared the exact same
skill name AND the exact same content digest -- one decision, answered thousands of times
(median 0.08s between asked_at and answered_at, i.e. re-affirmed against unchanged content
each time, not freshly reviewed). Only 20 of those ever fit under the cap, so the 20 shown were
almost certainly all copies of that single repeated decision -- an operator opening the panel
saw what looked like 3-ish distinct cards and had no way to tell there were thousands more
behind them, let alone anything that actually needed attention.

`job_approval_mode=bypass`/`auto` were the operator's suspicion, but reading
relay/task_router.py::job_gate shows those modes return ("ALLOW", "bypass"/"static check clean")
WITHOUT ever calling `_write_job_gate` -- no gate file is written for them at all, so there was
nothing from that path to hide. What actually floods the list is the SAME already-answered
decision recorded over and over (typically a Skill re-approved every time its 24h
`APPROVAL_TTL_SECONDS` challenge expires, against a bundle that never changed).

THE FIX, in `RefreshApprovalCenter()`: answered gates are folded by (kind, question, answer)
before the 20-card cap is applied, keeping only the newest instance of each distinct decision.
Nothing is deleted or rewritten on disk -- `.companion_gates/gate_*.json` remains the durable
log regardless of what the panel renders, so "what was auto-approved" stays answerable by
reading the directory even though the panel stops repeating it. A Skill's first approval, or
its re-approval after the bundle's content CHANGED, carries a different digest and therefore a
different `question` string (the digest is embedded in the question text itself) -- a different
key -- so first-time and post-change Skill approvals are never folded away by this change.

SOURCE ASSERTIONS: these read FleetCockpit.cs as text and cannot run the compiled control.
"""
from __future__ import annotations

import os
import re

UI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(UI, "FleetCockpit.cs")


def _executable(cs: str) -> str:
    """`cs` with // and /* */ comments removed, so the prose explaining this fix (which names
    every identifier asserted on below) cannot itself satisfy the assertions."""
    out, i, n = [], 0, len(cs)
    while i < n:
        if cs.startswith("//", i):
            j = cs.find("\n", i)
            i = n if j < 0 else j
        elif cs.startswith("/*", i):
            j = cs.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(cs[i]); i += 1
    return "".join(out)


def _src() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


SOURCE = _executable(_src())


def _method_body(name_snippet: str) -> str:
    """Brace-matched body of the method whose declaration contains `name_snippet`."""
    i = SOURCE.index(name_snippet)
    i = SOURCE.index("{", i)
    depth = 0
    for j in range(i, len(SOURCE)):
        if SOURCE[j] == "{":
            depth += 1
        elif SOURCE[j] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[i:j + 1]
    raise AssertionError("unbalanced braces reading %r" % name_snippet)


def _refresh_body() -> str:
    return _method_body("void RefreshApprovalCenter()")


def test_recent_decisions_are_collapsed_by_content_before_the_cap():
    """The regression: every answered gate became its own card, so thousands of copies of one
    decision could (and did) fill the entire 20-card window."""
    body = _refresh_body()
    assert "seenRecentKeys" in body, (
        "RefreshApprovalCenter no longer de-duplicates recent decisions -- re-derive this test "
        "if the mechanism was renamed")
    assert "new HashSet<string>(StringComparer.Ordinal)" in body
    # The de-dup must run BEFORE the 20-card slice, or it collapses a list that was already cut.
    dedup_pos = body.find("seenRecentKeys.Add(")
    limit_pos = body.find("int limit = Math.Min(20, recent.Count);")
    assert dedup_pos != -1 and limit_pos != -1
    assert dedup_pos < limit_pos, (
        "the de-dup runs after the 20-card cap is computed -- it must run first, or repeats "
        "still crowd out everything else inside the cap")


def test_the_dedup_key_carries_the_full_question_so_a_changed_digest_still_shows():
    """A Skill's approval question embeds its content digest (see relay/skills.py
    `_write_approval_gate`: "digest={skill.digest[:12]}"). Keying only on the Skill's name (or
    on kind alone) would fold a genuinely NEW approval -- first use, or re-approval after the
    bundle changed -- into an old one just because they share a name. Losing that is dangerous:
    it would hide the one card an operator actually needs to review."""
    body = _refresh_body()
    m = re.search(r'recentKey\s*=\s*([^;]+);', body)
    assert m, "no recentKey construction found in RefreshApprovalCenter"
    key_expr = m.group(1)
    assert 'S(gate, "question")' in key_expr, (
        "the de-dup key does not include the gate's question text -- without the embedded "
        "digest, a changed or first-time Skill approval could be folded into an old one")
    assert 'S(gate, "answer")' in key_expr, (
        "the de-dup key does not include the answer -- an approved and a denied decision for "
        "the same question must not collapse into a single card")
    assert "GateKind(gate)" in key_expr, (
        "the de-dup key does not include the gate kind -- unrelated kinds could collide")


def test_the_collapse_keeps_the_newest_instance_not_the_oldest():
    """`all` is sorted newest-first (see ReadAllGates's `Sort` by `asked_at` descending). The
    de-dup must keep the FIRST occurrence it sees in that order -- i.e. the most recent -- so
    the card shown is the freshest instance of the repeated decision, not a stale one."""
    body = _refresh_body()
    # HashSet.Add returns true only the first time a key is seen; recent.Add must be gated on
    # that same call, not on some separately-computed "is this the last one" condition.
    assert re.search(r'if\s*\(seenRecentKeys\.Add\(recentKey\)\)\s*recent\.Add\(gate\);', body), (
        "recent.Add(gate) is not gated directly on HashSet.Add(recentKey) -- the ordering "
        "guarantee (newest instance wins) depends on that exact pairing")


def test_pending_gates_are_never_folded_by_the_collapse():
    """Only ANSWERED gates go through the de-dup. A still-open request must always reach the
    operator, however many times an identical question was asked before."""
    body = _refresh_body()
    m = re.search(r'if\s*\(!GateAnswered\(gate\)\)\s*\{\s*pending\.Add\(gate\);\s*continue;\s*\}', body)
    assert m, (
        "unanswered gates no longer short-circuit straight to pending before the de-dup key is "
        "built -- a pending (not yet decided) request must never be collapsed away")


def test_the_collapse_never_touches_the_gate_files_on_disk():
    """The underlying gate_*.json is the durable log ("あとで何が自動承認されたか追える
    必要がある"). This method may only decide what to RENDER -- it must never delete or rewrite
    a gate file, or the log the operator asked to keep would disappear along with the card."""
    body = _refresh_body()
    for forbidden in ("File.Delete", "Directory.Delete", "File.WriteAllText", "File.Move"):
        assert forbidden not in body, (
            "RefreshApprovalCenter calls %s -- collapsing duplicate CARDS must never mutate or "
            "remove the on-disk gate record" % forbidden)


def test_bypass_and_auto_local_jobs_never_reach_the_gate_directory_at_all():
    """Confirms, by reading the actual writer, that `job_approval_mode=bypass`/`auto` cannot
    be the source of the flood this fix addresses: those modes return ALLOW without ever
    writing a gate file, so RefreshApprovalCenter (which only ever reads
    `.companion_gates/gate_*.json`) never sees them in the first place."""
    router_path = os.path.join(UI, "..", "relay", "task_router.py")
    with open(router_path, encoding="utf-8") as fh:
        router = fh.read()
    assert 'if mode == "bypass":' in router, (
        "job_gate's bypass fast-path changed shape -- re-derive this test against the current "
        "behavior before assuming bypass gates ever reach the approval directory")
    bypass_block = router[router.index('if mode == "bypass":'):router.index('level, why = _static_risk')]
    assert 'return "ALLOW", "bypass"' in bypass_block, (
        "bypass mode no longer returns ALLOW unconditionally -- re-derive this test")
    assert '_write_job_gate' not in bypass_block, (
        "bypass mode now writes a gate file on some path -- RefreshApprovalCenter's de-dup "
        "(keyed on content, not on policy mode) may need a policy-aware exception to keep "
        "hiding it from history")
    # In "auto" mode a clean payload also returns ALLOW without reaching _write_job_gate.
    auto_block = router[router.index('if mode == "auto":'):router.index('# mode == "default"')]
    assert '_write_job_gate' not in auto_block, (
        "auto mode now writes a gate file on some path -- if that path is answered "
        "automatically, RefreshApprovalCenter's de-dup (keyed on content, not on policy mode) "
        "may need a policy-aware exception to keep hiding it from history")
