# -*- coding: utf-8 -*-
"""`text=True` decodes child output with cp932 here, and that has cost real work twice.

`subprocess(..., text=True)` decodes with `locale.getpreferredencoding(False)`. On this machine
that is **cp932**. A child that writes UTF-8 -- git, ssh, pip, every script in this tree -- dies
on its first non-cp932 byte, and the caller does not receive a short string. It receives
nothing, or an exception raised inside the reader thread while `returncode` is 0.

MEASURED, TWICE, IN ONE WEEK:

  * `bench/swe_solve_decoupled.py` -- `git diff` over a patch holding one non-cp932 byte killed
    the reader thread, `.stdout` came back None, and the arm aborted with 60 instances already
    solved (4ef0d31).
  * `bench/swe_check_remote.py` -- the same defect in the GRADE path, where `(r.stdout or "")`
    turned it into a silent empty string and the run was misdiagnosed as "eval host
    unreachable" (1c83939).

Two call sites of ONE FAILURE CLASS, found and fixed one at a time, a day apart. This file
exists because the repository's own rule for that shape is to sweep every call site -- and a
sweep that is not enforced grows back.

WHAT THIS ENFORCES. Every `subprocess.*(..., text=True)` (or `universal_newlines=True`) that
does not also name an `encoding=`/`errors=` is counted, per file, against the inventory
below. The count may go DOWN and never up, a file that is clean must be removed from the
inventory, and a file that is not listed may not have any. It began at 59 files / 100 sites on
2026-09-12 and stands at two, both frozen on purpose.

HOW TO GET OUT OF THE LIST: use `tools.childproc.run` (or decode with `tools.childproc.decode`)
and delete the entry. `errors="replace"` on the call is also accepted -- it is the same policy
written inline -- but the helper is preferred because it states which policy, in one place.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = ("relay", "bench", "tools", "scripts", "bridge", "ui", "tests")

#: The subprocess entry points that decode for you when asked to.
_CALLS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


#: Files that still decode child output with the local code page, with how many sites each.
#:
#: 2026-09-12: the sweep began at 59 files / 100 sites and ends here at TWO FILES, and the two
#: that remain are not debt that was skipped -- they are debt that must not be paid inside a
#: sweep. Both are in the self-improvement FROZEN SET (relay/selfimprove/frozen.py's
#: FROZEN_MANIFEST, checksummed in frozen_baseline.json), and relay/selfimprove/test_frozen.py
#: refuses any edit to them with the reason: "a run whose judge changed produces numbers nobody
#: can trust, and unattended they look like any other row".
#:
#: The decode fix is correct on its own terms for both -- guards.py's powershell count lost to
#: the code page returns 0, which reads as "no problem" -- and it ships with a DELIBERATE
#: RE-FREEZE and its own record, not as a line in a sweep, and least of all with an A/B queued.
BASELINE = {
    "bench/evalhost_batch_grade.py": 2,
    "relay/selfimprove/guards.py": 1,
}


def _risky_calls(src: str) -> int:
    """How many subprocess calls in `src` decode with the locale codec.

    PARSED, NOT MATCHED. Matching the call shape as text counted a docstring that DESCRIBES
    this defect as an instance of it -- two inventory entries were exactly that, and neither
    could ever have been cleared by editing code, so they would have sat there as debt that
    does not exist. A ratchet whose list holds entries nobody can remove teaches its reader to
    ignore it.

    The AST also settles two quieter errors in the same direction: a `text=True` inside a
    comment, and an `encoding=` belonging to a DIFFERENT call nested in the same argument list.
    A window of text cannot tell which keyword belongs to which call; this can.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _CALLS
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if "encoding" in kw or "errors" in kw:
            continue
        for name in ("text", "universal_newlines"):
            v = kw.get(name)
            if isinstance(v, ast.Constant) and v.value is True:
                n += 1
                break
    return n


def _tracked():
    """Paths git actually has, or None when git cannot answer.

    THE INVENTORY IS OF THE REPOSITORY, NOT OF THIS WORKING TREE, and the difference broke CI
    in both directions on 2026-09-12:

      * `scripts/win/_mem_strata.py` is listed in .git/info/exclude, so it exists on this
        machine and NOT in CI. The inventory named a file CI cannot see, and the ratchet's
        "a clean file must be removed" check fired on its absence.
      * `bench/swe_diffgate.py` had an uncommitted fix here, so the seed recorded 0 sites
        where HEAD has 1, and CI -- which reads HEAD -- found an unlisted file.

    One defect, two shapes. Restricting the scan to tracked paths removes both: what CI sees
    and what the inventory counts become the same set of files.
    """
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, timeout=60)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return {p.strip().replace("\\", "/") for p in
            out.stdout.decode("utf-8", "replace").splitlines() if p.strip()}


