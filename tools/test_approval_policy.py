from pathlib import Path

from tools import approval_policy


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
