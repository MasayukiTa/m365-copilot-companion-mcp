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

Receipts are appended by the operation itself, inside the same transaction as the state
change, with a monotonic sequence and an HMAC chain over the previous receipt. The judge
reads them over a separate token that the agent is never given, and verifies the chain. An
agent that skips the API cannot produce a receipt; an agent that fabricates one cannot make
it chain.

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

sys.path.insert(0, sys.argv[1])
DB       = sys.argv[2]
AGENT_T  = sys.argv[3]
JUDGE_T  = sys.argv[4]
SECRET   = sys.argv[5].encode("utf-8")

from relay.local_job_store import LocalJobStore, JobStoreError

STORE = LocalJobStore(DB)
LOCK = threading.Lock()
RECEIPTS = []          # in memory only: the agent has no file to reach even if it looked

AGENT_OPS = ("claim_turn", "commit_turn", "resume_interaction", "get_job_status",
             "heartbeat")
JUDGE_OPS = ("create_job", "mark_waiting_interaction")


def _append(op, args, ok, result):
    """A receipt, chained. Written by the operation, not by a caller."""
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
                _append(op, args, True, result)
                return self._send(200, {"result": result})
            except JobStoreError as exc:
                _append(op, args, False, str(exc))
                return self._send(409, {"error": str(exc), "code": exc.args[0]
                                        if exc.args else ""})
            except Exception as exc:
                _append(op, args, False, str(exc))
                return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})


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

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self):
        self._proc = subprocess.Popen(
            [self.python, "-c", _server_source(), REPO, self.db,
             self.agent_token, self.judge_token, self._secret],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read() or b"{}")
        except Exception as exc:
            raise AuthorityError("authority unreachable: %s: %s" % (type(exc).__name__, exc))

    def as_judge(self, op, **args):
        return self._call(op, args, self.judge_token)

    def as_agent(self, op, **args):
        return self._call(op, args, self.agent_token)

    # -- evidence -------------------------------------------------------------------------

    def receipts(self, op=None):
        rows = self.as_judge("receipts").get("receipts") or []
        return [r for r in rows if op is None or r.get("op") == op]

    def receipts_intact(self):
        """Verify the chain. A forged or edited receipt breaks it; a missing one breaks it.

        Recomputing the MAC here, in the judge, is the point: the secret never leaves this
        process and the authority, so a receipt the agent invented cannot chain.
        """
        prev = ""
        for i, row in enumerate(self.as_judge("receipts").get("receipts") or [], start=1):
            body = {k: row[k] for k in ("seq", "op", "args", "ok", "result_digest", "prev")}
            expect = hmac.new(self._secret.encode("utf-8"),
                              json.dumps(body, sort_keys=True).encode("utf-8"),
                              hashlib.sha256).hexdigest()
            if row.get("seq") != i or row.get("prev") != prev or row.get("mac") != expect:
                return False
            prev = row["mac"]
        return True

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
