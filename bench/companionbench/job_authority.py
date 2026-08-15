"""A job store the solver can call and cannot edit.

WHY THIS EXISTS

The runtime episodes grade whether an agent drove the job store's API correctly -- took a
turn over, committed exactly once, resumed a parked job. They did that by reading the
store's own tables, in a SQLite file handed to the agent inside its workdir. So "the agent
called commit_turn" and "the agent wrote a row saying it did" were the same observation.

Every tightening reached the same wall. Requiring the audit event raised forgery from one
UPDATE to two or three SQL statements. Reserving the transition event types closed the
supported API path. Neither changes the fact that the judge was reading evidence the solver
could write, and the docstrings ended up saying so at length instead of fixing it.

WHAT THIS CHANGES

The store runs in a separate process, and its database lives in a directory the episode
never tells the agent about and never places under the workdir. The agent gets an HTTP
endpoint and a capability token that permit exactly five operations -- the ones a job store
is for. There is no endpoint that writes an arbitrary row, and no path from the agent to the
file.

Receipts are appended by the operation itself -- under the same lock as the state change,
though NOT inside the same database transaction: the store commits, and the receipt is
written immediately after, so a crash in the gap between them leaves a state change without
its receipt. That window is small and it is real, and describing it as transactional (as an
earlier version of this text did) would make a reader trust an ordering guarantee that does
not exist.

Each receipt carries a monotonic sequence and an HMAC chain over the previous one. The judge
reads them over a separate token the agent is never given, and verifies the chain. An agent
that skips the API cannot produce a receipt; an agent that fabricates one cannot make it
chain; and an agent that DELETES one is caught because every response carries the sequence
of the receipt it produced, so a caller can insist on finding the receipt it was promised --
without that, verification began from whatever list it was handed, and a list with its last
entry removed chained perfectly.

WHAT IT STILL DOES NOT DO

Both processes run as the same OS user, so this is a capability boundary rather than a
sandbox: something determined to bypass it can read the parent's memory or the temp
directory it chose. Closing that needs OS-level confinement, which is a deployment decision
rather than a benchmark one. What it does establish is that an ORDINARY solver -- one doing
the task, correctly or incorrectly -- cannot manufacture the evidence, which is what these
episodes actually need to measure.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The operations an agent may invoke. Deliberately a closed list of the store's real verbs:
#: an authority that can be asked to write an arbitrary row is the file it replaced.
AGENT_OPERATIONS = ("claim_turn", "commit_turn", "resume_interaction", "get_job_status",
                    "heartbeat")

#: Operations only the judge may invoke -- fixture construction and evidence reading. Kept
#: behind a second token so the agent cannot create the job it is supposed to find, and
#: cannot read the receipts it is being measured by.
JUDGE_OPERATIONS = ("create_job", "mark_waiting_interaction", "receipts", "state")


class AuthorityError(RuntimeError):
    """Raised when the authority cannot be reached, or answers something impossible."""


def _server_source() -> str:
    """The authority process. Held as source so the parent owns exactly what runs."""
    return r'''
import hashlib, hmac, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# CONFIGURATION ARRIVES ON STDIN, not in argv. An earlier version passed the DB path, both
# tokens and the HMAC secret as command-line arguments, where any process running as the same
# OS user could read them straight out of the process table -- so the "the agent cannot reach
# the evidence" boundary was one `wmic process get commandline` from being no boundary at all.
# stdin is not a boundary either, but it is not a public listing.
_CFG     = json.loads(sys.stdin.readline() or "{}")
sys.path.insert(0, _CFG["repo"])
DB       = _CFG["db"]
AGENT_T  = _CFG["agent_token"]
JUDGE_T  = _CFG["judge_token"]
SECRET   = _CFG["secret"].encode("utf-8")

from relay.local_job_store import LocalJobStore, JobStoreError

STORE = LocalJobStore(DB)
LOCK = threading.Lock()
RECEIPTS = []          # in memory only: the agent has no file to reach even if it looked

AGENT_OPS = ("claim_turn", "commit_turn", "resume_interaction", "get_job_status",
             "heartbeat")
JUDGE_OPS = ("create_job", "mark_waiting_interaction")


def _append(op, args, ok, result):
    """A receipt, chained. Written by the operation, not by a caller. Returns its seq.

    The seq goes back to the caller in the response, which is what makes a MISSING receipt
    detectable: a party that has been told "your call is receipt 7" can check that receipt 7
    is still there. Without it, a verifier starts from whatever list it is handed, and a list
    with the last entry removed chains perfectly.
    """
    prev = RECEIPTS[-1]["mac"] if RECEIPTS else ""
    body = {
        "seq": len(RECEIPTS) + 1,
        "op": op,
        "args": args,
        "ok": bool(ok),
        "result_digest": hashlib.sha256(
            json.dumps(result, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16],
        "prev": prev,
    }
    body["mac"] = hmac.new(
        SECRET, json.dumps(body, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()
    RECEIPTS.append(body)
    return body["seq"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "malformed request"})
        op = str(req.get("op") or "")
        token = str(req.get("token") or "")
        args = dict(req.get("args") or {})

        if op == "receipts":
            if token != JUDGE_T:
                return self._send(403, {"error": "receipts are judge-only"})
            return self._send(200, {"receipts": list(RECEIPTS)})
        if op == "state":
            if token != JUDGE_T:
                return self._send(403, {"error": "state is judge-only"})
            import sqlite3
            con = sqlite3.connect(DB)
            con.row_factory = sqlite3.Row
            out = {t: [dict(r) for r in con.execute("SELECT * FROM %s" % t)]
                   for t in ("jobs", "turns", "events")}
            con.close()
            return self._send(200, out)

        if op in JUDGE_OPS:
            if token != JUDGE_T:
                return self._send(403, {"error": "%s is judge-only" % op})
        elif op in AGENT_OPS:
            if token not in (AGENT_T, JUDGE_T):
                return self._send(403, {"error": "bad token"})
        else:
            return self._send(404, {"error": "no such operation: %r" % op})

        fn = getattr(STORE, op, None)
        if fn is None:
            return self._send(404, {"error": "no such operation: %r" % op})
        with LOCK:
            try:
                result = fn(**args)
                seq = _append(op, args, True, result)
                return self._send(200, {"result": result, "receipt_seq": seq})
            except JobStoreError as exc:
                seq = _append(op, args, False, str(exc))
                return self._send(409, {"error": str(exc), "receipt_seq": seq,
                                        "code": exc.args[0] if exc.args else ""})
            except Exception as exc:
                seq = _append(op, args, False, str(exc))
                return self._send(500, {"receipt_seq": seq,
                                        "error": "%s: %s" % (type(exc).__name__, exc)})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
print("__AUTHORITY_PORT__ %d" % srv.server_address[1], flush=True)
srv.serve_forever()
'''


class JobAuthority:
    """A running job store the agent can call and cannot edit. Use as a context manager."""

    def __init__(self, python=None):
        self.python = python or sys.executable
        # OUTSIDE the workdir, and the agent is never told the path. The episode's own
        # fixtures live in the workdir; the evidence does not.
        self.root = tempfile.mkdtemp(prefix="cb_authority_")
        self.db = os.path.join(self.root, "jobs.sqlite3")
        self.agent_token = secrets.token_hex(16)
        self.judge_token = secrets.token_hex(16)
        self._secret = secrets.token_hex(32)
        self.port = 0
        self._proc = None
        #: Every receipt seq the authority has told a caller it wrote. Verification starts
        #: from this rather than from the returned list, so a truncated list is detectable.
        self._promised = set()

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self):
        self._proc = subprocess.Popen(
            [self.python, "-c", _server_source()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._proc.stdin.write(json.dumps({
            "repo": REPO, "db": self.db, "agent_token": self.agent_token,
            "judge_token": self.judge_token, "secret": self._secret}) + "\n")
        self._proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if line.startswith("__AUTHORITY_PORT__ "):
                self.port = int(line.split()[1])
                return self
            if self._proc.poll() is not None:
                raise AuthorityError("authority exited: %s"
                                     % (self._proc.stderr.read() or "")[-400:])
        raise AuthorityError("authority did not report a port within 30s")

    def __exit__(self, *exc):
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=10)
        # The DB holds the episode's job state and lived outside the workdir precisely so the
        # agent could not reach it; leaving it in the temp directory afterwards keeps it
        # reachable for as long as the machine keeps temp files.
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    # -- calling ------------------------------------------------------------------------

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def _call(self, op, args=None, token=None):
        body = json.dumps({"op": op, "args": args or {}, "token": token or self.judge_token})
        req = urllib.request.Request(self.url, data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return self._note(json.loads(resp.read() or b"{}"))
        except urllib.error.HTTPError as exc:
            return self._note(json.loads(exc.read() or b"{}"))
        except Exception as exc:
            raise AuthorityError("authority unreachable: %s: %s" % (type(exc).__name__, exc))

    def _note(self, payload):
        """Remember the receipt seq this call was told it produced."""
        try:
            seq = payload.get("receipt_seq")
            if isinstance(seq, int):
                self._promised.add(seq)
        except Exception:
            pass
        return payload

    def as_judge(self, op, **args):
        return self._call(op, args, self.judge_token)

    def as_agent(self, op, **args):
        return self._call(op, args, self.agent_token)

    # -- evidence -------------------------------------------------------------------------

    def receipts(self, op=None):
        rows = self.as_judge("receipts").get("receipts") or []
        return [r for r in rows if op is None or r.get("op") == op]

    def receipts_intact(self):
        """Verify the chain, and that nothing was removed from the end of it.

        Two separate claims, and the first version only made the first one. Recomputing the
        MAC catches a forged or edited receipt, because the secret never leaves this process
        and the authority. It does NOT catch a receipt that is simply absent: verification
        began from whatever list the server returned, and a list with its last entry removed
        -- or an empty list -- chains perfectly and verified.

        So every response carries the seq of the receipt it produced, and every seq this
        process has been promised must still be present. A truncation now has to remove a
        receipt somebody was told about, which the caller's own record contradicts.
        """
        rows = self.as_judge("receipts").get("receipts") or []
        prev = ""
        for i, row in enumerate(rows, start=1):
            body = {k: row[k] for k in ("seq", "op", "args", "ok", "result_digest", "prev")}
            expect = hmac.new(self._secret.encode("utf-8"),
                              json.dumps(body, sort_keys=True).encode("utf-8"),
                              hashlib.sha256).hexdigest()
            if row.get("seq") != i or row.get("prev") != prev or row.get("mac") != expect:
                return False
            prev = row["mac"]
        present = {r.get("seq") for r in rows}
        return self._promised.issubset(present)

    def state(self):
        return self.as_judge("state")

    def prompt_fragment(self, job_id):
        """What the episode tells the agent: an endpoint, a token, and the verbs. No path."""
        return (
            "ジョブストアは HTTP API です。ファイルは存在しません。\n"
            "  エンドポイント: %s\n"
            "  トークン: %s\n"
            '  呼び出し方: POST に {"op": 操作名, "token": トークン, "args": {...}} を JSON で送る\n'
            "  使える操作: %s\n"
            "  対象ジョブ: %s\n"
            % (self.url, self.agent_token, ", ".join(AGENT_OPERATIONS), job_id))


def free_port() -> int:
    """An unused loopback port. Only for tests that need to point at nothing."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
