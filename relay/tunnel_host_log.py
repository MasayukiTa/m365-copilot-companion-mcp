# -*- coding: utf-8 -*-
"""Own the Dev Tunnel host so its verbose log is permanent, stamped and always read.

Before this module the host was started by a short-lived script that redirected the
host's stdout/stderr into ``.setup/logs/devtunnel-host.out.log`` and then exited. With no
live reader the OS pipe buffer filled and the host either blocked or its output was simply
lost -- which is exactly what cost us the log for the minutes a user reported a disconnect
on 2026-10-31.

The long-lived supervisor now runs this module as a child it owns (``python -m
relay.tunnel_host_log <tunnel-name>``). This process:

* spawns ``devtunnel -v host <name>`` (``-v`` turns on the per-channel open/close lines),
* reads BOTH stdout and stderr continuously on their own threads (never a pipe left
  without a reader),
* stamps every line with a millisecond local time,
* writes to ``.setup/logs/devtunnel-host-ts.log`` opened so other programs can read it
  while it is being written (Windows share-read; POSIX allows this already),
* rotates at 20 MB keeping 5 old files, appending across host restarts,
* when the host exits, logs the exit code and restarts it after a growing backoff.

It also drives :mod:`relay.tunnel_channels` to append one JSON line per closed channel to
``.setup/logs/tunnel_channels.jsonl`` as new log lines arrive, so the server /health route
has the counts without re-reading the whole log on every poll.

The decision helpers (:func:`millis_stamp`, :func:`backoff_seconds`, :class:`RotatingLog`
and :func:`pump_stream`) take no devtunnel and no real clock dependency, so the hermetic CI
suite exercises them on Linux with a fake command.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time

try:
    from relay import tunnel_channels
except Exception:  # pragma: no cover - only if run as a bare script without the package
    tunnel_channels = None

# The host is unattended: its parent (the supervisor) may have no console, and a console child
# started from a console-less parent gets a BRAND NEW window that Windows Terminal shows. So we
# state a console/process-ownership policy at every Popen below instead of inheriting whatever
# console we are given: childproc.tree_popen_kwargs(headless=True, priority=False) sets
# CREATE_NO_WINDOW (no stray window; the devtunnel host never prompts) and keeps the parent's
# priority (this is infrastructure, not a fleet worker's child), while also making the child
# the root of a killable tree. The call is expanded INLINE at each Popen on purpose:
# tools/launch_sites.py recognises the module-qualified attribute form as a stated policy.
from tools import childproc

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUPS = 5


def millis_stamp(epoch=None):
    """Local time as ``YYYY-MM-DD HH:MM:SS.mmm`` -- millisecond, not microsecond.

    relay.tunnel_channels parses exactly this shape, so the two modules agree on the
    stamp by construction rather than by comment.
    """
    if epoch is None:
        epoch = time.time()
    local = time.localtime(epoch)
    ms = int((epoch - int(epoch)) * 1000)
    return "%s.%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", local), ms)


def backoff_seconds(consecutive_failures, base=2.0, cap=60.0):
    """Seconds to wait before the next host restart after ``consecutive_failures`` in a row.

    1 failure -> base, then doubling, capped. A host that ran a healthy while resets the
    counter (the caller does that), so a single blip costs ``base`` seconds, not a minute.
    """
    n = max(1, int(consecutive_failures))
    return float(min(cap, base * (2 ** (n - 1))))


class RotatingLog:
    """Append stamped lines to a log other programs may read while it is written.

    * opened in append mode with sharing that permits concurrent readers (on Windows via
      the msvcrt share flags; on POSIX ordinary append already allows it),
    * rotates when the file would exceed ``max_bytes``: ``name`` -> ``name.1`` -> ... up to
      ``backups`` old files, the oldest discarded,
    * survives process restarts because it always appends to the live name.
    """

    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES, backups=DEFAULT_BACKUPS):
        self.path = path
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self._lock = threading.Lock()
        self._fh = None
        d = os.path.dirname(path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
        self._open()

    def _open(self):
        self._fh = self._open_shared(self.path)

    @staticmethod
    def _open_shared(path):
        """Open ``path`` for append, allowing other processes to read concurrently."""
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401 (import proves the platform; flags used below)
                # _O_* via os; share-deny-none is the default for files opened with the
                # CRT on Windows when not using exclusive flags, and Python's open() uses
                # _wfopen which already permits readers. A plain append open is therefore
                # shared-read on this platform too; we keep the hook explicit so the intent
                # is recorded and a future tightening has one place to change.
                return open(path, "a", encoding="utf-8", buffering=1)
            except Exception:
                return open(path, "a", encoding="utf-8", buffering=1)
        return open(path, "a", encoding="utf-8", buffering=1)

    def _rotate_locked(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        # name.(k) -> name.(k+1), oldest first so nothing is overwritten before it moves.
        oldest = "%s.%d" % (self.path, self.backups)
        try:
            if os.path.exists(oldest):
                os.remove(oldest)
        except OSError:
            pass
        for k in range(self.backups - 1, 0, -1):
            src = "%s.%d" % (self.path, k)
            dst = "%s.%d" % (self.path, k + 1)
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        if os.path.exists(self.path):
            try:
                os.replace(self.path, "%s.1" % self.path)
            except OSError:
                pass
        self._open()

    def write_line(self, text):
        """Stamp ``text`` and append it, rotating first if it would cross the size cap."""
        line = millis_stamp() + " " + text.rstrip("\r\n") + "\n"
        data = line.encode("utf-8")
        with self._lock:
            try:
                size = self._fh.tell()
            except (OSError, ValueError):
                size = 0
            if size and size + len(data) > self.max_bytes:
                self._rotate_locked()
            self._fh.write(line)
            try:
                self._fh.flush()
            except OSError:
                pass

    def close(self):
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass


def pump_stream(stream, sink, on_line=None):
    """Read ``stream`` line by line until EOF, handing each line to ``sink.write_line``.

    Blocks in its own thread; returns when the stream closes. ``on_line`` (optional) is
    called with each raw line after it is written -- the analyzer hook uses it. Decoding
    errors never stop the pump: a line that will not decode is replaced, not dropped.
    """
    if isinstance(stream, io.TextIOBase):
        iterator = stream
    else:
        iterator = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
    for line in iterator:
        text = line.rstrip("\r\n")
        sink.write_line(text)
        if on_line is not None:
            try:
                on_line(text)
            except Exception:
                pass


# THE RECONNECT LIMIT, SHARED WITH THE SUPERVISOR. scripts/supervisor_zero_conn_recovery.py
# ends this reader's host child from the outside once the relay has shown zero Host connections
# for longer than this; the reader does the SAME from the inside here, so recovery does not
# depend on the supervisor being the one to notice. The two numbers are kept equal on purpose
# (measured 2026-11-04: 157 s longest reconnect, ~149 s four-tick debounce, ~7 min gap this
# closes). The reader only ever ends ITS OWN host child -- it never starts a second reader.
ZERO_CONN_GRACE_SECONDS = 240

#: How often the reader asks the relay for its Host connections count while a host runs. One
#: `devtunnel show` was measured at a few seconds, so polling every 15 s is cheap next to a
#: host that normally runs for hours, and is frequent enough that a grace of 240 s is crossed
#: within one poll of the limit.
ZERO_CONN_POLL_SECONDS = 15

# "Host connections : N" in `devtunnel show <name>`, the exact line Get-TunnelHostConnections
# in scripts/supervisor.ps1 reads, so the reader and the supervisor count the same thing.
_HOST_CONNECTIONS = re.compile(r"Host connections\s*:\s*(\d+)", re.IGNORECASE)


def decide_zero_conn_host_recycle(seconds_at_zero, grace=ZERO_CONN_GRACE_SECONDS):
    """"recycle" when the relay has shown zero connections past ``grace``, else "wait".

    PURE: no clock, no process, no devtunnel. ``seconds_at_zero`` is how long the relay's
    Host connections count has been read as zero WITHOUT interruption (the caller resets it
    to None the moment a poll sees >= 1, or a poll cannot be read). ``None`` (never zero, or
    the count could not be read) -> "wait": an unreadable or not-yet-zero count must never be
    the reason a working host is recycled. Only a count that has been a clear zero for longer
    than ``grace`` yields "recycle".
    """
    if seconds_at_zero is None:
        return "wait"
    try:
        secs = float(seconds_at_zero)
    except (TypeError, ValueError):
        return "wait"
    try:
        g = float(grace)
    except (TypeError, ValueError):
        g = float(ZERO_CONN_GRACE_SECONDS)
    return "recycle" if secs > g else "wait"


def read_host_connections(devtunnel, tunnel_name, timeout=20):
    """The relay's Host connections count for ``tunnel_name``, or None when unreadable.

    Runs ``devtunnel show <name>`` and reads the same "Host connections : N" line the
    supervisor reads. None on any failure (CLI missing, offline, no such line, timeout):
    an unreadable count is "cannot tell", which decide_zero_conn_host_recycle treats as wait.
    """
    try:
        proc = subprocess.run(
            [devtunnel, "show", tunnel_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout,
            **childproc.tree_popen_kwargs(headless=True, priority=False),
        )
    except Exception:
        return None
    try:
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
    except Exception:
        return None
    m = _HOST_CONNECTIONS.search(out)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _watch_zero_connections(proc, sink, read_conn, stop_event,
                            grace=ZERO_CONN_GRACE_SECONDS, poll=ZERO_CONN_POLL_SECONDS,
                            now=time.time, sleep=time.sleep):
    """While ``proc`` runs, poll the connection count; end ONLY ``proc`` if zero past grace.

    ``read_conn()`` returns the current Host connections count or None. The reader is NOT
    stopped here -- only the host child (``proc``). serve_forever's loop then sees the host
    exit and starts a fresh host; no second reader is ever started. Returns True if it ended
    the host for a zero-connection recycle, False otherwise (host exited on its own, or the
    watcher was asked to stop). Pure of real time only through ``now``/``sleep`` so the test
    drives it with a fake clock.
    """
    zero_since = None
    while not stop_event.is_set():
        if proc.poll() is not None:
            return False  # the host exited on its own; the main wait() handles it.
        count = read_conn()
        if count is None or count >= 1:
            # Unreadable or a real connection -> not a zero streak; reset.
            zero_since = None
        else:
            if zero_since is None:
                zero_since = now()
            seconds_at_zero = now() - zero_since
            if decide_zero_conn_host_recycle(seconds_at_zero, grace) == "recycle":
                sink.write_line(
                    "[tunnel_host_log] Host connections stayed at 0 for %.0fs (> %ds): "
                    "ending this reader's host child pid %s to start a fresh host "
                    "(no second reader)" % (seconds_at_zero, int(grace), proc.pid))
                try:
                    proc.terminate()
                except Exception:
                    pass
                return True
        # Wake promptly when asked to stop; otherwise sleep the poll interval.
        if stop_event.wait(poll):
            return False
    return False


def run_once(cmd, sink, on_exit=None, hook=None, read_conn=None,
             grace=ZERO_CONN_GRACE_SECONDS, poll=ZERO_CONN_POLL_SECONDS):
    """Spawn ``cmd``, pump its stdout and stderr into ``sink`` until it exits.

    Returns ``(exit_code, recycled)``. ``recycled`` is True only when the host was ended by
    the zero-connection watcher (relay showed no Host connections past ``grace``); False when
    the host exited on its own. When ``read_conn`` is None no watcher runs and ``recycled`` is
    always False (the hermetic pipe tests use this form). Used by :func:`serve_forever` and,
    with a fake command, by the tests that prove the reader keeps consuming while nobody calls
    anything.
    """
    # bufsize=-1 (system default): the pipes are binary here and line buffering is invalid
    # for binary mode (it warns and is ignored). pump_stream wraps each pipe in a
    # TextIOWrapper and iterates it line by line, so the line granularity lives there.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=-1,
        **childproc.tree_popen_kwargs(headless=True, priority=False),
    )
    threads = []
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        t = threading.Thread(target=pump_stream, args=(stream, sink, hook), daemon=True)
        t.start()
        threads.append(t)
    recycled = {"value": False}
    stop_event = threading.Event()
    watcher = None
    if read_conn is not None:
        def _watch():
            recycled["value"] = _watch_zero_connections(
                proc, sink, read_conn, stop_event, grace=grace, poll=poll)
        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
    rc = proc.wait()
    stop_event.set()
    if watcher is not None:
        watcher.join(timeout=5)
    for t in threads:
        t.join(timeout=5)
    if on_exit is not None:
        try:
            on_exit(rc)
        except Exception:
            pass
    return rc, recycled["value"]


def _analyzer_hook(root):
    """Return an on_line callback that appends new close records, or None if unavailable."""
    if tunnel_channels is None:
        return None
    log_path = os.path.join(root, ".setup", "logs", "devtunnel-host-ts.log")
    jsonl = os.path.join(root, ".setup", "logs", "tunnel_channels.jsonl")

    def _hook(_text):
        try:
            tunnel_channels.append_new_records(log_path, jsonl)
        except Exception:
            pass

    return _hook


def serve_forever(tunnel_name, root=None, devtunnel="devtunnel",
                  max_bytes=DEFAULT_MAX_BYTES, backups=DEFAULT_BACKUPS,
                  max_restarts=None, watch_connections=True,
                  grace=ZERO_CONN_GRACE_SECONDS, poll=ZERO_CONN_POLL_SECONDS):
    """Host the tunnel, logging verbosely, restarting on exit with a backoff.

    ``max_restarts`` bounds the loop for tests (None = forever). A host that stayed up at
    least 30 s resets the failure counter so a long-lived host that finally drops is not
    treated as a fast-failing one.

    SELF-RECOVERY AT ZERO CONNECTIONS. When ``watch_connections`` is set (the default in
    production), a watcher polls ``devtunnel show`` while each host runs and, if the relay's
    Host connections count stays at zero for longer than ``grace``, ends ONLY that host child
    so this loop starts a fresh one -- it never starts a second reader. A host recycled that
    way is NOT a failure: the backoff and failure counter are skipped so the fresh host comes
    up at once (~8 s to re-register, measured 2026-11-04), closing the ~7-minute gap where a
    live host sat at zero connections and nothing replaced it.
    """
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_path = os.path.join(root, ".setup", "logs", "devtunnel-host-ts.log")
    sink = RotatingLog(log_path, max_bytes=max_bytes, backups=backups)
    hook = _analyzer_hook(root)
    cmd = [devtunnel, "-v", "host", tunnel_name]
    sink.write_line("[tunnel_host_log] starting: %s (pid=%d)" % (" ".join(cmd), os.getpid()))
    read_conn = None
    if watch_connections:
        read_conn = lambda: read_host_connections(devtunnel, tunnel_name)  # noqa: E731
    failures = 0
    count = 0
    try:
        while True:
            started = time.time()
            recycled = False
            try:
                rc, recycled = run_once(cmd, sink, hook=hook, read_conn=read_conn,
                                        grace=grace, poll=poll)
            except FileNotFoundError as e:
                sink.write_line("[tunnel_host_log] cannot start host: %s" % e)
                rc = 127
            ran_for = time.time() - started
            sink.write_line("[tunnel_host_log] host exited code=%s after %.1fs" % (rc, ran_for))
            count += 1
            if max_restarts is not None and count >= max_restarts:
                break
            if recycled:
                # A deliberate zero-connection recycle, not a crash: do not back off and do
                # not count it as a failure. Start a fresh host at once.
                failures = 0
                sink.write_line("[tunnel_host_log] restarting now (recycled at zero connections)")
                continue
            if ran_for >= 30:
                failures = 0
            failures += 1
            wait = backoff_seconds(failures)
            sink.write_line("[tunnel_host_log] restarting in %.0fs (failure #%d)" % (wait, failures))
            time.sleep(wait)
    finally:
        sink.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: python -m relay.tunnel_host_log <tunnel-name> [devtunnel-bin]\n")
        return 2
    tunnel_name = argv[0]
    devtunnel = argv[1] if len(argv) > 1 else os.environ.get("MCP_DEVTUNNEL_BIN", "devtunnel")
    serve_forever(tunnel_name, devtunnel=devtunnel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
