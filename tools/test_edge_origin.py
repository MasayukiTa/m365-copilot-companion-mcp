# -*- coding: utf-8 -*-
"""Tests for tools/trace_view/edge_origin.py.

The operator asked, three times, which process opened a burst of Edges -- with the 2 GB Teams
process explicitly set aside. These tests pin the classification to real msedge command-line
shapes, prove renderers are not counted as launches, prove Teams is excluded from the culprit
search, and prove the summary never carries a raw command line, URL, or profile path.
"""
from __future__ import annotations

from tools.trace_view.edge_origin import (
    attribute_edge_burst,
    child_type,
    classify_edge,
    is_browser_process,
)


# --- real-ish msedge rows --------------------------------------------------------------------

def _fleet_browser(pid, ppid, port="9222", parent="python.exe"):
    return {
        "pid": pid, "ppid": ppid, "parent_name": parent, "name": "msedge.exe",
        "cmdline": (
            "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe "
            "--remote-debugging-port=%s --headless=new "
            "--user-data-dir=C:\\Users\\x\\.fleet\\edge\\9222 --no-first-run" % port
        ),
    }


def _user_browser(pid, ppid, parent="explorer.exe"):
    return {
        "pid": pid, "ppid": ppid, "parent_name": parent, "name": "msedge.exe",
        "cmdline": (
            "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe "
            "--profile-directory=Default"
        ),
    }


def _teams_webview(pid, ppid, parent="ms-teams.exe"):
    return {
        "pid": pid, "ppid": ppid, "parent_name": parent, "name": "msedge.exe",
        "cmdline": (
            "C:\\Program Files\\WindowsApps\\MSTeams\\msedgewebview2...msedge.exe "
            "--embedded-browser-webview=1 "
            "--user-data-dir=C:\\Users\\x\\AppData\\Local\\Packages\\MSTeams\\EBWebView"
        ),
    }


def _renderer(pid, ppid, port="9222", parent="msedge.exe"):
    return {
        "pid": pid, "ppid": ppid, "parent_name": parent, "name": "msedge.exe",
        "cmdline": (
            "msedge.exe --type=renderer --remote-debugging-port=%s "
            "--lang=en-US --device-scale-factor=1" % port
        ),
    }


# --- classification --------------------------------------------------------------------------

def test_a_fleet_debug_port_edge_is_attributed_to_the_fleet():
    assert classify_edge(_fleet_browser(100, 10, port="9222")) == "fleet"
    assert classify_edge(_fleet_browser(101, 10, port="9224")) == "fleet"


def test_a_teams_webview_edge_is_attributed_to_teams():
    assert classify_edge(_teams_webview(200, 20)) == "teams"


def test_a_plain_interactive_edge_is_attributed_to_the_user():
    assert classify_edge(_user_browser(300, 30)) == "user"


def test_a_renderer_child_is_not_a_user_edge_even_on_a_fleet_port():
    # It carries the fleet port, so it is fleet by origin -- but it is a child, not a launch.
    row = _renderer(400, 100, port="9222")
    assert classify_edge(row) == "fleet"
    assert is_browser_process(row) is False
    assert child_type(row) == "renderer"


def test_a_non_edge_row_is_other_and_never_raises():
    assert classify_edge({"name": "chrome.exe", "cmdline": "chrome.exe"}) == "other"
    assert classify_edge({}) == "other"
    assert classify_edge({"name": None, "cmdline": None}) == "other"


# --- the burst attribution -------------------------------------------------------------------

def test_the_parent_that_opened_the_most_edges_is_the_culprit():
    rows = [
        _fleet_browser(100, 10, parent="python.exe"),
        _fleet_browser(101, 10, parent="python.exe"),
        _fleet_browser(102, 10, parent="python.exe"),
        _user_browser(300, 30, parent="explorer.exe"),
    ]
    out = attribute_edge_burst(rows)
    assert out["culprit"]["ppid"] == 10
    assert out["culprit"]["parent"] == "python.exe"
    assert out["culprit"]["edges"] == 3
    assert out["launches"] == 4


