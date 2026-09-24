# -*- coding: utf-8 -*-
r"""start_all.bat clicked ten times: exactly one bring-up, nobody left queued (2026-09-24).

The owner: "start_all.bat がユーザに10回くらいクリックされたら同じこと起こるね。こうしたテストが
できていない". What happened that day: a stack of full start_alls each showed its banner ("Another
startup is already running -- waiting for it to finish...") and queued on the single-instance lock
for ~20 minutes, each redoing the whole bring-up after the one before it. A person double-clicking
the desktop icon, or clicking it ten times because nothing seemed to happen, gets exactly that.

WHAT RUNS. A THROWAWAY tree holding this checkout's start_all.bat, start_all_hidden.vbs,
preflight_policy.ps1 and a copy of start_all.ps1 with three test-only changes made to the COPY:
  * the machine-wide lock name is replaced by a unique one, so nothing here can collide with (or
    be mistaken by) the production start_all / cockpit on this machine;
  * right after Invoke-Startup has the lock, the bring-up is replaced by Invoke-TestBringUp, which
    records "bringup-start" and sleeps -- the "full bring-up" counted below;
  * the UI surfaces (the banner, bringing a banner forward, the "already in progress" notice) are
    replaced by functions that RECORD what would have been shown, so no window ever appears.
Everything else -- the .bat, the hidden .vbs launch, the lineage, the lock, the entry decision,
start_all_runs.jsonl -- is the real code. No devtunnel, Edge, server or real lock is touched.

ASSERTED, per the owner: exactly one bring-up; every other invocation exits within a bound; no
process of the tree is left afterwards; at most one banner; start_all_runs.jsonl has ten lines,
one "got" and nine "already running". Plus the cases that must STILL wait: a full launch behind a
background (-NoUi) one (only ONE such waiter), and the post-update re-exec hand-over.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from _install_path_harness import crlf_copy, minimal_path, clean_env, _PS51_DEFAULT_MODULE_PATH  # noqa: E402
from tools import childproc  # noqa: E402

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.path.isfile(POWERSHELL),
                                reason="start_all is Windows-only (os.name=%r)" % os.name)

LOCK_BASE = "Global\\m365-copilot-companion-start-all"
BRINGUP_SEC = 6
#: What the CODE of a leaving copy may spend: from the script's first line ("ts" in
#: start_all_runs.jsonl) to its "already running" record ("end") -- the owner's "~2 s", with
#: headroom for other suites loading the machine. PowerShell's own start-up before the first
#: line ("proc_ts" -> "ts") is not the script's to spend; it is reported ("leaver_total_max_s")
#: and not bounded. Measured under load, 2026-09-24: this was 5.3-6.6 s while the lineage used
#: Get-CimInstance (2.7 s) and the run log was serialised through ConvertTo-Json (up to 2.5 s);
#: after those two changes 1.1 s under the same load.
LEAVE_BOUND_SEC = 3.0

_COPY = ["start_all.bat", "scripts/start_all_hidden.vbs", "scripts/preflight_policy.ps1",
         "scripts/win/wsh_vbs_check.ps1",
         "scripts/tunnel_name_util.ps1", "scripts/update_recovery.ps1",
         "scripts/win/env_defaults.ps1", "scripts/win/convenience_marker.ps1"]

TEST_HOOKS = r'''
# ---- inserted by scripts/test_start_all_ten_clicks.py into a THROWAWAY copy only ----
function Write-TestEvent([string]$Kind, [string]$Extra = "") {
    $p = Join-Path $root ".setup\test_events.log"
    $line = ("{0} {1} {2} {3}" -f [DateTime]::UtcNow.Ticks, $Kind, $PID, $Extra)
    for ($i = 0; $i -lt 100; $i++) {
        try { [IO.File]::AppendAllText($p, $line + "`r`n"); break } catch { Start-Sleep -Milliseconds 20 }
    }
}
function Invoke-TestBringUp {
    Write-TestEvent "bringup-start"
    Start-Sleep -Seconds ([int]$env:TEST_BRINGUP_SEC)
    Write-TestEvent "bringup-end"
}
function Start-Splash { Write-TestEvent "banner"; return @{ Form = $null; Status = $null; Start = (Get-Date) } }
function Test-TunnelServing { }
function Show-RunningBanner([int]$BudgetMs = 1200) {
    foreach ($role in @("holder", "waiter")) {
        $r = Read-StartAllRoleRecord $role
        if ($r -and $r.banner) { Write-TestEvent "front" ("role=" + $role + " pid=" + $r.pid); return [int]$r.pid }
    }
    return 0
}
function Show-AlreadyRunningNotice([int]$Ms = 1500) { Write-TestEvent "notice" }
# ---- end of test hooks ----
'''


def make_copy_text(src: str, lock_name: str) -> str:
    assert src.count(LOCK_BASE) >= 1, "start_all.ps1 no longer names the start-all lock"
    out = src.replace(LOCK_BASE, lock_name)
    i = out.index("function Invoke-Startup")
    j = out.index("\n    Ensure-EnvDefaults", i)
    out = out[:j] + "\n    Invoke-TestBringUp; return" + out[j:]
    k = out.index("# Drive startup. Prefer a MODAL splash")
    assert out.count("# Drive startup. Prefer a MODAL splash") == 1
    out = out[:k] + TEST_HOOKS + "\n" + out[k:]
    # AND right before the entry decision, which runs near the TOP of the script, above the real
    # Start-Splash etc. -- hence the hooks twice: this copy wins over the banner/notice functions
    # the leaving path calls, the later one over the definitions that come after the entry.
    e = out.index("# ONE STARTUP AT A TIME, DECIDED")
    assert out.count("# ONE STARTUP AT A TIME, DECIDED") == 1
    return out[:e] + TEST_HOOKS + "\n" + out[e:]


def build_tree(base: Path, start_all_text: str) -> Path:
    tree = Path(base) / ("t" + uuid.uuid4().hex[:6])
    for rel in _COPY:
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".bat"):
            crlf_copy(Path(REPO) / rel, dst)
        else:
            shutil.copyfile(os.path.join(REPO, rel), dst)
    lock = "Global\\m365-test-start-all-%s" % uuid.uuid4().hex
    (tree / "scripts" / "start_all.ps1").write_bytes(
        make_copy_text(start_all_text, lock).encode("utf-8"))
    (tree / ".setup" / "logs").mkdir(parents=True, exist_ok=True)
    return tree


def tree_env(tree: Path, **extra) -> dict:
    env = clean_env(tree, minimal_path(), TEST_BRINGUP_SEC=BRINGUP_SEC, **extra)
    for k in ("MCP_STARTALL_REEXEC", "MCP_STARTALL_HANDOFF_PID", "M365_LAUNCHED_BY_START_ALL"):
        env.pop(k, None)
    # Windows PowerShell 5.1's own module path, whatever shell runs pytest (d0190f9).
    for k in [k for k in env if k.upper() == "PSMODULEPATH"]:
        del env[k]
    env["PSModulePath"] = _PS51_DEFAULT_MODULE_PATH
    return env


def tree_processes(tree: Path) -> list:
    """(pid, name) of every process whose command line names the tree. -EncodedCommand, so
    this query's own command line does not contain the path it looks for."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
              "$_.CommandLine.IndexOf('%s', [StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
              "ForEach-Object { '' + $_.ProcessId + '|' + $_.Name }" % str(tree).replace("'", "''"))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = childproc.run([POWERSHELL, "-NoProfile", "-EncodedCommand", enc], timeout=120)
    out = []
    for line in r.stdout.splitlines():
        if "|" in line:
            p, n = line.strip().split("|", 1)
            out.append((int(p), n))
    return out


