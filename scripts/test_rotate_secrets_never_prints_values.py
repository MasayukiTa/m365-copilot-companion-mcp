from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location('rotate_secrets_no_stdout_test', HERE / 'rotate_secrets.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rotate_source_never_interpolates_new_secret_values_into_print():
    src = (HERE / 'rotate_secrets.py').read_text(encoding='utf-8')
    assert 'Bearer {new_api_key}' not in src
    assert 'New unlock password: {new_unlock}' not in src
    assert 'copilot_studio_values.bat' in src


def test_rotate_output_is_redacted_even_without_no_print(tmp_path, monkeypatch, capsys):
    mod = _load()
    env = tmp_path / '.env'
    env.write_text('MCP_API_KEY_PROTECTED=dpapi:old-api\nMCP_UNLOCK_PASSWORD_PROTECTED=dpapi:old-unlock\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'ENV_PATH', env)
    monkeypatch.setattr(mod, 'ENV_BAK_PATH', tmp_path / '.env.bak')
    monkeypatch.setattr(mod, 'gen_api_key', lambda: 'API-SENTINEL-SECRET')
    monkeypatch.setattr(mod, 'gen_unlock_password', lambda: 'UNLOCK-SENTINEL-SECRET')
    monkeypatch.setattr(mod, 'protect_secret', lambda v: 'dpapi:protected-' + v)

    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert 'API-SENTINEL-SECRET' not in out
    assert 'UNLOCK-SENTINEL-SECRET' not in out
    assert 'copilot_studio_values.bat' in out


def test_no_print_flag_remains_accepted_but_never_changes_redaction(tmp_path, monkeypatch, capsys):
    mod = _load()
    env = tmp_path / '.env'
    env.write_text('MCP_API_KEY_PROTECTED=dpapi:old-api\nMCP_UNLOCK_PASSWORD_PROTECTED=dpapi:old-unlock\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'ENV_PATH', env)
    monkeypatch.setattr(mod, 'ENV_BAK_PATH', tmp_path / '.env.bak')
    monkeypatch.setattr(mod, 'gen_api_key', lambda: 'API-SENTINEL-SECRET')
    monkeypatch.setattr(mod, 'gen_unlock_password', lambda: 'UNLOCK-SENTINEL-SECRET')
    monkeypatch.setattr(mod, 'protect_secret', lambda v: 'dpapi:protected-' + v)

    assert mod.main(['--no-print']) == 0
    out = capsys.readouterr().out
    assert 'API-SENTINEL-SECRET' not in out
    assert 'UNLOCK-SENTINEL-SECRET' not in out
