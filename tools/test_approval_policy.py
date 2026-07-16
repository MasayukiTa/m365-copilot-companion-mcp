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


def test_invalid_mode_fails_closed(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("TASK_JOB_APPROVAL_MODE", "not-a-mode")
    assert approval_policy.current_approval_mode() == "default"
