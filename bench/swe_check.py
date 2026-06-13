"""SWE-bench acceptance gate for the fleet. Given an instance_id, take the agent's edits in
its worktree (git diff), run the OFFICIAL swebench evaluation in WSL2 Docker, and exit 0 only
if the hidden tests pass (instance resolved). On failure, print actionable feedback (re-injected
to the agent by the relay).

  python bench/swe_check.py <instance_id> [<worktree_path>]
Exit 0 = resolved (DONE accepted). Exit 1 = not resolved / no patch (keep working).
"""
import glob
import json
import os
import re
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"
DISTRO = "MiasmaLab"


def wsl(script, timeout=1000, capture=False):
    # decode as utf-8/replace: WSL test logs contain bytes the Windows cp932 default can't decode,
    # which would otherwise crash the subprocess reader thread and yield empty output.
    return subprocess.run(["wsl.exe", "-d", DISTRO, "sh", "-c", script],
                          capture_output=capture, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def main():
    inst = sys.argv[1]
    wt = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)

    # 1. the agent's patch = git diff in its worktree
    g = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
    diff = g.stdout
    if not diff.strip():
        print("NO_PATCH_YET: you have not edited any files in the repository at %s. "
              "Read the relevant source, fix the bug, then save your edits." % wt)
        return 1

    # 2. write predictions (Windows path; WSL reads it via /mnt/c)
    preds_dir = os.path.join(REPO, ".fleet", "swe", "preds")
    os.makedirs(preds_dir, exist_ok=True)
    predpath = os.path.join(preds_dir, inst + ".json")
    with open(predpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{"instance_id": inst, "model_patch": diff,
                    "model_name_or_path": "companion"}], f)
    predwsl = "/mnt/c/Users/USER/companion-mcp/.fleet/swe/preds/" + inst + ".json"

    # 3. official eval in WSL Docker.
    #    cache_level: 'env' (default) keeps the per-version environment image for fast retries
    #    -- fine because docker lives on the WSL disk (/dev/sdd, ~935 GB free), NOT on the
    #    constrained C: drive. If the WSL disk ever fills, set SWE_CACHE_LEVEL=none (the
    #    bb6a806-validated low-footprint mode: rebuilds the env image each run but leaves no
    #    cached images behind).
    cache_level = os.environ.get("SWE_CACHE_LEVEL", "env")
    run_id = "agent_" + inst.replace("__", "_")
    # CRITICAL: swebench skips an instance whose report already exists for this run_id
    # ("1 instances already run, skipping... No instances to run"), returning the STALE
    # verdict from a prior attempt instead of evaluating the agent's NEW patch. run_id is
    # constant per instance, so once an instance is evaluated once (even in an earlier
    # killed run / prior round), every later verify would re-read the old result and the
    # failure-feedback retry loop could never credit a corrected patch. Clear this
    # instance's prior swebench state BEFORE each run so every verify re-evaluates fresh.
    purge = ("rm -rf logs/run_evaluation/" + run_id + " "
             "companion." + run_id + ".json " + run_id + ".*.json 2>/dev/null; ")
    script = (
        "pgrep dockerd >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 8); "
        "export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt; "
        # Activate the #19 false-negative shim: putting bench/swe_shim on PYTHONPATH makes
        # Python auto-import its sitecustomize at interpreter startup, which monkeypatches
        # make_eval_script_list to export PYTEST_ADDOPTS="-rA ..." in eval.sh. This survives
        # the bare `git checkout {base}` reset (process env, not a tracked file) so sphinx's
        # tox forwards -rA to pytest and parse_log_pytest_v2 sees PASSED lines. Additive
        # (${PYTHONPATH:+...} preserves any existing value) and a no-op for non-pytest repos.
        "export PYTHONPATH=/mnt/c/Users/USER/companion-mcp/bench/swe_shim${PYTHONPATH:+:$PYTHONPATH}; "
        "cd /root/swe; " + purge +
        "/root/swe-venv/bin/python -m swebench.harness.run_evaluation "
        "--dataset_name /root/swe/lite_local.json --predictions_path " + predwsl + " "
        "--instance_ids " + inst + " --run_id " + run_id +
        " --max_workers 1 --cache_level " + cache_level
    )
    try:
        wsl(script, timeout=1200)
    except subprocess.TimeoutExpired:
        # CRITICAL leak fix: the eval container is a DETACHED `docker run`, so killing the
        # wsl.exe subprocess on timeout does NOT stop it -- it keeps running (observed:
        # sympy-11870 container "Up 33 minutes" after its turn was long over), holding RAM
        # and inflating the vhdx on C:. _cleanup_docker force-removes the (still-running)
        # container + image, so the timeout path must call it too (the success/fail paths
        # already do).
        print("EVAL_TIMEOUT: the evaluation took too long. Likely the image pull is slow; "
              "your patch may still be fine. Keep it and it will be re-checked.")
        _cleanup_docker(inst, run_id)
        return 1

    # 4. read the report (swebench writes companion.<run_id>.json into /root/swe)
    r = wsl("cat /root/swe/companion." + run_id + ".json 2>/dev/null", timeout=60, capture=True)
    out = r.stdout if r.stdout and r.stdout.strip() else ""
    if not out:
        r2 = wsl("ls /root/swe/*" + run_id + "*.json 2>/dev/null | head -1 | xargs cat 2>/dev/null",
                 timeout=60, capture=True)
        out = r2.stdout or ""
    resolved = False
    try:
        d = json.loads(out)
        resolved = inst in (d.get("resolved_ids") or [])
    except Exception:
        resolved = False

    if resolved:
        print("RESOLVED: the hidden tests pass for %s." % inst)
        _cleanup_docker(inst, run_id)
        return 0

    # 5. NOT resolved -> surface the REAL test failure to the agent (failing test names +
    #    the assertion/traceback tail) so it can locate the exact missed spot. This is the
    #    feedback an Anthropic-grade harness gives; a generic "tests fail" leaves the agent blind.
    feedback = _failure_feedback(run_id, inst)
    print("NOT_RESOLVED: the hidden tests still fail with your current patch for %s. "
          "Find the real root cause in the SOURCE (do not edit tests) and fix it.\n%s"
          % (inst, feedback))
    _cleanup_docker(inst, run_id)
    return 1


