# -*- coding: utf-8 -*-
"""GUI submit idempotency (ledger #10/#15).

Runs the real PowerShell ledger functions (scripts/win/gui_submit_ledger.ps1) in a temp dir, and
pins the cockpit's bounded adopt watcher and pending-add guard in source (a WPF window is not
driven by this suite).
"""
import io
import os
import re
import tempfile

from tools import childproc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, "scripts", "win", "gui_submit_ledger.ps1")
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _ps(script):
    r = childproc.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                      timeout=60)
    return r.stdout.strip()


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


def test_duplicate_pending_add_is_not_written_twice():
    s = _cs()
    body = s[s.index("void TryAddGoalsToLiveFleet"):s.index("int LiveAddReceiptState")]
    assert body.index("_pendingAddCommands.TryGetValue") < body.index("SendTrackedCommand(")
    assert "File.Exists(pendingPath)" in body
