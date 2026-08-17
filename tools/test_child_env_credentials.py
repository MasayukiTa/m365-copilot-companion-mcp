"""The child environment: a denylist of names, plus a check on the shape of the value.

A review named several variables the name list misses entirely. `PIP_INDEX_URL` routinely
holds `https://user:credential@host/simple`; so do the package-index, database, cloud and
container equivalents. None of them contains "password", "secret" or "api_key", so all of them
reached the child while the module's docstring said secrets were stripped.

The value check is the part that matters most: a denylist of NAMES is only ever as complete as
the last person to think about it, and the next deployment invents a variable nobody listed.
URI userinfo is how most of these actually carry their secret, so a variable holding one is
withheld whatever it is called.
"""
import pytest

from tools._subproc import sanitized_child_env


@pytest.mark.parametrize("name,value", [
    ("PIP_INDEX_URL", "https://user:credential@internal/simple"),
    ("UV_INDEX_URL", "https://u:p@host/simple"),
    ("DATABASE_URL", "postgres://u:p@db/app"),
    ("AZURE_STORAGE_CONNECTION_STRING", "AccountKey=abc"),
    ("DOCKER_AUTH_CONFIG", "{}"),
    ("KUBECONFIG", "C:/k/config"),
    ("GITHUB_PAT", "ghp_placeholder"),
    ("HTTPS_PROXY", "http://u:p@proxy:8080"),
])
def test_credential_bearing_variables_are_withheld(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert name not in sanitized_child_env()


def test_a_variable_nobody_listed_is_withheld_on_its_VALUE(monkeypatch):
    """The whole point: the list cannot be finished, so the shape has to carry some of it."""
    monkeypatch.setenv("SOME_INTERNAL_THING", "https://user:pw@example.invalid/x")
    assert "SOME_INTERNAL_THING" not in sanitized_child_env()


@pytest.mark.parametrize("name,value", [
    ("PYTHONPATH", "C:/p"),
    ("TEMP", "C:/t"),
    ("MY_PLAIN_URL", "https://example.invalid/no-userinfo"),
])
def test_ordinary_variables_survive(monkeypatch, name, value):
    """A sanitiser that breaks execution gets turned off, which protects nothing."""
    monkeypatch.setenv(name, value)
    assert sanitized_child_env().get(name) == value


def test_path_is_always_kept():
    assert "PATH" in sanitized_child_env()