def kill_tree_processes(tree: Path) -> None:
    for pid, _ in tree_processes(tree):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _read_run_spool(tree: Path) -> list:
    """Not-yet-merged leave records (Write-StartAllRunRecordSpooled / Merge-StartAllRunSpool,
    2026-09-25): a leaver writes here, not to start_all_runs.jsonl directly any more, and only
    the next holder's bring-up folds these in. A reader wanting "every run so far" -- this test,
    scripts/ensure_m365_signin.py's _last_start_all_began -- must read both, the same as a
    reader with production consequences would have to."""
    d = tree / ".setup" / "logs" / "start_all_runs.d"
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (PermissionError, FileNotFoundError, ValueError):
            continue      # a write in flight right now: the NEXT poll (or the eventual merge) sees it
    return out


def read_runs(tree: Path) -> list:
    p = tree / ".setup" / "logs" / "start_all_runs.jsonl"
    # Read while copies replace the file (temp + rename): for a moment it is not openable
    # (PermissionError on Windows while the rename is pending). Retry, never fail on that.
    for _ in range(50):
        if not p.is_file():
            merged = []
        else:
            try:
                merged = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            except (PermissionError, FileNotFoundError, ValueError):
                time.sleep(0.1)
                continue
        # DEDUPED BY PID: Merge-StartAllRunSpool writes the merged line to start_all_runs.jsonl
        # and only THEN deletes the spool file, two separate operations -- a poll landing in
        # that gap would otherwise see the same run in both lists and double-count it.
        combined = merged + _read_run_spool(tree)
        seen = set()
        out = []
        for r in combined:
            key = r.get("pid")
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out
    raise AssertionError("start_all_runs.jsonl stayed unreadable for 5 s")


