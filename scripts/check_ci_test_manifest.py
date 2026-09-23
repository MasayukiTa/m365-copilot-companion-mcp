"""Fail CI when a new pytest file is silently omitted from the hermetic suite."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEST_ROOTS = ("bench", "bridge", "relay", "scripts", "tests", "tools", "ui")

#: Both of pytest's default collection patterns. Only `test_*.py` was discovered, so a file
#: named the other way -- which pytest DOES collect -- was invisible to this audit and could
#: sit unlisted and unrun for as long as nobody noticed. This project configures no
#: `python_files`, so pytest's defaults are what actually decide.
_TEST_FILE_RE = r"(?:test_[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+_test)\.py"
_TEST_GLOBS = ("test_*.py", "*_test.py")

# Each exception needs a concrete alternate runner. This list is intentionally tiny so a new
# test file cannot become permanently green-by-omission.
EXCLUDED = {
    "tools/test_file_ops.py": "Windows path semantics; run by windows-install-smoke",
    "tools/test_trace.py": "script-style checks; run directly in the Linux test job",

    # ADDED IN BULK, WHICH THIS LIST IS NOT SUPPOSED TO ALLOW, so the reason is recorded per
    # file rather than as one wave. These eight were among 79 that had never been listed at
    # all; they pass on Windows and fail on a Linux runner. Four are genuinely environment-
    # bound (real disk, real RAM, a live service, Windows profile layout). The other four are
    # TEST-side portability bugs -- source walks that assume a backslash separator, and one
    # that reads a gitignored .fleet artifact absent from a fresh checkout. Those four should
    # be fixed and moved back, not left here; they are parked, not resolved.
    "bench/test_pro_batching.py":
        "MEASURED ON THE RUNNER 2026-09-20, after this exclusion was doubted and tested rather "
        "than argued: FLEET_FLOOR_GIB comes from settings_disk_floor() and reads >5.6 GiB "
        "there (not the 3.0 fallback that was assumed), so concurrency_for collapses to 1 and "
        "four tests fail -- js at 8.0 GiB free wants >=3 and gets 1, python<js becomes 1<1, "
        "and batch width stays [1,1,1,1,1]. One more reads .fleet/swe/pro_slice50_full.json, "
        "which is gitignored and absent in a fresh checkout. Registering it in ci.yml turned "
        "main red; the exclusion is live, not stale",
    "bench/test_swe_run_facts.py":
        "Windows path semantics (separator and case-insensitive joins); run by windows-install-smoke",
    "relay/test_acceptance_contract.py":
        "asserts behaviour on a path the OS must reject, which differs between NTFS and ext4",
    "relay/test_repo_bug_fix_skill.py":
        "reads a gitignored .fleet artifact that is absent in a fresh checkout",
    "scripts/test_prune_edge_cache.py":
        "Edge profile layout under a Windows LOCALAPPDATA tree; run by windows-install-smoke",
    "tools/test_instructions_do_not_accumulate_cases.py":
        "asserts that skills/ is non-empty, but skills/ is gitignored on purpose "
        "(data, never code), so a fresh checkout has none by design",
    # tests/test_integration_evidence.py AND tests/test_outcome_enum_closed.py WERE HERE
    # AND ARE GONE, 2026-09-22. Their reasons said the source walk missed assignments
    # under Linux path separators. On 2026-09-19 a careful read of both said the same
    # thing -- one of them handles both separators explicitly -- and that read was NOT
    # acted on, because "no portability problem is visible in the source" is not "it runs
    # on Linux", and the pro_batching exclusion had just proved the difference by turning
    # main red.
    #
    # The reporting step added to the ubuntu job runs every excluded file there and
    # prints the outcome. First run: 13 passed and 21 passed. So the reasons had expired,
    # and these two are registered on evidence instead of on an argument. The read was
    # also WRONG IN DEGREE -- it suspected three stale exclusions and two were.
    "tools/test_judge_live_roundtrip.py":
        "live round trip against a running judge; no service in CI",
}


class GitUnavailable(RuntimeError):
    """git could not be run, or ran and failed. The audit cannot vouch for anything without
    it -- see the module docstring: a filter that silently stops filtering is the same silent
    pass this whole check exists to prevent."""


def _git(*args) -> set[str]:
    from tools.childproc import run as _run_child
    try:
        out = _run_child(["git", *args], cwd=ROOT, timeout=30)
    except Exception as exc:
        raise GitUnavailable(str(exc)) from exc
    if out.returncode != 0:
        why = (out.stderr or out.stdout or "").strip()[:300] or (
            "git exited %d" % out.returncode)
        raise GitUnavailable(why)
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _tracked() -> set[str]:
    """CI のチェックアウトに存在することになるファイル。取れなければ GitUnavailable（判定を
    諦めるのではなく、audit 自体を失敗させる -- 詳細は GitUnavailable のドキュメント参照）。

    追跡していないテストは CI のチェックアウトに存在しないので、一覧に載せようが
    ないし、載せれば CI が「そんなファイルは無い」で落ちる。手元にだけ置いてある
    ものを、載せ忘れとして扱わないための区別。

    `--cached` は明示だが既定と同じで、挙動は変わらない。以前これを「push するまで
    落ちない欠陥の修正」と書いたが、それは誤りだった。`git ls-files` は元から index を
    見ており、`git add` 済みのファイルは最初から見えていた。実際に CI で落ちた原因は
    チェックの欠陥ではなく、add する前にチェックを走らせた手順のほうにある。
    """
    return _git("ls-files", "--cached")


def discover_tests() -> set[str]:
    tracked = _tracked()
    found: set[str] = set()
    for root_name in TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for glob in _TEST_GLOBS:
            for path in base.rglob(glob):
                rel = path.relative_to(ROOT).as_posix()
                if rel not in tracked:
                    continue
                found.add(rel)
    return found


#: A path followed by a LITERAL backslash-n instead of a line continuation. Written by hand
#: this would be strange; written by a patch script through a shell heredoc it happens
#: constantly, and it happened seven times in one session. The audit could not see it because
#: it matches test paths anywhere in the file and both paths are present -- just welded onto
#: one line, where the shell hands pytest an argument called `n` and the run dies on a file
#: that does not exist. So the check that exists to stop a test being listed-but-unrun was
#: blind to a listing that is malformed rather than missing.
_WELDED = re.compile(r"\.py\s+\\n\s")


def malformed_listing() -> list[str]:
    """Lines where an escape arrived as two characters instead of a continuation."""
    out = []
    for i, line in enumerate(WORKFLOW.read_text(encoding="utf-8").splitlines(), 1):
        if _WELDED.search(line):
            out.append("%s:%d  %s" % (WORKFLOW.name, i, line.strip()[:100]))
    return out


def listed_tests() -> set[str]:
    """Test files the workflow actually RUNS.

    Comments are stripped first. Scanning the whole file counted a path in a `#` comment as
    listed, so deleting a test from the pytest command and leaving a note about why kept the
    audit green -- the exact "listed but not executed" state it exists to prevent. This still
    does not prove the file is on a pytest command line rather than in, say, an `echo`; it
    proves it is not in a comment, which is the hole that was reachable by accident.
    """
    lines = []
    for raw in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(raw.split(" #", 1)[0] if " #" in raw else raw)
    return set(re.findall(
        r"(?:%s)/(?:[A-Za-z0-9_.-]+/)*%s" % ("|".join(TEST_ROOTS), _TEST_FILE_RE),
        "\n".join(lines),
    ))


def defines_no_pytest_tests(path: Path) -> bool:
    """True when pytest would collect ZERO items from this file.

    Parsed rather than executed: importing a test module to ask what is in it runs whatever
    it does at import time, which for a script-style suite is the suite.

    THIS IS THE CHECK THAT WAS MISSING. 21 of 166 listed files defined no test_* function.
    pytest imported them, collected nothing, and CI stayed green over several hundred checks
    it had never run -- four of the files were red. "Listed in CI" and "run by CI" had
    quietly stopped meaning the same thing, and nothing said so.
    """
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return False        # unparseable is a different problem; pytest will say so loudly
    # WALKED, NOT JUST THE MODULE BODY. The first version looked at top-level defs and
    # `Test*` classes only, and flagged two files that pytest collects perfectly well:
    # `unittest.TestCase` subclasses are collected whatever they are named, so
    # `class LedgerTests(unittest.TestCase)` is 7 real tests that the check called hollow.
    # An audit with false positives gets an exception list bolted onto it, and then it is
    # the exception list rather than the audit.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("test"):
            return False
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("Test"):
                return False
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if "TestCase" in str(name):
                    return False
    return True


def script_style_suites() -> set[str]:
    """The files the script-style runner actually executes.

    Read from that module rather than duplicated here, so a file cannot be declared
    script-style in one place and forgotten in the other -- which would recreate the gap
    with a different name on it.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_sst", ROOT / "scripts" / "run_script_style_tests.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return set(mod.SUITES)
    except Exception:
        return set()


