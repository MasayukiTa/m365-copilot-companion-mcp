"""Integration proof for scope (2), the SLOW-BUT-ANSWERING shape: a server COPY that LISTENS
on a DIFFERENT PORT and keeps returning HTTP 200 for /health -- but always PAST the slow bound
-- is restarted by the health-fail gate after N consecutive unhealthy checks, EVEN WHILE ITS
PROCESS IS ALIVE and the port stays held.

WHY THIS TEST EXISTS
--------------------
The pure core ``supervisor_health_fail_restart`` is unit-tested from fixed inputs
(test_supervisor_health_fail_restart.py), and the FULL-SILENCE shape is pinned against a real
wedged listener by test_supervisor_hang_restart_port_copy.py (that one drives the 120s hang
gate ``decide_hang_restart``). Neither exercises the 2026-11-11 shape that the task names:
/health that KEEPS ANSWERING 200 but is too SLOW -- the supervisor logged "server slow (3 s)
but answering -- not restarting" and sat the degradation out for four minutes.

This test stands up a REAL TCP listener on an ephemeral DIFFERENT PORT that actually completes
each /health request with a 200, but only AFTER a settable server-side delay. It then measures
the same two facts Test-ServerUp measures per tick against a real connection:

  * ``answered`` -- did a real GET /health return 200 within the probe timeout;
  * ``seconds``  -- how long that real round-trip took (wall clock of an actual socket call).

and folds them through the pure ``next_streak`` / ``decide_health_fail_restart`` exactly as the
supervisor does, proving that a copy which answers-but-slowly on a different port drives the
streak to N and yields ``restart``, while a fast copy on the SAME machinery yields ``wait``.

The incident this guards (2026-11-11 17:22-17:26): main.py kept its PID and kept the port
LISTENING and kept ANSWERING /health -- just slowly and degrading -- and both existing gates
(strike-resets-on-any-answer, hang-needs-full-silence) declined. This gate is the third path.

DETERMINISTIC, NOT A WALL-CLOCK CEILING. We force the slow copy's per-request delay ABOVE the
bound we pass to the classifier and the fast copy's delay to ~0, so the healthy/unhealthy
classification is decided by the measured-vs-bound comparison, not by how the runner schedules
threads. We assert the streak reaches N and the verdict flips to ``restart`` at exactly the Nth
consecutive slow check, and that a single fast check in the middle resets it to ``wait``. No
upper time bound is asserted on the runner.

Stdlib only (socket, threading, http.client, time). No uvicorn, no main.py import, no pywin32,
nothing Windows-specific: this runs in the Linux CI test job exactly like the pure tests.
"""
import http.client
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supervisor_health_fail_restart import (  # noqa: E402
    next_streak,
    decide_health_fail_restart,
    SLOW_HEALTH_FAIL_STREAK,
)


class _DelayedHealthServer:
    """A real TCP listener on an ephemeral port that answers GET /health with 200 after a
    settable server-side delay.

    Models a degraded-but-alive server: the socket is bound and LISTENING, the process (this
    thread's owner) stays alive, and every /health DOES complete with a 200 -- just slowly when
    ``delay_s`` is large. This is the slow-but-answering shape, distinct from the wedged
    (never-replies) listener used by the full-silence hang test.
    """

    def __init__(self, delay_s):
        self.delay_s = float(delay_s)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))  # 0 = a DIFFERENT, ephemeral port
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
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.recv(4096)  # read (and ignore) the request line/headers
            if self.delay_s > 0:
                time.sleep(self.delay_s)
            body = b"ok"
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            conn.sendall(resp)
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


