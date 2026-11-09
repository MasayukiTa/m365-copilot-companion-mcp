"""CI guard: no internal analysis identifier of the retired prefix may reappear.

Fails if any git-tracked file contains the retired prefix immediately followed by a
word character (case-insensitive). The pattern is assembled from parts so this guard
file does not match itself.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Assemble the forbidden pattern from pieces so this file is not a hit on itself.
_PREFIX = "m" + "c" + "l" + "_"
PATTERN = re.compile(_PREFIX + r"\w", re.IGNORECASE)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True
    )
    text = out.stdout.decode("utf-8", "replace")
    return [p for p in text.splitlines() if p]


def test_no_retired_analysis_identifier_in_tracked_files() -> None:
    hits = []
    for rel in _tracked_files():
        p = ROOT / rel
        try:
            data = p.read_bytes()
        except (OSError, FileNotFoundError):
            continue
        text = data.decode("utf-8", "replace")
        for m in PATTERN.finditer(text):
            if rel == str(Path(__file__).relative_to(ROOT)).replace(chr(92), "/"):
                continue
            line = text.count(chr(10), 0, m.start()) + 1
            hits.append(rel + ":" + str(line))
    assert not hits, (
        "Retired internal analysis identifier found in tracked files: "
        + ", ".join(sorted(set(hits)))
    )
