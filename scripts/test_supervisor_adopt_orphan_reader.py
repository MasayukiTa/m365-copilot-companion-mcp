# -*- coding: utf-8 -*-
r"""The supervisor ADOPTS a live orphan reader instead of starting a second one.

THE INCIDENT (measured 2026-11-11/12, %TEMP%\m365-companion-supervisor.log). Supervisor
pid 25644 died at 23:58:02 (0xC00000FD) but its reader (pid 1144, python -m
relay.tunnel_host_log) and that reader's devtunnel host (pid 23400, alive since 22:52)
stayed alive as ORPHANS -- a reader whose parent supervisor is gone. At 00:29:46
start_all.ps1 -CoreOnly started a NEW supervisor; at 00:32:04 it logged
"tunnel host connection looks stale ... re-hosting" and "reader pid 33632 still running"
next to "reader pid 1144 still running": two Dev Tunnel host readers for one tunnel at
once, the state commit 6cfabfd9 forbids. 23 minutes later the token cache was 0 bytes.

WHAT IS PINNED HERE
  * the PURE decider (scripts/supervisor_host_launch.decide_orphan_adoption): no reader ->
    "launch"; a reader we already track -> "skip"; a live reader whose parent supervisor is
    gone and which we do not track -> "adopt"; a live reader with a live parent -> "skip".
    An unknown parent-alive value is treated as orphan ONLY for None/False; any other value
    reads as still-owned, so a reader is never seized from a live supervisor. Exercised on
    every platform.
  * supervisor.ps1 WIRES it: Get-ExistingTunnelReaders carries ParentPid; Adopt-OrphanTunnelReader
    reaches the decider the cross-language way the other gates do
    (& $Py -c ... supervisor_host_launch.decide_orphan_adoption) and, on "adopt", tracks the
    reader as $script:TunnelHostProc without launching a second one; Start-TunnelHost calls
    Adopt-OrphanTunnelReader before any launch and returns when it adopts.
  * start_all.ps1 -CoreOnly starts no reader itself and defers the orphan adoption to the
    supervisor it brings up (documented at the -CoreOnly return).

HOW THIS RUNS. The pure table needs no PowerShell and runs on Linux CI. The source-wiring
checks only read the two .ps1 files as text (no dot-source: supervisor.ps1 is a forever loop
with a machine-wide mutex and a live supervisor on the dev machine), so they too run on every
platform.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import supervisor_host_launch as H  # noqa: E402

SUPERVISOR_PS1 = os.path.join(REPO, "scripts", "supervisor.ps1")
START_ALL_PS1 = os.path.join(REPO, "scripts", "start_all.ps1")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract_braced_block(text, start_marker):
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting block at %r" % (start_marker,))


# -- every platform: the pure decider ---------------------------------------------------------

#: (reader_alive, parent_supervisor_alive, we_track_it) -> expected word.
_TABLE = [
    # No reader: there is nothing to adopt, the launch path owns this.
    (False, None, False, "launch"),
    (False, False, False, "launch"),
    (False, True, False, "launch"),
    (False, True, True, "launch"),   # tracked flag is irrelevant with no reader
    # A reader we ALREADY track: never adopt again, never duplicate.
    (True, None, True, "skip"),
    (True, False, True, "skip"),
    (True, True, True, "skip"),
    # A live reader we do NOT track, parent supervisor GONE -> adopt it.
    (True, None, False, "adopt"),    # parent cannot be told to be alive -> orphan
    (True, False, False, "adopt"),   # parent positively dead -> orphan
    # A live reader we do NOT track, parent supervisor ALIVE -> leave it to that supervisor.
    (True, True, False, "skip"),
]


@pytest.mark.parametrize("alive,parent,track,expected", _TABLE)
def test_the_pure_orphan_decider(alive, parent, track, expected):
    assert H.decide_orphan_adoption(alive, parent, track) == expected


def test_only_none_or_false_parent_counts_as_orphan():
    # The orphan path must fire only on a POSITIVE "no live parent" (None or False). Any other
    # value (a truthy non-bool, a string) must read as still-owned so a reader is never seized
    # from a supervisor whose liveness could not be read as gone.
    assert H.decide_orphan_adoption(True, None, False) == "adopt"
    assert H.decide_orphan_adoption(True, False, False) == "adopt"
    assert H.decide_orphan_adoption(True, True, False) == "skip"
    assert H.decide_orphan_adoption(True, "unknown", False) == "skip"
    assert H.decide_orphan_adoption(True, 1, False) == "skip"


def test_a_tracked_reader_is_never_adopted_even_if_orphaned():
    # If we already track it, the answer is skip regardless of parent state -- we do not re-adopt
    # our own reader, and we certainly do not launch a second one.
    assert H.decide_orphan_adoption(True, None, True) == "skip"
    assert H.decide_orphan_adoption(True, False, True) == "skip"


# -- every platform: the supervisor wires it (source text, no dot-source) ----------------------

def test_get_existing_tunnel_readers_carries_the_parent_pid():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Get-ExistingTunnelReaders")
    assert "ParentPid" in block, \
        "Get-ExistingTunnelReaders must carry each reader's ParentPid so an orphan (parent gone) " \
        "can be told from a reader a live supervisor still owns"
    assert "ParentProcessId" in block, \
        "ParentPid must come from the CIM ParentProcessId already available on the process record"


def test_adopt_function_reaches_the_pure_decider_the_cross_language_way():
    src = _read(SUPERVISOR_PS1)
    assert "supervisor_host_launch.decide_orphan_adoption(" in src, \
        "the adopt gate must reach the pure decider the cross-language way (& $Py -c ...), so " \
        "tools/unreached.py credits the caller and the two cannot drift"
    assert "function Invoke-OrphanAdoptionDecision" in src
    assert "function Adopt-OrphanTunnelReader" in src


def test_adopt_tracks_the_reader_without_launching_a_second():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Adopt-OrphanTunnelReader")
    # On an adopt it must take ownership (track the reader) ...
    assert "$script:TunnelHostProc = $proc" in block, \
        "adopting must track the orphan reader as ours ($script:TunnelHostProc)"
    # ... and it must NOT start a reader itself (no Start-Process in the adopt function).
    assert "Start-Process" not in block, \
        "Adopt-OrphanTunnelReader must never launch a reader -- adoption is the alternative to launching"
    # ... and it must not kill/re-host the existing live host.
    assert "Stop-Process" not in block, \
        "adoption must leave the existing live host untouched (no kill)"


def test_start_tunnel_host_adopts_before_it_launches():
    src = _read(SUPERVISOR_PS1)
    block = _extract_braced_block(src, "function Start-TunnelHost")
    assert "Adopt-OrphanTunnelReader" in block, \
        "Start-TunnelHost must try to adopt a live orphan reader before launching anything"
    # The adopt call must appear before the first reader launch (Start-Process) in the function.
    adopt_at = block.index("Adopt-OrphanTunnelReader")
    launch_at = block.index("Start-Process")
    assert adopt_at < launch_at, \
        "the adopt attempt must come before Start-TunnelHost's own reader launch"


def test_core_only_defers_orphan_adoption_to_the_supervisor():
    # start_all.ps1 -CoreOnly starts no reader itself; the orphan adoption is the supervisor's
    # job. The -CoreOnly path documents that so the intent is pinned where the behaviour lives.
    src = _read(START_ALL_PS1)
    assert "Adopt-OrphanTunnelReader" in src, \
        "start_all.ps1 -CoreOnly must document that the supervisor it starts adopts a live orphan " \
        "reader rather than a second reader being started"
    assert "decide_orphan_adoption" in src
