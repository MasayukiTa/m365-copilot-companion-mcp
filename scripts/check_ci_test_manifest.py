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

    ただし `git ls-files` だけを見ていたせいで、このチェックは自分が存在する理由その
    ものを取り逃がしていた。新しいテストを書いて、add する前にこれを走らせると「OK」
    と言う。add して commit して push して、はじめて CI で落ちる。実際にそうなった。
    index も見ることで、`git add` した瞬間から要求が発生する。
    """
    return _git("ls-files", "--cached")


def discover_tests() -> set[str]:
    tracked = _tracked()
    found: set[str] = set()
    for root_name in TEST_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in base.rglob("test_*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if tracked is not None and rel not in tracked:
                continue
            found.add(rel)
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

    # Untracked test files are not required yet -- they are not in the CI checkout -- but
    # saying nothing about them is how "OK" came to mean "OK until you commit". Name them.
    others = _git("ls-files", "--others", "--exclude-standard") or set()
    pending = sorted(p for p in others
                     if re.fullmatch(r"(?:%s)/(?:[^/]+/)*test_[^/]+\.py"
                                     % "|".join(TEST_ROOTS), p))
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