def _cleanup_docker(inst, run_id):
    """Remove the eval container and instance image for *inst* to reclaim C: space.

    swebench naming (from test_spec.py):
      container : sweb.eval.<instance_id.lower()>.<run_id>
      image     : sweb.eval.x86_64.<instance_id.lower()>:latest

    Set env var SWE_KEEP_IMAGES=1 to skip (useful when debugging eval failures).
    Never raises; all errors go to stderr only so callers are not affected.
    """
    if os.environ.get("SWE_KEEP_IMAGES") == "1":
        return

    inst_lower = inst.lower()
    container = f"sweb.eval.{inst_lower}.{run_id}"
    image = f"sweb.eval.x86_64.{inst_lower}:latest"

    cmds = [
        f"docker rm -f {container} 2>/dev/null || true",
        f"docker rmi {image} 2>/dev/null || true",
        "docker image prune -f 2>/dev/null || true",
    ]
    script = "; ".join(cmds)
    try:
        wsl(script, timeout=120)
    except Exception as exc:
        print(f"[swe_check] docker cleanup error (non-fatal): {exc}", file=sys.stderr)


# a bare/raised exception line, e.g. "AssertionError", "ValueError: bad name",
# "django.core.exceptions.ImproperlyConfigured: ...". Matches at column 0 (django/sympy
# custom runners) or after a pytest "E " prefix (stripped before this is applied).
_EXC_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Failure)(?::|$)")
# BROADER raised-exception line for the collection/import-error fallback (see relay/test_feedback.py,
# kept IN SYNC): "<dotted.Type>: <message>" with a capitalized final component. Catches django
# app-loading failures whose type does NOT end in Error/Exception (AppRegistryNotReady,
# ImproperlyConfigured) which abort collection with no "FAIL: test_x" header.
_RAISED_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*\.[A-Z]\w+|[A-Z]\w+): \S")
# pytest's source pointer printed under the traceback: "path/file.py:123: SomeError"
_PYTEST_PTR_RE = re.compile(r"^.+\.py:\d+: \w*(?:Error|Exception|Warning|Failed)\b")
# django unittest runner result header: "FAIL: test_x (module.Class)" / "ERROR: test_x (module.Class)"
_DJANGO_RES_RE = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)")
# sympy custom-runner failure banner: "____ sympy/.../test_foo.py:test_bar ____"
_SYMPY_BANNER_RE = re.compile(r"^_+ (\S+\.py:\S+) _+$")
# a traceback frame line: '  File ".../x.py", line 12, in test_foo'
_TB_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
# doctest failure frame (sympy/sphinx run docstrings as tests): 'File "...", line N, in mod.func'
# preceding a 'Failed example:' / 'Expected:' / 'Got:' block. Kept IN SYNC with test_feedback.py.
_DOCTEST_FAIL_RE = re.compile(r"^\s*(?:\*+\s*)?File \"([^\"]+)\", line (\d+), in (\S+)\s*$")
# col-0 markers that terminate a doctest Expected:/Got: body block.
_DOCTEST_STOP = ("Expected:", "Got:", "Failed example:", "Expected nothing", "Got nothing")


