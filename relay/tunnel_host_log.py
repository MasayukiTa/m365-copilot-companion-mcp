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


def run_once(cmd, sink, on_exit=None, hook=None):
    """Spawn ``cmd``, pump its stdout and stderr into ``sink`` until it exits.

    Returns the process exit code. Used by :func:`serve_forever` and, with a fake command,
    by the tests that prove the reader keeps consuming while nobody calls anything.
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
    rc = proc.wait()
    for t in threads:
        t.join(timeout=5)
    if on_exit is not None:
        try:
            on_exit(rc)
        except Exception:
            pass
    return rc


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
                  max_restarts=None):
    """Host the tunnel, logging verbosely, restarting on exit with a backoff.

    ``max_restarts`` bounds the loop for tests (None = forever). A host that stayed up at
    least 30 s resets the failure counter so a long-lived host that finally drops is not
    treated as a fast-failing one.
    """
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_path = os.path.join(root, ".setup", "logs", "devtunnel-host-ts.log")
    sink = RotatingLog(log_path, max_bytes=max_bytes, backups=backups)
    hook = _analyzer_hook(root)
    cmd = [devtunnel, "-v", "host", tunnel_name]
    sink.write_line("[tunnel_host_log] starting: %s (pid=%d)" % (" ".join(cmd), os.getpid()))
    failures = 0
    count = 0
    try:
        while True:
            started = time.time()
            try:
                rc = run_once(cmd, sink, hook=hook)
            except FileNotFoundError as e:
                sink.write_line("[tunnel_host_log] cannot start host: %s" % e)
                rc = 127
            ran_for = time.time() - started
            sink.write_line("[tunnel_host_log] host exited code=%s after %.1fs" % (rc, ran_for))
            count += 1
            if max_restarts is not None and count >= max_restarts:
                break
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
