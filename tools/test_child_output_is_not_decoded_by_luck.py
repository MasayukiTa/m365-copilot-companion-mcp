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
does not also name an `encoding=`/`errors=` is counted, per file, against the inventory taken
on 2026-09-12. The count may go DOWN and never up, a file that is clean must be removed from
the inventory, and a file that is not listed may not have any.

HOW TO GET OUT OF THE LIST: use `tools.childproc.run` (or decode with `tools.childproc.decode`)
and delete the entry. `errors="replace"` on the call is also accepted -- it is the same policy
written inline -- but the helper is preferred because it states which policy, in one place.
"""
from __future__ import annotations

import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = ("relay", "bench", "tools", "scripts", "bridge", "ui", "tests")

_CALL = re.compile(r"subprocess\.(?:run|Popen|check_output|check_call|call)\s*\(")
_TEXT = re.compile(r"(?:text|universal_newlines)\s*=\s*True")
_NAMED = re.compile(r"(?:encoding|errors)\s*=")


#: Files that still decode child output with the local code page, with how many sites each.
#: Taken 2026-09-12: 53 files, 93 sites (swept down from 59/100 the same day), then swept down
#: to 39 files, 73 sites the same day (the bench/ non-test sweep -- 15 files fixed, 20 sites).
#: THIS NUMBER IS A DEBT, NOT A SETTING -- every entry is a place where one byte can still
#: delete an entire command's output.
#:
#: Ordered by what it would cost when it fires:
#:   * relay/ and bench/ non-test files run unattended over real data, including paths under
#:     the owner's Desktop that are Japanese. These are the ones that have actually bitten.
#:   * scripts/ run at setup and maintenance time, with a person watching.
#:   * test files run over this repository's own ASCII paths and the output they read is
#:     produced by python itself; they are listed to keep the count honest, not because they
#:     are equally likely.
BASELINE = {
    # ── runs unattended, over real data ───────────────────────────────────
    "bench/evalhost_batch_grade.py": 2,
    # STAYS UNTIL A DELIBERATE RE-FREEZE. This file is in the self-improvement frozen set,
    # and test_frozen.py rejects any edit to it: "a run whose judge changed produces numbers
    # nobody can trust, and unattended they look like any other row". The decode fix is
    # correct on its own terms -- a powershell count lost to the code page returns 0, which
    # reads as "no problem" -- but it must ship with a re-freeze and its own record, not as a
    # line in a sweep, and least of all with an A/B queued.
    "relay/selfimprove/guards.py": 1,
    "tools/auto/autoloop.py": 1,
    "tools/coding_ops.py": 1,
    "tools/notify_ops.py": 1,
    # ── setup and maintenance, with a person watching ──────────────────
    "scripts/bootstrap.py": 6,
    "scripts/check_ci_test_manifest.py": 1,
    "scripts/check_no_identifying_names.py": 5,
    "scripts/diag_warmup_bias.py": 1,
    "scripts/run_route_campaign.py": 2,
    "scripts/run_transport_series.py": 1,
    "scripts/win/_mem_strata.py": 1,
    "scripts/win/checkpoint.py": 1,
    "scripts/win/edge_memory.py": 1,
    "scripts/win/reap_orphan_edge.py": 1,
    "scripts/win/resume_interrupted_fleet.py": 1,
    "scripts/win/verify_stack.py": 6,
    "scripts/win/watch_stack.py": 1,
    # ── tests, reading output produced by python over this repo's own ASCII paths ───
    "bench/companionbench/test_job_authority.py": 3,
    "bench/test_capture_survives_a_non_cp932_patch.py": 1,
    "relay/selfimprove/test_compare.py": 1,
    "relay/test_dotenv_before_imports.py": 1,
    "relay/test_reply_settle.py": 1,
    "relay/test_resend_checkable.py": 1,
    "scripts/test_prune_edge_cache.py": 1,
    "scripts/test_run_script_style_tests.py": 1,
    "scripts/test_stale_server_check.py": 2,
    "scripts/win/test_env_defaults.py": 1,
    "tests/test_dedicated_root_guard.py": 6,
    "tests/test_fleet_reaper_entry.py": 2,
    "tests/test_git_dedicated_root_guard.py": 2,
    "tests/test_git_shared_guard.py": 5,
    "tools/auto/test_autoloop.py": 1,
    "tools/test_bench_worktree_wiring.py": 2,
    "tools/test_local_loop_registration.py": 2,
    "tools/test_powershell_hardening.py": 2,
    "tools/test_shell_timeout_tree.py": 1,
    "tools/test_skill_registration.py": 1,
    "tools/test_worktree_lifecycle.py": 2,
}


def _call_text(src: str, start: int) -> str:
    """The whole call expression starting at `start`, by matching parentheses.

    NOT a line, and not a fixed slice. A regex over a window reported calls wrongly when an
    argument list wrapped -- the same mistake `test_every_fleet_side_record_in_relay_fleet
    _names_a_run` already made once and fixed the same way.
    """
    depth = 0
    for i in range(start, min(len(src), start + 4000)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:start + 4000]


def risky_sites():
    """{relative path: count} for every locale-decoded subprocess call in the tree."""
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
                try:
                    src = io.open(path, encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                n = 0
                for m in _CALL.finditer(src):
                    call = _call_text(src, m.end() - 1)
                    if _TEXT.search(call) and not _NAMED.search(call):
                        n += 1
                if n:
                    found[os.path.relpath(path, REPO).replace(os.sep, "/")] = n
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
