"""Watch the desktop for console windows the running stack puts up, and name who did it.

WHY THIS EXISTS. The owner, 2026-09-24: console windows "pop up four times", and text typed
while one is up is lost because the window takes the keyboard. A console window appears when a
console program is started by a parent that has NO console and the launch does not say
CREATE_NO_WINDOW (tools/childproc.py::headless_creationflags has the measurement). The source
guard (tools/test_no_new_launch_inherits_its_console_by_accident.py) reads Python source; it
cannot see a .ps1 Start-Process, a .bat, a C# Process.Start, or a grandchild. This reads the
DESKTOP instead, which sees all of them.

HOW IT SEES A WINDOW THAT LIVES FOR 80 ms. Two instruments, because either alone misses one:

  * a WinEvent hook (EVENT_OBJECT_SHOW, out of context) -- fires for every window shown, so a
    flash shorter than any polling interval is still caught, with its owner resolved in the
    callback while the process still exists;
  * a poll every `--interval` seconds (0.2 by default) of the visible top-level windows of the
    console classes, as a cross-check that the hook is alive.

HOW IT NAMES THE CAUSE. The window's owner is often not the launcher: with Windows Terminal as
the default terminal the window belongs to WindowsTerminal.exe. So every flash is reported with
(a) the owner's ancestor chain and (b) every process created in the preceding 2 s with ITS chain
-- the process table is sampled every interval and dead processes are kept, so a chain survives
its members exiting. A flash is attributed to this repository when any process on any of those
chains has a command line or executable under the repo root.

READ-ONLY. It starts nothing and stops nothing.

    .venv\\Scripts\\python.exe scripts\\win\\console_flash_watch.py --minutes 10 --out flashes.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Top-level window classes a console program's window can have. conhost's own window, the
#: Windows Terminal host that the default-terminal handoff uses, and the pseudo-console stub.
CONSOLE_CLASSES = frozenset({
    "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow",
})

#: How far back from a window's appearance a process creation still counts as its possible cause.
LOOKBACK_S = 2.0


class ProcTable:
    """pid -> {ppid, name, exe, cmdline, created}, kept after the process exits.

    Keyed by (pid, create_time) internally so a recycled pid does not splice two chains.
    """

    def __init__(self):
        self.rows = {}          # (pid, created) -> row
        self.live = {}          # pid -> (pid, created) of the current holder

    def sample(self):
        import psutil
        seen = set()
        for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            info = p.info
            pid, created = info["pid"], info.get("create_time") or 0.0
            key = (pid, created)
            seen.add(pid)
            if key in self.rows:
                continue
            row = {"pid": pid, "ppid": info.get("ppid"), "name": info.get("name") or "",
                   "created": created, "exe": "", "cmdline": ""}
            try:
                row["exe"] = p.exe() or ""
                row["cmdline"] = " ".join(p.cmdline() or [])
            except Exception:
                pass
            self.rows[key] = row
            self.live[pid] = key
        for pid in list(self.live):
            if pid not in seen:
                del self.live[pid]

    def add(self, row):
        key = (row["pid"], row["created"])
        self.rows.setdefault(key, row)
        self.live[row["pid"]] = key

    def row(self, pid, not_after=None):
        """The row for `pid`, preferring the live holder, else the newest one created before
        `not_after` (a parent must predate its child)."""
        key = self.live.get(pid)
        if key is not None and (not_after is None or key[1] <= not_after):
            return self.rows[key]
        best = None
        for (p, c), r in self.rows.items():
            if p == pid and (not_after is None or c <= not_after):
                if best is None or c > best["created"]:
                    best = r
        return best

    def chain(self, pid, limit=12):
        """[row, parent row, ...] as far up as the table knows."""
        out, cur, not_after = [], pid, None
        while cur and len(out) < limit:
            r = self.row(cur, not_after)
            if r is None:
                break
            out.append(r)
            not_after = r["created"]
            if r["ppid"] == cur:
                break
            cur = r["ppid"]
        return out

    def created_between(self, lo, hi):
        return [r for r in self.rows.values() if lo <= r["created"] <= hi]


def under_repo(row, repo=REPO):
    root = os.path.normcase(os.path.abspath(repo))
    text = os.path.normcase((row.get("cmdline") or "") + " " + (row.get("exe") or ""))
    return root in text


def attribute(table, owner_pid, when, repo=REPO, lookback=LOOKBACK_S):
    """The evidence for one window: the owner's chain and the chains of recent creations,
    and whether anything on them belongs to this repository."""
    owner_chain = table.chain(owner_pid)
    recent = []
    for r in sorted(table.created_between(when - lookback, when + 0.5),
                    key=lambda r: r["created"]):
        recent.append(table.chain(r["pid"]))
    chains = [owner_chain] + recent
    repo_hit = any(under_repo(r, repo) for ch in chains for r in ch)
    brief = lambda ch: [{"pid": r["pid"], "name": r["name"], "cmdline": r["cmdline"][:400]}
                        for r in ch]
    return {"owner_chain": brief(owner_chain), "recent": [brief(c) for c in recent],
            "under_repo": repo_hit}


# ---- Win32 -------------------------------------------------------------------------------------

def _win():
    import ctypes
    import ctypes.wintypes as W
    return ctypes, W, ctypes.windll.user32


def window_info(hwnd):
    ctypes, W, u = _win()
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    pid = W.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, title, 256)
    return buf.value, pid.value, title.value


def visible_console_windows():
    """{hwnd: (class, owner pid, title)} for visible top-level console-class windows now."""
    ctypes, W, u = _win()
    found = {}

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            cls, pid, title = window_info(h)
            if cls in CONSOLE_CLASSES:
                found[int(h)] = (cls, pid, title)
        return True
    u.EnumWindows(cb, 0)
    return found


def watch(minutes, interval, out_path, repo=REPO, emit=print, on_ready=None):
    """Run for `minutes`; append one JSON line per flash to `out_path`; return the flashes."""
    ctypes, W, u = _win()
    import psutil

    table = ProcTable()
    table.sample()
    baseline = set(visible_console_windows())
    reported = set()
    flashes = []
    pending = []            # (hwnd, cls, pid, title, t) seen by the hook, resolved in the loop

    EVENT_OBJECT_SHOW = 0x8002
    OBJID_WINDOW = 0
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002

    @ctypes.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND, W.LONG, W.LONG, W.DWORD, W.DWORD)
    def on_event(_hook, _ev, hwnd, id_object, _child, _thread, _time):
        if id_object != OBJID_WINDOW or not hwnd:
            return
        try:
            cls, pid, title = window_info(hwnd)
        except Exception:
            return
        if cls not in CONSOLE_CLASSES:
            return
        # Resolve the owner NOW, while it still exists -- a flash's owner is often gone by the
        # next sample.
        try:
            p = psutil.Process(pid)
            table.add({"pid": pid, "ppid": p.ppid(), "name": p.name(),
                       "created": p.create_time(), "exe": p.exe() or "",
                       "cmdline": " ".join(p.cmdline() or [])})
        except Exception:
            pass
        pending.append((int(hwnd), cls, pid, title, time.time(), "hook"))

    u.SetWinEventHook.restype = W.HANDLE
    hook = u.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, on_event, 0, 0,
                             WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
    msg = W.MSG()
    PM_REMOVE = 1
    # THE CLOCK STARTS HERE, NOT AT ENTRY. The first process-table sample reads every
    # process's command line and was measured at ~5 s on this machine; a window shown during it
    # happens before the hook exists and is invisible to both instruments.
    if on_ready is not None:
        on_ready()
    deadline = time.time() + minutes * 60.0
    next_sample = 0.0
    ticks = 0
    try:
        while time.time() < deadline:
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            now = time.time()
            if now >= next_sample:
                next_sample = now + interval
                ticks += 1
                table.sample()
                for h, (cls, pid, title) in visible_console_windows().items():
                    if h not in baseline:
                        pending.append((h, cls, pid, title, now, "poll"))
            while pending:
                h, cls, pid, title, t, via = pending.pop(0)
                if h in baseline or h in reported:
                    continue
                reported.add(h)
                table.sample()
                ev = {"t": round(t, 3),
                      "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                      "via": via, "hwnd": h, "class": cls, "title": title, "owner_pid": pid}
                ev.update(attribute(table, pid, t, repo))
                flashes.append(ev)
                if out_path:
                    with open(out_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                emit("FLASH %s %s owner=%s repo=%s title=%r"
                     % (ev["at"], cls, pid, ev["under_repo"], title))
            time.sleep(0.02)
    finally:
        if hook:
            u.UnhookWinEvent(hook)
    emit("watched %.1f min, %d samples, %d console window(s) appeared, %d under the repo"
         % (minutes, ticks, len(flashes), sum(1 for f in flashes if f["under_repo"])))
    return flashes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    if os.name != "nt":
        print("console windows are a Windows mechanism; nothing to watch here")
        return 0
    flashes = watch(a.minutes, a.interval, a.out)
    return 1 if any(f["under_repo"] for f in flashes) else 0


if __name__ == "__main__":
    sys.exit(main())
