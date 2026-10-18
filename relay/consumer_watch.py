# -*- coding: utf-8 -*-
"""Notice that submitted work is sitting where nothing can consume it, repair what is safe, say the rest.

THE CRITERION (owner, 2026-10-18): not "do the jobs I submitted run", but "does ALL work submitted by
anyone, through ANY route, actually move to real work". A watcher of one directory is not that.

WHAT WENT WRONG (measured 2026-10-17). Jobs written by tools/fleet_intake.fleet_submit sat in
.fleet/tasks/pending for 4 h 45 min and for 12 min because the only thing that calls
relay/task_router.dispatch_once -- scripts/supervisor.ps1 (a drain every 15 s, an express pass
every 3 s) -- was not running. Its log's last line is 08:44:58 and the next is a fresh
"supervisor up" at 22:01:56, with no exit record in between. The MCP server (main.py) kept
answering on :8000 the whole time, so submissions were accepted, said "queued", and nobody was
told. Nothing in the product compared "jobs are waiting" with "something is draining them".

WHY THE SERVER HOSTS THIS. It is the one piece that is demonstrably alive in that state --
it is the process that accepted the job. A watcher inside the supervisor dies with it.

THE ROUTES (every way work reaches the fleet, and what consumes each; ROUTES below is the table):

  pending       tasks/pending/<id>.json   tools/fleet_intake.fleet_submit, a CLI run's own goal entry.
                consumer: the supervisor's router pass (task_router.dispatch_once, every 15 s).
  for_fleet     tasks/for_fleet/<id>.txt  a goal the router parked because no run was in flight.
                consumer: the same router pass (_deliver_waiting_goals: join a live run, else ONE
                autostart per pass, subject to AUTOSTART_BACKOFF_S / AUTOSTART_GRACE_S).
  awaiting_ack  tasks/awaiting_ack/*.json a goal written into a live run's command channel whose
                landing receipt has not come back. consumer: the router's _reconcile_landings.
  commands      commands.d/*.json (add_goal)  the cockpit and chat windows (ui/FleetCommands.cs),
                task_router.add_goal_to_live_fleet, relay/code_task.py. consumer: ONLY a live
                relay.fleet_runner (claim_commands) -- or the cockpit's own bounded rescuer, which
                exists only while that window is open (3 tries, 15 minutes). Nothing in the
                supervisor starts a run for a file sitting here.
  running       tasks/running/<id>.json   a job the router claimed (atomic rename) and has not yet
                moved to done/ or awaiting/. consumer: that same router process; there is NO
                recovery pass, so a router killed mid-job leaves the file for ever.
  (not watched, and why) awaiting/ waits for a HUMAN's answer to an approval gate (the cockpit
  shows the gate); for_claude/ is a hand-off to an external agent, not to the fleet; the LOCAL_LOOP
  durable campaign is a SQLite manifest whose age is not a queue age.

WHAT IT DOES, each period (PERIOD_S):
  * supervisor routes (pending / for_fleet / awaiting_ack) and no supervisor alive (its mutex) ->
    start the product's own entry (scripts/start_all.ps1 -NoUi -NoSplash), at most once per
    RESTART_MIN_INTERVAL_S and RESTART_MAX_PER_HOUR per hour;
  * commands.d add_goal files and NO run live -> start a run that adopts exactly that command
    (`relay.fleet_runner --adopt-command`, the cockpit rescuer's own call), but only where the
    product's autostart is allowed (task_router.AUTOSTART and autostart_status): a run costs a
    browser and Copilot budget, which the operator opted into with FLEET_INTAKE_AUTOSTART and
    which the interrupted-run hold and the autostart backoff protect;
  * everything with no safe automatic repair (a supervisor that is alive and not draining, a
    live run that does not read its commands, an autostart that is blocked, an orphan in
    running/ -- re-running it could execute a job twice) -> an alarm that names the route, the age,
    what happened and what the owner does;
  * while any of that holds, write .fleet/consumer_alarm.json (the cockpit shows text_ja/text_en
    on the surface the person already looks at), removed as soon as nothing is stuck.

THE NUMBERS (each from a measurement, none a guess):
  STALE_AFTER_S = 90. A job written to pending was dispatched 11-19 s later in the supervisor log
    (23:40:47 -> 23:40:58, 22:29:40 -> 22:29:59 on 2026-10-17), one 15 s drain; 90 s is six drains.
    Liveness is the supervisor's mutex, not its speed, so a long legitimate drain never looks like
    a death. Used for every supervisor route when the supervisor is NOT alive.
  STALLED_AFTER_S = 2 * task_router.LOCAL_TIMEOUT_S + 60 = 300. With the supervisor alive, a pass
    works through the queue one job at a time and a local job may take up to LOCAL_TIMEOUT_S
    (120 s), so a file older than two of those plus a pass has been skipped, not queued.
  COMMANDS_NO_RUN_AFTER_S = 300. Measured on the 464 landing receipts in .fleet/acks (read time
    minus the command file's own time_ns name): median 2.2 s, p90 39 s, p95 67 s, p99 293 s,
    max 935 s. 300 s is the p99; and this limit applies only when NO run is live, where there is no
    drain lag to wait out at all.
  COMMANDS_STALLED_AFTER_S = 600. A run IS live but has not read the file: twice the p99 (the
    cockpit's own comment records a 30-190 s drain lag on 2026-10-14).
  RUNNING_ORPHAN_AFTER_S = 3 * LOCAL_TIMEOUT_S = 360, and no task_router process alive.
  FOR_FLEET_BLOCKED_AFTER_S = AUTOSTART_BACKOFF_S + AUTOSTART_GRACE_S (900 + 240): the longest the
    product itself waits between autostart attempts; a parked goal older than that has been
    refused more than once.
  START_GRACE_S = 240: start_all takes 65-90 s (start_all_runs.jsonl).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

STALE_AFTER_S = 90.0
STALLED_AFTER_S = 300.0
COMMANDS_NO_RUN_AFTER_S = 300.0
COMMANDS_STALLED_AFTER_S = 600.0
RUNNING_ORPHAN_AFTER_S = 360.0
FOR_FLEET_BLOCKED_AFTER_S = 1140.0
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

#: Route name -> (subdirectory under the fleet dir, file suffix, who consumes it). The one table
#: the scan, the alarm and the tests read.
ROUTES = {
    "pending": (("tasks", "pending"), ".json", "supervisor"),
    "for_fleet": (("tasks", "for_fleet"), ".txt", "supervisor"),
    "awaiting_ack": (("tasks", "awaiting_ack"), ".json", "supervisor"),
    "commands": (("commands.d",), ".json", "run"),
    "running": (("tasks", "running"), ".json", "router"),
}

#: Every task_router queue directory that is NOT in ROUTES, with the reason nothing here watches it.
#: relay/test_consumer_watch_routes.py fails when task_router grows a directory that is in neither
#: table, so a new route cannot be added without deciding who watches it.
UNWATCHED = {
    "done": "terminal records, nothing is waiting in it",
    "awaiting": "waits for a HUMAN's answer to an approval gate, which the cockpit shows; "
                "no consumer's absence can unstick it",
    "for_claude": "a hand-off to an external Claude agent that polls it, not work for the fleet",
}

#: (Japanese, English) noun phrase per route, for the alarm.
ROUTE_LABEL = {
    "pending": ("投入済みのジョブ", "A submitted job"),
    "for_fleet": ("フリート待ちのジョブ", "A job waiting for a fleet"),
    "awaiting_ack": ("実行中のフリートへ渡したジョブ(受領確認待ち)",
                     "A job handed to the running fleet (receipt not confirmed)"),
    "commands": ("コマンドチャネル(commands.d)へ追加したタスク",
                 "A task added through the command channel (commands.d)"),
    "running": ("ルーターが取り込んだまま完了していないジョブ",
                "A job the router claimed and never finished"),
}


def _route_dir(fleet, route):
    return os.path.join(fleet, *ROUTES[route][0])


def _pending_dir(fleet):
    return _route_dir(fleet, "pending")


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


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _owner_alive(pid):
    """Whether a CLI run's own pending entry is still held by its writer. None when unknown."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        return None