def test_renderer_children_are_not_counted_as_launches():
    rows = [
        _fleet_browser(100, 10),
        _renderer(400, 100), _renderer(401, 100), _renderer(402, 100), _renderer(403, 100),
    ]
    out = attribute_edge_burst(rows)
    # One browser process opened; the four renderers are its children, not launches.
    assert out["launches"] == 1
    assert out["culprit"]["edges"] == 1
    # But every msedge.exe is still categorised, renderers included.
    assert out["by_category"]["fleet"] == 5


def test_the_teams_edge_is_excluded_from_the_culprit_search():
    rows = [
        _teams_webview(200, 20), _teams_webview(201, 20), _teams_webview(202, 20),
        _fleet_browser(100, 10),
    ]
    out = attribute_edge_burst(rows)
    # Three Teams browser processes exist and are counted and set aside, not blamed.
    assert out["teams_excluded"] == 3
    assert out["by_category"]["teams"] == 3
    # The culprit is the fleet parent, never the Teams parent.
    assert out["culprit"]["ppid"] == 10
    assert all(p["parent"] != "ms-teams.exe" for p in out["by_parent"])


def test_a_parent_name_can_be_supplied_when_the_row_lacks_it():
    rows = [
        {"pid": 100, "ppid": 4242, "name": "msedge.exe",
         "cmdline": "msedge.exe --remote-debugging-port=9222"},
        {"pid": 101, "ppid": 4242, "name": "msedge.exe",
         "cmdline": "msedge.exe --remote-debugging-port=9222"},
    ]
    out = attribute_edge_burst(rows, parent_names={4242: "relay_fleet.py"})
    assert out["culprit"]["parent"] == "relay_fleet.py"
    assert out["culprit"]["edges"] == 2


def test_by_parent_is_ordered_most_edges_first_and_is_stable():
    rows = [
        _fleet_browser(100, 10), _fleet_browser(101, 10),
        _user_browser(300, 30),
    ]
    out = attribute_edge_burst(rows)
    edges = [p["edges"] for p in out["by_parent"]]
    assert edges == sorted(edges, reverse=True)
    assert out["by_parent"][0]["ppid"] == 10


def test_the_summary_never_carries_a_raw_command_line_url_or_profile_path():
    rows = [
        _fleet_browser(100, 10),
        _teams_webview(200, 20),
        _user_browser(300, 30),
        _renderer(400, 100),
    ]
    out = attribute_edge_burst(rows)
    blob = repr(out)
    assert "--remote-debugging-port" not in blob
    assert "user-data-dir" not in blob
    assert "EBWebView" not in blob
    assert "Program Files" not in blob
    assert ".fleet\\edge" not in blob


def test_an_empty_or_garbage_snapshot_never_raises():
    assert attribute_edge_burst([])["culprit"] is None
    assert attribute_edge_burst(None)["culprit"] is None
    # A msedge row with no command line is still a browser process (no --type=), so it counts
    # as one launch with an unknown parent -- the point here is only that garbage never raises.
    weird = attribute_edge_burst([{"name": "msedge.exe"}, 123, None, {"name": "x"}])
    assert weird["launches"] == 1
    assert weird["culprit"]["parent"] == ""
    assert weird["culprit"]["ppid"] is None


def test_only_browser_processes_become_launches_counted_correctly():
    rows = [
        _fleet_browser(100, 10),
        _renderer(400, 100),
        _user_browser(300, 30),
        _teams_webview(200, 20),
    ]
    out = attribute_edge_burst(rows)
    # Three browser processes (fleet, user, teams), one renderer child.
    assert out["launches"] == 3
    # teams excluded -> two parents remain in the culprit search.
    assert len(out["by_parent"]) == 2
