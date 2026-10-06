"""docs/architecture/*.md cite real files by path. A rename or deletion of any of them is
exactly the drift those documents exist to prevent -- and a diagram that is wrong is worse
than none, because it is trusted.

WHAT THIS DOES AND DOES NOT CATCH. It extracts every repo-relative path mentioned in the two
documents (matched by a known source/config extension) and asserts the file still exists.
It cannot tell whether a `file:line` citation still points at the right line, or whether a
diagram still describes what the file actually does -- only that the file itself has not
been renamed or deleted out from under the citation. `tools/win_hit_test.py` becoming
`tools/window_probe.py` (2026-09-15) is the shape of bug this catches; a function moving to
a different line in the same file is not.

Deliberately excludes anything under `.fleet/` (gitignored runtime state, not a checked-in
file) and bare filenames with no directory separator (too easy to false-positive on an
English word that happens to end in a source-like suffix, e.g. a sentence ending in
".json" as prose rather than a path).
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO, "docs", "architecture")

DOCS = [
    os.path.join(DOCS_DIR, "system_flow_map.md"),
    os.path.join(DOCS_DIR, "fact_duplication_ledger.md"),
]

#: Extensions worth checking. Deliberately narrow: wide enough to catch every file these
#: two documents actually cite, narrow enough that ordinary prose (".", "etc.") never matches.
_EXTS = r"py|cs|ps1|json|jsonl|bat|md"

#: A repo-relative path: at least one directory component, then a filename with one of the
#: extensions above. Allows a trailing ":123" or ":123-456" line reference, which is stripped
#: before the existence check. Word-boundary anchored so it does not grab a leading quote/backtick.
_PATH_RE = re.compile(
    r"(?<![\w./])"
    r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:%s))"
    r"(?::\d+(?:-\d+)?)?" % _EXTS
)

#: Root-level files with no directory component, named explicitly rather than by relaxing
#: the path regex to accept bare filenames generally -- a bare "status.json" or "main.py"
#: used as PROSE (not a path) is common in these documents, and would false-positive if the
#: regex admitted any bare filename. Add here only a name that is unambiguous at repo root.
_BARE_ROOT_FILES = ("main.py",)
_BARE_ROOT_RE = re.compile(
    r"(?<![\w./])(%s)(?::\d+(?:-\d+)?)?(?![\w/])" % "|".join(re.escape(n) for n in _BARE_ROOT_FILES)
)


def _cited_paths(text):
    found = set()
    for m in _PATH_RE.finditer(text):
        p = m.group(1)
        if p.startswith(".fleet/"):
            continue
        found.add(p)
    for m in _BARE_ROOT_RE.finditer(text):
        found.add(m.group(1))
    return found


@pytest.mark.parametrize("doc_path", DOCS, ids=[os.path.basename(d) for d in DOCS])
def test_every_cited_file_exists(doc_path):
    assert os.path.isfile(doc_path), "the doc itself is missing: %s" % doc_path
    with open(doc_path, encoding="utf-8") as fh:
        text = fh.read()
    cited = _cited_paths(text)
    assert cited, ("no repo-relative paths were found in %s -- the extraction regex is "
                   "almost certainly broken, not the document" % doc_path)
    missing = sorted(p for p in cited if not os.path.isfile(os.path.join(REPO, p)))
    assert not missing, (
        "%s cites %d file(s) that do not exist at that path any more "
        "(renamed or deleted -- update the citation): %s"
        % (os.path.basename(doc_path), len(missing), missing)
    )


def test_extraction_finds_the_known_anchor_files():
    """A cheap check on the checker: both documents must at least cite the files their own
    top-level sections are structurally about, so a regex that silently stopped matching
    anything would not pass test_every_cited_file_exists vacuously."""
    with open(DOCS[0], encoding="utf-8") as fh:
        flow_map_paths = _cited_paths(fh.read())
    with open(DOCS[1], encoding="utf-8") as fh:
        ledger_paths = _cited_paths(fh.read())
    for expected in ("relay/relay_fleet.py", "relay/fleet_runner.py", "main.py",
                     "tools/security.py", "ui/FleetCockpit.cs", "ui/CopilotChat.cs"):
        assert expected in flow_map_paths, "%s missing from system_flow_map.md" % expected
    for expected in ("tools/lock_state.py", "relay/relay_fleet.py",
                     "relay/copilot_autopilot_relay.py"):
        assert expected in ledger_paths, "%s missing from fact_duplication_ledger.md" % expected
