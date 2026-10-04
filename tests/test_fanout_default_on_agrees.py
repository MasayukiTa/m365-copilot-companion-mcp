# -*- coding: utf-8 -*-
"""Fan-out is ON by default in every layer, and nothing seeds `fanout=off`.

Owner decision (2026-10-01): fan-out defaults to ON; a setting that can only be changed from the
command line does not count as a feature, so the GUI must carry a visible control for it.

The default has several owners that drifted apart before (see tools/settings_keys.py). They are
compared here: the settings registry, the cockpit's field default, the runner's CLI default, the
autostart decision with no settings key, and the autostart command line, which must NAME the
flag in both directions (omitting it for "no" silently turned an explicit off into ON).
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

from relay import fleet_runner as FR  # noqa: E402
from relay import task_router as TR  # noqa: E402
from tools import childproc  # noqa: E402
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_default_is_on():
    assert SK.default("fanout") is True


def test_the_cockpit_field_default_matches_the_registry():
    m = re.search(r"bool\s+_fanout\s*=\s*(true|false)\s*;", _read("ui", "FleetCockpit.cs"))
    assert m and (m.group(1) == "true") == SK.default("fanout")
    m = re.search(r"public\s+const\s+bool\s+DefaultOn\s*=\s*(true|false)\s*;", _read("ui", "EffortPolicy.cs"))
    assert m and (m.group(1) == "true") == SK.default("fanout")


def test_the_runner_cli_default_matches_the_registry():
    src = _read("relay", "fleet_runner.py")
    m = re.search(r'add_argument\("--fanout",\s*action=argparse\.BooleanOptionalAction,\s*default=(True|False)', src)
    assert m and (m.group(1) == "True") == SK.default("fanout")


@pytest.fixture
def no_key(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("dark=0\n", encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(p))
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    return p


def test_an_absent_key_means_on_at_autostart(no_key):
    assert FR.settings_fanout() is None
    assert TR._wants_fanout([{"text": "x"}]) is SK.default("fanout")


class _Launcher(object):
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 4242


@pytest.mark.parametrize("line,flag", [("", "--fanout"), ("fanout=on\n", "--fanout"),
                                       ("fanout=off\n", "--no-fanout")])
def test_the_autostart_command_names_the_flag_both_ways(tmp_path, monkeypatch, line, flag):
    p = tmp_path / "settings.txt"
    p.write_text("dark=0\n" + line, encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(p))
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    sd = tmp_path / "fleet"
    sd.mkdir()
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(sd))
    monkeypatch.setattr(TR, "AUTOSTART", True)
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://example.invalid/agent")
    TR.ensure_dirs()
    launcher = _Launcher()
    out = TR.autostart_fleet([{"text": "read mail"}], str(sd), launcher=launcher)
    assert out["ok"] is True
    cmd = launcher.calls[0]
    assert flag in cmd
    assert ("--no-fanout" if flag == "--fanout" else "--fanout") not in cmd


def test_the_coordinator_reports_what_it_was_started_with():
    FR._RUN_FANOUT.clear()
    assert FR._fanout_run_block() == {}
    FR._record_run_fanout(False, argv=["--no-fanout"])
    assert FR._fanout_run_block() == {"fanout_run": {"enabled": False, "source": "flag"}}
    FR._record_run_fanout(True, argv=["--goals-file", "g"])
    assert FR._fanout_run_block() == {"fanout_run": {"enabled": True, "source": "default"}}
    FR._RUN_FANOUT.clear()


def test_the_cockpit_has_a_visible_control_not_only_the_slash_command():
    src = _read("ui", "FleetCockpit.cs")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(FanoutControl());" in src
    assert "ctrls.Children.Add(FanoutControl());" not in src
    assert "_fanoutBox = new ComboBox();" in src
    assert 'Obj(root, "fanout_run")' in src
    # the combo persists through the same key the slash command writes
    assert len(re.findall(r'SaveKey\("fanout", _fanout \? "on" : "off"\)', src)) >= 2


def _tracked():
    out = childproc.run(["git", "ls-files"], cwd=REPO, check=True).stdout
    return [f for f in out.splitlines() if f.endswith((".ps1", ".bat", ".cmd", ".cs", ".py", ".txt", ".example", ".template"))]


def test_nothing_seeds_fanout_off_into_a_new_settings_file():
    """No product script, template or setup file writes a literal `fanout=off`. (The one
    historical hand-editor, scripts/win/run_swe_via_ui.ps1, is audited in
    docs/private/20261001_cli_only_settings_audit.md; it only flips an existing on to off.)"""
    # a string literal that BEGINS with the line, i.e. text a program would write to the file
    pat = re.compile(r"""["']fanout\s*=\s*off""", re.I)
    bad = []
    for rel in _tracked():
        base = os.path.basename(rel)
        if base.startswith("test_") or rel.startswith(("tests/", "docs/", "bench/")) or "/test_" in rel:
            continue
        if rel == "scripts/win/run_swe_via_ui.ps1":
            continue
        try:
            txt = _read(*rel.split("/"))
        except Exception:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            if pat.search(ln) and not ln.lstrip().startswith(("#", "//")):
                bad.append("%s:%d" % (rel, i))
    assert not bad, "a file writes fanout=off as if it were a default: %s" % bad