def read_events(tree: Path) -> list:
    p = tree / ".setup" / "test_events.log"
    if not p.is_file():
        return []
    out = []
    for l in p.read_text(encoding="utf-8").splitlines():
        parts = l.split(" ", 3)
        if len(parts) >= 3:
            out.append({"ticks": int(parts[0]), "kind": parts[1], "pid": int(parts[2]),
                        "extra": parts[3] if len(parts) > 3 else ""})
    return out


def wait_for_runs(tree: Path, n: int, timeout: float) -> list:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = read_runs(tree)
        if len(runs) >= n:
            return runs
        time.sleep(0.5)
    return read_runs(tree)


def wait_until_no_processes(tree: Path, timeout: float = 30) -> list:
    deadline = time.time() + timeout
    left = tree_processes(tree)
    while left and time.time() < deadline:
        time.sleep(1)
        left = tree_processes(tree)
    return left


def _parse_ts(s: str) -> float:
    # "yyyy-MM-ddTHH:mm:ss.fff+09:00"
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def summarize(tree: Path, runs: list, t_launch: float, t_all_done: float) -> dict:
    ev = read_events(tree)
    kinds = [e["kind"] for e in ev]
    durations = [round(_parse_ts(r["end"]) - _parse_ts(r["ts"]), 2) for r in runs]
    leavers = [round(_parse_ts(r["end"]) - _parse_ts(r["ts"]), 2) for r in runs
               if r.get("outcome") == "already running"]
    leaver_totals = [round(_parse_ts(r["end"]) - _parse_ts(r["proc_ts"]), 2) for r in runs
                     if r.get("outcome") == "already running" and r.get("proc_ts")]
    return {
        "runs": len(runs),
        "bringups": kinds.count("bringup-start"),
        "banners": kinds.count("banner"),
        "fronts": kinds.count("front"),
        "notices": kinds.count("notice"),
        "lock": sorted(r.get("lock") for r in runs),
        "outcomes": sorted(r.get("outcome") for r in runs),
        "durations_s": sorted(durations),
        "leaver_max_s": max(leavers) if leavers else None,
        "leaver_total_max_s": max(leaver_totals) if leaver_totals else None,
        "wall_s": round(t_all_done - t_launch, 1),
        # Which leaving copies showed nothing (neither brought a banner forward nor the notice),
        # and what each leaving copy's UI step was -- so a count that is off says who and why.
        "leavers_without_ui": sorted(r["pid"] for r in runs if r.get("outcome") == "already running"
                                     and not any(e["pid"] == r["pid"] and e["kind"] in ("front", "notice")
                                                 for e in ev)),
        "ui_events": sorted("%s:%s:%s" % (e["pid"], e["kind"], e["extra"]) for e in ev
                            if e["kind"] in ("front", "notice")),
    }


# ------------------------------------------------------------------------------ launchers

