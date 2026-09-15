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
    ("TEMP", "C:/t"),
    ("MY_PLAIN_URL", "https://example.invalid/no-userinfo"),
])
def test_ordinary_variables_survive(monkeypatch, name, value):
    """A sanitiser that breaks execution gets turned off, which protects nothing."""
    monkeypatch.setenv(name, value)
    assert sanitized_child_env().get(name) == value


def test_the_operators_pythonpath_still_reaches_the_child(monkeypatch):
    """PYTHONPATH is the one ordinary variable something else deliberately adds to.

    It used to be asserted byte-identical alongside TEMP, and that stopped being the right
    question when `_with_pptx_autostamp` began prepending its own directory so the deck stamp
    reaches code the worker composed. Equality then failed while nothing was actually broken --
    the operator's entries were all still there, with one more in front.

    What this test protects is the property the old assertion was standing in for: whatever the
    operator put on PYTHONPATH still reaches the child, in their order. A sanitiser (or a
    feature) that DROPPED or REORDERED their entries would break imports in ways that look like
    the child being broken rather than the environment being edited, and that is what must not
    happen. That something may be prepended is deliberate and documented; that anything of
    theirs may be lost is not.
    """
    import os

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["C:/p", "C:/q"]))
    got = (sanitized_child_env().get("PYTHONPATH") or "").split(os.pathsep)
    assert "C:/p" in got and "C:/q" in got, got
    assert got.index("C:/p") < got.index("C:/q"), ("their order changed", got)


def test_path_is_always_kept():
    assert "PATH" in sanitized_child_env()
