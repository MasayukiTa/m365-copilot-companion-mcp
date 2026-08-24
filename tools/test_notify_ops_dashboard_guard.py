"""The guard that decides whether another dashboard may open.

Measured on the live machine: with the ordinary Fleet Cockpit window up -- which is nearly
always -- four consecutive calls to open_authority_dashboard() were all refused with "already
running", and no dashboard ever appeared. One executable serves three windows (the cockpit,
the approval prompt, this dashboard), so matching the image name answers a different question
than the one being asked.

The other half is worth stating in a test too, because the file used to imply otherwise: the
cooldown is module state, so it only sees repeats inside ONE python process. The 24 windows
that started this were 24 separate CLI invocations. What actually bounds the count is the
named mutex FleetCockpit.exe takes on --authority (ui/test_fleet_cockpit_authority_window.py).

Run: pytest -q tools/test_notify_ops_dashboard_guard.py
"""
import tools.notify_ops as N


class _P:
    """Enough of a psutil.Process for process_iter(["name", "cmdline"])."""

    def __init__(self, name, cmdline):
        self.info = {"name": name, "cmdline": cmdline}


def _fake_psutil(monkeypatch, procs):
    import types
    mod = types.SimpleNamespace(process_iter=lambda _fields: list(procs))
    real_import = __import__

    def fake_import(name, *a, **kw):
        if name == "psutil":
            return mod
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)


EXE = "FleetCockpit.exe"


def test_the_ordinary_cockpit_does_not_count_as_the_dashboard(monkeypatch):
    """The whole failure: the cockpit is open, so the control never opens."""
    _fake_psutil(monkeypatch, [_P(EXE, [r"C:\ui\FleetCockpit.exe"])])
    assert N.cockpit_running() is False


def test_the_approval_prompt_does_not_count_either(monkeypatch):
    _fake_psutil(monkeypatch, [_P(EXE, [r"C:\ui\FleetCockpit.exe", "--approval-gate", "x"])])
    assert N.cockpit_running() is False


def test_an_open_dashboard_does_count(monkeypatch):
    """The narrowing must not turn the guard off -- a second window is still refused."""
    _fake_psutil(monkeypatch, [_P(EXE, [r"C:\ui\FleetCockpit.exe", "--authority"])])
    assert N.cockpit_running() is True


def test_it_is_found_among_other_windows_of_the_same_exe(monkeypatch):
    _fake_psutil(monkeypatch, [
        _P(EXE, [r"C:\ui\FleetCockpit.exe"]),
        _P("msedge.exe", ["msedge", "--authority"]),          # right argv, wrong program
        _P(EXE, [r"C:\ui\FleetCockpit.exe", "--authority"]),
    ])
    assert N.cockpit_running() is True


def test_an_unreadable_cmdline_is_not_treated_as_a_dashboard(monkeypatch):
    """Access denied on a foreign owner yields None. One extra window is a small bounded
    harm; silently refusing to open the control is the failure this path exists to prevent."""
    _fake_psutil(monkeypatch, [_P(EXE, None)])
    assert N.cockpit_running() is False


def test_the_program_name_alone_never_decides(monkeypatch):
    """A different program that happens to be passed --authority is not this dashboard."""
    _fake_psutil(monkeypatch, [_P("python.exe", ["python", "x.py", "--authority"])])
    assert N.cockpit_running() is False


def test_the_cooldown_still_short_circuits_a_repeat_in_one_process(monkeypatch):
    import time
    monkeypatch.setattr(N, "_DASHBOARD_LAST", [time.time()])
    assert N._dashboard_already_up() != ""


def test_the_cooldown_is_process_local_and_the_file_says_so():
    """Stated in a test because the docstring used to present these two as the guard, and a
    reader who believed that would not have gone looking for the mutex."""
    from pathlib import Path
    src = (Path(N.__file__)).read_text(encoding="utf-8")
    body = src[src.index("def _dashboard_already_up"):]
    body = body[:body.index("\ndef ")]
    assert "mutex" in body.lower()


def test_opening_the_dashboard_is_inert_under_pytest(monkeypatch):
    """notify_desktop has been inert by construction since it was written; this one launches a
    window on the operator's real desktop and had no such guard. Both are reached from
    pending._notify, so the layer stopped half way across a single call -- today only two test
    files' local fixtures keep it quiet, which is the fail-open shape of a hand-written
    allowlist."""
    called = []
    monkeypatch.setattr(N.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert N.open_authority_dashboard() == ""
    assert called == []


def test_the_guard_is_the_same_one_notify_desktop_uses():
    """Two spellings of "are we under test" is two things to keep in step."""
    import io
    src = io.open(N.__file__, encoding="utf-8").read()
    assert src.count('os.environ.get("PYTEST_CURRENT_TEST")') >= 2


def test_the_summary_cache_is_redirected_for_the_whole_session():
    """A test that regenerated it would replace summaries that cost real model calls, and the
    damage would look like nothing at all: the screen just falls back to raw reasons."""
    from pathlib import Path
    src = (Path(N.__file__).parent.parent / "conftest.py").read_text(encoding="utf-8")
    assert "record_summary" in src
    assert '"CACHE_PATH"' in src