def risky_sites():
    """{relative path: count} for every locale-decoded subprocess call the repository tracks."""
    tracked = _tracked()
    found = {}
    for root in ROOTS:
        base = os.path.join(REPO, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", ".venv") and not d.startswith("wt_")]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, REPO).replace(os.sep, "/")
                if tracked is not None and rel not in tracked:
                    continue          # locally-excluded or untracked: CI does not have it
                try:
                    src = io.open(path, encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                n = _risky_calls(src)
                if n:
                    found[rel] = n
    return found


# ── the ratchet ───────────────────────────────────────────────────────────────────────────

def test_no_new_file_decodes_child_output_by_luck():
    """A file that is not already carrying this debt may not start carrying it."""
    new = sorted(set(risky_sites()) - set(BASELINE))
    assert not new, (
        "these files decode child output with the local code page (cp932 here), which loses the "
        "WHOLE output on one bad byte -- use tools.childproc.run, or decode with "
        "tools.childproc.decode: %s" % ", ".join(new))


def test_no_file_grows_its_debt():
    grew = {p: (n, BASELINE[p]) for p, n in risky_sites().items()
            if p in BASELINE and n > BASELINE[p]}
    assert not grew, (
        "more locale-decoded subprocess calls than the 2026-09-12 inventory allows "
        "(file: now vs allowed): %r" % grew)


def test_a_file_that_is_clean_is_removed_from_the_list():
    """THE HALF THAT MAKES IT A RATCHET. Without this the inventory is a permanent excuse: a
    file could be fixed and its entry would sit there forever, and the next reader would take
    the list as the current state when it is a historical one."""
    now = risky_sites()
    stale = {p: n for p, n in BASELINE.items() if now.get(p, 0) < n}
    assert not stale, (
        "these entries overstate the debt -- lower or delete them (file: allowed, actual "
        "now %r): %r" % ({p: now.get(p, 0) for p in stale}, stale))


def test_every_entry_is_a_file_the_repository_actually_has():
    """CI only ever sees tracked files. An entry for anything else is a debt CI cannot find,
    and the ratchet then fails on its absence -- which is exactly what happened on 2026-09-12
    with a path listed in .git/info/exclude."""
    tracked = _tracked()
    if tracked is None:
        pytest.skip("git could not list the tracked files here")
    untracked = sorted(p for p in BASELINE if p not in tracked)
    assert not untracked, (
        "the inventory names files git does not track, so CI cannot see them: %s"
        % ", ".join(untracked))


def test_the_inventory_is_not_silently_empty():
    """A refactor that stops the scanner matching anything would make every test above pass by
    finding nothing. The scanner has to still be able to see."""
    assert risky_sites(), "the scanner found no sites at all, which means it stopped working"


# ── the policy it points at ───────────────────────────────────────────────────────────────

def test_the_helper_survives_the_byte_that_started_this():
    from tools import childproc
    # 0x82 0xA0 is cp932 'あ'; it is not valid UTF-8, and this is the shape that killed the
    # reader thread in both incidents.
    assert childproc.decode(b"\x82\xa0") != ""
    assert childproc.decode("diff --git".encode("utf-8")) == "diff --git"


def test_the_helper_answers_for_a_dead_reader_thread():
    """`.stdout` came back None while returncode was 0. The caller then did `(r.stdout or "")`
    and carried on with an empty string, which is how it was misread as an unreachable host."""
    from tools import childproc
    assert childproc.decode(None) == ""


def test_the_helper_refuses_to_put_the_locale_codec_back():
    from tools import childproc
    for bad in ({"text": True}, {"universal_newlines": True}, {"encoding": "cp932"}):
        with pytest.raises(TypeError):
            childproc.run(["cmd"], **bad)


def test_the_helper_really_decodes_a_child(tmp_path):
    """End to end through a real process, because the whole class is about what the reader
    thread does rather than what the decode function does."""
    import sys
    from tools import childproc
    p = childproc.run([sys.executable, "-c",
                       "import sys; sys.stdout.buffer.write('\\u3042'.encode('utf-8'))"])
    assert p.returncode == 0
    assert p.stdout == "あ"
