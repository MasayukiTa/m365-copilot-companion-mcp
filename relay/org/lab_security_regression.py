# -*- coding: utf-8 -*-
"""Security regression suite for Evolution Lab candidates (slice F5).

WHAT DECISION THIS SERVES. Whether a candidate (a lab copy of the repository) still honours every
frozen guard before anyone looks at its benchmark score. The frozen set (FROZEN_MANIFEST in
relay/selfimprove/frozen.py) names the files that define success or enforce honesty: the permission
gate, the distrust of external content, the folder policy, the graders, the evolution boundary and
the manifest itself. A candidate that edits one of them can look better exactly because a check was
softened. Each guard here pairs frozen files with the existing tests that protect them; the tests
are run against the CANDIDATE's own copy of the code, so a softened guard fails its own test.
Nothing here decides promotion (that stays with the frozen promotion machinery).

WHAT IT DOES. This module is pure: it reads no file, writes no file, starts no process and has no
clock. It provides
  guard_plan(exists)              which test files of each guard exist in the candidate;
  coverage_problems(manifest)     frozen files that no guard names, and guard files that are not
                                  frozen (so the table cannot silently fall behind the manifest);
  manifest_from_source(text)      FROZEN_MANIFEST read from source text with ast.literal_eval (the
                                  candidate's code is parsed, never imported or executed here);
  build_command(python, tests)    the pytest argument list for one guard;
  summarize(plan, results)        per-guard status and an overall verdict from run results.
The subprocess part lives only in scripts/org_lab_security_regression.py, run by hand, with a
timeout, with the candidate as working directory. Default off: no runtime module imports this one.

STATUSES (per guard; each is a stable code).
  pass          every registered test file exists and pytest exited 0.
  fail          pytest exited non-zero (including "no tests collected").
  timeout       pytest did not finish in time. Reason: a hung guard test must not read as a pass.
  tests_missing a registered test file is absent in the candidate. Reason: deleting the test is
                the cheapest way to defeat a guard, so absence is a failure, not a skip.
  no_test       the guard has no dedicated test registered. Reported, but it does not fail the run:
                it is a gap in the table, not a candidate fault.
  not_run       a result for the guard was not supplied (for example it was filtered out).
The overall verdict is ok only if no guard is fail, timeout or tests_missing and at least one guard
passed. A bad `results` shape raises LabSecurityError.
"""
from __future__ import annotations

import ast
import json
import re

VERSION = 1
PASS, FAIL, TIMEOUT = "pass", "fail", "timeout"
TESTS_MISSING, NO_TEST, NOT_RUN = "tests_missing", "no_test", "not_run"
FAILING = (FAIL, TIMEOUT, TESTS_MISSING)

_COMPANION_TESTS = (
    "bench/companionbench/test_companionbench.py",
    "bench/companionbench/test_runner.py",
    "bench/companionbench/test_sealed.py",
    "bench/companionbench/test_runtime_episodes.py",
    "bench/companionbench/test_office_episodes.py",
    "bench/test_a_benchmark_episode_is_not_split.py",
)

# id -> (frozen files protected, existing test files protecting them). Paths are repo-relative with
# forward slashes. Only tests that already exist in the repository are listed.
GUARDS = (
    ("frozen_manifest",
     ("relay/selfimprove/frozen.py",),
     ("relay/selfimprove/test_frozen.py",
      "relay/selfimprove/test_frozen_surfaces.py",
      "relay/selfimprove/test_frozen_queue_refused.py")),
    ("enforcing_guards",
     ("relay/selfimprove/guards.py",),
     ("relay/selfimprove/test_guards.py",)),
    ("permission_gate",
     ("tools/security.py", "docs/SECURITY.md"),
     ("tools/test_security.py",
      "tools/test_layer1_security.py",
      "tests/test_unlock_token.py",
      "tests/test_unlock_oracle.py",
      "tests/test_security_xff.py",
      "tests/test_security_state_is_not_readable.py")),
    ("untrusted_content",
     ("tools/_untrusted.py",),
     ("tools/test_untrusted_coverage.py",)),
    ("folder_policy",
     ("tools/folder_policy.py",),
     ("tools/test_folder_policy.py",)),
    ("evolution_boundary",
     ("relay/selfimprove/manifest.py", "relay/selfimprove/decision.py"),
     ("relay/selfimprove/test_controller.py",
      "relay/selfimprove/test_measurement_integrity.py")),
    ("routing_and_tree",
     ("relay/selfimprove/routing.py", "relay/selfimprove/harness_tree.py"),
     ("relay/selfimprove/test_routing.py",
      "relay/selfimprove/test_harness_tree.py")),
    ("authority_ledger",
     ("relay/selfimprove/authority_ledger.py",),
     ("relay/selfimprove/test_authority_ledger.py",
      "relay/selfimprove/test_the_chain_is_walked_before_it_is_extended.py")),
    ("route_judge_and_breaker",
     ("relay/selfimprove/route_evaluator.py", "relay/socket_route.py"),
     ("relay/selfimprove/test_route_evaluator.py",
      "relay/selfimprove/test_fallback_rule.py",
      "relay/test_socket_route.py")),
    ("swebench_grader",
     ("bench/swe_grade_swebench.py",),
     ("bench/test_swe_grade_launch_verification.py",)),
    ("batch_grader",
     ("bench/evalhost_batch_grade.py",),
     ()),
    ("companionbench_judge",
     ("bench/companionbench/episode.py", "bench/companionbench/pools.py",
      "bench/companionbench/runner.py", "bench/companionbench/episodes/core.py",
      "bench/companionbench/episodes/office.py", "bench/companionbench/episodes/runtime.py",
      "bench/companionbench/episodes/sealed.py"),
     _COMPANION_TESTS),
)

