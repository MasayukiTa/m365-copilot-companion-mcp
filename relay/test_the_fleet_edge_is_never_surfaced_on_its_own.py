"""The fleet's Edge (:9222) and the evaluation Edge (:9224) are automation browsers: no automatic
code path may put a window of them on the screen.

2026-10-08 11:29: a token-capture helper (relay_fleet._open_fresh) met a sign-in-host URL while the
browser was bouncing through single sign-on and called edge_recover.surface(). The launcher killed
the headless fleet Edge and relaunched it with a window (no --headless, the agent URL), which sat
in front of the owner, and the launch gate ("no browser window") then refused three starts in a row.

The rule these tests pin: surface() is refused for those ports unless the caller says a person is
present; refusal launches nothing; the automatic callers do not claim a surface that did not happen;
the window-less baseline of the launcher is untouched.
"""
import json
import os
import re
import subprocess

import pytest

from relay import edge_recover as R

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _code_only(src):
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


@pytest.fixture
def launched(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(R, "_record_refused_surface",
                        lambda port, url, caller: calls.append(("refused", port)))
    return calls


@pytest.mark.parametrize("port", [9222, 9224])
def test_an_automatic_surface_of_an_automation_edge_is_refused_and_launches_nothing(launched, port):
    assert R.surface(port=port, open_url="https://m365.cloud.microsoft/chat/?titleId=T_x") is False
    assert launched == [("refused", port)], "a refused surface must not run the launcher: %r" % launched


def test_the_default_port_is_the_fleet_edge_and_is_refused(launched):
    assert R.surface() is False
    assert [c for c in launched if c != ("refused", 9222)] == []


def test_a_person_present_surface_still_reaches_the_launcher(monkeypatch, launched):
    monkeypatch.setattr(R, "_headed_process_present", lambda *a, **k: True)
    assert R.surface(port=9222, person_present=True) is True
    argv = [c for c in launched if isinstance(c, list)]
    assert argv and "start_companion_edge.ps1" in " ".join(argv[0])


def test_the_bridge_port_is_not_in_the_hidden_only_set():
    # The bridge's sign-in is a person-facing flow with its own latch (ensure_m365_signin
    # --bridge-watch); this change must not take it away.
    assert R.HIDDEN_ONLY_PORTS == (9222, 9224)


def test_a_refusal_leaves_a_trace(monkeypatch, tmp_path):
    monkeypatch.undo()
    fake_file = tmp_path / "relay" / "edge_recover.py"
    fake_file.parent.mkdir()
    monkeypatch.setattr(R, "__file__", str(fake_file))
    R._record_refused_surface(9222, "https://x", "surface")
    row = json.loads((tmp_path / ".fleet" / "visible_edge_refused.jsonl").read_text("utf-8").splitlines()[0])
    assert row["port"] == 9222 and row["open_url"] == "https://x"


def test_open_fresh_asks_once_and_does_not_claim_a_surface_that_did_not_happen():
    body = _code_only(_src("relay/relay_fleet.py"))
    seg = body[body.index("def _open_fresh"):]
    seg = seg[:seg.index("\ndef ", 10)]
    assert "surfaced = bool(surface(open_url=url))" in seg, \
        "`surfaced = True` regardless of the result keeps the 300 s hidden wait for a sign-in nobody can do"
    assert "surface_tried" in seg, "the sign-in URL is seen every second; ask once, not 75 times"
    assert not re.search(r"surface\(open_url=url\)\s*;\s*surfaced\s*=\s*True", seg)


def test_no_automatic_caller_passes_person_present():
    """Only the interactive helper may. Grep the whole tree (git-tracked python), not a list."""
    out = []
    for top in ("relay", "bridge", "scripts", "tools", "bench", "tests", "ui"):
        for root, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = [d for d in dirs if d not in (".venv", "node_modules", "__pycache__")]
            out.extend(os.path.relpath(os.path.join(root, f), REPO) for f in files if f.endswith(".py"))
    out.append("main.py")
    offenders = []
    for rel in out:
        if rel.endswith(("edge_recover.py",)) or os.path.basename(rel).startswith("test_"):
            continue
        if rel.replace("\\", "/") == "scripts/ensure_m365_signin.py":
            continue
        try:
            text = _code_only(_src(rel))
        except Exception:
            continue
        if "person_present=True" in text:
            offenders.append(rel)
    assert offenders == [], "automatic code asked for a visible fleet browser: %r" % offenders


def test_the_interactive_helper_declares_a_person():
    assert "person_present=True" in _code_only(_src("scripts/ensure_m365_signin.py"))


def test_a_refused_surface_schedules_no_headless_relaunch(monkeypatch):
    """edge_auth's way-back timer ends in a HardReset; after a refusal it would kill a healthy
    hidden browser fifteen minutes later."""
    import threading
    from relay import edge_auth as A
    monkeypatch.setattr(R, "surface", lambda port=None, open_url="", **kw: False)
    started = []
    monkeypatch.setattr(threading, "Timer", lambda *a, **k: started.append(a) or (_ for _ in ()).throw(AssertionError("timer")))
    assert A._surface_with_a_way_back("http://127.0.0.1:9222", "https://x", 5.0) is False
    assert started == []


def test_the_cockpit_never_auto_fixes_sign_in_with_a_window():
    src = _src("ui/FleetCockpit.cs")
    i = src.index("int AutoFixTargetDot()")
    body = _code_only(src[i:i + 2500])
    assert re.search(r"if \(dot == 3\) continue;", body), \
        "the auto-fix may pick the sign-in repair, which relaunches the fleet Edge with a window"


def test_the_launcher_still_defaults_to_headless():
    ps1 = _code_only(_src("scripts/start_companion_edge.ps1"))
    assert "$useHeadless = -not $Foreground" in ps1
    assert '"--headless=new"' in ps1