def launch_bat(tree: Path, env: dict, count: int, gap: float) -> list:
    procs = []
    for i in range(count):
        procs.append(subprocess.Popen(["cmd", "/c", str(tree / "start_all.bat")], cwd=str(tree),
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if gap:
            time.sleep(gap)
    for p in procs:
        p.wait(timeout=120)          # the .bat returns once it has handed off (wscript / Start-Process)
    return procs


def launch_ps(tree: Path, env: dict, count: int, gap: float, *switches) -> list:
    procs = []
    for i in range(count):
        procs.append(subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(tree / "scripts" / "start_all.ps1"), *switches],
            cwd=str(tree), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if gap:
            time.sleep(gap)
    return procs


def wait_for_holder(tree: Path, timeout: float = 60) -> None:
    """Until the first copy holds the lock and has said so (.setup/start_all_holder.json)."""
    p = tree / ".setup" / "start_all_holder.json"
    deadline = time.time() + timeout
    while not p.is_file() and time.time() < deadline:
        time.sleep(0.2)
    assert p.is_file(), "the first copy never took the lock"


def run_scenario(tree: Path, launcher, expect_runs: int, timeout: float = 240) -> dict:
    t0 = time.time()
    try:
        launcher()
        runs = wait_for_runs(tree, expect_runs, timeout)
        left = wait_until_no_processes(tree)
        t1 = time.time()
        s = summarize(tree, runs, t0, t1)
        s["left_over"] = left
        return s
    finally:
        kill_tree_processes(tree)


def _current_text() -> str:
    # TEN_CLICKS_START_ALL_SRC: a mutated COPY, for mutation checks; never the live file edited.
    src = os.environ.get("TEN_CLICKS_START_ALL_SRC") or os.path.join(REPO, "scripts", "start_all.ps1")
    return Path(src).read_text(encoding="utf-8")


def _extract(text: str, name: str) -> str:
    idx = text.index("function " + name)
    depth = 0
    for i in range(text.index("{", idx), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[idx:i + 1]
    raise AssertionError(name)


def test_who_waits_and_who_leaves_truth_table(tmp_path):
    """Get-StartAllBusyAction, exhaustively over the modes a copy and a holder can have."""
    src = _current_text()
    fns = _extract(src, "Get-RankOfStartAllMode") + "\n" + _extract(src, "Get-StartAllBusyAction")
    modes = ["full", "background (-NoUi)", "core (-CoreOnly)"]
    body = fns + "\n$o = @{}\n"
    for m in modes:
        for h in modes + [""]:
            for ho in (False, True):
                body += "$o['%s|%s|%s'] = Get-StartAllBusyAction -Mode '%s' -HolderMode '%s' -Handoff $%s\n" % (
                    m, h, ho, m, h, "true" if ho else "false")
    body += "'RESULT:' + ($o | ConvertTo-Json -Compress)\n"
    p = tmp_path / "tt.ps1"
    p.write_text(body, encoding="utf-8-sig")
    r = childproc.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                      timeout=120)
    line = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")][-1]
    got = json.loads(line[len("RESULT:"):])
    rank = {"full": 3, "background (-NoUi)": 2, "core (-CoreOnly)": 1, "": 2}
    for key, act in got.items():
        m, h, ho = key.split("|")
        if ho == "True" or m == "core (-CoreOnly)":
            want = "wait"                 # the re-exec hand-over; quickstart's synchronous step
        elif rank[h] >= rank[m]:
            want = "leave"                # the running startup already does what this one would
        else:
            want = "wait-one"             # needs more (e.g. windows after a background start)
        assert act == want, (key, act, want)


# ------------------------------------------------------------------------------ the tests

def test_the_role_record_round_trips_without_convertfrom_json(tmp_path):
    """The holder/waiter record is written and read in a fixed shape (no ConvertFrom-Json: its
    first call costs ~0.33 s in a leaving copy). Round trip, and anything else is refused."""
    src = _current_text()
    fns = _extract(src, "ConvertTo-StartAllRoleJson") + "\n" + _extract(src, "ConvertFrom-StartAllRoleJson")
    body = fns + r"""
$t = ConvertTo-StartAllRoleJson 4242 638000000000000000 'background (-NoUi)' $true 197612
$r = ConvertFrom-StartAllRoleJson $t
$bad = @((ConvertFrom-StartAllRoleJson ''), (ConvertFrom-StartAllRoleJson '{"pid":"x"}'), (ConvertFrom-StartAllRoleJson ($t + 'junk')))
'RESULT:' + (@{ t = $t; pid = $r.pid; started = [string]$r.started; mode = $r.mode; banner = $r.banner; hwnd = $r.hwnd
               bad = @($bad | Where-Object { $_ }).Count; json = ($t | ConvertFrom-Json).mode } | ConvertTo-Json -Compress)
"""
    p = tmp_path / "rt.ps1"
    p.write_text(body, encoding="utf-8-sig")
    r = childproc.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)], timeout=120)
    got = json.loads([l for l in r.stdout.splitlines() if l.startswith("RESULT:")][-1][len("RESULT:"):])
    assert got["pid"] == 4242 and got["started"] == "638000000000000000", got
    assert got["mode"] == "background (-NoUi)" and got["banner"] is True and got["hwnd"] == 197612, got
    assert got["bad"] == 0, got
    assert got["json"] == "background (-NoUi)", "the fixed shape is still JSON other readers can parse"