def _is_held_by_live_owner(path, now):
    """A pending entry a live CLI run wrote about ITSELF (task_router._still_owned): the run executes
    it, nothing needs to consume it, and it is not waiting work."""
    job = _read_json(path)
    pid = job.get("owner_pid")
    if not pid:
        return False
    try:
        if now - float(job.get("created") or 0.0) > 3600.0:     # task_router.OWNER_PID_GRACE_S
            return False
    except (TypeError, ValueError):
        return False
    return _owner_alive(pid) is not False


def _has_add_goal(path):
    """commands.d holds control commands too (steer, close, set_maxtabs); only add_goal is work."""
    return bool(_read_json(path).get("add_goal"))


def scan(fleet, now=None):
    """{route: [(age_s, file name), ...]} oldest first, only the files that are waiting WORK."""
    now = time.time() if now is None else now
    out = {}
    for route, (_sub, ext, _who) in ROUTES.items():
        d = _route_dir(fleet, route)
        items = []
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for n in names:
            if not n.endswith(ext):
                continue
            p = os.path.join(d, n)
            try:
                age = max(0.0, now - os.path.getmtime(p))
            except OSError:
                continue
            if route == "commands" and not _has_add_goal(p):
                continue
            if route == "pending" and _is_held_by_live_owner(p, now):
                continue
            items.append((age, n))
        items.sort(reverse=True)
        out[route] = items
    return out