def _probe_once(port, timeout_s):
    """One real GET /health against the copy. Returns (answered, seconds).

    answered is True only on a real 200 within ``timeout_s``; seconds is the measured
    wall-clock round-trip. A timeout/refusal returns (False, elapsed) -- the unhealthy,
    did-not-answer case.
    """
    start = time.monotonic()
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        elapsed = time.monotonic() - start
        return (resp.status == 200, elapsed)
    except (OSError, http.client.HTTPException):
        return (False, time.monotonic() - start)
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _connect_ok(port):
    """Is the port actually LISTENING (a plain TCP connect succeeds)? This is the 'listening'
    fact the supervisor feeds the gate."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


# ---- a REAL slow-but-answering copy on a different port drives the streak to N -> restart ----

def test_real_slow_answering_copy_restarts_at_streak_n():
    # The copy answers 200 every time, but each answer takes ~0.20s. We classify against a
    # bound of 0.05s so every real answer lands PAST the bound = unhealthy, reproducing the
    # "answered but too slow" tick without waiting a real 30s. The port is a DIFFERENT,
    # ephemeral one; the process (thread owner) stays alive the whole time.
    srv = _DelayedHealthServer(delay_s=0.20)
    bound = 0.05
    try:
        assert _connect_ok(srv.port) is True  # really LISTENING on the different port

        streak = 0
        verdicts = []
        fired_tick = None
        for tick in range(1, SLOW_HEALTH_FAIL_STREAK + 3):
            answered, seconds = _probe_once(srv.port, timeout_s=5)
            assert answered is True        # it DID answer -- this is the slow-but-answering shape
            assert seconds >= bound        # ... but slower than the bound: an unhealthy tick
            streak = next_streak(streak, answered, seconds, bound_s=bound)
            v = decide_health_fail_restart(
                streak,
                listening=_connect_ok(srv.port),   # still held -> not the vanished path
                own_main_py_alive=True,            # the copy's process did NOT die
                booting=False,
            )
            verdicts.append(v)
            if v == "restart" and fired_tick is None:
                fired_tick = tick

        # exactly one crossing, at the Nth consecutive unhealthy check, and it holds thereafter
        assert fired_tick == SLOW_HEALTH_FAIL_STREAK
        assert verdicts[: SLOW_HEALTH_FAIL_STREAK - 1] == ["wait"] * (SLOW_HEALTH_FAIL_STREAK - 1)
        assert all(v == "restart" for v in verdicts[SLOW_HEALTH_FAIL_STREAK - 1:])
    finally:
        srv.close()


# ---- a REAL fast-answering copy on the SAME machinery never restarts -------------------------

def test_real_fast_answering_copy_never_restarts():
    # Same real listener, but it answers immediately. Against a bound WELL above the round-trip,
    # every real tick is healthy, the streak stays 0, and the verdict never leaves "wait".
    srv = _DelayedHealthServer(delay_s=0.0)
    bound = 2.0
    try:
        assert _connect_ok(srv.port) is True
        streak = 0
        for _ in range(SLOW_HEALTH_FAIL_STREAK + 3):
            answered, seconds = _probe_once(srv.port, timeout_s=5)
            assert answered is True
            assert seconds < bound
            streak = next_streak(streak, answered, seconds, bound_s=bound)
            assert streak == 0
            assert decide_health_fail_restart(
                streak, listening=True, own_main_py_alive=True, booting=False
            ) == "wait"
    finally:
        srv.close()


# ---- a single fast tick mid-stream resets the real streak, preventing the restart ------------

def test_real_one_fast_tick_prevents_restart():
    # Alternate a slow copy and a fast copy so a healthy tick lands before the streak reaches N;
    # proves recovery on a real socket (not just fixed inputs) stops the kill.
    slow = _DelayedHealthServer(delay_s=0.20)
    fast = _DelayedHealthServer(delay_s=0.0)
    bound = 0.05
    try:
        streak = 0
        saw_restart = False
        # slow, slow, FAST, slow, slow -> never 4 unhealthy in a row
        plan = [slow, slow, fast, slow, slow]
        for srv in plan:
            answered, seconds = _probe_once(srv.port, timeout_s=5)
            streak = next_streak(streak, answered, seconds, bound_s=bound)
            if decide_health_fail_restart(
                streak, listening=True, own_main_py_alive=True, booting=False
            ) == "restart":
                saw_restart = True
        assert saw_restart is False
    finally:
        slow.close()
        fast.close()
