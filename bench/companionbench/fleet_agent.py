"""The fleet execution target: an arm that really does run under the manifest being tested.

WHY THIS EXISTS, AND WHY IT IS A SUBPROCESS

CompanionBench's only real adapter drove the bridge, and the bridge's turn path reads nothing
from the manifest -- so a live A/B ran the deployed harness twice and reported a p-value about
the difference between two identical programs. It took five review rounds to surface, because
everything around it looked correctly wired.

The manifest's actual consumers are `relay.project_memory.load_notes` (the memory component
and its budget) and `relay.relay_fleet.run_relay_fleet` (the retry and refuter budgets). That
makes the FLEET the execution target the manifest governs, and this module the adapter for it.

A child process, not a thread or an in-process call:

  * `_ManifestArm` exports MCP_HARNESS_MANIFEST, and a child inherits it -- so the manifest
    reaches the code without any protocol of its own;
  * runtime-config caches, Playwright's lifetime and any accumulated global state die with
    the child, so one episode cannot condition the next;
  * the manifest is immutable for the child's lifetime, which is what "the arm ran under
    harness X" has to mean;
  * and the child can ATTEST -- report the harness id it actually loaded -- which is the
    difference between a checked contract and a Boolean promise.

WHAT IT REFUSES, AND WHY EACH REFUSAL EXISTS

Every one of these is a way to produce a number that cannot be attributed to the candidate,
which is the failure this whole module is a correction for:

  * a manifest field the fleet does not read -> both arms are the same program;
  * an explicit argument that would mask a manifest field -- `run_relay_fleet` lets explicit
    arguments win over the manifest, deliberately, so passing one silences the thing under
    test;
  * `max_refute_passes` differing while the refuter is off, where the field is inert;
  * a shared memory directory -- fleet memory is read AND written every run, so one arm
    would prime the next and the comparison becomes a sequence;
  * a harness id from the child that is not the one we sent.

NOT VERIFIED AGAINST A LIVE FLEET -- and the first version of this note UNDERSTATED that.
It said "not verified", which invites the reading "probably works, untested". A reviewer
found it could not have worked at all: the parent put a live Playwright context into a JSON
payload, and a browser handle does not survive json.dumps. The child now attaches to a CDP
endpoint and owns its own context, which is both the only arrangement that can work and the
one that keeps each arm's cookies and session state to itself. A second defect in the same
path read the fleet's result under the wrong key, so even a successful run returned an empty
reply.

Both are fixed and neither is exercised by a browser here. What IS tested: the refusals, the
attestation protocol, the per-arm state isolation, and that an error inside the child arrives
as infrastructure rather than as a wrong answer. What is NOT: a single real episode. Treat
the live path as unproven code, because that is what it is.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from bench.companionbench.agents import FLEET, FLEET_FIELDS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The child prints exactly one line with this prefix, so its stdout can carry the fleet's
#: own logging without the parent having to parse it.
RESULT_PREFIX = "__COMPANIONBENCH_RESULT__ "


class FleetContractError(RuntimeError):
    """Raised when a run would produce a result nobody could attribute to the candidate."""


def _child_source() -> str:
    """The child program. Kept as source rather than a module so the parent controls it.

    It answers two questions: which harness did I load, and what did the fleet produce. The
    attestation is read back through the runtime accessors -- asking the manifest we were
    handed would be a tautology.
    """
    return r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC

mode = sys.argv[2]
active = RC.active_manifest(refresh=True)
attest = {
    "harness_id": M.harness_id(active),
    "execution_target": "relay_fleet/v1",
    "effective": {
        "memory_version": RC.component("memory"),
        "memory_max_items": RC.memory_max_items(),
        "max_retries": RC.max_retries(),
        "max_refute_passes": RC.max_refute_passes(),
    },
}
if mode == "attest":
    print("__COMPANIONBENCH_RESULT__ " + json.dumps({"attest": attest}))
    sys.exit(0)

payload = json.loads(sys.stdin.read() or "{}")
out = {"attest": attest, "reply": "", "error": ""}
try:
    # THE CHILD OPENS ITS OWN BROWSER. The first version took a `context` out of the JSON
    # payload, which cannot work for even one run: a Playwright context is a live handle to
    # another process's objects and does not survive json.dumps. That made the adapter
    # unrunnable rather than merely unverified, and the docstring's "not verified against a
    # live fleet" hid it. Owning the browser is also the correct arrangement -- a per-arm
    # context is what stops one arm's cookies and session state reaching the other.
    from playwright.sync_api import sync_playwright
    from relay.relay_fleet import run_relay_fleet
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(payload["cdp_url"])
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        res = run_relay_fleet(
            context, [payload["goal"]], payload["agent_url"],
            max_concurrent=1, refuter=payload.get("refuter", False),
            # max_transient / max_refute are LEFT UNSET on purpose: run_relay_fleet takes
            # them from the active manifest when they are None, and passing them here would
            # silence the very fields under test.
        )
    # `response` was a guess and it was wrong; the fleet reports the final text as
    # last_response, so every successful run also returned an empty reply.
    first = (res or [{}])[0] if isinstance(res, list) else {}
    out["reply"] = (first.get("last_response") or "") if isinstance(first, dict) else str(res)
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print("__COMPANIONBENCH_RESULT__ " + json.dumps(out, ensure_ascii=False))
'''


class FleetAgent:
    """Runs one episode through the real fleet, in a child that carries the manifest.

    `memory_seed` is a directory whose contents are copied into each arm's private `.fleet`
    before the run. It is not optional in spirit: a memory experiment against an empty store
    exercises nothing, and a memory experiment against a SHARED store lets the first arm
    write what the second one reads.
    """

    applies_manifest = True
    execution_target = FLEET
    covered_fields = FLEET_FIELDS

    def __init__(self, *, agent_url, cdp_url=None, refuter=False, memory_seed=None,
                 python=None, timeout_s=1800):
        self.agent_url = agent_url
        # A CDP endpoint, not a live context: the child attaches its own. See the child
        # source for why a context cannot be handed across the boundary.
        self.cdp_url = cdp_url
        self.refuter = bool(refuter)
        self.memory_seed = memory_seed
        self.python = python or sys.executable
        self.timeout_s = timeout_s
        self.runs = []
        self._expected_harness_id = ""

    # -- the contract ------------------------------------------------------------------

    def check_genome(self, base_manifest, candidate_manifest):
        """Refuse a comparison the fleet cannot actually make. Raises FleetContractError.

        Called by an operator before a campaign; `paired_evaluate` performs the same
        covered-fields check itself, so this is the earlier and louder of the two.
        """
        from relay.selfimprove import manifest as M

        changed = set(M.diff(base_manifest, candidate_manifest))
        uncovered = sorted(changed - set(self.covered_fields))
        if uncovered:
            raise FleetContractError(
                "the fleet does not read %s; an arm differing only there is the same "
                "program twice" % ", ".join(uncovered))
        if "parameters.max_refute_passes" in changed and not self.refuter:
            raise FleetContractError(
                "max_refute_passes differs but the refuter is off, so the field is inert -- "
                "the two arms would behave identically and the result would be noise")
        if not self.memory_seed and any(f.startswith("components.memory")
                                        or f == "parameters.memory_max_items"
                                        for f in changed):
            raise FleetContractError(
                "a memory experiment needs a memory_seed: against an empty store the "
                "component is never exercised, and against a shared one the first arm "
                "primes the second")

    def describe(self):
        """The configuration that changes a result. No credentials, no URLs with tokens.

        The memory seed is recorded by DIGEST rather than by path: which seed was used is the
        reproducibility question, and the path is a temp directory that will not exist later.
        """
        seed_id = ""
        if self.memory_seed and os.path.isdir(self.memory_seed):
            h = hashlib.sha256()
            for root_dir, _dirs, files in sorted(os.walk(self.memory_seed)):
                for name in sorted(files):
                    full = os.path.join(root_dir, name)
                    h.update(os.path.relpath(full, self.memory_seed).encode("utf-8"))
                    try:
                        with open(full, "rb") as fh:
                            h.update(fh.read())
                    except OSError:
                        pass
            seed_id = h.hexdigest()[:16]
        return {
            "class": "FleetAgent",
            "execution_target": self.execution_target,
            "refuter": self.refuter,
            "memory_seed_digest": seed_id,
            "timeout_s": self.timeout_s,
            "python": os.path.basename(self.python),
            "has_cdp_url": bool(self.cdp_url),
        }

    def attest(self, manifest):
        """Ask a child which harness it loads, and hand back its answer verbatim.

        The answer is also remembered, so every subsequent run in this arm can be checked
        against it rather than trusting that nothing moved in between.
        """
        from relay.selfimprove import manifest as M
        out = self._run_child("attest", None)
        got = out.get("attest") or {}
        self._expected_harness_id = M.harness_id(manifest)
        return got

    # -- the episode contract ------------------------------------------------------------

    def __call__(self, prompt, workdir):
        payload = {"goal": prompt, "agent_url": self.agent_url, "cdp_url": self.cdp_url,
                   "refuter": self.refuter}
        out = self._run_child("run", payload, workdir=workdir)
        self.runs.append({"workdir": workdir, "attest": out.get("attest"),
                          "error": out.get("error")})
        # AN ERROR IN THE CHILD IS INFRASTRUCTURE, NOT A WRONG ANSWER. The error field was
        # collected and then dropped, so a browser that would not start arrived at the grader
        # as an empty reply -- scored as a task the candidate failed. Raising lets the runner
        # classify it, which is the whole point of its infra/agent distinction.
        if out.get("error"):
            raise FleetContractError("the fleet child failed: %s" % out["error"])
        # The per-run attestation is checked here, not only in the preflight: a manifest that
        # was right when we asked and wrong when we ran is exactly the case worth catching.
        got = (out.get("attest") or {}).get("harness_id")
        if self._expected_harness_id and got != self._expected_harness_id:
            raise FleetContractError(
                "the child ran under harness %s but %s was active when the arm began"
                % (str(got)[:12], self._expected_harness_id[:12]))
        return out.get("reply", "")

    # -- internals -----------------------------------------------------------------------

    def _arm_state_dir(self):
        """A private .fleet for this run, seeded identically for both arms.

        Fleet memory is read at the start of a run and written at the end, so a shared
        directory turns a paired comparison into a sequence: whatever the baseline learned is
        what the candidate starts from.
        """
        d = tempfile.mkdtemp(prefix="cb_fleet_")
        state = os.path.join(d, ".fleet")
        if self.memory_seed and os.path.isdir(self.memory_seed):
            shutil.copytree(self.memory_seed, state)
        else:
            os.makedirs(state, exist_ok=True)
        return d, state

    def _run_child(self, mode, payload, workdir=None):
        cwd, state = self._arm_state_dir()
        env = dict(os.environ)          # MCP_HARNESS_MANIFEST is inherited from _ManifestArm
        # THE SEEDED MEMORY WAS BEING BUILT AND THEN IGNORED. project_memory resolves `.fleet`
        # relative to the CURRENT DIRECTORY, and the child was run in the episode's workdir --
        # so the carefully isolated, identically-seeded store sat unused in a temp folder
        # while both arms shared whatever `.fleet` the workdir happened to have. The
        # isolation this class advertises did not exist. FLEET_STATE_DIR carries it
        # explicitly; the cwd carries it for anything that still resolves relatively.
        env["FLEET_STATE_DIR"] = state
        try:
            proc = subprocess.run(
                [self.python, "-c", _child_source(), REPO, mode],
                input=json.dumps(payload or {}), capture_output=True, text=True,
                cwd=cwd, env=env, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise FleetContractError("the %s child exceeded %ss" % (mode, self.timeout_s))
        for line in (proc.stdout or "").splitlines():
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):])
        raise FleetContractError(
            "the %s child produced no result line (rc=%s); stderr: %s"
            % (mode, proc.returncode, (proc.stderr or "")[-400:]))
