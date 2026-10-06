# -*- coding: utf-8 -*-
"""The optional helper wrapper: off unless asked, two gates, and a thin call into a program
that is not in this repository. Everything here is hermetic: the "program" is a few lines
written into a temporary folder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from tools import auto_loader
from tools.auto import ext_helper as eh

NAMES = ["validity_explain_reason", "validity_judge", "validity_list_claims",
         "validity_negative_control", "validity_verify_lock"]


def _calls():
    return [
        lambda: eh.validity_list_claims(),
        lambda: eh.validity_judge("A1"),
        lambda: eh.validity_negative_control("A1"),
        lambda: eh.validity_verify_lock(),
        lambda: eh.validity_explain_reason("X"),
    ]


def _entry(tmp_path, body):
    (tmp_path / "helper_entry.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


ECHO = '''
    import json, os, sys
    req = json.loads(sys.stdin.read())
    sys.stdout.write("noise line\\n")
    sys.stdout.write(json.dumps({"ok": True, "req": req, "env": sorted(os.environ)}))
'''


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv("MCP_EXT_HELPER", raising=False)
    monkeypatch.delenv("MCP_EXT_HELPER_DIR", raising=False)
    monkeypatch.delenv("MCP_EXT_HELPER_PYTHON", raising=False)


@pytest.fixture
def on(monkeypatch, tmp_path, off):
    _entry(tmp_path, ECHO)
    monkeypatch.setenv("MCP_EXT_HELPER", "1")
    monkeypatch.setenv("MCP_EXT_HELPER_DIR", str(tmp_path))
    return tmp_path


def test_the_five_names_are_the_ones_other_code_depends_on():
    got = sorted(n for n, f in vars(eh).items()
                 if callable(f) and getattr(f, "__module__", "") == eh.__name__
                 and not n.startswith("_"))
    assert got == NAMES


@pytest.mark.parametrize("value", [None, "0", "", "true", "yes", "2"])
def test_only_exactly_one_enables(monkeypatch, off, value):
    if value is not None:
        monkeypatch.setenv("MCP_EXT_HELPER", value)
    assert eh._enabled() is False
    assert eh._auto_tools_enabled() is False
    for call in _calls():
        assert json.loads(call())["code"] == "DISABLED"


def test_disabled_never_starts_a_process(monkeypatch, off):
    def boom(*a, **k):
        raise AssertionError("a child was started while disabled")
    monkeypatch.setattr(subprocess, "Popen", boom)
    for call in _calls():
        assert json.loads(call())["code"] == "DISABLED"


def test_the_live_package_publishes_none_of_them_by_default(monkeypatch, off):
    monkeypatch.setenv("MCP_EXT_HELPER", "0")
    published = {f.__name__ for _, f in auto_loader.load_auto_tools()}
    assert not published & set(NAMES)


def test_enabled_without_a_folder_publishes_nothing_and_errors(monkeypatch, off, capsys):
    monkeypatch.setenv("MCP_EXT_HELPER", "1")
    assert eh._auto_tools_enabled() is False
    assert "[ext_helper] enabled but entry not found" in capsys.readouterr().err
    for call in _calls():
        out = json.loads(call())
        assert out["code"] == "NOT_AVAILABLE"
        assert "\\" not in out["error"] and "/" not in out["error"]


def test_a_folder_without_the_program_is_not_available(monkeypatch, off, tmp_path):
    monkeypatch.setenv("MCP_EXT_HELPER", "1")
    monkeypatch.setenv("MCP_EXT_HELPER_DIR", str(tmp_path))
    assert eh._auto_tools_enabled() is False
    assert json.loads(eh.validity_list_claims())["code"] == "NOT_AVAILABLE"


def test_enabled_with_the_program_publishes_all_five(on):
    published = {f.__name__ for _, f in auto_loader.load_auto_tools()}
    assert set(NAMES) <= published


def test_a_request_reaches_the_program_and_its_last_line_comes_back(on):
    out = json.loads(eh.validity_judge("A1"))
    assert out["ok"] is True
    assert out["req"] == {"op": "judge", "args": {"config_id": "A1", "engine_version": ""}}
    out = json.loads(eh.validity_negative_control("A1", m=5000, engine_version="v9"))
    assert out["req"]["op"] == "negctl"
    assert out["req"]["args"] == {"config_id": "A1", "m": 1000, "engine_version": "v9"}
    out = json.loads(eh.validity_list_claims(set_name="s1"))
    assert out["req"]["args"] == {"engine_version": "", "set_name": "s1"}
    assert json.loads(eh.validity_verify_lock())["req"]["op"] == "verify"
    assert json.loads(eh.validity_explain_reason(" X "))["req"]["args"]["code"] == "X"


def test_secrets_in_the_environment_are_not_handed_on(on, monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "secret-value")
    monkeypatch.setenv("SOME_TOKEN", "secret-value")
    keys = json.loads(eh.validity_verify_lock())["env"]
    upper = [k.upper() for k in keys]
    assert "MCP_API_KEY" not in upper and "SOME_TOKEN" not in upper
    assert "MCP_EXT_HELPER" in upper


@pytest.mark.parametrize("call", [
    lambda: eh.validity_judge("../x"),
    lambda: eh.validity_judge(""),
    lambda: eh.validity_judge(5),
    lambda: eh.validity_judge("A1", engine_version="v 1"),
    lambda: eh.validity_judge("A1", engine_version=3),
    lambda: eh.validity_list_claims(set_name="a/b"),
    lambda: eh.validity_negative_control("A1", m="x"),
    lambda: eh.validity_negative_control("A1", m=True),
    lambda: eh.validity_explain_reason("x" * 129),
    lambda: eh.validity_explain_reason(None),
])
def test_bad_arguments_are_refused_before_anything_runs(on, monkeypatch, call):
    def boom(*a, **k):
        raise AssertionError("a child was started for a bad argument")
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert json.loads(call())["code"] == "BAD_ARGS"


def test_a_slow_program_is_stopped(on, monkeypatch):
    _entry(on, "import time\ntime.sleep(60)\n")
    monkeypatch.setitem(eh._TIMEOUTS, "verify", 1)
    t0 = time.time()
    assert json.loads(eh.validity_verify_lock())["code"] == "TIMEOUT"
    assert time.time() - t0 < 30


def test_oversized_output_is_refused(on):
    _entry(on, "import sys\nsys.stdout.write('{\"x\": \"' + 'a' * 70000 + '\"}')\n")
    assert json.loads(eh.validity_verify_lock())["code"] == "OUTPUT_TRUNCATED"


def test_output_that_is_not_json_is_refused(on):
    _entry(on, "print('not json')\n")
    assert json.loads(eh.validity_verify_lock())["code"] == "BAD_OUTPUT"


def test_a_program_that_prints_nothing_is_not_available(on):
    _entry(on, "raise SystemExit(3)\n")
    assert json.loads(eh.validity_verify_lock())["code"] == "NOT_AVAILABLE"


def test_descriptions_come_from_the_folder_only_when_present(on):
    original = eh.validity_judge.__doc__
    try:
        (on / "tool_docs.json").write_text(json.dumps(
            {"validity_judge": "Described elsewhere.", "Path": "ignored", "_err": "ignored"}),
            encoding="utf-8")
        eh._apply_docs(on)
        assert eh.validity_judge.__doc__ == "Described elsewhere."
        assert eh.Path.__doc__ != "ignored" and eh._err.__doc__ != "ignored"
    finally:
        eh.validity_judge.__doc__ = original
    eh._apply_docs(on / "missing")          # never raises


def test_the_default_descriptions_say_nothing_about_what_it_does():
    for name in NAMES:
        doc = getattr(eh, name).__doc__
        assert doc.startswith("Optional analysis helper") and "disabled by default" in doc
        assert "\n" not in doc.strip()


def test_the_server_registers_the_second_optional_tool_only_when_enabled():
    """A child process, because registration happens at import. MCP_EXT_HELPER is stated, never
    inherited: main.py loads the repository's .env, which could hold either value."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys,json;sys.path.insert(0,'.');import main;"
            "print('<<<'+json.dumps(sorted(set(main._ALL_TOOLS)))+'>>>')")
    results = {}
    for flag in ("0", "1"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
        env.update({"MCP_API_KEY": "test-only", "MCP_TOOL_MAP": "1", "MCP_EXT_HELPER": flag})
        proc = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                              capture_output=True, timeout=240)
        body = proc.stdout.decode("utf-8", "replace")
        assert "<<<" in body, proc.stderr.decode("utf-8", "replace")[-2000:]
        results[flag] = set(json.loads(body.split("<<<", 1)[1].split(">>>", 1)[0]))
    assert "validity_audit_ledger" not in results["0"]
    assert "validity_audit_ledger" in results["1"]
    assert not results["0"] & set(NAMES)