_GUARD_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LabSecurityError(ValueError):
    """An input is not the shape this module expects."""


def guard_ids(guards=GUARDS):
    return [g[0] for g in guards]


def manifest_from_source(text):
    """FROZEN_MANIFEST (a list of path strings) read from Python source text, never executed."""
    if not isinstance(text, str):
        raise LabSecurityError("source must be text")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise LabSecurityError("frozen source does not parse: %s" % exc)
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "FROZEN_MANIFEST" in targets and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                raise LabSecurityError("FROZEN_MANIFEST is not a literal list of strings")
            if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
                raise LabSecurityError("FROZEN_MANIFEST must be a list of path strings")
            return list(value)
    raise LabSecurityError("FROZEN_MANIFEST not found in the frozen source")


def coverage_problems(manifest, guards=GUARDS):
    """{"uncovered": [frozen files no guard names], "not_frozen": [guard files not in manifest]}."""
    manifest = list(manifest)
    named = set()
    for _gid, files, _tests in guards:
        named.update(files)
    return {"uncovered": sorted(set(manifest) - named),
            "not_frozen": sorted(named - set(manifest))}


def guard_plan(exists, guards=GUARDS, only=None):
    """Plan per guard: [{"id", "files", "tests", "missing"}].

    `exists(rel_path) -> bool` says whether a test file is present in the candidate; the caller
    decides how (the pure part never touches the disk). `only` is an optional collection of guard
    ids; an id that is not a guard raises LabSecurityError.
    """
    ids = guard_ids(guards)
    if only is not None:
        unknown = sorted(set(only) - set(ids))
        if unknown:
            raise LabSecurityError("unknown guard id(s): %s" % ", ".join(unknown))
    plan = []
    for gid, files, tests in guards:
        if only is not None and gid not in only:
            continue
        missing = [t for t in tests if not exists(t)]
        plan.append({"id": gid, "files": list(files), "tests": list(tests), "missing": missing})
    return plan


def build_command(python, tests):
    """pytest argument list for one guard. -p no:cacheprovider keeps the run from writing a cache."""
    if not isinstance(python, str) or not python:
        raise LabSecurityError("python must be a non-empty path string")
    if not tests:
        raise LabSecurityError("a guard with no tests has nothing to run")
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + list(tests)


def _status(entry, result):
    if entry["missing"]:
        return TESTS_MISSING
    if not entry["tests"]:
        return NO_TEST
    if result is None:
        return NOT_RUN
    if not isinstance(result, dict):
        raise LabSecurityError("result for %r must be an object" % entry["id"])
    if result.get("timed_out"):
        return TIMEOUT
    rc = result.get("returncode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        raise LabSecurityError("result for %r needs an integer returncode" % entry["id"])
    return PASS if rc == 0 else FAIL


def summarize(plan, results, coverage=None):
    """Report: {"version", "ok", "guards": [{id, status, files, tests, missing, tail}], "counts"}.

    `results` maps guard id -> {"returncode": int, "timed_out": bool, "tail": text}. The verdict is
    ok only if no guard failed, timed out or lost a test file, and at least one guard passed.
    `coverage` (from coverage_problems on the candidate's manifest), when given, is added to the
    report; a guard file the candidate no longer lists as frozen ("not_frozen") makes the verdict
    not ok, because dropping a file from the frozen set is how a guard is switched off. Frozen
    files with no guard ("uncovered") are reported only: that is a gap in the table.
    """
    if not isinstance(results, dict):
        raise LabSecurityError("results must be an object")
    out, counts = [], {}
    for entry in plan:
        res = results.get(entry["id"])
        status = _status(entry, res)
        counts[status] = counts.get(status, 0) + 1
        tail = ""
        if isinstance(res, dict) and isinstance(res.get("tail"), str):
            tail = res["tail"]
        out.append({"id": entry["id"], "status": status, "files": entry["files"],
                    "tests": entry["tests"], "missing": entry["missing"], "tail": tail})
    ok = counts.get(PASS, 0) > 0 and not any(counts.get(s, 0) for s in FAILING)
    report = {"version": VERSION, "ok": ok, "guards": out, "counts": counts}
    if coverage is not None:
        report["coverage"] = coverage
        if coverage.get("not_frozen"):
            report["ok"] = False
    return report


def to_json(obj):
    """Stable JSON text for a report."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
