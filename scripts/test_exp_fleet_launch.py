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


def test_the_launcher_runs_from_a_bare_command_line_with_isolated_python(tmp_path):
    """The documented commands are `python -I -X utf8 scripts/exp_fleet_launch.py server|prepare|stop`.
    Isolated mode puts neither the script's folder nor the checkout on sys.path, so the launcher itself
    must; otherwise `server` dies on `from tools.childproc import ...` before it starts anything."""
    import subprocess
    script = os.path.join(REPO, "scripts", "exp_fleet_launch.py")
    code = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='probe'); "
            "import tools.childproc, scripts.sibling_dup_report; print('ok')")
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, script], capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-400:]


def test_the_launcher_floor_follows_the_operators_value_and_a_run_inherits_it(tmp_path):
    """prepare copies the operator's two floors into the run's settings (one implementation:
    carried_floor_lines); the launcher's own refusal then follows that value, 0 meaning off."""
    prod = tmp_path / "prod.txt"
    prod_body = "a=1" + chr(10) + "disk_floor_gb=0" + chr(10) + "ram_floor_mb=0" + chr(10)
    prod.write_text(prod_body, encoding="utf-8")
    own = tmp_path / "own.txt"
    own.write_text(L.settings_body(), encoding="utf-8")
    lines = L.carried_floor_lines(L.settings_body(), str(own), candidates=[str(prod)])
    assert sorted(lines) == ["disk_floor_gb=0", "ram_floor_mb=0"]
    assert prod.read_text(encoding="utf-8") == prod_body                    # read only
    nl = chr(10)
    assert L.own_disk_floor(L.settings_body() + "disk_floor_gb=0" + nl) == 0.0
    assert L.own_disk_floor(L.settings_body() + "disk_floor_gb=2.5" + nl) == 2.5
    assert L.own_disk_floor(L.settings_body()) == L.MIN_FREE_GB            # none carried: the launcher's own
