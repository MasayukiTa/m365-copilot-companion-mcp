"""The approval mode, arranged rather than inherited.

THESE TESTS SET %APPDATA% AND NOTHING ELSE, which was enough while the shared settings file
lived under %APPDATA%. It moved into the repository on 2026-09-17 (the panel and the fleet had
been reading two different files at one absolute path for a month), and the resolver prefers
the new location WHEN IT EXISTS. So the moment a real settings file appeared on this machine,
every test here quietly started reading the operator's own choices: the first one to notice
failed with `assert 'auto' == 'bypass'` -- 'auto' being what the operator had just selected in
the panel, half a minute earlier.

That is the third time today a test read the machine instead of its fixture. The rule it
keeps re-teaching: a test that arranges only part of the environment is a test that inherits
the rest, and what it inherits is invisible until the day it differs.
"""
from pathlib import Path

import pytest

from tools import approval_policy
from tools import settings_path


@pytest.fixture(autouse=True)
def _settings_are_arranged(monkeypatch, tmp_path: Path):
    """Point BOTH locations at this test's tmp directory.

    Autouse because the next test added here will set %APPDATA% and forget the other half --
    that is exactly what happened, and a fixture is the only version of this rule that holds
    without being remembered.
    """
    monkeypatch.setattr(settings_path, "NEW_PATH",
                        str(tmp_path / "repo" / ".config" / "settings.txt"))
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_env_default_when_no_ui_setting(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("TASK_JOB_APPROVAL_MODE", "auto")
    assert approval_policy.current_approval_mode() == "auto"


def test_ui_setting_overrides_env_live(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("TASK_JOB_APPROVAL_MODE", "default")
    settings = tmp_path / "copilot-bridge" / "settings.txt"
    settings.parent.mkdir(parents=True)
    settings.write_text("job_approval_mode=bypass\n", encoding="utf-8")
    assert approval_policy.current_approval_mode() == "bypass"
    settings.write_text("job_approval_mode=auto\n", encoding="utf-8")
    assert approval_policy.current_approval_mode() == "auto"


def test_invalid_mode_never_becomes_bypass(monkeypatch, tmp_path: Path):
    """THE INVARIANT THAT SURVIVES A CHANGE OF DEFAULT.

    This asserted `== "default"` and was named "fails closed". The three modes are not totally
    ordered, so that was never what it proved: `auto` REFUSES a STOP-pattern operation that
    `default` merely puts to a human, who can approve it. `auto` is stricter at the dangerous
    end and looser only where the deterministic classifier finds nothing.

    What matters about an unrecognised value is that it cannot silently turn confirmation off,
    and that it lands somewhere the code recognises. It resolves to the RECOMMENDED mode
    because falling back to the one nobody reads is the appearance of safety, not safety.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("TASK_JOB_APPROVAL_MODE", "not-a-mode")
    mode = approval_policy.current_approval_mode()
    assert mode != "bypass", "a typo in the setting turned confirmation off"
    assert mode in approval_policy.VALID_APPROVAL_MODES
    assert mode == approval_policy.FALLBACK_APPROVAL_MODE


def test_an_empty_setting_is_treated_as_unset(monkeypatch, tmp_path: Path):
    """Same reasoning as above, for the shape a half-written .env produces."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("TASK_JOB_APPROVAL_MODE", "   ")
    assert approval_policy.current_approval_mode() == approval_policy.FALLBACK_APPROVAL_MODE
