"""pip_install must not be able to reach the harness's own environment.

On 2026-08-30 07:02 a fleet worker solving an open-source instance installed that project's
requirements through `pip_install`. The function ran `[sys.executable, "-m", "pip", ...]`,
and `sys.executable` is the virtual environment that runs the MCP server, the tool gateway
and the approval gate. pydantic went 2.13 -> 2.1.0 and httpx 0.28.1 -> 0.24.1; the harness
declares httpx>=0.28.1 and stopped importing.

Nothing was bypassed. The unlock gate passed, the flag allowlist passed, the package-name
validation passed, and the function did exactly what its docstring promised -- into the wrong
environment. That is why this is tested at the level of WHICH INTERPRETER IS CHOSEN rather
than by pattern-matching the request.

This is not confinement and does not pretend to be: a worker running as the same user can
still write into site-packages directly. It removes the accident, not the adversary.
"""
import os
import sys

from tools import env_ops as EO


def test_the_harness_environment_is_never_the_target(tmp_path, monkeypatch):
    """With no project environment anywhere, the answer is refusal, not the harness."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert EO._project_interpreter() is None


def test_a_refusal_says_which_environment_it_protected():
    """A refusal a worker cannot act on is a refusal it will route around."""
    import inspect
    src = inspect.getsource(EO.pip_install)
    assert "sys.prefix" in src
    assert "refused" in src


def test_a_checkout_with_its_own_venv_is_used(tmp_path, monkeypatch):
    """The ordinary case must keep working, or the guard gets removed."""
    rel = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
    cand = tmp_path / ".venv" / rel
    cand.parent.mkdir(parents=True)
    cand.write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert EO._project_interpreter() == str(cand)


def test_a_venv_that_IS_the_harness_is_not_accepted(tmp_path, monkeypatch):
    """VIRTUAL_ENV pointing at the harness must not launder it into an allowed target."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    assert EO._project_interpreter() is None


def test_pip_install_no_longer_names_sys_executable_as_the_command():
    """The defect in one line: the command began with the harness interpreter."""
    import inspect
    src = inspect.getsource(EO.pip_install)
    assert '[sys.executable, "-m", "pip"' not in src
    assert '[target, "-m", "pip"' in src


def _make_venv(root):
    rel = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
    cand = os.path.join(str(root), ".venv", rel)
    os.makedirs(os.path.dirname(cand), exist_ok=True)
    with open(cand, "w"):
        pass
    return cand


def test_the_production_venv_is_refused_through_an_aliased_path(tmp_path, monkeypatch):
    """The production checkout's .venv IS the harness environment. Reaching it through a
    different spelling of the same directory -- a symlink here, an 8.3 short name or a
    junction in the field -- must still be refused, not handed back as an install target.

    This is the hole 'refuse pip into production venv' closes: abspath compares two spellings
    of one directory as unequal, so the harness-prefix check passed and the server's own venv
    was offered to pip.
    """
    real = tmp_path / "real"
    real.mkdir()
    _make_venv(real)
    # The harness runs out of real/.venv.
    monkeypatch.setattr(EO, "_HARNESS_PREFIX", EO._canon(str(real / ".venv")))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    # Same directory, reached through a symlink alias. If the platform cannot make one, the
    # canonical-path guarantee is not exercised here -- skip rather than assert nothing.
    alias = tmp_path / "alias"
    try:
        os.symlink(str(real), str(alias), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        import pytest
        pytest.skip("platform does not permit creating a directory symlink")

    assert EO._canon(str(alias)) == EO._canon(str(real))
    monkeypatch.chdir(str(alias))
    assert EO._project_interpreter() is None


def test_an_aliased_worker_checkout_is_still_accepted(tmp_path, monkeypatch):
    """Canonicalising must not over-refuse: a worker's OWN venv, reached through an alias,
    is a different directory from the harness and must remain a valid target."""
    harness = tmp_path / "harness"
    harness.mkdir()
    _make_venv(harness)
    monkeypatch.setattr(EO, "_HARNESS_PREFIX", EO._canon(str(harness / ".venv")))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    work = tmp_path / "work"
    work.mkdir()
    cand = _make_venv(work)
    alias = tmp_path / "work_alias"
    try:
        os.symlink(str(work), str(alias), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        import pytest
        pytest.skip("platform does not permit creating a directory symlink")

    monkeypatch.chdir(str(alias))
    got = EO._project_interpreter()
    assert got is not None
    assert EO._canon(got) == EO._canon(cand)
