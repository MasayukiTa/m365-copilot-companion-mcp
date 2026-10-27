# -*- coding: utf-8 -*-
"""Fix the Fleet Cockpit's C: disk strip to its C# source.

relay/disk_series.py publishes an hs_disk block into .fleet/health_strip.json (free_gb, the
10-minute slope, the swing count, warn, warn_gb). ui/FleetCockpit.Header.cs reads it and draws a
\"C: NN.N GB\" chip on the header row beside the rate gauge. The owner's rule is explicit: the disk
floor is the OWNER's setting, and a low reading WARNS ONLY -- it never stops intake. These asserts
keep the source honest about that, the same way ui/test_fleet_cockpit_health.py fixes the health
dots: they read the real C# (through cockpit_source, which globs every FleetCockpit*.cs part) and
fail if the rule is quietly changed.
"""
from tools import cockpit_source

SOURCE = cockpit_source.read(encoding="utf-8")


def _build_disk_strip():
    return cockpit_source.member(SOURCE, "UIElement BuildDiskStrip(")


def _read_hs_disk():
    return cockpit_source.member(SOURCE, "Dictionary<string, object> ReadHsDisk(")


def test_disk_strip_reads_hs_disk_from_health_strip_json():
    """The reading comes from .fleet/health_strip.json's hs_disk block, read off disk so it does
    not depend on a fleet running."""
    reader = _read_hs_disk()
    assert '"health_strip.json"' in reader
    assert '"hs_disk"' in reader
    assert "Encoding.UTF8" in reader, "the file must be read with the encoding stated explicitly"
    body = _build_disk_strip()
    assert "ReadHsDisk()" in body
    assert '"free_gb"' in body


def test_disk_strip_warns_only_and_never_stops_intake():
    """The owner's rule: the floor is the owner's setting; a low reading is colour + a tooltip and
    nothing else. The strip must not contain any intake-stopping verb."""
    body = _build_disk_strip()
    assert '"warn"' in body
    assert "SeverityColor(1.0)" in body, "warning colour should reuse the shared severity ramp"
    assert "WARNS " in body or "warns" in body.lower()
    assert "投入は止めません" in body
    assert "never stops intake" in body
    for forbidden in ("StopIntake", "HaltIntake", "BlockSubmission", "PauseFleet", "admission"):
        assert forbidden not in body, "%s in the disk strip would turn a warning into a gate" % forbidden


def test_disk_strip_shows_level_slope_and_swings_bilingually():
    """The chip shows the free level; the tooltip carries the 10-minute slope and the swing count,
    in Japanese and English, so a reader sees the trend and not just the level."""
    body = _build_disk_strip()
    assert '"slope_gb_per_10min"' in body
    assert '"swings_10min"' in body
    assert "GB/10分" in body and "GB/10min" in body
    assert "揺れ" in body and "swings" in body
    assert '" GB"' in body


def test_disk_strip_is_null_safe_when_nothing_measured():
    """No hs_disk, or no free_gb (disk_series writes free_gb=null / checked_age_s=-1 when it has
    measured nothing), draws no chip rather than a '0 GB' that reads as a full disk."""
    reader = _read_hs_disk()
    assert "return null;" in reader
    body = _build_disk_strip()
    assert "if (hd == null) return null;" in body
    assert 'TryGetValue("free_gb"' in body and "return null;" in body
