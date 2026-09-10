# -*- coding: utf-8 -*-
"""The cockpit that is RUNNING has to be the cockpit that was written.

THE DEFECT THIS EXISTS FOR (codex-plan item 1, measured 2026-09-11).
`ui/FleetCockpit.cs` archives four fields that let an archived row be joined back to the run
that produced it -- verified, verify_attempts, run_id and, since commit 8447cbf, jid. Every one
of them is in the source. In `.fleet/history.json`, across all 215 rows:

    run_id  non-empty in 170 rows
    jid     non-empty in   0 rows

Cause, proved at the binary rather than inferred from a timestamp: the deployed
`ui/FleetCockpit.exe` contains the UTF-16 literal "verified" 4 times, "verify_attempts" twice,
"run_id" once, and **"jid" zero times**. Recompiling the current source produces a binary that
contains it once. The build was simply never re-run after the source change -- the exe predates
it by about four and a half hours.

WHY THE EXISTING GUARD COULD NOT CATCH IT. `relay/test_verification_survives_the_archive.py`
pins this boundary with `test_both_archive_sites_carry_the_field`, which counts regex hits in
`ui/FleetCockpit.cs`. It is green, and it was green for every one of those 215 rows, because a
source assertion answers "was it written" and the question is "is it running". A compiled
artifact is exactly where those two come apart, and the whole point of this boundary is that
the archive is produced by a SECOND PROGRAM -- the one place in this repo where "the code says
so" is not evidence about behaviour.

WHY THERE ARE TWO CHECKS. The literal check is specific and reads well when it fails, but it
only sees fields that introduce a NEW string; a changed VALUE for an existing field would slip
past it. The staleness check needs no knowledge of the change at all -- an artifact older than
its sources cannot contain them, whatever the change was. The same reasoning the verifier
already uses for IMPOSSIBLY_FAST_S: a timestamp is evidence no matter what the content says.

SKIPPED, NOT FAILED, WHERE THERE IS NO BINARY. ui/*.exe is gitignored, so CI (and any fresh
checkout) has nothing to inspect. A skip there is honest: the question cannot be asked. It is
asked on the machine that actually runs the cockpit, which is the only machine where a stale
build can hurt anything.
"""
from __future__ import annotations

import os
import re

import pytest

_UI = os.path.dirname(os.path.abspath(__file__))
_EXE = os.path.join(_UI, "FleetCockpit.exe")

#: The .cs files build_cockpit.bat / rebuild_ui.ps1 compile into FleetCockpit.exe. Read from the
#: build scripts' own list rather than globbed, so a new source file added to the build is a
#: deliberate edit here and an unrelated .cs in ui/ does not make every run look stale.
_SOURCES = ("FleetCockpit.cs", "SelfImproveDashboard.cs", "Theme.cs")

#: The archive writer's own assignments: `e["<name>"] = ...` inside FleetCockpit.cs. Derived
#: from the source instead of hand-listed so a field added tomorrow is covered without anyone
#: remembering this file exists.
_ARCHIVE_FIELD = re.compile(r'e\["([a-z_]+)"\]\s*=')

#: Fields whose name is built at runtime or that the writer copies wholesale -- a literal for
#: them is not expected in the binary and their absence proves nothing.
_NOT_LITERALS = frozenset()


def _skip_without_a_binary():
    if not os.path.isfile(_EXE):
        pytest.skip("ui/FleetCockpit.exe is not present (it is gitignored); there is no "
                    "deployed artifact on this machine to compare against its source")


def _utf16_count(blob: bytes, token: str) -> int:
    """.NET stores string literals as UTF-16LE, so that is what to look for."""
    return blob.count(token.encode("utf-16-le"))


def _archive_fields() -> set:
    with open(os.path.join(_UI, "FleetCockpit.cs"), encoding="utf-8", errors="replace") as fh:
        return set(_ARCHIVE_FIELD.findall(fh.read())) - _NOT_LITERALS


def test_the_source_still_names_the_join_keys():
    """A cheap precondition: if these ever leave the source, the two tests below would pass by
    having nothing to look for, which is the quiet way a guard stops guarding."""
    fields = _archive_fields()
    for name in ("verified", "verify_attempts", "run_id", "jid"):
        assert name in fields, (
            "%r is no longer archived by ui/FleetCockpit.cs; codex-plan item 1 depends on it "
            "to join an archived row back to the run that produced it" % name)


def test_every_archived_field_is_present_in_the_deployed_binary():
    """The literal check. Fails today with exactly one name: jid."""
    _skip_without_a_binary()
    with open(_EXE, "rb") as fh:
        blob = fh.read()
    missing = sorted(f for f in _archive_fields() if _utf16_count(blob, f) == 0)
    assert not missing, (
        "ui/FleetCockpit.exe does not contain %d field name(s) that ui/FleetCockpit.cs archives: "
        "%s.\nThe deployed cockpit cannot write a field whose name is not in it, so every row it "
        "archives from now on is missing them -- measured once already: jid appears in 0 of 215 "
        "rows in .fleet/history.json.\nRebuild: powershell -ExecutionPolicy Bypass -File "
        "ui\\rebuild_ui.ps1   (it stops FleetCockpit and CopilotChat first, because csc cannot "
        "overwrite a running exe, then relaunches both)"
        % (len(missing), ", ".join(missing)))


def test_the_deployed_binary_is_not_older_than_the_source_it_was_built_from():
    """The check that needs no knowledge of what changed.

    A literal check only sees fields that introduce a new string. This one catches any edit at
    all, including one that changes what an existing field is set to.
    """
    _skip_without_a_binary()
    exe_mtime = os.path.getmtime(_EXE)
    stale = []
    for name in _SOURCES:
        path = os.path.join(_UI, name)
        if not os.path.isfile(path):
            continue
        src_mtime = os.path.getmtime(path)
        if src_mtime > exe_mtime:
            stale.append("%s (%.0f min newer)" % (name, (src_mtime - exe_mtime) / 60.0))
    assert not stale, (
        "ui/FleetCockpit.exe predates the source it is built from: %s.\nWhatever those edits "
        "changed, the running cockpit does not have it. Rebuild: powershell -ExecutionPolicy "
        "Bypass -File ui\\rebuild_ui.ps1" % "; ".join(stale))