def _assert_one_bringup(s: dict, banners: int, fronts: int):
    assert s["runs"] == 10, s
    assert s["bringups"] == 1, s
    assert s["banners"] == banners, s
    assert s["fronts"] == fronts and s["notices"] == 0, json.dumps(s)
    assert s["lock"].count("got") == 1 and s["lock"].count("busy") == 9, s
    assert s["outcomes"].count("already running") == 9, s
    assert s["leaver_max_s"] is not None and s["leaver_max_s"] <= LEAVE_BOUND_SEC, s
    assert s["left_over"] == [], s


@pytest.mark.parametrize("wsh", ["real", "disabled"])
def test_ten_concurrent_clicks_start_one_bringup(tmp_path, wsh):
    """Ten `start_all.bat` at once. "disabled" forces the .bat's PowerShell fallback (WSH off)."""
    tree = build_tree(tmp_path, _current_text())
    extra = {} if wsh == "real" else {"PREFLIGHT_TEST_WSH_ENABLED": "0"}
    env = tree_env(tree, **extra)
    s = run_scenario(tree, lambda: launch_bat(tree, env, 10, 0), 10)
    print("RESULT", json.dumps(s))
    _assert_one_bringup(s, banners=1, fronts=9)


def test_ten_clicks_200ms_apart_start_one_bringup(tmp_path):
    tree = build_tree(tmp_path, _current_text())
    env = tree_env(tree)
    s = run_scenario(tree, lambda: launch_bat(tree, env, 10, 0.2), 10)
    print("RESULT", json.dumps(s))
    _assert_one_bringup(s, banners=1, fronts=9)


def test_ten_concurrent_background_repairs_start_one_bringup(tmp_path):
    """The cockpit auto-repair path: `start_all.ps1 -NoUi -NoSplash`, ten at once. Silent:
    no banner, nothing brought forward, no notice."""
    tree = build_tree(tmp_path, _current_text())
    env = tree_env(tree)
    procs = []

    def go():
        procs.extend(launch_ps(tree, env, 10, 0, "-NoUi", "-NoSplash"))
        for p in procs:
            p.wait(timeout=240)

    s = run_scenario(tree, go, 10)
    print("RESULT", json.dumps(s))
    assert all(p.returncode == 0 for p in procs), [p.returncode for p in procs]
    _assert_one_bringup(s, banners=0, fronts=0)


