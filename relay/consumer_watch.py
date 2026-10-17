# -*- coding: utf-8 -*-
"""Notice that nothing is consuming the job queue, bring the consumer back, and say so.

WHAT WENT WRONG (measured 2026-10-17). Jobs written by tools/fleet_intake.fleet_submit sat in
.fleet/tasks/pending for 4 h 45 min and for 12 min because the only thing that calls
relay/task_router.dispatch_once -- scripts/supervisor.ps1 (a drain every 15 s, an express pass
every 3 s) -- was not running. Its log's last line is 08:44:58 and the next is a fresh
"supervisor up" at 22:01:56, with no exit record in between. The MCP server (main.py) kept
answering on :8000 the whole time, so submissions were accepted, said "queued", and nobody was
told. Nothing in the product compared "jobs are waiting" with "something is draining them":
the supervisor cannot watch itself, the Startup shortcut only runs at logon, and the cockpit
only printed "N submitted task(s) still unstarted".

WHY THE SERVER HOSTS THIS. It is the one piece that is demonstrably alive in that state --
it is the process that accepted the job. A watcher inside the supervisor dies with it.

WHAT IT DOES, each period:
  * oldest job in pending older than STALE_AFTER_S and no supervisor alive -> start the
    product's own entry (scripts/start_all.ps1 -NoUi -NoSplash, the thing that revived it by
    hand), at most once per RESTART_MIN_INTERVAL_S and RESTART_MAX_PER_HOUR per hour, and log
    why;
  * while that holds, write .fleet/consumer_alarm.json -- the cockpit shows its text on the
    surface the person already looks at -- naming the age of the oldest job and either "being
    restarted automatically" or "could not restart: re-run start_all.bat";
  * the alarm is removed as soon as a consumer is alive again or the queue is empty.

THE NUMBERS. STALE_AFTER_S = 90: a job written to pending was dispatched 11-19 s later in the
supervisor log (23:40:47 -> 23:40:58, 22:29:40 -> 22:29:59 on 2026-10-17), i.e. one 15 s
drain; 90 s is six drains, and liveness is judged by the supervisor's mutex, not by its speed,
so a long legitimate drain never looks like a death.  START_GRACE_S = 240: start_all takes
65-90 s (start_all_runs.jsonl).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

STALE_AFTER_S = 90.0
PERIOD_S = 15.0
RESTART_MIN_INTERVAL_S = 300.0
RESTART_MAX_PER_HOUR = 3
START_GRACE_S = 240.0

#: The supervisor takes this named mutex for its whole life (scripts/supervisor.ps1).
SUPERVISOR_MUTEX = "Global\\m365-copilot-companion-supervisor"


#: The operator's record directory. A module CONSTANT, so conftest.LIVE_RECORD_REDIRECTS moves it
#: for the whole test session and no test of this module can write the live .fleet; every path
#: below is derived from the `fleet` argument, which defaults to it.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET_DIR = os.path.join(REPO_ROOT, ".fleet")


def _pending_dir(fleet):
    return os.path.join(fleet, "tasks", "pending")


def _alarm_path(fleet):
    return os.path.join(fleet, "consumer_alarm.json")


def _state_path(fleet):
    return os.path.join(fleet, "consumer_watch.json")


def _log_path(fleet):
    return os.path.join(fleet, "consumer_watch.log")


def consumer_alive():
    """True / False when the supervisor's mutex says so, None when that cannot be asked.

    None is not False: an unanswerable question must never restart anything.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenMutexW.restype = ctypes.c_void_p
        h = k32.OpenMutexW(0x00100000, False, SUPERVISOR_MUTEX)  # SYNCHRONIZE
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
            return True
        err = ctypes.get_last_error()
        if err == 2:        # ERROR_FILE_NOT_FOUND: nobody holds it
            return False
        if err == 5:        # ERROR_ACCESS_DENIED: it exists, we may not open it
            return True
        return None
    except Exception:
        return None


def pending_ages(fleet, now=None):
    """Ages (s) of the finished job files in pending, oldest first."""
    now = time.time() if now is None else now
    ages = []
    try:
        for n in os.listdir(_pending_dir(fleet)):
            if not n.endswith(".json"):
                continue
            try:
                ages.append(max(0.0, now - os.path.getmtime(os.path.join(_pending_dir(fleet), n))))
            except OSError:
                pass
    except OSError:
        pass
    return sorted(ages, reverse=True)


