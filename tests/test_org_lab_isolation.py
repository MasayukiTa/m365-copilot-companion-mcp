# -*- coding: utf-8 -*-
"""Lab / production layout isolation check (slice F1)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_isolation as L  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_isolation.py")
COMMIT = "d8f975b17af12e2cbced6341660726b5753f261d"


def codes(v):
    return [x["code"] for x in v["violations"]]


@pytest.fixture
def world(tmp_path):
    prod = tmp_path / "prod"
    pstate = prod / ".fleet"
    lab = tmp_path / "lab"
    for d in (prod, pstate, lab):
        d.mkdir()
    (lab / L.MARKER_NAME).write_text(json.dumps(L.make_marker(COMMIT, "t0")), encoding="utf-8")
    return {"prod": str(prod), "pstate": str(pstate), "lab": str(lab), "tmp": tmp_path}


def check(w, lab=None, **kw):
    lab = lab or w["lab"]
    m, e = L.read_marker(lab)
    return L.check_lab_layout(lab, w["prod"], w["pstate"], marker=m, marker_error=e, **kw)


def test_separate_lab_with_marker_is_ok(world):
    v = check(world)
    assert v["ok"] and v["violations"] == []


def test_lab_inside_production_tree(world):
    sub = os.path.join(world["prod"], "lab")
    os.mkdir(sub)
    assert "inside_production_root" in codes(check(world, lab=sub))


def test_lab_inside_production_state_dir(world):
    sub = os.path.join(world["pstate"], "lab")
    os.mkdir(sub)
    c = codes(check(world, lab=sub))
    assert "inside_production_state" in c and "inside_production_root" in c


def test_lab_containing_or_equal_to_production(world):
    assert "contains_production_root" in codes(check(world, lab=str(world["tmp"])))
    assert "same_as_production_root" in codes(check(world, lab=world["prod"]))


def test_symlink_into_production_is_caught(world):
    link = world["tmp"] / "link"
    try:
        os.symlink(world["prod"], str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert "same_as_production_root" in codes(check(world, lab=str(link)))


def test_case_insensitive_compare(world):
    up = world["prod"].upper()
    v = L.check_lab_layout(up, world["prod"], world["pstate"], case_insensitive=True)
    assert "same_as_production_root" in codes(v)
    v = L.check_lab_layout(os.path.join(up, "x"), world["prod"], world["pstate"],
                           case_insensitive=True)
    assert "inside_production_root" in codes(v)


def test_h_drive_and_unc_are_refused():
    v = L.check_lab_layout("H:\\work\\lab", "C:\\prod", "C:\\prod\\.fleet", case_insensitive=True)
    assert "on_h_drive" in codes(v)
    v = L.check_lab_layout("h:/lab", "C:\\prod", "C:\\prod\\.fleet", case_insensitive=True)
    assert "on_h_drive" in codes(v)
    v = L.check_lab_layout("\\\\srv\\share\\lab", "C:\\prod", "C:\\prod\\.fleet",
                           case_insensitive=True)
    assert "unc_path" in codes(v)


def test_state_dir_must_be_separate(world):
    for ls in (world["pstate"], os.path.join(world["pstate"], "x"), world["prod"]):
        assert "lab_state_shared_with_production" in codes(check(world, lab_state_dir=ls))
    own = os.path.join(world["lab"], "state")
    assert check(world, lab_state_dir=own)["ok"]


def test_marker_missing_unreadable_and_invalid(world):
    other = world["tmp"] / "lab2"
    other.mkdir()
    assert "marker_missing" in codes(check(world, lab=str(other)))
    (other / L.MARKER_NAME).write_text("{not json", encoding="utf-8")
    assert "marker_missing" in codes(check(world, lab=str(other)))
    (other / L.MARKER_NAME).write_text(json.dumps({"version": 1, "source_commit": "zz"}),
                                       encoding="utf-8")
    assert "marker_invalid" in codes(check(world, lab=str(other)))


def test_make_marker_is_pure_and_validated(tmp_path):
    before = sorted(os.listdir(str(tmp_path)))
    m = L.make_marker(COMMIT.upper(), "2026-10-22T00:00:00Z")
    assert m["source_commit"] == COMMIT and L.marker_problems(m) == []
    assert sorted(os.listdir(str(tmp_path))) == before
    for bad in (("nothex", "t"), (COMMIT, ""), (None, "t")):
        with pytest.raises(L.LabIsolationError):
            L.make_marker(*bad)
    assert L.marker_problems("x") and L.marker_problems({"version": True})


def test_bad_path_inputs_refused():
    with pytest.raises(L.LabIsolationError):
        L.check_lab_layout("", "p", "s")


def run(*args):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT] + list(args),
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_ok_violation_marker_and_refusal(world):
    base = ["--lab", world["lab"], "--production-root", world["prod"],
            "--production-state", world["pstate"]]
    r = run(*base, "--format", "json")
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r = run("--lab", world["prod"], "--production-root", world["prod"],
            "--production-state", world["pstate"])
    assert r.returncode == 1 and "same_as_production_root" in r.stdout
    before = sorted(os.listdir(world["lab"]))
    r = run("--print-marker", COMMIT + ":2026-10-22T00:00:00Z")
    assert r.returncode == 0 and json.loads(r.stdout)["source_commit"] == COMMIT
    assert sorted(os.listdir(world["lab"])) == before
    assert run("--print-marker", "nocolon").returncode == 2
    assert run("--lab", world["lab"]).returncode == 2