def _doctest_block_after(lines, start, header, max_lines=3):
    """Indented body line(s) following a doctest 'header:' line at/after *start*. The Expected:/
    Got: body is indented UNDER the header; the block ends at the next col-0 marker (another
    header, a '****' separator, a File frame, an empty line, or any unindented line). One-line
    collapsed summary. Kept IN SYNC with test_feedback._block_after."""
    n = len(lines)
    for k in range(start, min(n, start + 12)):
        if lines[k].strip() == header.rstrip(":") + ":" or lines[k].strip() == header:
            body = []
            for m in range(k + 1, min(n, k + 1 + max_lines)):
                raw = lines[m]
                s = raw.strip()
                if (not s or s.startswith("***") or _DOCTEST_FAIL_RE.match(raw)
                        or s in _DOCTEST_STOP or not (raw.startswith(" ") or raw.startswith("\t"))):
                    break
                body.append(s)
            return " ".join(body)[:120] if body else ""
    return ""


def _doctest_failures(lines):
    """Summarize failing doctests (sympy/sphinx run docstring examples as tests). Returns short
    'module.func @ file:line: expected <X> got <Y>' strings. Kept IN SYNC with
    test_feedback._doctest_failures."""
    out, seen = [], set()
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].lstrip().startswith("Failed example:"):
            where = ""
            for j in range(i - 1, max(-1, i - 6), -1):
                fm = _DOCTEST_FAIL_RE.match(lines[j])
                if fm:
                    where = "%s @ %s:%s" % (fm.group(3), fm.group(1), fm.group(2))
                    break
            exp = _doctest_block_after(lines, i, "Expected:")
            got = _doctest_block_after(lines, i, "Got:")
            desc = (where or "doctest")
            if exp or got:
                desc += ": expected %s got %s" % (exp or "<nothing>", got or "<nothing>")
            if desc not in seen:
                seen.add(desc)
                out.append(desc)
        i += 1
    return out


def _collection_error(err_tail, ptr, lines):
    """Fallback for import/collection errors carrying NO test name (django app-loading failures:
    AppRegistryNotReady / ImproperlyConfigured raised at import time). Compose
    '<ErrorType>: <msg> at <file:line>' from the strongest error line + the deepest frame. Kept
    IN SYNC with test_feedback._collection_error."""
    err = (err_tail[-1] if err_tail else "")
    if not err:
        for raw in lines:
            t = raw.strip()
            if _EXC_RE.match(t) or _RAISED_EXC_RE.match(t):
                err = t
    if not err:
        return ""
    where = (ptr[-1] if ptr else "")
    if not where:
        frames = ["%s:%s in %s" % m.groups()
                  for ln in lines for m in [_TB_FRAME_RE.match(ln)] if m]
        where = frames[-1] if frames else ""
    return (err + (" at " + where if where else ""))[:240]


def _failure_feedback(run_id, inst):
    """Extract failing test names + the last assertion/traceback from the swebench test log.

    Robust across the three test-runner output formats SWE-bench Lite repos use:
      * pytest (pytest, pylint, sphinx, flask, requests, seaborn, scikit-learn, matplotlib,
        astropy, xarray): "FAILED path::test - reason" + "E   <Error>: ..." blocks.
      * django's unittest runner: "FAIL:/ERROR: test_x (module.Class)" headers followed by a
        bare "Traceback (most recent call last):" and a column-0 "<Error>: ..." line.
      * sympy's custom runner: "____ path.py:test_x ____" banners followed by a frame list and
        a bare exception line.
    Earlier versions only understood pytest, so django/sympy retries got NO failing test name
    or assertion -- the agent retried blind. Parsing is wrapped so any error still yields a
    useful log tail rather than breaking the verify flow.
    """
    logp = "/root/swe/logs/run_evaluation/" + run_id + "/companion/" + inst + "/test_output.txt"
    r = wsl("cat " + logp + " 2>/dev/null", timeout=60, capture=True)
    log = r.stdout or ""
    if not log.strip():
        return "(no test log captured; re-read the failing test and trace each code path it exercises.)"
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)  # strip ANSI color codes pytest emits
    try:
        return _parse_failure_log(log)
    except Exception as exc:  # never let feedback parsing break the verify flow
        tail = [ln for ln in log.splitlines() if ln.strip()][-25:]
        return ("--- TEST FAILURE (raw tail; parser error: %s) ---\n%s" % (exc, "\n".join(tail)))


