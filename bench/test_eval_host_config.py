# -*- coding: utf-8 -*-
"""Whether the eval host is CONFIGURED, asked before anything is blamed on the host.

WHAT THIS COST. Every grade for an entire night returned "EVALERR (launch failed)". That reads
as the eval host being down, and it was reported as the eval host being down, repeatedly. The
host was never contacted at all -- `ssh` was being invoked with an empty hostname.

THREE FAIL-OPEN LAYERS, EACH HARMLESS ALONE:
  1. .env was never loaded by the module, so a value written there could not arrive;
  2. the variable the module read and the one .env defined had different names;
  3. the guard that would have said so sat behind `__name__ == "__main__"`, and the batch
     grader IMPORTS the module. The one path anybody uses skipped the check entirely.

A guard that only runs on the path nobody takes is not a guard. That is the general lesson and
it is what these tests hold: every transport entry point refuses on its own when there is no
host, so no caller can reach `ssh` with an empty argument by taking a different route in.

NOT TESTED HERE: whether the host answers. That is a fact about someone else's machine, and
conflating it with configuration is the confusion this file exists to end.
"""
import importlib
import os

import pytest


def load(monkeypatch, **env):
    """Import the module with a controlled environment, and with dotenv neutralised so a real
    .env on the machine running the tests cannot decide the outcome."""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for k in ("EVAL_SSH_HOST", "SWE_EVAL_HOST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import bench.swe_check_remote as R
    return importlib.reload(R)


# -- configuration is a question with its own answer ---------------------------------------

def test_an_unconfigured_host_says_so_in_words(monkeypatch):
    """"launch failed" pointed at the transport. The message has to name the actual cause, and
    say explicitly that nothing was sent anywhere."""
    R = load(monkeypatch)
    why = R.configured()
    assert why
    assert "EVAL_SSH_HOST" in why
    assert "not the host being unreachable" in why


def test_a_configured_host_reports_no_problem(monkeypatch):
    R = load(monkeypatch, EVAL_SSH_HOST="example.invalid")
    assert R.configured() == ""


@pytest.mark.parametrize("name", ["EVAL_SSH_HOST", "SWE_EVAL_HOST"])
def test_both_spellings_of_the_variable_are_accepted(monkeypatch, name):
    """The names diverged and nothing noticed. Accepting both costs one `or` and removes the
    entire class -- but the divergence is also why the reason string names them both."""
    R = load(monkeypatch, **{name: "example.invalid"})
    assert R.SSH_HOST == "example.invalid"


def test_the_environment_wins_over_nothing_at_all(monkeypatch):
    R = load(monkeypatch, EVAL_SSH_HOST="  spaced.invalid  ")
    assert R.SSH_HOST == "spaced.invalid"


# -- every entry point refuses on its own --------------------------------------------------

def test_no_host_means_ssh_is_never_invoked(monkeypatch):
    """THE ACTUAL DEFECT. `ssh -o ... ""` connects to nothing and fails in a way that reads as
    the host's fault."""
    R = load(monkeypatch)
    called = []
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: called.append(a))
    assert R._ssh_ps("'x'") == ""
    assert called == [], "ssh was invoked with no host"


def test_no_host_means_scp_is_never_invoked(monkeypatch, tmp_path):
    R = load(monkeypatch)
    called = []
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: called.append(a))
    p = tmp_path / "f"
    p.write_text("x", encoding="utf-8")
    assert R._scp(str(p), "C:/x") is False
    assert R._scp_from("C:/x", str(tmp_path / "out")) is False
    assert called == [], "scp was invoked with no host"


def test_the_guard_is_not_only_on_the_main_path():
    """The whole reason this went unnoticed: the check existed and the grader imports the
    module, so the check never ran. Asserted on the source because the __main__ branch cannot
    be exercised by importing it -- which is precisely the point being made."""
    import io
    import os as _os
    src = io.open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                "swe_check_remote.py"), encoding="utf-8").read()
    body = src[src.index("def _ssh_ps("):src.index("def _wsl_token(")]
    assert "if not SSH_HOST:" in body, "the transport no longer refuses without a host"


def test_dotenv_is_loaded_so_a_configured_value_can_arrive():
    """The value lived in .env and the module never read it. Every other part of this
    repository loads dotenv; this one did not, and nothing pointed that out."""
    import io
    import os as _os
    src = io.open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                "swe_check_remote.py"), encoding="utf-8").read()
    assert "load_dotenv" in src[:src.index("SSH_HOST =")]
