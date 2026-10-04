# -*- coding: utf-8 -*-
"""Compile the work ledger: the frozen baseline plus one file per new entry.

WHY. docs/private/claude_work_ledger.md is append-only and every PR used to add its entry at
the END of that one file, so every PR conflicted with every other. `merge=union` in
.gitattributes fixes that for LOCAL merges only; GitHub's merge button ignores it. A new entry
now lives in its own file, docs/private/ledger.d/<date>-<slice>.md, which no other PR touches,
so it cannot conflict anywhere. The baseline file is left exactly as it was (history is not
rewritten) and this script prints baseline + entries in filename order, deterministically.

    python scripts/ledger_compile.py            # compiled ledger to stdout
    python scripts/ledger_compile.py --out F    # ... to a file (do not commit it: it would
                                                #     reintroduce the shared file)
    python scripts/ledger_compile.py --check    # validate entries; used by CI
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "private" / "claude_work_ledger.md"
ENTRY_DIR = ROOT / "docs" / "private" / "ledger.d"
_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9._-]*\.md$")
_HEADING_RE = re.compile(r"^## \S", re.M)


def entry_files(entry_dir: Path = ENTRY_DIR) -> list[Path]:
    """Entry files in deterministic (filename) order; README.md is not an entry."""
    if not entry_dir.is_dir():
        return []
    return sorted(p for p in entry_dir.glob("*.md") if p.name.lower() != "readme.md")


def problems(entry_dir: Path = ENTRY_DIR) -> list[str]:
    out = []
    for p in entry_files(entry_dir):
        if not _NAME_RE.match(p.name):
            out.append(f"{p.name}: name must be <YYYY-MM-DD>-<slice>.md (lowercase)")
        text = p.read_text(encoding="utf-8")
        if not _HEADING_RE.search(text):
            out.append(f"{p.name}: must contain a '## <date> - <title>' heading")
        if not text.endswith("\n"):
            out.append(f"{p.name}: must end with a newline")
    return out


def compile_ledger(baseline: Path = BASELINE, entry_dir: Path = ENTRY_DIR) -> str:
    parts = [baseline.read_text(encoding="utf-8").rstrip("\n") + "\n"]
    for p in entry_files(entry_dir):
        parts.append("\n" + p.read_text(encoding="utf-8").rstrip("\n") + "\n")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate entry files and exit")
    ap.add_argument("--out", help="write the compiled ledger here instead of stdout")
    args = ap.parse_args(argv)
    bad = problems()
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    if args.check:
        print(f"ledger ok: {len(entry_files())} entry file(s)")
        return 0
    text = compile_ledger()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())