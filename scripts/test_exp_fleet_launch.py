# -*- coding: utf-8 -*-
"""scripts/exp_fleet_launch.py: the parts that must hold before any run is started (no server, no browser,
no fleet process is started by these tests)."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import exp_fleet_launch as L  # noqa: E402


@pytest.mark.parametrize("server,cdp", [(8000, 9340), (8100, 9222), (8765, 9340), (8100, 9224), (9340, 9340)])
def test_production_ports_and_shared_ports_are_refused(server, cdp):
    with pytest.raises(ValueError):
        L.check_ports(server, cdp)


def test_the_defaults_are_allowed():
    L.check_ports(L.SERVER_PORT, L.CDP_PORT)
    assert L.SERVER_PORT not in L.FORBIDDEN_PORTS and L.CDP_PORT not in L.FORBIDDEN_PORTS


def test_the_two_arms_differ_only_in_the_fanout_flag():
    on = L.fleet_args("on", "S", "G")
    off = L.fleet_args("off", "S", "G")
    assert [x for x in on if x not in off] == ["--fanout"]
    assert [x for x in off if x not in on] == ["--no-fanout"]
    for a in (on, off):
        assert "--stall-s" in a and a[a.index("--stall-s") + 1] == "0" and "--no-recycle" in a
        assert a[a.index("--max-concurrent") + 1] == "3" and a[a.index("--effort") + 1] == "min"
    with pytest.raises(ValueError):
        L.fleet_args("both", "S", "G")


def test_settings_problems_names_what_is_missing_or_different():
    assert L.settings_problems(L.settings_body()) == []
    assert len(L.settings_problems("")) == len(L.REQUIRED_SETTINGS)
    body = L.settings_body().replace("fanout_max_depth=1", "fanout_max_depth=2")
    assert L.settings_problems(body) == ["fanout_max_depth=1 (found 2)"]
    assert L.settings_problems("# tool_transport=connector\n" + L.settings_body()) == []


def test_disk_floor(tmp_path):
    assert L.disk_ok(str(tmp_path), 0.0)
    assert not L.disk_ok(str(tmp_path), 1e9)


def test_a_run_is_refused_for_an_unknown_label_without_starting_anything(tmp_path, capsys):
    root = tmp_path
    with open(root / "plan.json", "w", encoding="utf-8") as fh:
        json.dump({"runs": []}, fh)
    rc = L.main(["run", "--root", str(root), "--label", "nope", "--url-file", str(root / "none.txt")])
    assert rc == 2 and "unknown label" in capsys.readouterr().out
