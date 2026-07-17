"""Fail CI when a new pytest file is silently omitted from the hermetic suite."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEST_ROOTS = ("bench", "bridge", "relay", "scripts", "tests", "tools", "ui")

# Each exception needs a concrete alternate runner. This list is intentionally tiny so a new
# test file cannot become permanently green-by-omission.
EXCLUDED = {
    "tools/test_file_ops.py": "Windows path semantics; run by windows-install-smoke",
    "tools/test_trace.py": "script-style checks; run directly in the Linux test job",
}


def discover_tests() -> set[str]:
    found: set[str] = set()
    for root_name in TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in base.rglob("test_*.py"):
            found.add(path.relative_to(ROOT).as_posix())
    return found


def listed_tests() -> set[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    return set(re.findall(
        r"(?:bench|bridge|relay|scripts|tests|tools|ui)/"
        r"(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py",
        text,
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

    print("CI test manifest OK: %d pytest files listed, %d explicit exception(s)." % (
        len(listed), len(excluded)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
