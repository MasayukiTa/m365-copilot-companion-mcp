"""kill_8011 must target ONLY our venv-python endpoint, and _8011_up must not
trust a bare socket.

These cover two ways restart_8011 could go wrong on a shared box:

  * kill_8011 previously matched every python.exe whose command line merely
    contained the substring "relay.openai_endpoint_server". A different python
    that carried that string as an ordinary argument -- or the controller's own
    parent shell -- was a valid kill target. _endpoint_pids is the pure selector
    now; it must require the venv python's absolute path, require the module as
    a real -m run target, and never return an ancestor pid.

  * _8011_up previously reported the port alive on ANY HTTP answer, including a
    plain 401 from something that is not our server. _looks_like_models_payload
    is the body check that replaces that: only the /v1/models envelope counts.

Pure-function tests only -- no process table, no sockets -- so they run on the
Linux CI runner exactly as on Windows.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "retry_controller",
    os.path.join(ROOT, "bench", "gaia", "retry_controller.py"))
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


VENV = r"C:\proj\.venv\Scripts\python.exe"


def _endpoint(rows, ancestors=frozenset(), venv=VENV):
    return rc._endpoint_pids(rows, set(ancestors), venv)


# ---- _endpoint_pids: who kill_8011 is allowed to kill ----------------------

def test_the_venv_python_running_our_module_is_selected():
    rows = [(100, VENV + " -m relay.openai_endpoint_server --port 8011")]
    assert _endpoint(rows) == [100]


def test_a_stray_python_that_only_mentions_the_module_is_left_alone():
    """The exact over-broad match: a different python carrying the module name as
    an argument, e.g. an editor or a grep wrapper. It is NOT our server."""
    rows = [(200, r"C:\\Python311\\python.exe some_tool.py --note relay.openai_endpoint_server")]
    assert _endpoint(rows) == []


def test_the_module_must_be_an_actual_run_target_not_a_substring():
    """venv python is present, but the module appears only inside an unrelated
    argument, not as `-m relay.openai_endpoint_server`."""
    rows = [(210, VENV + " logscan.py --grep relay.openai_endpoint_server.log")]
    assert _endpoint(rows) == []


def test_dash_m_run_target_matches():
    rows = [(220, VENV + "  -m   relay.openai_endpoint_server")]
    assert _endpoint(rows) == [220]


def test_path_style_run_target_matches():
    rows = [(230, VENV + " relay/openai_endpoint_server.py")]
    assert _endpoint(rows) == [230]


def test_our_own_process_and_ancestors_are_never_killed():
    """Killing the controller's own parent shell is the destructive self-match.
    Even a perfect module match must be dropped when the pid is an ancestor."""
    rows = [(300, VENV + " -m relay.openai_endpoint_server")]
    assert _endpoint(rows, ancestors={300}) == []


def test_match_is_case_insensitive_on_the_venv_path():
    rows = [(310, VENV.upper() + " -m relay.openai_endpoint_server")]
    assert _endpoint(rows) == [310]


def test_only_the_endpoint_is_selected_among_several_pythons():
    rows = [
        (400, VENV + " -m relay.openai_endpoint_server --port 8011"),
        (401, r"C:\\Python311\\python.exe unrelated.py"),
        (402, VENV + " -m relay.something_else"),
    ]
    assert _endpoint(rows) == [400]


def test_a_blank_command_line_is_not_a_candidate():
    assert _endpoint([(500, ""), (501, None)]) == []


# ---- _looks_like_models_payload / _8011_up body gate -----------------------

def test_the_models_envelope_is_accepted():
    body = b'{"object": "list", "data": [{"id": "gpt-x"}]}'
    assert rc._looks_like_models_payload(body) is True


def test_an_empty_data_list_is_still_the_envelope():
    assert rc._looks_like_models_payload(b'{"object":"list","data":[]}') is True


def test_a_bare_ok_body_is_not_the_endpoint():
    """Something else on :8011 answering 200 OK with its own text is NOT up."""
    assert rc._looks_like_models_payload(b"OK") is False


def test_a_401_json_error_body_is_not_alive():
    assert rc._looks_like_models_payload(b'{"error":"unauthorized"}') is False


def test_wrong_object_type_is_rejected():
    assert rc._looks_like_models_payload(b'{"object":"model","data":[]}') is False


def test_data_must_be_a_list():
    assert rc._looks_like_models_payload(b'{"object":"list","data":"nope"}') is False


def test_non_json_is_rejected():
    assert rc._looks_like_models_payload(b"<html>503</html>") is False


# ---- the selector must not depend on the host OS --------------------------

def test_the_selector_never_resolves_an_already_absolute_venv_path():
    """THE REGRESSION THESE FIVE TESTS SAT ON. The selector folded the venv path with
    os.path.normcase(os.path.abspath(...)). Off Windows, abspath treats "C:\...\python.exe"
    as relative and prepends the cwd, and normcase lowercases nothing -- so every comparison
    failed and _endpoint_pids returned [] for every row. The five tests above were red on the
    Linux CI runner for as long as that line stood, under a module docstring claiming they run
    there exactly as on Windows.

    Asserting the OUTPUT alone cannot catch it here (this suite runs on Windows, where both
    calls happen to be harmless), so assert the behaviour instead: a drive-letter path is
    already absolute and must never be resolved against the cwd."""
    calls = []
    real_abspath = os.path.abspath

    def spy(p):
        calls.append(p)
        return real_abspath(p)

    os.path.abspath = spy
    try:
        rows = [(600, VENV + " -m relay.openai_endpoint_server")]
        assert _endpoint(rows) == [600]
    finally:
        os.path.abspath = real_abspath
    assert calls == [], "an absolute venv path was resolved against the cwd: %s" % calls


def test_separators_and_case_are_folded_the_windows_way():
    """The command lines are Windows command lines whichever OS reads them, so the folding
    must be explicit rather than borrowed from the host's os.path."""
    assert rc._win_norm("C:/Proj/.Venv/Scripts/Python.exe") == r"c:\proj\.venv\scripts\python.exe"
    assert rc._looks_absolute(r"c:\proj\x") is True
    assert rc._looks_absolute("\\\\host\\share\\x") is True
    assert rc._looks_absolute(r"proj\x") is False


def test_a_forward_slash_venv_path_still_matches_a_backslash_command_line():
    """Same interpreter, written two ways. Folding separators is what makes them one."""
    rows = [(610, VENV + " -m relay.openai_endpoint_server")]
    assert _endpoint(rows, venv=VENV.replace("\\", "/")) == [610]