def _parse_failure_log(log):
    lines = log.splitlines()
    failed = []   # human-readable failing test identifiers
    seen = set()

    def _add_fail(name):
        if name and name not in seen:
            seen.add(name)
            failed.append(name)

    for raw in lines:
        ln = raw.rstrip()
        s = ln.lstrip()
        if s.startswith("FAILED ") or (s.startswith("ERROR ") and "::" in s):  # pytest summary
            name = s.split(" - ", 1)[0].strip()
            if not name.startswith(("FAILED (", "ERROR (")):  # skip django's "FAILED (errors=1)" footer
                _add_fail(name)
            continue
        m = _DJANGO_RES_RE.match(ln)  # django unittest runner
        if m:
            _add_fail("%s (%s)" % (m.group(1), m.group(2)))
            continue
        m = _SYMPY_BANNER_RE.match(ln)  # sympy custom runner
        if m:
            _add_fail(m.group(1))

    # error/assertion lines: pytest prefixes them with 'E '; django/sympy print them at col 0.
    # DeprecationWarning/etc. lines are noise (not the failure cause), so prefer real errors and
    # only fall back to warnings if nothing else surfaced.
    err, warn = [], []
    for raw in lines:
        s = raw.lstrip()
        body = None
        if s.startswith("E ") or s == "E":
            body = s[2:].strip() or s.strip()
        elif _EXC_RE.match(raw.strip()):
            body = raw.strip()
        if body:
            (warn if "Warning" in body.split(":", 1)[0] else err).append(body)
    err_tail = (err or warn)[-6:]

    # source pointer: pytest prints "file.py:NN: Error"; django/sympy give traceback frames --
    # use the LAST frames of each traceback (the deepest = where it was raised). Drop pointers
    # that are Warning emissions (sympy/astroid import-time DeprecationWarnings flood these).
    ptr = [ln.strip() for ln in lines
           if _PYTEST_PTR_RE.match(ln.strip()) and "Warning" not in ln]
    if not ptr:
        frames = ["%s:%s in %s" % m.groups() for ln in lines
                  for m in [_TB_FRAME_RE.match(ln)] if m and "Warning" not in ln]
        # prefer frames in the project's own test/source files over library import frames
        test_frames = [f for f in frames if "/tests/" in f or "/testbed/" in f]
        ptr = (test_frames or frames)[-4:]

    # doctest divergences (sympy/sphinx run docstring examples as tests): which example
    # expected X but got Y. Kept IN SYNC with test_feedback.py.
    doctests = _doctest_failures(lines)

    parts = ["--- ACTUAL TEST FAILURE (use this to find the exact spot) ---"]
    if failed:
        parts.append("Failing tests (%d):" % len(failed))
        parts.extend("  " + f for f in failed[:8])
        if len(failed) > 8:
            parts.append("  ... and %d more" % (len(failed) - 8))
    if doctests:
        parts.append("Failing doctests (%d):" % len(doctests))
        parts.extend("  " + d for d in doctests[:8])
        if len(doctests) > 8:
            parts.append("  ... and %d more" % (len(doctests) - 8))
    if err_tail:
        parts.append("Error:")
        parts.extend("  " + e for e in err_tail)
    if ptr:
        parts.append("Raised at:")
        parts.extend("  " + p for p in ptr[-4:])
    if not failed and not doctests:
        # No per-test header -> the django import/collection-error shape (AppRegistryNotReady /
        # ImproperlyConfigured raised at import time, col 0, no "FAIL: test_x" banner). Still
        # tell the agent WHAT broke and WHERE. Kept IN SYNC with test_feedback.py.
        ce = _collection_error(err_tail, ptr, lines)
        if ce:
            parts.append("Collection/import error (no test name extracted):")
            parts.append("  " + ce)
    if not failed and not doctests and not err_tail and not ptr:
        # unknown format -> give the agent the meaningful tail rather than nothing
        tail = [ln for ln in lines if ln.strip()][-20:]
        parts.append("Log tail:")
        parts.extend("  " + t for t in tail)
    parts.append("Hint: the same bug pattern often appears in MORE than one place in the file; "
                 "search for every occurrence, not just the first.")
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
