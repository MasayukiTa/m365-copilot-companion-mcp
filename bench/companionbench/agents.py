"""Agents an episode can be run against.

The suite takes `agent(prompt, workdir) -> reply` and nothing else. That deliberate
narrowness is what lets the same episodes grade a simulated agent in CI, the real companion
on a workstation, and eventually a candidate harness under evaluation, without any of them
knowing about the others.

THE SSE TRAP, WRITTEN DOWN SO IT IS NOT REDISCOVERED

The bridge answers /stream as text/event-stream with Connection: keep-alive and periodic
`: ping` comment frames. A client that calls read() and waits for EOF therefore blocks
forever even though the answer arrived seconds earlier. That cost most of a day: five
"hangs" were measured at 954s, 949s, 448s, 428s and 443s against a bridge that was
answering in about 28 seconds throughout, and three separate product "fixes" were attributed
to it before the client turned out to be the fault. So BridgeAgent sends Connection: close
and stops at `event: done`.

WHY THE PROMPT CARRIES THE WORKDIR

The episode builds fixtures in a temp directory the agent has never heard of. Telling it the
absolute path is not a hint, it is the whole address space of the task -- without it the
agent works in whatever folder it last used and the grader correctly reports that nothing
happened.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.parse

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765


#: The execution targets an adapter may name, and what each one can actually exercise.
#: "Covered" means a production reader on that target consults the field -- not that the
#: field exists. A target that names a field it cannot reach reintroduces the defect this
#: whole mechanism exists to prevent: two arms differing in something nothing reads.
IN_PROCESS = "in_process/v1"
IN_PROCESS_FIELDS = frozenset({
    "components.memory",              # project_memory.MEMORY_VERSIONS dispatches on it
    "parameters.memory_max_items",    # project_memory.load_notes reads it
})

#: The fleet target additionally reaches the retry and refuter budgets, because
#: run_relay_fleet defaults them from the manifest. It is declared here so the contract can
#: refuse a fleet experiment over a field even the fleet does not read.
FLEET = "relay_fleet/v1"
FLEET_FIELDS = IN_PROCESS_FIELDS | frozenset({
    "parameters.max_retries",         # -> run_relay_fleet max_transient
    "parameters.max_refute_passes",   # -> run_relay_fleet max_refute (only when refuter=True)
})


def attest_in_process(manifest):
    """What harness this process actually loaded, and the values it resolved.

    Read back through the ordinary runtime accessors rather than from the manifest that was
    handed in -- the question is what the CODE sees, and answering it from the argument would
    make the attestation a tautology.
    """
    from relay.selfimprove import manifest as M
    from relay.selfimprove import runtime_config as RC
    active = RC.active_manifest(refresh=True)
    return {
        "harness_id": M.harness_id(active),
        "execution_target": IN_PROCESS,
        "effective": {
            "memory_version": RC.component("memory"),
            "memory_max_items": RC.memory_max_items(),
        },
    }


class SimulatedAgent:
    """A scripted agent for testing the harness itself, never for measuring capability.

    Takes {episode_id: callable(workdir) -> reply}. Anything unscripted does nothing and
    says so, which grades as a failure -- the honest outcome for an agent that was not
    given an answer.
    """

    #: Runs IN THIS PROCESS, so the active manifest genuinely governs everything it touches.
    #: The claim is about the execution mechanism, not about intent -- and it is checked
    #: rather than believed, via attest() below.
    applies_manifest = True
    execution_target = IN_PROCESS
    covered_fields = IN_PROCESS_FIELDS

    def attest(self, manifest):
        return attest_in_process(manifest)

    def __init__(self, script=None, default_reply="(no action taken)"):
        self.script = dict(script or {})
        self.default_reply = default_reply
        self.calls = []

    def for_episode(self, episode_id):
        def agent(prompt, workdir):
            self.calls.append({"episode_id": episode_id, "prompt": prompt})
            fn = self.script.get(episode_id)
            if fn is None:
                return self.default_reply
            return fn(workdir) or ""
        return agent


class BridgeAgent:
    """Drives the real companion through the bridge's /stream endpoint.

    One conversation per episode by default: episodes are independent by construction, and
    letting one carry the previous one's context would make the suite's results depend on
    the order it happened to run in.

    THIS ADAPTER CANNOT RUN AN A/B, and saying so here is the point of the flag below. The
    work happens inside a bridge process that was started with whatever harness it was
    started with; this class posts a prompt and reads a reply. Setting the active manifest in
    the evaluator changes nothing about that process, so a paired comparison driven through
    here executes the SAME program on both arms and any p-value it produces is about noise.
    An independent review found this after four rounds, and it is the most damaging kind of
    defect: everything looks wired up and the number means nothing.

    Making it true requires the bridge to accept a harness for a request and honour it --
    a real change on the bridge side, not here. Until then paired_evaluate refuses this
    adapter rather than producing an unattributable result.
    """

    #: See the class docstring. Do not set this True until the bridge honours a per-request
    #: harness; the flag is what stops an invalid comparison from being reported as valid.
    applies_manifest = False

    def __init__(self, *, host=BRIDGE_HOST, port=BRIDGE_PORT, timeout=300,
                 fresh_conversation=True, retry_busy_s=180):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fresh_conversation = fresh_conversation
        self.retry_busy_s = retry_busy_s
        self.transcript = []

    # -- transport ---------------------------------------------------------------------

    def _request(self, path, timeout=None):
        """One HTTP/1.0-style request that ends at `event: done` or EOF.

        Connection: close is not decoration -- see the module docstring. `event: done` is
        checked first so a keep-alive server that never closes still terminates the read.
        """
        s = socket.create_connection((self.host, self.port), timeout=15)
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                   % (path, self.host)).encode())
        s.settimeout(timeout or self.timeout)
        buf = b""
        started = time.time()
        try:
            while time.time() - started < (timeout or self.timeout):
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
                if b"event: done" in buf:
                    break
        except socket.timeout:
            pass
        finally:
            s.close()
        return buf.decode("utf-8", "replace")

    @staticmethod
    def _answer(raw):
        """The final answer out of an SSE body.

        `replace` frames carry the settled answer and supersede the streamed deltas; the
        deltas are only a fallback for a turn that streamed and never settled.
        """
        replace, deltas = "", []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("replace"):
                replace = d["replace"]
            elif d.get("delta"):
                deltas.append(d["delta"])
        return replace or "".join(deltas)

    def _new_conversation(self):
        deadline = time.time() + self.retry_busy_s
        while time.time() < deadline:
            raw = self._request("/new", timeout=90)
            if '"busy"' not in raw:
                return True
            time.sleep(15)
        return False

    # -- the contract ------------------------------------------------------------------

    def __call__(self, prompt, workdir):
        if self.fresh_conversation:
            self._new_conversation()
        full = (
            "作業フォルダは %s です。このフォルダの中だけで作業し、"
            "指示されていないファイルは変更しないでください。\n\n%s" % (workdir, prompt)
        )
        started = time.time()
        deadline = time.time() + self.retry_busy_s
        raw = ""
        while time.time() < deadline:
            raw = self._request("/stream?msg=" + urllib.parse.quote(full))
            if '"busy"' not in raw[:200]:
                break
            time.sleep(15)
        reply = self._answer(raw)
        self.transcript.append({
            "prompt": full, "reply": reply,
            "elapsed_s": round(time.time() - started, 1),
            "settled": "event: done" in raw,
        })
        return reply


class in_process:
    """Mark a plain callable as running in THIS process, so paired_evaluate will accept it.

        agent = in_process(lambda prompt, workdir: ...)

    Deliberately explicit: writing `in_process(...)` around a lambda that actually posts to
    another machine is a lie somebody has to type -- and one the attestation would then catch,
    because a remote executor cannot report this process's harness id.
    """

    applies_manifest = True
    execution_target = IN_PROCESS
    covered_fields = IN_PROCESS_FIELDS

    def __init__(self, fn):
        self._fn = fn

    def __call__(self, prompt, workdir):
        return self._fn(prompt, workdir)

    def attest(self, manifest):
        return attest_in_process(manifest)


def bridge_available(host=BRIDGE_HOST, port=BRIDGE_PORT) -> bool:
    """True iff something is listening. Used to SKIP live runs, never to fail them.

    A missing bridge is an environment fact. Scoring it as a capability result would be the
    same mistake the suite refuses everywhere else.
    """
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False
