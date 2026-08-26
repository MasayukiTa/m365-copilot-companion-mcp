"""What the guard must catch, and what it must not accuse.

A watcher that only draws a screen is read while somebody is watching it and proves nothing
about the hours nobody was. That is how a browser sat headed for ten hours flashing a window
onto the desk, and how a Copilot page nobody was reading held 135 MB through an afternoon of
measurements that never mentioned it. So the judgement is separated from the display and
pinned here.

The second half matters as much as the first: a guard that cries about the user's own browser
is one that gets turned off, and then it catches nothing at all.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "watch_stack", os.path.join(ROOT, "scripts", "win", "watch_stack.py"))
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)


def _sample(**kw):
    s = {"t": "12:00:00", "iso": "2026-08-26T12:00:00", "free_mb": 4000, "mcp_mb": 50,
         "edge_mb": {"copilot-companion-edge": 400}, "pages": {}, "page_urls": {},
         "headed": [], "runs": [], "route": {}}
    s.update(kw)
    return s


def _kinds(s):
    return [k for k, _ in ws.violations(s)]


# ---- the window ---------------------------------------------------------------------------

def test_a_browser_with_a_window_is_a_breach():
    """A headed browser raises itself every time the socket re-keys and opens a tab."""
    assert "HEADED" in _kinds(_sample(headed=["copilot-bridge-edge"]))


def test_a_headless_stack_is_clean():
    assert _kinds(_sample(headed=[])) == []


def test_only_the_browser_process_counts_as_headed():
    """Renderer children do not carry --headless and would every one of them look headed."""
    procs = [
        {"Name": "msedge.exe", "CommandLine":
            "msedge.exe --headless=new --user-data-dir=C:/x/copilot-bridge-edge"},
        {"Name": "msedge.exe", "CommandLine":
            "msedge.exe --type=renderer --user-data-dir=C:/x/copilot-bridge-edge"},
    ]
    assert ws.headed_profiles(procs) == []


def test_a_real_headed_browser_is_found():
    procs = [{"Name": "msedge.exe", "CommandLine":
              "msedge.exe --window-size=1400,1000 --user-data-dir=C:/x/copilot-bridge-edge"}]
    assert ws.headed_profiles(procs) == ["copilot-bridge-edge"]


def test_the_users_own_edge_is_never_reported():
    """It is not ours, it is always headed, and accusing it teaches the reader to ignore this."""
    procs = [{"Name": "msedge.exe", "CommandLine":
              "msedge.exe --user-data-dir=C:/Users/x/AppData/Local/Microsoft/Edge/User Data"}]
    assert ws.headed_profiles(procs) == []


# ---- pages nobody is reading ---------------------------------------------------------------

def test_a_copilot_page_with_no_run_in_flight_is_a_breach():
    s = _sample(page_urls={"eval": ["https://m365.cloud.microsoft/chat"]}, runs=[])
    assert "IDLE_PAGE" in _kinds(s)


def test_a_blank_keepalive_page_is_not():
    """about:blank costs a few MB and exists because Edge exits with its last page."""
    s = _sample(page_urls={"bridge": ["about:blank"]}, runs=[])
    assert "IDLE_PAGE" not in _kinds(s)


def test_a_copilot_page_during_a_run_is_work_not_waste():
    s = _sample(page_urls={"companion": ["https://m365.cloud.microsoft/chat"]},
                runs=[{"pid": 1}])
    assert "IDLE_PAGE" not in _kinds(s)


def test_a_browser_that_is_not_running_is_not_a_breach():
    """None means CDP could not be reached, which is a browser that is down, not a leak."""
    assert _kinds(_sample(page_urls={"eval": None}, runs=[])) == []


# ---- memory --------------------------------------------------------------------------------

def test_the_mcp_server_has_a_ceiling():
    """It is in no fleet status file, which is how it grew past 8 GB unnoticed."""
    assert "MCP_MEM" in _kinds(_sample(mcp_mb=ws.MCP_MB_CEILING + 1))


def test_managed_browsers_have_a_ceiling():
    s = _sample(edge_mb={"copilot-companion-edge": ws.EDGE_MB_CEILING + 1})
    assert "EDGE_MEM" in _kinds(s)


def test_the_users_own_browser_is_not_charged_to_the_stack():
    """The guard's first live run reported a breach for the user's personal Edge."""
    s = _sample(edge_mb={"(default)": 9000, "copilot-companion-edge": 300})
    assert "EDGE_MEM" not in _kinds(s)


def test_low_free_ram_is_a_breach():
    assert "FREE_RAM" in _kinds(_sample(free_mb=ws.FREE_MB_FLOOR - 1))


def test_a_healthy_stack_reports_nothing_at_all():
    """Silence has to mean something, or the log cannot be used as evidence."""
    s = _sample(page_urls={"companion": ["about:blank"], "bridge": ["about:blank"]},
                headed=[], mcp_mb=50, free_mb=5000,
                edge_mb={"copilot-companion-edge": 400, "copilot-bridge-edge": 150})
    assert ws.violations(s) == []


def test_every_breach_carries_the_number_that_caused_it():
    """A breach line that does not say how far over it was cannot be acted on."""
    for _, detail in ws.violations(_sample(mcp_mb=99999, free_mb=1, headed=["copilot-eval-edge"])):
        assert any(ch.isdigit() for ch in detail) or "window" in detail
