"""Fail CI when a new pytest file is silently omitted from the hermetic suite."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
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
}


def _git(*args) -> set[str] | None:
    import subprocess
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _tracked() -> set[str] | None:
    """CI のチェックアウトに存在することになるファイル。取れなければ None（判定を諦める）。

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
                if tracked is not None and rel not in tracked:
                    continue
                found.add(rel)
    return found


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


def main() -> int:
    discovered = discover_tests()
    listed = listed_tests()
    excluded = set(EXCLUDED)
    missing = sorted(discovered - listed - excluded)
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
    if missing or stale_listed or stale_excluded:
        return 1

    # Untracked test files are not required yet -- they are genuinely not in the CI checkout,
    # so demanding they be listed would make CI fail on a file it cannot run. But saying
    # nothing about them lets a test be written, never committed, and never noticed. Named,
    # not enforced; the enforcement happens the moment they are added.
    others = _git("ls-files", "--others", "--exclude-standard") or set()
    pending = sorted(p for p in others
                     if re.fullmatch(r"(?:%s)/(?:[^/]+/)*%s"
                                     % ("|".join(TEST_ROOTS), _TEST_FILE_RE), p))
    if pending:
        print("NOTE: not tracked yet, so not required yet -- but required the moment you "
              "`git add` them:")
        for path in pending:
            print("  -", path)

    print("CI test manifest OK: %d pytest files listed, %d explicit exception(s)." % (
        len(listed), len(excluded)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
