# -*- coding: utf-8 -*-
"""A floor that blocks work must say whose number it is, and a number the panel does not show must
be called out.

MEASURED 2026-10-20 18:05. The cockpit panel showed disk floor 0 and RAM floor 0
(.config/settings.txt of the main checkout). An isolated experiment fleet, started from a worktree
whose own .config/settings.txt had no floor line, printed "admitting nothing: 5.14 GB free on C:,
floor 6.0 GB" and refused all work: the key was absent there, so the built-in default applied. The
block message named the winner and not the link of the chain that produced it, status.json carried
the number and not its source, and the cockpit banner showed neither.

This pins: the chain's links are named (cli | settings | env | default | live), the block message and
toast carry the words, a settings file elsewhere with another number produces a WARNING, a floor flag
replayed by --resume yields to a floor the file carries, status.json carries the source, the
experiment launcher carries the operator's floors into its private settings file, and the cockpit
banner reads the source and the warnings.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from relay import fleet_resume as FRS  # noqa: E402
from relay import fleet_runner as FR  # noqa: E402
from relay import relay_fleet as RF  # noqa: E402
from tools import settings_path as SP  # noqa: E402
from tools import ui_source  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_provenance(monkeypatch):
    monkeypatch.setattr(RF, "FLOOR_PROVENANCE", {})
    monkeypatch.delenv("SWE_DISK_FLOOR_GB", raising=False)


@pytest.fixture
def two_files(tmp_path, monkeypatch):
    """A 'read' settings file without floors and an 'other' one (the panel's) with floors 0."""
    read = tmp_path / "wt" / ".config" / "settings.txt"
    other = tmp_path / "main" / ".config" / "settings.txt"
    for p in (read, other):
        p.parent.mkdir(parents=True)
    read.write_text("maxtabs=3\n", encoding="utf-8")
    other.write_text("disk_floor_gb=0\nram_floor_mb=0\n", encoding="utf-8")
    monkeypatch.setattr(SP, "settings_file", lambda: str(read))
    monkeypatch.setattr(SP, "old_path", lambda: str(tmp_path / "none" / "settings.txt"))
    monkeypatch.setattr(SP, "main_checkout_root", lambda repo=None: str(tmp_path / "main"))
    monkeypatch.setattr(FR, "_settings_path", lambda: str(read))
    return read, other


# ── the links of the chain are named ────────────────────────────────────────────────────────
def test_each_link_of_the_chain_is_named(monkeypatch):
    assert FR.floor_source_of(2.0, "disk_floor_gb", "SWE_DISK_FLOOR_GB", trace={}) == "cli"
    assert FR.floor_source_of(-1.0, "disk_floor_gb", "SWE_DISK_FLOOR_GB",
                              trace={"disk_floor_gb": "read '0' from x"}) == "settings"
    assert FR.floor_source_of(-1.0, "disk_floor_gb", "SWE_DISK_FLOOR_GB",
                              trace={"disk_floor_gb": "no file at x"}) == "default"
    monkeypatch.setenv("SWE_DISK_FLOOR_GB", "4")
    assert FR.floor_source_of(-1.0, "disk_floor_gb", "SWE_DISK_FLOOR_GB",
                              trace={"disk_floor_gb": "no disk_floor_gb= line in x"}) == "env"
    # the RAM floor's lower flag counts as cli only when the file had no value
    assert FR.floor_source_of(-1.0, "ram_floor_mb", None, fallback_cli=1400.0, trace={}) == "cli"
    assert FR.floor_source_of(-1.0, "ram_floor_mb", None, fallback_cli=1400.0,
                              trace={"ram_floor_mb": "read '0' from x"}) == "settings"
    assert FR.floor_source_of(-1.0, "ram_floor_mb", None, fallback_cli=-1.0, trace={}) == "default"


def test_the_words_name_the_source(monkeypatch):
    RF.set_floor_provenance("disk", "default")
    assert "default: no disk_floor_gb in settings" in RF.floor_source_text("disk")
    RF.set_floor_provenance("disk", "settings", "read '2' from f")
    assert RF.floor_source_text("disk").startswith("settings.txt")
    RF.set_floor_provenance("disk", "cli")
    assert "command line --disk-floor-gb" in RF.floor_source_text("disk")
    RF.set_floor_provenance("disk", "env")
    assert "environment SWE_DISK_FLOOR_GB" in RF.floor_source_text("disk")


def test_a_launcher_that_recorded_nothing_still_gets_an_honest_source(monkeypatch):
    assert RF.floor_source("disk") == "default"
    monkeypatch.setenv("SWE_DISK_FLOOR_GB", "3")
    assert RF.floor_source("disk") == "env"


# ── the warning ─────────────────────────────────────────────────────────────────────────────
def test_a_settings_file_elsewhere_with_another_number_is_a_warning(two_files):
    read, other = two_files
    RF.set_floor_provenance("disk", "default")
    w = RF.floor_warning("disk", 6.0)
    assert w.startswith("WARNING")
    assert str(other) in w and "disk_floor_gb=0" in w and str(read) in w


def test_agreeing_files_make_no_warning(two_files):
    _read, other = two_files
    other.write_text("disk_floor_gb=6\n", encoding="utf-8")
    RF.set_floor_provenance("disk", "default")
    assert RF.floor_warning("disk", 6.0) == ""


def test_a_flag_that_beat_the_file_it_read_is_a_warning(two_files):
    read, other = two_files
    other.unlink()
    read.write_text("disk_floor_gb=1\n", encoding="utf-8")
    RF.set_floor_provenance("disk", "cli")
    w = RF.floor_warning("disk", 2.0)
    assert "disk_floor_gb=1" in w and "cli applies" in w


def test_a_live_command_is_not_compared_against_the_file(two_files):
    read, other = two_files
    other.unlink()
    read.write_text("disk_floor_gb=8\n", encoding="utf-8")
    RF.set_floor_provenance("disk", "live")
    assert RF.floor_warning("disk", 0.0) == ""


# ── the block message and the toast ─────────────────────────────────────────────────────────
def test_the_block_line_and_the_toast_carry_the_source_and_the_warning(two_files, capsys, monkeypatch):
    RF.set_floor_provenance("disk", "default")
    monkeypatch.setattr(RF, "free_disk_gb", lambda *a, **k: 5.14)
    monkeypatch.setattr(RF, "_DISK_DEFER_SINCE", [1.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_LAST", [0.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_NOTIFIED", [0.0])
    sent = []
    RF._note_disk_defer(6.0, 5, notify=lambda title, body: sent.append((title, body)),
                        now=1.0 + RF.DISK_DEFER_ALERT_AFTER_S + 1)
    out = capsys.readouterr().out
    assert "floor 6.0 GB (default: no disk_floor_gb in settings" in out
    assert "WARNING" in out
    assert len(sent) == 1
    assert "default: no disk_floor_gb in settings" in sent[0][1] and "WARNING" in sent[0][1]


# ── status.json ─────────────────────────────────────────────────────────────────────────────
def test_the_disk_block_carries_the_source():
    blk = FRS.disk_status(10 * FRS.GB, 100 * FRS.GB, 6.0, 1.0, 0, floor_source="default")
    assert blk["floor_source"] == "default"
    assert "floor_source" not in FRS.disk_status(10 * FRS.GB, 100 * FRS.GB, 6.0, 1.0, 0)


def test_the_snapshot_carries_both_sources(two_files):
    RF.set_floor_provenance("disk", "default")
    RF.set_floor_provenance("ram", "settings", "read '0'")
    snap = FR._snapshot([], 0.0, 0, disk_floor_gb=6.0, ram_floor_mb=0.0)
    assert snap["disk_floor_source"] == "default"
    assert snap["ram_floor_source"] == "settings"
    assert snap["disk"]["floor_source"] == "default"
    assert "default: no disk_floor_gb in settings" in snap["disk_floor_source_text"]
    assert snap["floor_warnings"] and snap["floor_warnings"][0].startswith("WARNING")


# ── a replayed flag yields to the file ──────────────────────────────────────────────────────
def test_a_flag_replayed_by_resume_yields_to_a_floor_the_file_carries(tmp_path, monkeypatch):
    f = tmp_path / "settings.txt"
    f.write_text("disk_floor_gb=0\nram_floor_mb=256\n", encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(f))
    assert FR.floors_after_resume(2.0, 1400.0, True) == (-1.0, -1.0)


def test_a_flag_is_kept_when_the_file_has_no_floor(tmp_path, monkeypatch):
    f = tmp_path / "settings.txt"
    f.write_text("maxtabs=3\n", encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(f))
    assert FR.floors_after_resume(2.0, 1400.0, True) == (2.0, 1400.0)


def test_a_flag_typed_on_a_fresh_launch_still_beats_the_file(tmp_path, monkeypatch):
    f = tmp_path / "settings.txt"
    f.write_text("disk_floor_gb=0\nram_floor_mb=256\n", encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(f))
    assert FR.floors_after_resume(2.0, 1400.0, False) == (2.0, 1400.0)


def test_the_run_resolves_through_the_resume_rule_and_records_provenance():
    src = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8-sig").read()
    assert "floors_after_resume(args.disk_floor_gb, args.ram_floor_mb, args.resume)" in src
    assert 'set_floor_provenance(\n        "disk"' in src.replace("\r\n", "\n")
    assert 'set_floor_provenance("disk", "live"' in src and 'set_floor_provenance("disk", "settings"' in src


# ── the worktree pointer ────────────────────────────────────────────────────────────────────
def test_a_linked_worktree_finds_the_main_checkout(tmp_path):
    main = tmp_path / "main"
    gd = main / ".git" / "worktrees" / "wt"
    gd.mkdir(parents=True)
    (gd / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: %s\n" % gd, encoding="utf-8")
    assert os.path.normcase(SP.main_checkout_root(str(wt))) == os.path.normcase(str(main))
    assert SP.main_checkout_root(str(main)) is None          # a main checkout has a .git DIRECTORY


# ── the experiment launcher ─────────────────────────────────────────────────────────────────
def test_the_experiment_launcher_carries_the_operators_floors(tmp_path):
    from scripts import exp_fleet_launch as EX
    own = tmp_path / "wt" / "settings.txt"
    other = tmp_path / "main" / "settings.txt"
    own.parent.mkdir()
    other.parent.mkdir()
    own.write_text("maxtabs=3\n", encoding="utf-8")
    other.write_text("disk_floor_gb=0\nram_floor_mb=0\n", encoding="utf-8")
    lines = EX.carried_floor_lines("maxtabs=3\n", str(own), candidates=[str(other)])
    assert lines == ["disk_floor_gb=0", "ram_floor_mb=0"]
    assert EX.carried_floor_lines("disk_floor_gb=2\n", str(own), candidates=[str(other)]) == ["ram_floor_mb=0"]


# ── the cockpit banner ──────────────────────────────────────────────────────────────────────
def test_the_cockpit_banner_reads_the_source_and_the_warnings():
    src = ui_source.read_parts("FleetCockpit", encoding="utf-8", errors="replace")
    body = src[src.index("void UpdateCapBanner("):src.index("void ForceStart(")]
    assert "FloorProvenanceText(root, diskGated, ramGated, floor, ramFloorNow)" in body
    helper = src[src.index("string FloorProvenanceText("):src.index("void ForceStart(")]
    for needle in ('"disk_floor_source"', '"ram_floor_source"', '"floor_warnings"',
                   "Math.Abs(_diskFloor - diskFloor)", "Math.Abs(_ramFloor - ramFloor)"):
        assert needle in helper, needle
    # the fields the banner reads are the ones the runner writes
    runner = open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8-sig").read()
    for key in ("disk_floor_source", "ram_floor_source", "floor_warnings"):
        assert '"%s"' % key in runner