def fmt_age(s):
    s = int(max(0, s))
    if s < 90:
        return "%d秒" % s, "%ds" % s
    if s < 5400:
        return "%d分" % (s // 60), "%d min" % (s // 60)
    return "%d時間%d分" % (s // 3600, (s % 3600) // 60), "%d h %d min" % (s // 3600, (s % 3600) // 60)


def alarm_text(age_s, restarting):
    ja, en = fmt_age(age_s)
    if restarting:
        return (
            "投入済みのジョブが %s 待たされています。ジョブを処理する監視プロセスが動いていないため、自動で再起動しています。" % ja,
            "A submitted job has been waiting %s: nothing is processing the queue (the supervisor "
            "is not running). It is being restarted automatically." % en,
        )
    return (
        "投入済みのジョブが %s 待たされています。監視プロセスを自動で再起動できませんでした: start_all.bat をもう一度実行してください。" % ja,
        "A submitted job has been waiting %s and the supervisor could not be restarted "
        "automatically: run start_all.bat again." % en,
    )


def _log(fleet, msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print("consumer_watch: " + msg, flush=True)
    except Exception:
        pass
    try:
        with open(_log_path(fleet), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, obj):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_alarm(fleet):
    try:
        os.remove(_alarm_path(fleet))
        return True
    except OSError:
        return False


def launch_start_all(fleet, repo=None):
    """The product's own bring-up, detached and windowless. Returns True when it was spawned."""
    repo = repo or REPO_ROOT
    ps1 = os.path.join(repo, "scripts", "start_all.ps1")
    if not os.path.isfile(ps1):
        return False
    flags = 0
    if os.name == "nt":
        flags = 0x08000000 | 0x00000008 | 0x00000200  # NO_WINDOW | DETACHED | NEW_GROUP
    try:
        out = open(os.path.join(fleet, "consumer_watch_start_all.log"), "ab")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", ps1, "-NoUi", "-NoSplash"],
            cwd=repo, stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags)
        return True
    except Exception as exc:
        _log(fleet, "could not launch start_all.ps1: %s: %s" % (type(exc).__name__, exc))
        return False


def _forensics(fleet):
    """What the next investigation needs and this one lacked: when the supervisor last wrote."""
    bits = []
    st = _read_json(os.path.join(fleet, "supervisor_state.json"))
    if st.get("pid"):
        bits.append("last supervisor pid=%s started=%s" % (st.get("pid"), st.get("start_ts")))
    for p in (os.path.join(os.environ.get("TEMP", ""), "m365-companion-supervisor.log"),):
        try:
            bits.append("supervisor log last written %s"
                        % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
        except OSError:
            pass
    return "; ".join(bits)


_PROBE = object()


def tick(fleet=None, now=None, alive=_PROBE, launcher=None):
    """One pass. Returns the state name: ok | waiting | restarting | failed | unknown."""
    fleet = FLEET_DIR if fleet is None else fleet
    now = time.time() if now is None else now
    alive = consumer_alive() if alive is _PROBE else alive
    launcher = launch_start_all if launcher is None else launcher
    ages = pending_ages(fleet, now)
    if alive is None:
        return "unknown"
    if alive or not ages:
        if _clear_alarm(fleet):
            _log(fleet, "consumer alive=%s, pending=%d: alarm cleared" % (alive, len(ages)))
        return "ok"
    oldest = ages[0]
    if oldest < STALE_AFTER_S:
        return "waiting"
    st = _read_json(_state_path(fleet))
    starts = [t for t in st.get("starts", []) if now - t < 3600]
    last = starts[-1] if starts else 0.0
    can = len(starts) < RESTART_MAX_PER_HOUR and now - last >= RESTART_MIN_INTERVAL_S
    if can:
        why = ("oldest pending job is %.0fs old (limit %.0fs) and no supervisor holds %s; %s"
               % (oldest, STALE_AFTER_S, SUPERVISOR_MUTEX, _forensics(fleet)))
        if launcher(fleet):
            starts.append(now)
            _log(fleet, "restarting through start_all.ps1 -NoUi -NoSplash (%d in the last hour): %s"
                 % (len(starts), why))
        else:
            _log(fleet, "restart NOT launched: " + why)
    _write_json(_state_path(fleet), {"starts": starts, "last_tick": now})
    restarting = bool(starts) and (now - starts[-1] < START_GRACE_S or len(starts) < RESTART_MAX_PER_HOUR)
    ja, en = alarm_text(oldest, restarting)
    _write_json(_alarm_path(fleet), {"ts": now, "state": "restarting" if restarting else "failed",
                                    "oldest_pending_age_s": round(oldest, 1),
                                    "text_ja": ja, "text_en": en})
    return "restarting" if restarting else "failed"


def start_watcher(fleet=None, period=PERIOD_S):
    """Daemon thread; a failure is logged and the next period tries again."""
    import threading

    def loop():
        while True:
            try:
                tick(fleet)
            except Exception as exc:
                try:
                    _log(fleet, "tick failed: %s: %s" % (type(exc).__name__, exc))
                except Exception:
                    pass
            time.sleep(period)

    t = threading.Thread(target=loop, name="consumer-watch", daemon=True)
    t.start()
    return t
