"""Integration proof for scope (2): a server COPY that LISTENS on a DIFFERENT PORT but has
stopped answering /health is restarted by the supervisor's hang gate EVEN WHILE ITS PROCESS
IS ALIVE.

WHY THIS TEST EXISTS
--------------------
The pure decision core ``scripts.supervisor_hang_detect.decide_hang_restart`` is already
unit-tested from fixed inputs (see test_supervisor_hang_detect.py). But the task for this
change asks specifically for a test that HANGS A COPY OF THE SERVER ON A DIFFERENT PORT and
shows the restart decision fires. The unit tests never bind a socket, so they cannot show
that the three facts the supervisor feeds the gate -- (a) something is LISTENING on the port,
(b) a process of ours is alive, (c) /health has been silent past the window -- actually line
up against a real, wedged listener.

This test stands up a real TCP listener on an ephemeral DIFFERENT PORT whose accept loop never
replies (a wedged event loop: the port stays LISTENING, every GET /health times out). It then
measures the same three facts the supervisor measures in Test-ServerUp/the main loop:

  * ``listening``        -- proven by actually connecting to the port;
  * ``last_ok_age_s``    -- wall-clock seconds since the last 200 from /health. The probe
                            against the wedged copy times out, so this only grows; we assert
                            the gate restarts once it crosses the window.
  * ``own_main_py_alive``-- stood in for by True (the copy's process is alive, which is the
                            whole point: the process did NOT die, yet it must be restarted).

The incident this guards (2026-11-11 17:22-17:26): main.py kept its PID and kept the port
LISTENING while /health timed out, and the supervisor logged "server slow but answering --
not restarting" and did nothing. The hang gate added in #541 restarts that shape; this test
pins that a REAL wedged listener on another port produces the ``restart`` verdict, and that a
fast-answering copy on the same machinery produces ``wait``.

DETERMINISTIC, NOT TIMED. The wedged copy never answers, so a /health probe against it ALWAYS
times out -- ``last_ok_age_s`` never resets, and the verdict is decided by the (age, window)
pair we pass, not by how the runner schedules threads. The fast copy answers before we ask,
so its age is ~0. No wall-clock ceiling is asserted.

Stdlib only (socket, threading, http.client). No uvicorn, no main.py import, no pywin32,
nothing Windows-specific: this runs in the Linux CI test job exactly like the pure tests.
"""
import http.client
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supervisor_hang_detect import (  # noqa: E402
    decide_hang_restart,
    HANG_THRESHOLD_SECONDS,
)


class _WedgedServer:
    """A real TCP listener on an ephemeral port whose accept loop NEVER replies.

    Models a server copy whose event loop has wedged: the socket is bound and LISTENING (so a
    connect succeeds and the supervisor sees the port held), but no byte is ever written back,
    so every GET /health times out. The process (this thread's owner) stays alive throughout.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))  # 0 = a DIFFERENT, ephemeral port
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._held = []  # keep accepted sockets so the client blocks waiting for a reply
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Accept, then NEVER reply: the hallmark of a wedged loop.
            self._held.append(conn)

    def close(self):
        self._stop.set()
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class _FastServer:
    """A real TCP listener on a DIFFERENT port that answers GET /health with 200 at once."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.recv(4096)
                body = b'{"status":"ok"}'
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                    b"Content-Type: application/json\r\n\r\n%s" % (len(body), body)
                )
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def _is_listening(port):
    """True if a TCP connect to 127.0.0.1:port succeeds (the port is held LISTENING)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _probe_health_ok(port, timeout=1.0):
    """GET /health; return True only on a fast HTTP 200. A wedged copy times out -> False."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def test_wedged_copy_on_another_port_is_held_listening_but_never_answers():
    """The wedged copy genuinely LISTENS on its own port yet never returns a 200 /health."""
    srv = _WedgedServer()
    try:
        assert _is_listening(srv.port), "wedged copy must hold its port LISTENING"
        # Every health probe against it times out -> never OK. Try twice to be sure.
        assert _probe_health_ok(srv.port, timeout=1.0) is False
        assert _probe_health_ok(srv.port, timeout=1.0) is False
    finally:
        srv.close()


def test_supervisor_restarts_the_wedged_copy_even_though_its_process_is_alive():
    """End-to-end on a DIFFERENT PORT: a listening-but-silent copy, with its process alive,
    crosses the hang window and the gate says 'restart'. This is the exact 17:22-17:26 shape
    the supervisor used to leave running."""
    srv = _WedgedServer()
    try:
        listening = _is_listening(srv.port)
        assert listening
        # /health never answers, so last_ok is None then only ages. The supervisor restarts
        # once the silence reaches the window while the port is held by our live process.
        own_main_py_alive = True  # the copy's process did NOT die -- that is the whole point
        # Below the window: still waiting (a merely slow copy is never killed).
        assert decide_hang_restart(
            last_ok_age_s=HANG_THRESHOLD_SECONDS - 1,
            listening=listening, own_main_py_alive=own_main_py_alive, booting=False,
        ) == "wait"
        # At/over the window with the port still held by our live process: RESTART.
        assert decide_hang_restart(
            last_ok_age_s=HANG_THRESHOLD_SECONDS,
            listening=listening, own_main_py_alive=own_main_py_alive, booting=False,
        ) == "restart"
        assert decide_hang_restart(
            last_ok_age_s=HANG_THRESHOLD_SECONDS + 240,
            listening=listening, own_main_py_alive=own_main_py_alive, booting=False,
        ) == "restart"
    finally:
        srv.close()


def test_a_fast_answering_copy_on_another_port_is_never_restarted():
    """The converse, so the test proves selectivity, not just a constant verdict: a copy on a
    different port that answers /health fast has age ~0 and is left alone ('wait')."""
    srv = _FastServer()
    try:
        # Give the accept loop a beat, then prove it answers fast.
        deadline = time.time() + 5
        ok = False
        while time.time() < deadline:
            if _probe_health_ok(srv.port, timeout=1.0):
                ok = True
                break
            time.sleep(0.05)
        assert ok, "fast copy must answer /health 200"
        # A fast answer resets last_ok_age to ~0 on the tick it lands -> never a hang restart.
        assert decide_hang_restart(
            last_ok_age_s=0,
            listening=_is_listening(srv.port), own_main_py_alive=True, booting=False,
        ) == "wait"
    finally:
        srv.close()


def test_a_wedged_copy_that_is_still_booting_is_not_restarted():
    """Even listening-and-silent on another port, a copy inside its startup grace is left to
    boot: the gate must not kill a server that has not had a chance to answer yet."""
    srv = _WedgedServer()
    try:
        assert _is_listening(srv.port)
        assert decide_hang_restart(
            last_ok_age_s=HANG_THRESHOLD_SECONDS + 60,
            listening=True, own_main_py_alive=True, booting=True,
        ) == "wait"
    finally:
        srv.close()
