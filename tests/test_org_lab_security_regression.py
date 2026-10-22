# -*- coding: utf-8 -*-
"""Lab security regression suite (slice F5)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_security_regression as S  # noqa: E402
from relay.selfimprove import frozen as F  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_security_regression.py")


def everything_exists(_rel):
    return True


def test_guard_table_is_well_formed():
    ids = S.guard_ids()
    assert len(ids) == len(set(ids))
    for gid, files, tests in S.GUARDS:
        assert S._GUARD_ID_RE.match(gid)
        assert files
        assert len(files) == len(set(files)) and len(tests) == len(set(tests))
        assert all(t.endswith(".py") and "/" in t and "\\" not in t for t in tests)


def test_every_frozen_file_has_a_guard_and_no_guard_names_an_unfrozen_file():
    cov = S.coverage_problems(F.FROZEN_MANIFEST)
    assert cov == {"uncovered": [], "not_frozen": []}


def test_every_registered_test_file_exists_in_this_repository():
    missing = [t for _g, _f, tests in S.GUARDS for t in tests
               if not os.path.isfile(os.path.join(REPO, *t.split("/")))]
    assert missing == []


def test_coverage_problems_both_directions():
    cov = S.coverage_problems(["tools/security.py", "brand/new_frozen.py"])
    assert "brand/new_frozen.py" in cov["uncovered"]
    assert "relay/selfimprove/guards.py" in cov["not_frozen"]
    assert "tools/security.py" not in cov["uncovered"] + cov["not_frozen"]


def test_manifest_from_source_reads_without_executing():
    src = "import os\nboom = 1/0\nFROZEN_MANIFEST = ['a.py', 'b/c.py']\n"
    assert S.manifest_from_source(src) == ["a.py", "b/c.py"]
    assert S.manifest_from_source("FROZEN_MANIFEST: list = ('x.py',)\n") == ["x.py"]
    # the real module's manifest parses to the same list as importing it
    with open(os.path.join(REPO, "relay", "selfimprove", "frozen.py"), encoding="utf-8") as fh:
        assert S.manifest_from_source(fh.read()) == list(F.FROZEN_MANIFEST)


@pytest.mark.parametrize("src", ["x = 1\n", "FROZEN_MANIFEST = [1, 2]\n",
                                 "FROZEN_MANIFEST = [f()]\n", "def (:\n"])
def test_manifest_from_source_rejects_bad_input(src):
    with pytest.raises(S.LabSecurityError):
        S.manifest_from_source(src)
    with pytest.raises(S.LabSecurityError):
        S.manifest_from_source(None)


def test_guard_plan_reports_missing_test_files_and_filters():
    plan = S.guard_plan(lambda rel: rel != "tools/test_folder_policy.py")
    by = {p["id"]: p for p in plan}
    assert by["folder_policy"]["missing"] == ["tools/test_folder_policy.py"]
    assert by["enforcing_guards"]["missing"] == []
    only = S.guard_plan(everything_exists, only=["folder_policy"])
    assert [p["id"] for p in only] == ["folder_policy"]
    with pytest.raises(S.LabSecurityError):
        S.guard_plan(everything_exists, only=["nope"])


def test_build_command():
    cmd = S.build_command("py", ["a/test_x.py", "b/test_y.py"])
    assert cmd[:3] == ["py", "-m", "pytest"] and cmd[-2:] == ["a/test_x.py", "b/test_y.py"]
    assert "no:cacheprovider" in cmd
    with pytest.raises(S.LabSecurityError):
        S.build_command("py", [])
    with pytest.raises(S.LabSecurityError):
        S.build_command("", ["t.py"])


def ok_result():
    return {"returncode": 0, "timed_out": False, "tail": "1 passed"}


def test_summarize_all_pass():
    plan = S.guard_plan(everything_exists, only=["enforcing_guards", "folder_policy"])
    rep = S.summarize(plan, {"enforcing_guards": ok_result(), "folder_policy": ok_result()})
    assert rep["ok"] is True and rep["counts"] == {"pass": 2}


def test_one_failure_fails_the_run():
    plan = S.guard_plan(everything_exists, only=["enforcing_guards", "folder_policy"])
    bad = {"returncode": 1, "timed_out": False, "tail": "1 failed"}
    rep = S.summarize(plan, {"enforcing_guards": ok_result(), "folder_policy": bad})
    assert rep["ok"] is False
    assert {g["id"]: g["status"] for g in rep["guards"]}["folder_policy"] == "fail"
    assert rep["guards"][1]["tail"] == "1 failed"


def test_timeout_is_not_a_pass():
    plan = S.guard_plan(everything_exists, only=["folder_policy"])
    rep = S.summarize(plan, {"folder_policy": {"returncode": 0, "timed_out": True, "tail": ""}})
    assert rep["ok"] is False and rep["guards"][0]["status"] == "timeout"


def test_deleted_test_file_fails_even_if_other_guards_pass():
    plan = S.guard_plan(lambda rel: rel != "tools/test_folder_policy.py",
                        only=["enforcing_guards", "folder_policy"])
    rep = S.summarize(plan, {"enforcing_guards": ok_result()})
    assert rep["ok"] is False
    assert {g["id"]: g["status"] for g in rep["guards"]}["folder_policy"] == "tests_missing"


def test_no_test_guard_is_reported_but_does_not_fail_and_nothing_run_is_not_ok():
    plan = S.guard_plan(everything_exists, only=["batch_grader", "enforcing_guards"])
    rep = S.summarize(plan, {"enforcing_guards": ok_result()})
    assert {g["id"]: g["status"] for g in rep["guards"]}["batch_grader"] == "no_test"
    assert rep["ok"] is True
    empty = S.summarize(S.guard_plan(everything_exists, only=["batch_grader"]), {})
    assert empty["ok"] is False        # nothing passed, so nothing was shown


def test_not_run_guard_does_not_count_as_pass():
    plan = S.guard_plan(everything_exists, only=["folder_policy"])
    rep = S.summarize(plan, {})
    assert rep["guards"][0]["status"] == "not_run" and rep["ok"] is False


def test_dropping_a_file_from_the_candidate_manifest_fails_the_run():
    plan = S.guard_plan(everything_exists, only=["enforcing_guards"])
    cov = S.coverage_problems([f for f in F.FROZEN_MANIFEST if f != "relay/selfimprove/guards.py"])
    rep = S.summarize(plan, {"enforcing_guards": ok_result()}, coverage=cov)
    assert rep["ok"] is False
    assert rep["coverage"]["not_frozen"] == ["relay/selfimprove/guards.py"]
    clean = S.summarize(plan, {"enforcing_guards": ok_result()},
                        coverage=S.coverage_problems(F.FROZEN_MANIFEST))
    assert clean["ok"] is True


def test_summarize_rejects_bad_shapes():
    plan = S.guard_plan(everything_exists, only=["folder_policy"])
    with pytest.raises(S.LabSecurityError):
        S.summarize(plan, [])
    with pytest.raises(S.LabSecurityError):
        S.summarize(plan, {"folder_policy": {"returncode": "0"}})
    with pytest.raises(S.LabSecurityError):
        S.summarize(plan, {"folder_policy": 3})


def test_report_is_stable_json():
    plan = S.guard_plan(everything_exists, only=["folder_policy"])
    rep = S.summarize(plan, {"folder_policy": ok_result()})
    assert S.to_json(rep) == S.to_json(S.summarize(plan, {"folder_policy": ok_result()}))


# ---- CLI (real child process, tiny fake candidate in a temp dir) ----

def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def make_candidate(root, manifest, guards_test="def test_ok():\n    assert True\n"):
    write(os.path.join(root, "relay", "selfimprove", "frozen.py"),
          "FROZEN_MANIFEST = %r\n" % (manifest,))
    if guards_test is not None:
        write(os.path.join(root, "relay", "selfimprove", "test_guards.py"), guards_test)


def cli(*args):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, timeout=300)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def test_cli_passes_on_a_clean_candidate(tmp_path):
    cand = str(tmp_path / "cand")
    make_candidate(cand, list(F.FROZEN_MANIFEST))
    rc, out, err = cli("--candidate", cand, "--production-root", REPO,
                       "--guard", "enforcing_guards", "--timeout", "120")
    assert rc == 0, out + err
    assert "enforcing_guards" in out and "pass" in out


def test_cli_fails_when_the_candidate_guard_test_fails(tmp_path):
    cand = str(tmp_path / "cand")
    make_candidate(cand, list(F.FROZEN_MANIFEST), "def test_bad():\n    assert False\n")
    rc, out, _ = cli("--candidate", cand, "--production-root", REPO,
                     "--guard", "enforcing_guards", "--format", "json", "--timeout", "120")
    assert rc == 1
    assert '"status": "fail"' in out


def test_cli_fails_when_the_guard_test_was_deleted(tmp_path):
    cand = str(tmp_path / "cand")
    make_candidate(cand, list(F.FROZEN_MANIFEST), guards_test=None)
    rc, out, _ = cli("--candidate", cand, "--production-root", REPO,
                     "--guard", "enforcing_guards")
    assert rc == 1 and "tests_missing" in out


def test_cli_refuses_the_production_tree_and_bad_input(tmp_path):
    rc, _, err = cli("--candidate", REPO, "--production-root", REPO, "--guard", "folder_policy")
    assert rc == 2 and "same_as_production_root" in err
    rc, _, err = cli("--candidate", os.path.join(REPO, "relay"), "--production-root", REPO)
    assert rc == 2 and "inside_production_root" in err
    rc, _, err = cli("--candidate", str(tmp_path / "missing"), "--production-root", REPO)
    assert rc == 2 and "not a directory" in err
    cand = str(tmp_path / "cand")
    make_candidate(cand, list(F.FROZEN_MANIFEST))
    rc, _, err = cli("--candidate", cand, "--production-root", REPO, "--guard", "nope")
    assert rc == 2 and "unknown guard" in err
    rc, _, err = cli("--candidate", cand)
    assert rc == 2


def test_cli_list_prints_the_table():
    rc, out, _ = cli("--list")
    assert rc == 0 and "permission_gate" in out and "companionbench_judge" in out


def test_module_is_pure_no_process_no_file_access():
    path = os.path.join(REPO, "relay", "org", "lab_security_regression.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("import subprocess", "import os\n", "open(", "os.system", "time.time", "datetime"):
        assert banned not in src, banned
