"""The near side of the execution boundary.

The property that matters most is a negative one: this must never fall back to running the
command locally. A transport that silently degrades to the thing it was built to avoid is
worse than one that fails, because the run carries on, looks identical, and is unconfined.
"""
import base64
import json

import pytest

from relay import broker_client as BC


class _Proc:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


def test_it_is_off_unless_switched_on(monkeypatch, tmp_path):
    # THE MARKER FILE TOO. Isolating only the environment variable left this test reading the
    # repository's real .fleet/BROKER_ON, so its result depended on whether routing happened
    # to be switched on -- a suite that passes or fails on the operator's current state is
    # not measuring the code.
    monkeypatch.setattr(BC, "MARKER", str(tmp_path / "BROKER_ON"))
    """Step 2 changes where every fleet tool lands. That is a decision, not an import."""
    monkeypatch.delenv("SWE_BROKER", raising=False)
    assert BC.enabled() is False
    monkeypatch.setenv("SWE_BROKER", "on")
    assert BC.enabled() is True


def test_the_request_goes_on_stdin_not_the_command_line(monkeypatch):
    """The far side is an SSH forced command: the command line is discarded, so it is not a
    channel. Putting the request there would silently send nothing."""
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return _Proc(out=json.dumps({"ok": True, "pong": True}))
    monkeypatch.setattr(BC.subprocess, "run", fake_run)
    BC.ping()
    assert json.loads(seen["input"]) == {"verb": "ping"}
    # The argv carries the host and transport flags only -- no request payload.
    assert not any("ping" in str(a) for a in seen["argv"])


def test_a_refusal_from_the_broker_is_raised_not_returned(monkeypatch):
    monkeypatch.setattr(BC.subprocess, "run",
                        lambda *a, **k: _Proc(out=json.dumps({"ok": False, "error": "nope"})))
    with pytest.raises(BC.BrokerError) as e:
        BC.ping()
    assert "nope" in str(e.value)


def test_ssh_failing_is_told_apart_from_the_broker_refusing(monkeypatch):
    """One means fix the key, the other means fix the request. Collapsing them wastes the
    next hour on the wrong half."""
    monkeypatch.setattr(BC.subprocess, "run",
                        lambda *a, **k: _Proc(out="", err="Permission denied (publickey).", rc=255))
    with pytest.raises(BC.BrokerError) as e:
        BC.ping()
    assert "returned nothing" in str(e.value) and "publickey" in str(e.value)


def test_there_is_no_local_fallback_anywhere_in_the_module():
    """THE PROPERTY THIS MODULE EXISTS FOR, asserted against the source.

    Every failure path must raise. A single `except ... : run_locally()` would undo the whole
    containment while leaving every test that checks behaviour still passing."""
    import inspect
    src = inspect.getsource(BC)
    # The only subprocess call is the ssh one.
    assert src.count("subprocess.run(") == 1
    assert '"ssh"' in src
    for bad in ("shell=True", "os.system", "run_python", "pwsh", "cmd.exe"):
        assert bad not in src, "a local execution path appeared: %s" % bad


def test_a_command_is_base64d_rather_than_quoted(monkeypatch):
    """Quoting a shell command through JSON, ssh and two shells is four chances to change what
    runs, and a command that changes on the way is worse than one that is refused."""
    seen = {}
    def fake_run(argv, **kw):
        seen["input"] = kw.get("input")
        return _Proc(out=json.dumps({"ok": True, "rc": 0, "output": ""}))
    monkeypatch.setattr(BC.subprocess, "run", fake_run)
    BC.exec_("inst1", "echo 'it\"s $HOME' && ls | wc -l")
    req = json.loads(seen["input"])
    assert base64.b64decode(req["cmd"]).decode() == "echo 'it\"s $HOME' && ls | wc -l"


def test_a_non_json_answer_is_a_refusal(monkeypatch):
    """If the forced command is not what we think it is, the answer will not be one object."""
    monkeypatch.setattr(BC.subprocess, "run",
                        lambda *a, **k: _Proc(out="Last login: Tue ...\n$ "))
    with pytest.raises(BC.BrokerError) as e:
        BC.ping()
    assert "not JSON" in str(e.value)


def test_the_network_is_stated_not_defaulted_silently(monkeypatch):
    """A container that can reach the internet can fetch arbitrary code and send anything out,
    and these run third-party build systems. The choice is per-instance and explicit."""
    seen = {}
    def fake_run(argv, **kw):
        seen["input"] = kw.get("input")
        return _Proc(out=json.dumps({"ok": True, "container": "c", "network": "none"}))
    monkeypatch.setattr(BC.subprocess, "run", fake_run)
    BC.create("i1", "jefzda/sweap-images:x", network="none")
    assert json.loads(seen["input"])["network"] == "none"


def test_an_invented_network_is_refused_before_it_reaches_docker(monkeypatch):
    """docker would happily attach the container to a named network the caller made up."""
    with pytest.raises(BC.BrokerError):
        BC.create("i1", "jefzda/sweap-images:x", network="host")


def test_a_marker_file_can_switch_routing_without_a_restart(tmp_path, monkeypatch):
    """The MCP server reads its environment once at start. Requiring a restart to move
    execution off this machine means the switch is only usable when nothing is running --
    which is exactly the moment nobody reaches for it."""
    monkeypatch.delenv("SWE_BROKER", raising=False)
    marker = tmp_path / "BROKER_ON"
    monkeypatch.setattr(BC, "MARKER", str(marker))
    assert BC.enabled() is False
    marker.write_text("on", encoding="utf-8")
    assert BC.enabled() is True
    marker.unlink()
    assert BC.enabled() is False


def test_a_missing_marker_directory_is_not_an_error(monkeypatch):
    """A switch that raises when its file is absent would fail every call it is asked about."""
    monkeypatch.delenv("SWE_BROKER", raising=False)
    monkeypatch.setattr(BC, "MARKER", "Z:\nope\BROKER_ON")
    assert BC.enabled() is False
