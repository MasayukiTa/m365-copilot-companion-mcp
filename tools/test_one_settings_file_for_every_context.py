# -*- coding: utf-8 -*-
"""The same absolute path resolved to two different files, and nobody could see it.

%APPDATA% is redirected for a process running inside an MSIX package. On 2026-09-16 the
cockpit -- relaunched from an agent session, which runs inside one -- wrote its settings into
a package-private copy, while every fleet coordinator read the operator's real file at the
identical path. The panel showed 1 GB and 512 MB; every run reserved 4 GB and 1024 MB; the
file the operator was looking at said 1 and 512 and was correct about itself. That lasted a
month.

The repository is the one directory every context agrees about -- the fleet already shares it
through --state-dir, and a file written there from inside the package is read unchanged from
outside (measured both ways that day). So the settings move there.

THE PATH HAD MORE READERS THAN ANYONE HAD COUNTED. relay/fleet_runner.py,
bridge/session_store.py, tools/approval_policy.py, ui/CopilotChat.cs, and ui/FleetCockpit.cs
TWICE -- ApprovalPromptWindow and CockpitWindow each built their own. Six copies of one fact,
and they did not agree: only approval_policy fell back to the home directory when APPDATA was
unset. The others produced a RELATIVE path, so a service or a scheduled task would read
"copilot-bridge/settings.txt" against its working directory, find nothing, and use defaults
while the operator's settings sat untouched.

MIGRATION ORDER, which these tests pin: readers learn both locations FIRST, the writer moves
SECOND, the old location is retired LAST. What is deliberately absent is a copy-on-startup --
the cockpit and the fleet would each be a copier and the winner would depend on which started
last, which is the same shape as the defect being fixed.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import settings_path as SP  # noqa: E402


# --------------------------------------------------------------------- resolution order
def test_the_new_location_wins_when_it_exists(tmp_path, monkeypatch):
    new = tmp_path / ".config" / "settings.txt"
    new.parent.mkdir(parents=True)
    new.write_text("disk_floor_gb=1\n", encoding="utf-8")
    old = tmp_path / "roaming" / "copilot-bridge" / "settings.txt"
    old.parent.mkdir(parents=True)
    old.write_text("disk_floor_gb=4\n", encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(new))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert SP.settings_file() == str(new)


def test_the_old_location_still_works_for_a_machine_that_has_not_migrated(tmp_path, monkeypatch):
    """A machine mid-migration keeps working; one that never migrates behaves as before."""
    old = tmp_path / "roaming" / "copilot-bridge" / "settings.txt"
    old.parent.mkdir(parents=True)
    old.write_text("disk_floor_gb=4\n", encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(tmp_path / ".config" / "settings.txt"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert SP.settings_file() == str(old)


def test_a_fresh_machine_starts_where_everything_is_going(tmp_path, monkeypatch):
    monkeypatch.setattr(SP, "NEW_PATH", str(tmp_path / ".config" / "settings.txt"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert SP.settings_file() == str(tmp_path / ".config" / "settings.txt")


def test_writes_always_go_to_the_new_location(tmp_path, monkeypatch):
    """Reading follows the machine; writing leads it. Otherwise nothing ever migrates."""
    old = tmp_path / "roaming" / "copilot-bridge" / "settings.txt"
    old.parent.mkdir(parents=True)
    old.write_text("x=1\n", encoding="utf-8")
    new = tmp_path / ".config" / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(new))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert SP.settings_file() == str(old), "reads still follow the file that exists"
    assert SP.settings_file_for_write() == str(new)
    assert new.parent.is_dir(), "the write path must be usable, not merely named"


# --------------------------------------------------------------------- the relative-path trap
def test_an_unset_appdata_never_produces_a_relative_path(monkeypatch):
    """Four of the six copies did exactly this. A relative settings path silently reads
    nothing and falls back to defaults, while the operator's real file sits untouched."""
    monkeypatch.delenv("APPDATA", raising=False)
    got = SP.old_path()
    assert os.path.isabs(got), got
    assert ".copilot-bridge" in got


# --------------------------------------------------------------------- identity, not path
def test_describe_reports_identity_because_the_path_was_never_the_difference(tmp_path,
                                                                            monkeypatch):
    """281 bytes modified today versus 237 bytes modified four days earlier, at one path.
    A report that prints only the path cannot show the split that actually happened."""
    new = tmp_path / ".config" / "settings.txt"
    new.parent.mkdir(parents=True)
    new.write_text("disk_floor_gb=1\n", encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(new))
    said = SP.describe()
    assert "bytes" in said and "modified" in said and str(new) in said


def test_describe_says_so_when_the_file_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(SP, "NEW_PATH", str(tmp_path / "nope" / "settings.txt"))
    monkeypatch.delenv("APPDATA", raising=False)
    assert "unreadable" in SP.describe()


# --------------------------------------------------------------------- every reader agrees
@pytest.mark.parametrize("module,getter", [
    ("relay.fleet_runner", "_settings_path"),
    ("bridge.session_store", "_settings_path"),
    ("tools.approval_policy", "settings_path"),
])
def test_every_python_reader_uses_the_one_resolver(module, getter):
    """Six copies is how the fact drifted. One resolver is the fix; these assert nobody
    quietly grew a seventh."""
    import importlib
    mod = importlib.import_module(module)
    got = str(getattr(mod, getter)())
    assert got == str(SP.settings_file()), "%s.%s resolved %r" % (module, getter, got)


def test_no_python_module_builds_the_old_path_by_hand():
    """The literal that was copied six times. A new one appearing is the drift returning."""
    offenders = []
    for root, _dirs, files in os.walk(REPO):
        if any(part in root for part in (".git", ".venv", ".fleet", "node_modules", "output")):
            continue
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) == os.path.abspath(SP.__file__):
                continue          # the one place that is allowed to know
            try:
                body = io.open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if re.search(r'["\']copilot-bridge["\']\s*,\s*["\']settings\.txt["\']', body):
                offenders.append(os.path.relpath(path, REPO))
    assert not offenders, "these build the settings path themselves: %r" % offenders


# --------------------------------------------------------------------- both languages agree
def test_the_settings_path_is_the_same_in_every_language():
    """The C# apps carry their own copy because ui/rebuild_ui.ps1 enumerates its sources by
    hand and a file added there and forgotten breaks a button silently. Copies are allowed;
    disagreeing copies are not."""
    for cs in ("ui/FleetCockpit.cs", "ui/CopilotChat.cs"):
        body = io.open(os.path.join(REPO, cs), encoding="utf-8-sig").read()
        assert 'Path.Combine(RepoRootForSettings(), ".config", "settings.txt")' in body, cs
        assert "SettingsFileForWrite" in body, "%s still writes to the read path" % cs
        # The old location must remain READABLE -- retiring it is the last step, not this one.
        assert '"copilot-bridge", "settings.txt"' in body, \
            "%s dropped the old location before anything migrated" % cs


def test_nothing_copies_the_file_on_startup():
    """DELIBERATELY ABSENT. Two copiers racing on startup is the defect being fixed, wearing
    different clothes: which copy wins would depend on which process started last."""
    body = io.open(SP.__file__, encoding="utf-8").read()
    assert "shutil.copy" not in body and "copyfile" not in body