def fmt_age(s):
    s = int(max(0, s))
    if s < 90:
        return "%d秒" % s, "%ds" % s
    if s < 5400:
        return "%d分" % (s // 60), "%d min" % (s // 60)
    return "%d時間%d分" % (s // 3600, (s % 3600) // 60), "%d h %d min" % (s // 3600, (s % 3600) // 60)


def alarm_text(age_s, restarting, route="pending"):
    """The supervisor-is-down text (the original alarm), for any supervisor route."""
    ja, en = fmt_age(age_s)
    lja, len_ = ROUTE_LABEL[route]
    if restarting:
        return (
            "%sが %s 待たされています。ジョブを処理する監視プロセスが動いていないため、自動で再起動しています。" % (lja, ja),
            "%s has been waiting %s: nothing is processing the queue (the supervisor "
            "is not running). It is being restarted automatically." % (len_, en),
        )
    return (
        "%sが %s 待たされています。自動再起動は効果がありませんでした(監視プロセスが戻っていません): start_all.bat をもう一度実行してください。" % (lja, ja),
        "%s has been waiting %s and the automatic restart did not take effect (the "
        "supervisor did not come back): run start_all.bat again." % (len_, en),
    )


def finding_text(f, in_progress):
    """(ja, en) for one finding: the route, the age, what happened and what the owner does."""
    ja, en = fmt_age(f["age_s"])
    lja, len_ = ROUTE_LABEL[f["route"]]
    cause, why = f["cause"], f.get("why", "")
    if cause == "supervisor_down":
        return alarm_text(f["age_s"], in_progress, f["route"])
    if cause == "no_run":
        if in_progress:
            return ("%sが %s 待たされています。受け取る実行が動いていないため、このタスクを引き継ぐ実行を自動で起動しています。" % (lja, ja),
                    "%s has been waiting %s: no fleet run is live to read it. A run that adopts it is "
                    "being started automatically." % (len_, en))
        return ("%sが %s 待たされています。受け取る実行が動いておらず、自動では起動できませんでした(%s): "
                "コックピットから実行を開始するか、start_all.bat を実行してください。" % (lja, ja, why),
                "%s has been waiting %s: no fleet run is live to read it and one could not be started "
                "automatically (%s). Start a run from the cockpit or run start_all.bat." % (len_, en, why))
    if cause == "blocked":
        return ("%sが %s 待たされています。フリートを自動起動できません(%s): "
                "コックピットから実行を開始するか、start_all.bat を実行してください。" % (lja, ja, why),
                "%s has been waiting %s and no fleet can be started automatically (%s). Start a run from "
                "the cockpit or run start_all.bat." % (len_, en, why))
    if cause == "run_stalled":
        return ("%sが %s 待たされています。実行は動いていますがこのコマンドを読み取っていません: "
                "コックピットで実行の状態を確認してください。" % (lja, ja),
                "%s has been waiting %s. A run is live but has not read it: check the run in the "
                "cockpit." % (len_, en))
    if cause == "orphan":
        return ("%sが %s 止まっています(%s)。二重実行を避けるため自動では戻しません。内容を確認し、"
                "実行されていなければ tasks\\pending へ戻してください。" % (lja, ja, why),
                "%s has been stuck for %s (%s). It is not put back automatically because that could run "
                "a job twice: inspect it and, if it did not run, move it back to tasks\\pending." % (len_, en, why))
    # router_stalled: the supervisor is alive but is not draining
    return ("%sが %s 待たされています。監視プロセスは動いていますがキューを処理していません: "
            "start_all.bat をもう一度実行してください(ログ: %%TEMP%%\\m365-companion-supervisor.log)。" % (lja, ja),
            "%s has been waiting %s. The supervisor is running but is not draining the queue: run "
            "start_all.bat again (log: %%TEMP%%\\m365-companion-supervisor.log)." % (len_, en))


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


#: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP. NOT DETACHED_PROCESS (0x08): measured 2026-10-18,
#: a powershell started with it exits with code 0 inside a second, prints nothing and never runs
#: the script, so start_all "launched" and did nothing while the alarm said it was restarting.
LAUNCH_FLAGS_NT = 0x08000000 | 0x00000200

#: A real start_all takes 65-90 s (start_all_runs.jsonl), so a child gone within this many seconds
#: did not run it.
EARLY_EXIT_WINDOW_S = 3.0


def launch_start_all(fleet, repo=None, script=None, args=("-NoUi", "-NoSplash"),
                     wait_s=EARLY_EXIT_WINDOW_S):
    """The product's own bring-up, windowless. Returns (took_effect, note).

    took_effect is False when nothing was spawned or the child was already gone after wait_s;
    note says why (exit code included), for the log and the alarm.
    """
    repo = repo or REPO_ROOT
    ps1 = script or os.path.join(repo, "scripts", "start_all.ps1")
    if not os.path.isfile(ps1):
        return False, "%s does not exist" % ps1
    flags = LAUNCH_FLAGS_NT if os.name == "nt" else 0
    try:
        out = open(os.path.join(fleet, "consumer_watch_start_all.log"), "ab")
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", ps1] + list(args),
            cwd=repo, stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags)
    except Exception as exc:
        return False, "could not launch %s: %s: %s" % (os.path.basename(ps1), type(exc).__name__, exc)
    try:
        rc = proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return True, "pid %d running after %.0fs" % (proc.pid, wait_s)
    return False, ("the child (pid %d) had already exited with code %s after %.0fs, so %s did not "
                   "run; its output is in consumer_watch_start_all.log"
                   % (proc.pid, rc, wait_s, os.path.basename(ps1)))


def launch_adopt(fleet, command_path, repo=None, python=None, wait_s=EARLY_EXIT_WINDOW_S):
    """Start a run that adopts exactly one pending add_goal command -- the cockpit rescuer's own
    call (ui/FleetCockpit.Submit.cs SpawnFleetAdoptCommand): `relay.fleet_runner --adopt-command`.
    Windowless (same flags as launch_start_all). Returns (took_effect, note).

    The runner takes the state-dir lock before it touches the command, so a run that already
    exists wins and this child exits; that is why the caller asks "is a run live" first and why
    a second attempt cannot create a second coordinator.
    """
    repo = repo or REPO_ROOT
    flags = LAUNCH_FLAGS_NT if os.name == "nt" else 0
    try:
        out = open(os.path.join(fleet, "consumer_watch_adopt.log"), "ab")
        proc = subprocess.Popen(
            [python or sys.executable, "-m", "relay.fleet_runner", "--adopt-command", command_path,
             "--state-dir", fleet, "--fanout"],
            cwd=repo, stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags)
    except Exception as exc:
        return False, "could not launch the adopting run: %s: %s" % (type(exc).__name__, exc)
    try:
        rc = proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return True, "pid %d running after %.0fs" % (proc.pid, wait_s)
    return False, ("the adopting run (pid %d) had already exited with code %s after %.0fs; its "
                   "output is in consumer_watch_adopt.log" % (proc.pid, rc, wait_s))


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


# -- probes (each injectable, each lazy: asked only when something is old enough to matter) ---------

def fleet_live(fleet):
    """True / False / None (cannot tell) -- the router's own definition of a run in flight."""
    try:
        from relay import task_router as TR
        return bool(TR.fleet_is_live(fleet))
    except Exception:
        return None


def router_running():
    """True / False / None: is a `task_router` process alive (the thing that owns running/ files)."""
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            if any("task_router" in str(a) for a in (p.info.get("cmdline") or [])):
                return True
        return False
    except Exception:
        return None


def autostart_ok(fleet):
    """(may_start_a_run, reason) -- the product's autostart gate, plus whether the operator opted in."""
    try:
        from relay import task_router as TR
        if not TR.AUTOSTART:
            return False, "autostart is off (FLEET_INTAKE_AUTOSTART)"
        return TR.autostart_status(fleet)
    except Exception as exc:
        return False, "could not ask the autostart gate: %s: %s" % (type(exc).__name__, exc)


_PROBE = object()


def find_stuck(scanned, alive, live, router, autostart):
    """Pure decision: the findings, each {route, age_s, count, cause, why, repair, oldest_file}.

    `repair` is "start_all", "adopt" or None (alarm only). `live`, `router` and `autostart` are
    callables evaluated only when a route needs them.
    """
    out = []

    def add(route, cause, age, count, name, why="", repair=None):
        out.append({"route": route, "cause": cause, "age_s": age, "count": count,
                    "oldest_file": name, "why": why, "repair": repair})

    for route in ("pending", "for_fleet", "awaiting_ack"):
        items = scanned.get(route) or []
        if not items or alive is None:
            continue
        age, name = items[0]
        if alive is False:
            if age >= STALE_AFTER_S:
                add(route, "supervisor_down", age, len(items), name, repair="start_all")
            continue
        # The supervisor is alive. A file that survived this long was not queued, it was skipped.
        limit = STALLED_AFTER_S
        why = ""
        if route == "for_fleet" and live() is False:
            may, reason = autostart()
            if not may:
                limit, why = FOR_FLEET_BLOCKED_AFTER_S, reason
                if age >= limit:
                    add(route, "blocked", age, len(items), name, why)
                continue
        if age >= limit:
            add(route, "router_stalled", age, len(items), name, why)

    items = scanned.get("commands") or []
    if items:
        age, name = items[0]
        lv = live()
        if lv is False and age >= COMMANDS_NO_RUN_AFTER_S:
            may, reason = autostart()
            if may:
                add("commands", "no_run", age, len(items), name, repair="adopt")
            else:
                add("commands", "no_run", age, len(items), name, why=reason)
        elif lv is True and age >= COMMANDS_STALLED_AFTER_S:
            add("commands", "run_stalled", age, len(items), name)

    items = scanned.get("running") or []
    if items and items[0][0] >= RUNNING_ORPHAN_AFTER_S and router() is not True:
        age, name = items[0]
        add("running", "orphan", age, len(items), name,
            why="%s, and no task_router process is running" % name)
    return out


def tick(fleet=None, now=None, alive=_PROBE, launcher=None, adopter=None, live=_PROBE,
         router=_PROBE, autostart=_PROBE):
    """One pass over every route. Returns ok | waiting | restarting | failed | unknown.

    restarting = every stuck route has a repair in progress; failed = at least one needs the owner.
    """
    fleet = FLEET_DIR if fleet is None else fleet
    now = time.time() if now is None else now
    alive = consumer_alive() if alive is _PROBE else alive
    launcher = launch_start_all if launcher is None else launcher
    adopter = launch_adopt if adopter is None else adopter
    live_fn = (lambda: fleet_live(fleet)) if live is _PROBE else (lambda: live)
    router_fn = router_running if router is _PROBE else (lambda: router)
    auto_fn = (lambda: autostart_ok(fleet)) if autostart is _PROBE else (lambda: autostart)

    scanned = scan(fleet, now)
    waiting_any = any(scanned.values())
    findings = find_stuck(scanned, alive, live_fn, router_fn, auto_fn)
    if not findings:
        if alive is None and waiting_any:
            return "unknown"
        if _clear_alarm(fleet):
            _log(fleet, "nothing stuck (consumer alive=%s): alarm cleared" % alive)
        young = any(items for items in scanned.values())
        return "waiting" if (young and alive is False) else "ok"

    st = _read_json(_state_path(fleet))
    starts = [t for t in st.get("starts", []) if now - t < 3600]
    adopts = [t for t in st.get("adopts", []) if now - t < 3600]
    eff_start = st.get("last_effective", True)
    eff_adopt = st.get("last_adopt_effective", True)

    def may_repeat(times):
        last = times[-1] if times else 0.0
        return len(times) < RESTART_MAX_PER_HOUR and now - last >= RESTART_MIN_INTERVAL_S

    need_start = [f for f in findings if f["repair"] == "start_all"]
    if need_start and may_repeat(starts):
        f = need_start[0]
        why = ("oldest %s job is %.0fs old (limit %.0fs) and no supervisor holds %s; %s"
               % (f["route"], f["age_s"], STALE_AFTER_S, SUPERVISOR_MUTEX, _forensics(fleet)))
        res = launcher(fleet)
        took, note = res if isinstance(res, tuple) else (bool(res), "")
        starts.append(now)
        eff_start = took
        if took:
            _log(fleet, "restarting through start_all.ps1 -NoUi -NoSplash (%d in the last hour; %s): %s"
                 % (len(starts), note, why))
        else:
            _log(fleet, "restart DID NOT TAKE EFFECT (%d in the last hour): %s -- %s"
                 % (len(starts), note, why))

    need_adopt = [f for f in findings if f["repair"] == "adopt"]
    if need_adopt and may_repeat(adopts):
        f = need_adopt[0]
        path = os.path.join(_route_dir(fleet, "commands"), f["oldest_file"])
        why = ("oldest add_goal command %s is %.0fs old (limit %.0fs) and no run is live"
               % (f["oldest_file"], f["age_s"], COMMANDS_NO_RUN_AFTER_S))
        res = adopter(fleet, path)
        took, note = res if isinstance(res, tuple) else (bool(res), "")
        adopts.append(now)
        eff_adopt = took
        _log(fleet, ("starting a run that adopts the command (%d in the last hour; %s): %s"
                     if took else "adopting run DID NOT TAKE EFFECT (%d in the last hour): %s -- %s")
             % (len(adopts), note, why))

    _write_json(_state_path(fleet), {"starts": starts, "adopts": adopts, "last_tick": now,
                                     "last_effective": eff_start, "last_adopt_effective": eff_adopt})

    routes, ja_parts, en_parts, all_in_progress = [], [], [], True
    for f in findings:
        if f["repair"] == "start_all":
            prog = bool(starts) and eff_start and now - starts[-1] < START_GRACE_S
        elif f["repair"] == "adopt":
            prog = bool(adopts) and eff_adopt and now - adopts[-1] < START_GRACE_S
        else:
            prog = False
        if f["repair"] == "adopt" and not prog and not f["why"]:
            f = dict(f, why="the automatic start did not take effect or is rate limited")
        all_in_progress = all_in_progress and prog
        ja, en = finding_text(f, prog)
        ja_parts.append(ja)
        en_parts.append(en)
        routes.append({"route": f["route"], "cause": f["cause"], "count": f["count"],
                       "oldest_age_s": round(f["age_s"], 1), "repair": f["repair"],
                       "in_progress": prog, "text_ja": ja, "text_en": en})
    state = "restarting" if all_in_progress else "failed"
    _write_json(_alarm_path(fleet), {
        "ts": now, "state": state,
        "oldest_pending_age_s": round(max(f["age_s"] for f in findings), 1),
        "routes": routes,
        "text_ja": " / ".join(ja_parts), "text_en": " / ".join(en_parts)})
    return state


def start_watcher(fleet=None, period=PERIOD_S):
    """Daemon thread; a failure is logged and the next period tries again."""
    import threading

    def loop():
        while True:
            try:
                tick(fleet)
            except Exception as exc:
                try:
                    _log(FLEET_DIR if fleet is None else fleet,
                         "tick failed: %s: %s" % (type(exc).__name__, exc))
                except Exception:
                    pass
            time.sleep(period)

    t = threading.Thread(target=loop, name="consumer-watch", daemon=True)
    t.start()
    return t
