# -*- coding: utf-8 -*-
"""What "done" means for one task, written down before the task starts, by someone else.

WHY IT HAS TO BE WRITTEN FIRST, AND BY THE CONTROLLER. Today the only thing standing between a
worker and a DONE is the worker's own judgement of whether it finished. Measured: precision
0.718 -- 11 of 39 claims wrong on a 40-instance slice. A worker that picks its own acceptance
test after the fact will pick one it passes; that is not dishonesty, it is what "check whether
you are finished" means when the checker and the checked are the same process.

So the acceptance commands are recorded AT ADMISSION, before the worker's first turn, and
hashed. After that:

  * the worker cannot choose a different test, because the contract already names one
  * a test run BEFORE the final edit cannot qualify, because the contract records when it was
    created and the verifier records when it ran
  * "there was no test for this" becomes visible at admission rather than at the end, where it
    reads as "nothing to check" and passes

WHAT THIS FILE DOES NOT DO. It does not run anything and it does not decide anything. It writes
a contract and reads it back. The verifier that executes the commands and promotes
CANDIDATE_DONE to DONE is a separate step, deliberately: a component that both defines the test
and judges the result is the same collapse this file exists to prevent, one level up.

A TASK WITH NO ACCEPTANCE COMMANDS IS NOT A FAILURE. Plenty of real work -- a summary, an
investigation, a question -- has no mechanical oracle. The contract says so explicitly
(`checks: []`, `verifiable: false`), because "no contract" and "a contract that says this cannot
be checked mechanically" must not look alike. The first is an omission; the second is a fact
about the task.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(_REPO, ".fleet", "acceptance_contracts.jsonl")

SCHEMA_VERSION = 1


def _canonical(payload: dict) -> str:
    """The bytes the hash is taken over. Sorted keys and no incidental whitespace, so the same
    contract hashes the same on any machine and a later reader can recompute it."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contract_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:32]


def build(task: str, goal: str = "", checks=None, cwd: str = "",
          allowed_paths=None, forbidden_paths=None, ts: float = None) -> dict:
    """Assemble a contract. Pure -- no I/O, so the shape can be tested without a filesystem.

    `checks` is a list of {"id", "command", "expect"} written by the CONTROLLER. `expect` is
    deliberately small: "exit_zero" is the only mechanically checkable expectation that does
    not need a parser, and a richer language here would be a second place for the definition of
    success to live.
    """
    rows = []
    for i, c in enumerate(checks or []):
        if isinstance(c, str):
            c = {"command": c}
        if not isinstance(c, dict) or not str(c.get("command") or "").strip():
            continue
        rows.append({
            "id": str(c.get("id") or "c%d" % (i + 1)),
            "command": str(c["command"]).strip(),
            "expect": str(c.get("expect") or "exit_zero"),
        })
    payload = {
        "schema": SCHEMA_VERSION,
        "task": str(task or ""),
        "goal_sha16": hashlib.sha256((goal or "").encode("utf-8")).hexdigest()[:16],
        "cwd": str(cwd or ""),
        "checks": rows,
        # STATED, NOT INFERRED FROM AN EMPTY LIST. "nobody wrote checks" and "this task has no
        # mechanical oracle" are different facts, and only one of them is a problem.
        "verifiable": bool(rows),
        "created_ts": float(ts if ts is not None else time.time()),
    }
    payload["hash"] = contract_hash({k: v for k, v in payload.items() if k != "hash"})
    return payload


def record(contract: dict, path: str = None) -> bool:
    """Append a contract. Never raises; returns whether it landed.

    Append-only, and never updated in place: a contract that could be rewritten after the work
    started is not a contract. A second contract for the same task is written as a new line and
    `load` returns the FIRST, so a later write cannot quietly replace the terms.
    """
    try:
        target = path or CONTRACT_PATH
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(contract, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load(task: str, path: str = None):
    """The contract for a task, or None. THE FIRST ONE WINS -- see `record`."""
    try:
        with open(path or CONTRACT_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("task") == task:
                    return row
    except OSError:
        return None
    return None


def intact(contract: dict) -> bool:
    """Whether a contract still hashes to what it claims.

    The hash is not a security boundary -- anyone who can edit the file can recompute it. What
    it catches is the accident: a contract edited by hand, a partially-written line, a
    schema-changing refactor that quietly altered the terms of tasks already in flight.
    """
    if not isinstance(contract, dict) or not contract.get("hash"):
        return False
    body = {k: v for k, v in contract.items() if k != "hash"}
    return contract_hash(body) == contract["hash"]


def ensure(task: str, goal: str = "", checks=None, cwd: str = "", path: str = None,
           ts: float = None) -> dict:
    """The admission-time call: return the existing contract, or write one and return it.

    Idempotent on purpose. Admission can be retried -- a re-queued goal, a resumed run -- and a
    retry must not change what the task was accepted under.
    """
    existing = load(task, path)
    if existing is not None:
        return existing
    contract = build(task, goal=goal, checks=checks, cwd=cwd, ts=ts)
    record(contract, path)
    return contract


def missing_contract_tasks(tasks, path: str = None) -> list:
    """Tasks admitted with no contract at all. The observable for step 2 being done."""
    return [t for t in (tasks or []) if load(t, path) is None]
