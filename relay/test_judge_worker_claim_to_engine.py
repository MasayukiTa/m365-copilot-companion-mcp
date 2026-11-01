# -*- coding: utf-8 -*-
"""Tests: a worker's registered claim reaches the validity engine, and the engine-unavailable
messages name what to fix (feat/judge-worker-claim-to-engine).

WHAT THIS PINS. relay/judge_gate.run_engine_judge used to look for the claim ONLY at
ENGINE_ROOT/configs/<id>.json. The harness register_and_lock writes the worker's registered
claim to <root>/registry/<id>.json, which the engine never read, so every worker claim ended as
"registered claim not found". The engine CLI accepts an arbitrary --claim path, so when there is
no human-pre-registered configs/<id>.json we stage the worker's registered claim OUTSIDE the
engine root and pass its absolute path as --claim. A human-pre-registered configs claim still
wins. The engine root is never written to.

The real verdict engine is secret and outside the repo, so these tests use a FAKE engine root
(temp dirs) whose `python -m validity` is a tiny script that echoes a JSON verdict and records
the --claim path it was given. No secret data, measurement row or employee id appears here --
all fixtures are synthetic.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

jg = pytest.importorskip(
    "relay.judge_gate",
    reason="final-step judge harness relay/judge_gate.py not implemented yet",
)
from relay.judge_gate import run_engine_judge


def _fake_engine_root(tmp_path, *, with_configs_id=None, with_lock=False,
                      verdict=None, require_lock=False):
    """Build a fake engine checkout whose `python -m validity` is a tiny script.

    The fake CLI parses --claim/--root/--data/--lock, writes the claim path and args it received
    to <root>/../_received.json (outside the engine root so it does not perturb the engine-root
    snapshot), and prints a JSON verdict on stdout. When require_lock is True it
    prints an engine-side 'claim not in lock' error to stderr and exits non-zero WITHOUT a
    verdict, so run_engine_judge surfaces that as a clear not-run reason (nothing faked).
    """
    root = tmp_path / "engine"
    (root / "validity").mkdir(parents=True)
    if with_configs_id:
        (root / "configs").mkdir()
        (root / "configs" / ("%s.json" % with_configs_id)).write_text(
            json.dumps({"id": with_configs_id, "claim_type": "between_lot",
                        "dataset_id": "synthetic", "source": "human-configs"}),
            encoding="utf-8")
    if with_lock:
        (root / "lock.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    verdict = verdict or {"id": "SYN1", "verdict": "HOLD", "verdict_ja": "\u4fdd\u7559",
                          "finding": None, "claim_supported": None,
                          "reasons": ["D_FAIL_NO_LOCK"], "notes": [], "evidence": {"K": 3},
                          "hashes": {"input_sha256": "0" * 64}}
    script = (
        "import json, os, sys\n"
        "a = sys.argv\n"
        "def opt(name):\n"
        "    return a[a.index(name) + 1] if name in a else None\n"
        "claim = opt('--claim'); root = opt('--root'); data = opt('--data'); lock = opt('--lock')\n"
        # Write the receipt OUTSIDE the engine root (one level above it) so the fake CLI's own
        # bookkeeping does not perturb the engine-root snapshot the test asserts is unchanged.
        "rp = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '_received.json')\n"
        "open(rp, 'w', encoding='utf-8').write(json.dumps({'claim': claim, 'root': root, 'data': data, 'lock': lock, 'cwd': os.getcwd()}))\n"
        "REQUIRE_LOCK = %r\n"
        "if REQUIRE_LOCK:\n"
        "    sys.stderr.write('claim is not covered by lock.json\\n'); sys.exit(3)\n"
        "with open(claim, encoding='utf-8-sig') as f:\n"
        "    c = json.load(f)\n"
        # verdict is embedded as a JSON string and parsed at runtime so Python's None/True
        # survive as JSON null/true (embedding the repr would write a bare `null` token).
        "v = json.loads(%r)\n"
        "v['id'] = c.get('id', v.get('id'))\n"
        "sys.stdout.write(json.dumps(v) + '\\n')\n"
    ) % (bool(require_lock), json.dumps(verdict))
    # `python -m validity` runs validity/__main__.py with cwd=root.
    (root / "validity" / "__main__.py").write_text(script, encoding="utf-8")
    (root / "validity" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _snapshot(root):
    """Map of every file under root -> its bytes, to prove the engine root was not modified.

    __pycache__ is skipped: running `python -m validity` compiles the fake CLI and writes
    .pyc files, which is the interpreter's own bytecode cache, not a change run_engine_judge
    made to the engine's source. We assert the engine's SOURCE is byte-for-byte unchanged.
    """
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


WORKER_CLAIM = {"id": "SYN1", "claim_type": "between_lot", "dataset_id": "synthetic",
                "source": "worker-registered"}


def test_worker_claim_passed_via_temp_claim_path_and_engine_root_unchanged(tmp_path):
    """No human configs/<id>.json, but the request carried the registered claim: it is written
    OUTSIDE the engine root and passed as an absolute --claim path; the engine runs; the engine
    root is byte-for-byte unchanged."""
    root = _fake_engine_root(tmp_path)  # no configs/SYN1.json
    judge_folder = tmp_path / "job"
    judge_folder.mkdir()
    before = _snapshot(root)
    verdict = run_engine_judge("SYN1", root=str(root), python_exe=sys.executable,
                               registered_claim=WORKER_CLAIM, judge_folder=str(judge_folder))
    assert verdict["engine_available"] is True
    assert verdict["verdict"] == "HOLD"
    assert verdict["claim_source"] == "worker-registered via harness registry"
    # The engine root must be unchanged: no configs/ written, every file identical.
    assert not os.path.isdir(os.path.join(str(root), "configs"))
    assert _snapshot(root) == before
    # The claim the engine judged was the temp file under the job folder, not inside the engine.
    received = json.loads((root.parent / "_received.json").read_text(encoding="utf-8"))
    claim_path = received["claim"]
    assert os.path.isabs(claim_path)
    assert os.path.abspath(str(judge_folder)) in os.path.abspath(claim_path)
    assert os.path.commonpath([os.path.abspath(claim_path), os.path.abspath(str(root))]) \
        != os.path.abspath(str(root))
    staged = json.loads(open(claim_path, encoding="utf-8").read())
    assert staged["source"] == "worker-registered"


def test_human_configs_claim_is_preferred_over_worker_claim(tmp_path):
    """When configs/<id>.json exists it WINS: the engine is pointed at configs/<id>.json (relative,
    cwd=root) and the worker claim is not staged."""
    root = _fake_engine_root(tmp_path, with_configs_id="SYN1")
    judge_folder = tmp_path / "job"
    judge_folder.mkdir()
    verdict = run_engine_judge("SYN1", root=str(root), python_exe=sys.executable,
                               registered_claim=WORKER_CLAIM, judge_folder=str(judge_folder))
    assert verdict["engine_available"] is True
    assert verdict["claim_source"] == "engine configs (human pre-registered)"
    received = json.loads((root.parent / "_received.json").read_text(encoding="utf-8"))
    assert received["claim"] == "configs/SYN1.json"  # relative, used from cwd=root
    # No temp claim was staged under the job folder.
    assert not any(name.startswith("engine_claim_") for name in os.listdir(str(judge_folder)))


def test_engine_that_requires_claim_in_lock_gives_a_clear_not_run_reason(tmp_path):
    """If an engine were to reject a claim that is not in its lock, run_engine_judge surfaces the
    engine's failure as a clear 'judge not run' reason (engine produced no JSON verdict) and
    fakes nothing."""
    root = _fake_engine_root(tmp_path, with_lock=True, require_lock=True)
    judge_folder = tmp_path / "job"
    judge_folder.mkdir()
    verdict = run_engine_judge("SYN1", root=str(root), python_exe=sys.executable,
                               registered_claim=WORKER_CLAIM, judge_folder=str(judge_folder))
    assert verdict["engine_available"] is False
    assert "no JSON verdict" in verdict["unavailable_reason"]
    # the engine's own stderr (naming the lock requirement) is carried, not hidden
    assert "lock" in (verdict.get("stderr_tail") or "").lower()
    # nothing was faked: no verdict token invented
    assert "verdict" not in verdict or verdict.get("verdict") is None


def test_root_not_found_message_names_the_env_var_and_value(tmp_path):
    missing = tmp_path / "no_engine_here"
    verdict = run_engine_judge("SYN1", root=str(missing))
    assert verdict["engine_available"] is False
    assert "engine root not found" in verdict["unavailable_reason"]
    assert "VALIDITY_ENGINE_ROOT" in verdict["unavailable_reason"]
    assert str(missing) in verdict["unavailable_reason"]


def test_claim_not_found_message_names_both_paths_and_what_to_do(tmp_path):
    """No configs claim and no worker claim in the request: the reason names the configs path
    tried, the registry path tried, and what to do (pre-register, or include the claim)."""
    root = _fake_engine_root(tmp_path)  # no configs/SYN1.json
    judge_folder = tmp_path / "job"
    judge_folder.mkdir()
    verdict = run_engine_judge("SYN1", root=str(root), judge_folder=str(judge_folder),
                               registered_claim=None)
    assert verdict["engine_available"] is False
    reason = verdict["unavailable_reason"]
    assert "configs" in reason and "SYN1" in reason        # the configs path tried
    assert "registry" in reason                              # the registry path tried
    assert "pre-register" in reason or "include the registered claim" in reason  # what to do


def test_worker_claim_without_a_job_folder_reports_clearly(tmp_path):
    """A worker claim but no per-job folder to stage it in: do not write inside the engine root;
    report that there is nowhere outside the engine root to stage the claim."""
    root = _fake_engine_root(tmp_path)
    verdict = run_engine_judge("SYN1", root=str(root), registered_claim=WORKER_CLAIM,
                               judge_folder=None)
    assert verdict["engine_available"] is False
    assert "judge folder" in verdict["unavailable_reason"] \
        or "outside the engine root" in verdict["unavailable_reason"]
    assert not os.path.isdir(os.path.join(str(root), "configs"))
