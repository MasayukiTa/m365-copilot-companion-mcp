"""The pre-wedge admission brake must weigh ONLY the fleet's own companion-Edge
processes, not every process whose name contains "msedge". Before this fix the brake
counted len(_msedge_cmdlines()), which also returns msedgewebview2.exe (the embedded
WebView2 runtime used by OS widgets and other apps) and sibling Edge profiles on other
ports. On a machine with 24 companion-owned msedge processes and 36 unrelated webview2
processes the raw count was 60 >= the 40 wedge cap, so the ceiling clamped the fleet to
its single open lane even with ample free RAM and the operator asking for autoscale.

All fakes: synthetic command-line strings only, no psutil, no real processes, no browser.
The wedge cap (40) keeps its ORIGINAL meaning -- it was measured on the companion's own
msedge processes -- so a webview2 process must never add to it.
"""
import os

from relay import edge_recover as ER
from relay import fleet_runner as FR

HUGE = 1 << 29  # the "no ceiling" sentinel is 1<<30; anything above HUGE means "do not clamp"


def _companion_dir():
    # The same owner key owns_edge() uses: LOCALAPPDATA\<profile>.
    os.environ.setdefault("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    return ER.profile_dir(ER._profile_for_port(9222))


def _companion_main():
    return "msedge.exe --user-data-dir=%s --remote-debugging-port=9222" % _companion_dir()


def _companion_child():
    return "msedge.exe --type=renderer --user-data-dir=%s" % _companion_dir()


def _webview2(i=0):
    # A WebView2 host of some OTHER application -- different profile, name msedgewebview2.exe.
    return ("msedgewebview2.exe --type=renderer "
            "--user-data-dir=C:\\Users\\x\\AppData\\Local\\Packages\\"
            "MicrosoftWindows.Client.CBS_%d\\EBWebView" % i)


def _sibling_profile_edge():
    # A real msedge.exe, but the interactive bridge profile on another port: not the fleet's.
    d = ER.profile_dir("copilot-bridge-edge")
    return "msedge.exe --user-data-dir=%s --remote-debugging-port=9223" % d


# ---- population: fleet_companion_proc_count counts ONLY companion-owned processes ----

def test_counts_only_companion_owned_processes():
    owned = [_companion_main()] + [_companion_child() for _ in range(8)]   # 1 main + 8 children = 9
    foreign = [_webview2(i) for i in range(36)] + [_sibling_profile_edge()]
    cmds = foreign + owned
    assert ER.fleet_companion_proc_count(cmds, 9222) == 9


def test_webview2_never_counts():
    cmds = [_webview2(i) for i in range(50)]
    assert ER.fleet_companion_proc_count(cmds, 9222) == 0


# ---- the EXACT observed situation must NOT clamp ----

def test_observed_24_msedge_plus_36_webview2_does_not_clamp():
    # 24 companion-owned msedge (1 main + 23 children) + 36 webview2, 3300 MB free RAM.
    owned = [_companion_main()] + [_companion_child() for _ in range(23)]
    cmds = [_webview2(i) for i in range(36)] + owned
    own_n = ER.fleet_companion_proc_count(cmds, 9222)
    assert own_n == 24                      # not 60
    assert own_n < FR.EDGE_WEDGE_PROC_CAP   # 24 < 40
    assert FR._preemptive_tab_ceiling(1, 3300, own_n) > HUGE   # no clamp


# ---- a real wedge-like situation (premise still holds) MUST clamp ----

def test_real_companion_wedge_still_clamps():
    # The companion browser itself has >= cap processes: the premise the brake was built for.
    owned = [_companion_main()] + [_companion_child() for _ in range(FR.EDGE_WEDGE_PROC_CAP)]
    cmds = [_webview2(i) for i in range(5)] + owned
    own_n = ER.fleet_companion_proc_count(cmds, 9222)
    assert own_n >= FR.EDGE_WEDGE_PROC_CAP
    assert FR._preemptive_tab_ceiling(3, 8000, own_n) == 3   # clamp to open count


def test_low_free_ram_still_clamps_regardless_of_process_count():
    # RAM floor arm is independent of the process count: few processes but RAM at/under floor.
    assert FR._preemptive_tab_ceiling(2, FR.EDGE_WEDGE_RAM_FLOOR_MB, 1) == 2


# ---- the clamp reason is reported ----

def test_clamp_reason_is_reported_via_brake_box():
    # Mirror the admission wiring: the brake records a {clamped, reason, counts} dict so
    # status.json and the one-line log can show WHY, and release clears it.
    brake_box = [None]
    free_mb, own_n, socket_cap = 8000, FR.EDGE_WEDGE_PROC_CAP + 4, 50
    ceiling = FR._preemptive_tab_ceiling(3, free_mb, own_n)
    clamped = ceiling < socket_cap
    assert clamped
    reason = ("free RAM %s MB <= floor" % free_mb) if free_mb <= FR.EDGE_WEDGE_RAM_FLOOR_MB         else ("companion msedge %s >= cap" % own_n)
    brake_box[0] = {"clamped": True, "reason": reason, "free_mb": free_mb,
                    "companion_msedge": own_n, "ceiling": ceiling}
    assert brake_box[0]["clamped"] is True
    assert "companion msedge" in brake_box[0]["reason"]
    assert brake_box[0]["companion_msedge"] == own_n


def test_snapshot_surfaces_admission_brake_field():
    # _snapshot must carry the admission_brake field next to max_concurrent.
    state = {"clamped": True, "reason": "companion msedge 44 >= cap", "free_mb": 8000,
             "companion_msedge": 44, "ceiling": 3}
    snap = FR._snapshot([], 0.0, 0, max_concurrent=3, admission_brake=state)
    assert snap["admission_brake"] == state