def main(argv=None) -> int:
    # `--strict-untracked` PROMOTES THE NOTE BELOW TO AN ERROR, and exists because the note was
    # not enough. This module's own docstring already said the failure mode is procedural --
    # 「実際に CI で落ちた原因はチェックの欠陥ではなく、add する前にチェックを走らせた手順の
    # ほうにある」 -- and on 2026-09-14 it happened again exactly as written: preflight printed
    # the note, exited 0, the file was committed, and CI went red on the next push.
    #
    # A note that is printed and ignored is a note that does not work. The gate keeps its
    # lenient default, because run by hand while a test is being written it is right to be
    # lenient -- but preflight, whose whole contract is "everything CI runs, run here", passes
    # this flag. In CI the flag changes nothing: a checkout has no untracked test files.
    import argparse

    ap = argparse.ArgumentParser(description="Audit the CI test manifest.")
    ap.add_argument("--strict-untracked", action="store_true",
                    help="fail when an untracked test file exists (pre-push use)")
    args = ap.parse_args(argv)

    try:
        discovered = discover_tests()
    except GitUnavailable as exc:
        print("could not run git: %s; the audit did not run" % exc)
        return 2
    listed = listed_tests()
    excluded = set(EXCLUDED)
    # Covered by the script-style runner rather than by pytest. Read from that module, not
    # restated here -- a second copy of the list is a second place for it to go stale.
    scripted_early = script_style_suites()
    missing = sorted(discovered - listed - excluded - scripted_early)
    stale_listed = sorted(path for path in listed if not (ROOT / path).is_file())
    stale_excluded = sorted(excluded - discovered)

    if missing:
        print("ERROR: test files missing from CI and EXCLUDED:")
        for path in missing:
            print("  -", path)
    if stale_listed:
        print("ERROR: CI lists test files that do not exist:")
        for path in stale_listed:
            print("  -", path)
    if stale_excluded:
        print("ERROR: stale CI test exclusions:")
        for path in stale_excluded:
            print("  -", path)
    welded = malformed_listing()
    if welded:
        print("ERROR: a literal '\\n' welds two test paths onto one line, so the shell will "
              "pass pytest an argument called 'n':")
        for line in welded:
            print("  -", line)
    # A LISTED FILE THAT COLLECTS NOTHING IS NOT A TEST THAT RUNS.
    scripted = scripted_early
    hollow = sorted(p for p in listed
                    if (ROOT / p).is_file() and defines_no_pytest_tests(ROOT / p)
                    and p not in scripted and p not in excluded)
    if hollow:
        print("ERROR: listed under the pytest step but defines no test_* function, so pytest "
              "collects zero items and CI runs none of it:")
        for path in hollow:
            print("  -", path)
        print("  Either give it test_* functions, or add it to "
              "scripts/run_script_style_tests.py so it is executed as a script.")

    # And the converse: a script-style file must not ALSO sit in the pytest list, where it
    # contributes nothing and reads as covered.
    double = sorted(scripted & listed)
    if double:
        print("ERROR: run as a script AND listed under pytest, where it collects nothing and "
              "reads as covered:")
        for path in double:
            print("  -", path)

    orphan = sorted(p for p in scripted if not (ROOT / p).is_file())
    if orphan:
        print("ERROR: the script-style runner names files that do not exist:")
        for path in orphan:
            print("  -", path)

    if missing or stale_listed or stale_excluded or welded or hollow or double or orphan:
        return 1

    # Untracked test files are not required yet -- they are genuinely not in the CI checkout,
    # so demanding they be listed would make CI fail on a file it cannot run. But saying
    # nothing about them lets a test be written, never committed, and never noticed. Named,
    # not enforced; the enforcement happens the moment they are added.
    try:
        others = _git("ls-files", "--others", "--exclude-standard")
    except GitUnavailable:
        # Advisory-only section (see the comment above): git already proved usable in
        # discover_tests() above, so a failure here is not "git is unavailable" -- it is not
        # worth failing the whole audit over a NOTE-level, best-effort listing.
        others = set()
    pending = sorted(p for p in others
                     if re.fullmatch(r"(?:%s)/(?:[^/]+/)*%s"
                                     % ("|".join(TEST_ROOTS), _TEST_FILE_RE), p))
    if pending:
        if args.strict_untracked:
            print("ERROR: untracked test file(s). Under --strict-untracked these are a "
                  "failure, because a push turns them into a CI failure and this audit "
                  "cannot see them until they are staged. `git add` them and list them in "
                  "ci.yml, or add them to EXCLUDED with a reason:")
            for path in pending:
                print("  -", path)
            return 1
        print("NOTE: not tracked yet, so not required yet -- but required the moment you "
              "`git add` them:")
        for path in pending:
            print("  -", path)

    print("CI test manifest OK: %d pytest files listed, %d explicit exception(s)." % (
        len(listed), len(excluded)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
