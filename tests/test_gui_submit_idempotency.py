# -*- coding: utf-8 -*-
"""GUI submit idempotency (ledger #10/#15).

Runs the real PowerShell ledger functions (scripts/win/gui_submit_ledger.ps1) in a temp dir, and
pins the cockpit's bounded adopt watcher and pending-add guard in source (a WPF window is not
driven by this suite).
"""
import io
import os
import re
import shutil
import tempfile

import pytest

from tools import childproc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, "scripts", "win", "gui_submit_ledger.ps1")
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _ps(script):
    r = childproc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                      timeout=60)
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("powershell") is None,
                    reason="runs the real PowerShell ledger; Windows PowerShell is not on this runner")
def test_unconfirmed_invocation_blocks_retry_until_confirmed():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "gui_submissions.json")
        out = _ps(". '%s'; $k = Get-GoalSetKey @('a','b'); "
                  "$k2 = Get-GoalSetKey @('a','c'); "
                  "$none = Get-RecentInvocation -Path '%s' -Key $k; "
                  "Add-SubmissionRecord -Path '%s' -Key $k; "
                  "$blocked = Get-RecentInvocation -Path '%s' -Key $k; "
                  "$other = Get-RecentInvocation -Path '%s' -Key $k2; "
                  "Remove-SubmissionRecord -Path '%s' -Key $k; "
                  "$after = Get-RecentInvocation -Path '%s' -Key $k; "
                  "Write-Output ('{0}|{1}|{2}|{3}' -f ($none -eq 0), ($blocked -gt 0), ($other -eq 0), ($after -eq 0))"
                  % ((LEDGER,) + (p,) * 6))
        assert out.endswith("True|True|True|True"), out


def _cs():
    import sys
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from tools import cockpit_source   # the cockpit source is split across ui/FleetCockpit*.cs
    return cockpit_source.read(encoding="utf-8", errors="replace")


def test_adopt_watcher_is_bounded():
    s = _cs()
    i = s.index("void WatchLiveAddHandoff")
    body = s[i:s.index("\n    }\n", i)]   # up to the method's closing brace (parts are reordered)
    assert "rescueCount >= 3" in body
    assert "TotalSeconds < 30.0" in body
    assert "!File.Exists(commandPath)" in body


def test_a_second_command_is_not_written_while_the_first_is_unconsumed():
    """The double-write guard survives, but keyed on IDENTITY, not content.

    It must defer writing a SECOND add_goal command while THIS UI's previous command is still
    sitting unconsumed in commands.d (the drain-lag race that turned 3 goals into 5 workers),
    and it must do so by checking whether that command's FILE still exists -- never by hashing
    the goal text.
    """
    s = _cs()
    body = s[s.index("void TryAddGoalsToLiveFleet"):s.index("int LiveAddReceiptState")]
    # The guard runs before the command is written, and it is the unconsumed-command check.
    assert body.index("HasUnconsumedAddCommand()") < body.index("SendTrackedCommand(")
    # Identity, by file existence of this UI's own prior command -- not by goal text.
    helper = s[s.index("bool HasUnconsumedAddCommand()"):]
    helper = helper[:helper.index("\n    }\n") + 6]
    assert "File.Exists(pp)" in helper
    assert "_pendingAddCommandPaths" in body


def test_the_live_add_guard_never_keys_on_goal_text():
    """Content resemblance must never stop a live add (owner instruction 2026-10-26/27). The
    guard must not hash the goal text: no AddGoalSetKey, no GoalKey(goal), no content-hash
    dictionary in the live-add path.
    """
    s = _cs()
    body = s[s.index("void TryAddGoalsToLiveFleet"):s.index("int LiveAddReceiptState")]
    # Judge CODE only: strip // line comments so historical prose mentioning the old hash in an
    # explanatory comment does not count as the guard still using it.
    code = "\n".join(ln.split("//", 1)[0] for ln in body.splitlines())
    assert "AddGoalSetKey" not in code, "the live-add guard still builds a goal-text hash key"
    assert "_pendingAddCommands" not in code, "content-hash dictionary guard still present"
    assert "GoalKey(" not in code, "the live-add guard still hashes goal text"
    # The goal-set content-hash helper must be gone from the cockpit source entirely (code lines).
    s_code = "\n".join(ln.split("//", 1)[0] for ln in s.splitlines())
    assert "static string AddGoalSetKey" not in s_code, "the goal-set content-hash helper was not removed"