def test_clicks_during_a_background_start_queue_exactly_one_that_opens_the_windows(tmp_path):
    """The case the lock was written to WAIT for: a double-click during a background logon start
    (-NoUi) still has to open the windows. One full copy waits behind it; the other eight leave
    and bring that waiter's banner forward. Two bring-ups, not ten."""
    tree = build_tree(tmp_path, _current_text())
    env = tree_env(tree)
    bg = []

    def go():
        # The background start runs longer than the clicks take to arrive, and they are sent
        # only once it holds the lock -- otherwise a click can find it already finished.
        bg.extend(launch_ps(tree, dict(env, TEST_BRINGUP_SEC="15"), 1, 0, "-NoUi", "-NoSplash"))
        wait_for_holder(tree)
        launch_bat(tree, env, 9, 0)

    s = run_scenario(tree, go, 10, timeout=300)
    print("RESULT", json.dumps(s))
    assert s["runs"] == 10, s
    assert s["bringups"] == 2, s
    assert s["banners"] == 1, s                      # the one waiter's
    assert s["fronts"] == 8 and s["notices"] == 0, s
    assert s["lock"].count("got") == 1 and s["lock"].count("got after waiting") == 1, s
    assert s["outcomes"].count("already running") == 8, s
    assert s["leaver_max_s"] <= LEAVE_BOUND_SEC, s
    assert s["left_over"] == [], s


def test_the_post_update_handover_still_waits(tmp_path):
    """Invoke-PostUpdateTail hands the startup to a fresh copy (MCP_STARTALL_HANDOFF_PID = the
    old process). If a click took the lock in between, the fresh copy must still run after it --
    it IS the startup that was in progress. A copy that merely INHERITED the variable (its parent
    is not that pid) is not a hand-over and leaves like any other."""
    tree = build_tree(tmp_path, _current_text())
    env = tree_env(tree)
    ps1 = str(tree / "scripts" / "start_all.ps1").replace("'", "''")
    # A parent powershell that plays the old process: sets the variable to ITS pid and starts
    # the fresh copy as its child; a second child gets a variable naming someone else.
    parent = ("$env:MCP_STARTALL_HANDOFF_PID = [string]$PID; "
              "$a = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList "
              "@('-NoProfile','-ExecutionPolicy','Bypass','-File','\"%s\"','-NoUi','-NoSplash'); "
              "$env:MCP_STARTALL_HANDOFF_PID = '1'; "
              "$b = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList "
              "@('-NoProfile','-ExecutionPolicy','Bypass','-File','\"%s\"','-NoUi','-NoSplash'); "
              "$a.WaitForExit(); $b.WaitForExit()" % (ps1, ps1))
    enc = base64.b64encode(parent.encode("utf-16-le")).decode("ascii")

    def go():
        launch_ps(tree, dict(env, TEST_BRINGUP_SEC="15"), 1, 0, "-NoUi", "-NoSplash")
        wait_for_holder(tree)
        subprocess.run([POWERSHELL, "-NoProfile", "-EncodedCommand", enc], env=env, timeout=240,
                       capture_output=True)

    s = run_scenario(tree, go, 3, timeout=240)
    print("RESULT", json.dumps(s))
    assert s["runs"] == 3, s
    assert s["bringups"] == 2, s                     # the holder, then the hand-over
    assert s["lock"].count("got") == 1 and s["lock"].count("got after waiting") == 1, s
    assert s["outcomes"].count("already running") == 1, s
    assert s["left_over"] == [], s


if __name__ == "__main__":
    # `python scripts/test_start_all_ten_clicks.py <start_all.ps1 path>` measures any version
    # of start_all.ps1 (e.g. `git show HEAD~1:scripts/start_all.ps1 > old.ps1`) with the same
    # rig and prints one line per scenario -- how the before/after table was produced.
    import tempfile
    src = Path(sys.argv[1]).read_text(encoding="utf-8")
    base = Path(tempfile.mkdtemp(prefix="tenclicks_"))
    scen = {
        "bat x10 concurrent": lambda t, e: launch_bat(t, e, 10, 0),
        "bat x10 200ms apart": lambda t, e: launch_bat(t, e, 10, 0.2),
        "ps -NoUi -NoSplash x10": lambda t, e: [p.wait(600) for p in launch_ps(t, e, 10, 0, "-NoUi", "-NoSplash")],
    }
    for name, fn in scen.items():
        tree = build_tree(base, src)
        env = tree_env(tree)
        s = run_scenario(tree, lambda: fn(tree, env), 10, timeout=900)
        print(name, json.dumps(s), flush=True)
