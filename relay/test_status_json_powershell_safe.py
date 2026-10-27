# -*- coding: utf-8 -*-
"""status.json must be readable by Windows PowerShell 5.1's ConvertFrom-Json.

THE DEFECT. PowerShell 5.1 parses every JSON object into a CASE-INSENSITIVE dictionary, so
two keys in the same object that differ only in case ("turn"/"Turn") make ConvertFrom-Json
throw "duplicate keys" and read NOTHING -- while python's json keeps both and writes the
file without complaint. The fleet writes status.json with json; the cockpit's PowerShell
helpers read it. Measured 2026-10-27: a ~295 KB status.json loaded fine in python and failed
a few thousand characters into ConvertFrom-Json.

THE FIX under test. relay.json_casefold.fold_case_collisions folds each object once on the
way out (last writer wins, as the reader would), and relay.fleet_runner._write_atomic runs
it before writing. These tests pin both the helper and that the writer uses it; the PS 5.1
case is skipped off Windows / when powershell.exe is absent.
"""
import json
import os
import shutil
import subprocess

import pytest

from relay.json_casefold import fold_case_collisions, has_case_collision
from relay import fleet_runner as FR


# --------------------------------------------------------------------- the helper, in python
def test_fold_drops_case_only_duplicates_last_wins():
    out = fold_case_collisions({"turn": 1, "Turn": 2})
    assert out == {"Turn": 2}                 # last spelling and last value win
    assert not has_case_collision(out)


def test_fold_keeps_a_lone_key_and_its_value_unchanged():
    out = fold_case_collisions({"turn": 7, "root_id": "r1", "pill": "完了"})
    assert out == {"turn": 7, "root_id": "r1", "pill": "完了"}


def test_fold_walks_lists_and_nested_objects():
    payload = {"workers": [{"turn": 1, "Turn": 2}, {"a": {"X": 1, "x": 9}}]}
    out = fold_case_collisions(payload)
    assert not has_case_collision(out)
    assert out["workers"][0] == {"Turn": 2}
    assert out["workers"][1]["a"] == {"x": 9}


def test_fold_is_a_noop_when_there_is_no_collision():
    payload = {"workers": [{"turn": 1, "root_id": "r"}], "meta": {"a": 1, "b": 2}}
    assert fold_case_collisions(payload) == payload


def test_fold_leaves_a_nested_turn_in_a_different_object_alone():
    # effort_last_switch carries its OWN inner 'turn'; it must not clash with a sibling 'turn'
    # one level up because they live in different objects.
    payload = {"turn": 4, "effort_last_switch": {"turn": 2, "from": "min", "to": "max"}}
    assert fold_case_collisions(payload) == payload
    assert not has_case_collision(payload)


# --------------------------------------------------------------- the writer folds before write
def test_write_atomic_folds_case_collisions(tmp_path):
    p = tmp_path / "status.json"
    FR._write_atomic(str(p), {"workers": [{"turn": 1, "Turn": 2}]})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert not has_case_collision(data)
    assert data["workers"][0] == {"Turn": 2}


# ----------------------------------------------------------------- the real reader, on Windows
_PWSH = shutil.which("powershell.exe")


@pytest.mark.skipif(_PWSH is None, reason="powershell.exe not available (not Windows)")
def test_written_status_is_read_by_powershell_5_1(tmp_path):
    """A status.json the writer produced from a colliding payload parses in ConvertFrom-Json."""
    p = tmp_path / "status.json"
    FR._write_atomic(str(p), {"workers": [{"turn": 1, "Turn": 2, "pill": "完了"}],
                              "meta": {"A": 1, "a": 2}})
    cmd = ("$ErrorActionPreference='Stop'; try { Get-Content -Raw -Encoding UTF8 '%s' | "
           "ConvertFrom-Json | Out-Null; 'PS_OK' } catch { 'PS_FAIL: '+$_.Exception.Message }"
           % str(p))
    r = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=120)
    assert (r.stdout or "").strip() == "PS_OK", (r.stdout, r.stderr)


@pytest.mark.skipif(_PWSH is None, reason="powershell.exe not available (not Windows)")
def test_unfolded_colliding_json_really_does_break_powershell(tmp_path):
    """Guard the guard: the raw colliding file MUST fail, or the test above proves nothing."""
    p = tmp_path / "raw.json"
    p.write_text(json.dumps({"workers": [{"turn": 1, "Turn": 2}]}, ensure_ascii=False),
                 encoding="utf-8")
    cmd = ("$ErrorActionPreference='Stop'; try { Get-Content -Raw -Encoding UTF8 '%s' | "
           "ConvertFrom-Json | Out-Null; 'PS_OK' } catch { 'PS_FAIL' }" % str(p))
    r = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=120)
    assert (r.stdout or "").strip() == "PS_FAIL", (r.stdout, r.stderr)
