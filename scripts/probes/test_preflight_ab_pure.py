# -*- coding: utf-8 -*-
"""The pure parts of the preflight A/B harness: arm order, denial list, disk guard, summary, port rules."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preflight_ab as AB          # noqa: E402
import preflight_ab_server as SRV  # noqa: E402


def test_arms_alternate_and_cover_both():
    assert AB.arm_order(1) == ("on", "off")
    assert AB.arm_order(2) == ("off", "on")
    assert all(set(AB.arm_order(i)) == {"on", "off"} for i in range(1, 12))


def test_write_tools_and_outlook_stay_denied():
    for nm in ("write_file", "edit_and_verify", "delete_file", "fleet_submit", "unlock", "Outlook_send"):
        assert AB.is_denied(nm), nm
    assert AB.is_denied("read_file") == ""
    assert AB.is_denied("") == ""


def test_disk_guard_stops_under_three_gb():
    assert not AB.disk_ok(2.9)
    assert AB.disk_ok(3.0)


def test_private_server_refuses_production_ports():
    for p in (8000, 8765, 9222, 9223, 9224):
        try:
            SRV.pick_port(["x", str(p)])
        except SystemExit:
            continue
        raise AssertionError("port %d was accepted" % p)
    assert SRV.pick_port(["x"]) == 8100
    assert AB.PORT == 8100 and AB.PORT not in AB.FORBIDDEN_PORTS


def test_summary_reports_median_and_spread():
    rows = [{"event": "run", "goal": "a", "arm": "on", "ok": True, "wall_s": w, "turns": t, "tool_calls": 2,
             "preflight_s": 0.3} for w, t in ((10, 2), (20, 3), (30, 4))]
    rows.append({"event": "run", "goal": "a", "arm": "off", "ok": False, "wall_s": 50, "turns": 5, "tool_calls": 3})
    rows.append({"event": "stop"})
    s = AB.summarize(rows)["a"]
    assert s["on"]["wall"]["median"] == 20 and s["on"]["wall"]["min"] == 10 and s["on"]["wall"]["max"] == 30
    assert s["on"]["n"] == 3 and s["on"]["ok"] == 3
    assert s["off"]["n"] == 1 and s["off"]["ok"] == 0 and s["off"]["pre"] is None


def test_scrub_hides_urls_and_tokens():
    out = AB.scrub("see https://example.com/x and eyJabcdefghijklmnopqrstuvwxyz0123")
    assert "example.com" not in out and "eyJ" not in out
