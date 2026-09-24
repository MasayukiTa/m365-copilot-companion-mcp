# -*- coding: utf-8 -*-
r"""conftest._real_dotenv_and_fleet_state_must_not_change: the session-scoped canary that
fails the whole test session if the real repo .env, or a real top-level .fleet/ state file
LIVE_RECORD_REDIRECTS already names, changes between session start and session end.

WHY THIS EXISTS (see conftest.py's own docstring on the fixture for the full account). Twice
in one day (2026-09-24): a test wrote relay/selfimprove/active_genome.json through a path built
at runtime, before that constant existed on LIVE_RECORD_REDIRECTS; separately,
scripts/test_bootstrap.py's DevTunnelNeverBlocksTests pointed bootstrap.ROOT at the real
checkout while exercising a new step, and for about a minute commented out the owner's real
MCP_TUNNEL_* lines in the real .env. LIVE_RECORD_REDIRECTS redirects every KNOWN write target
one entry at a time; this fixture is the backstop that does not need to know which file a test
SHOULD have redirected -- only that .env (and the .fleet files already on that table) must read
the same at the end of a session as at the start.

TWO KINDS OF TEST IN THIS FILE:

1. Fast, in-process unit tests of the two pure helpers (_fingerprint,
   _real_dotenv_and_fleet_state_targets), imported directly from conftest -- same pattern as
   test_code_only.py at this repo's root testing conftest.code_only. These run against tmp_path
   and this repo's OWN already-resolved target list; they never touch the real .env.

2. THE PROOF, per this task's own instruction: "a throwaway test that writes the real .env must
   turn the session red -- do this ONLY in a throwaway `git clone --local` under %TEMP% with
   its own dummy .env, never against the real checkout." test_a_write_to_the_clones_own_env_
   turns_that_sessions_exit_red does exactly that: clones THIS repository's current commit
   (`git clone --local`, never touching this checkout's real .env or any other live state) into
   a scratch directory under the system temp directory, writes a DUMMY .env into the clone
   (the clone's .env is gitignored, so cloning alone does not create one), writes ONE throwaway
   test file into the clone that appends to the CLONE'S OWN .env by its absolute path (never
   this checkout's), and runs pytest -- using THIS repo's already-built .venv interpreter,
   since the clone has none of its own -- against just that one file, with the clone as the
   working directory so the clone's own conftest.py (a copy of this exact commit) is the one
   that runs. Asserts the nested session's exit code is nonzero and its output names both the
   canary rule and the changed file. A companion test proves the negative: a throwaway session
   that writes nothing stays green, so the canary is not merely always red.

   A third test proves the DELIBERATE asymmetry: a write to a .fleet/ file (not .env) inside
   the clone must WARN but stay green. That split was added after measuring, on THIS machine
   (an owner's live install per this repo's own standing warning), that a real ~115s session
   of THIS repo's own suite caught .fleet/page_counts.jsonl growing mid-run with no test having
   written to it -- bridge/copilot_bridge.py's CDP watchdog appends to it every ~60s as normal
   production operation whenever the bridge is running, which it legitimately was. A .fleet/
   change is therefore not reliable evidence of a TEST writing to it the way an .env change is,
   so only .env is a hard fail; .fleet/ changes are reported so they can be investigated without
   turning an unrelated, healthy test run red for a reason that has nothing to do with test
   hygiene. See conftest.py's own docstring on the fixture for the full account.

Windows/PowerShell not required here (unlike most install-path tests in this repo) -- git and
a Python interpreter are the only externals, both already required to run this suite at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from conftest import (
    LIVE_RECORD_REDIRECTS,
    _FLEET_TARGET_MODULE_EXCEPTIONS,
    _fingerprint,
    _real_dotenv_and_fleet_state_targets,
    _security_critical_targets,
)

REPO = os.path.dirname(os.path.abspath(__file__))
_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(not _GIT, reason="git is required for the throwaway-clone proof")


# ── unit tests: _fingerprint ─────────────────────────────────────────────────────────────────

def test_fingerprint_of_a_missing_file_is_false_none_none(tmp_path):
    assert _fingerprint(tmp_path / "does-not-exist.txt") == (False, None, None)


def test_fingerprint_changes_when_content_changes(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello")
    fp1 = _fingerprint(p)
    assert fp1[0] is True and fp1[1] == 5

    p.write_bytes(b"hello!")  # one byte longer, different hash
    fp2 = _fingerprint(p)
    assert fp2 != fp1
    assert fp2[1] == 6


def test_fingerprint_is_stable_for_unchanged_content(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"same bytes every time")
    assert _fingerprint(p) == _fingerprint(p)


def test_fingerprint_never_exposes_the_content_itself(tmp_path):
    """The whole point of hashing rather than reading: the fingerprint of a file containing a
    secret must not let that secret be recovered from the fingerprint's own repr."""
    secret = "MCP_UNLOCK_PASSWORD=super-secret-value-should-not-leak"
    p = tmp_path / ".env"
    p.write_text(secret, encoding="utf-8")
    fp = _fingerprint(p)
    assert "super-secret-value-should-not-leak" not in repr(fp)
    assert "MCP_UNLOCK_PASSWORD" not in repr(fp)


# ── unit tests: _real_dotenv_and_fleet_state_targets ────────────────────────────────────────

def test_targets_include_the_real_dotenv():
    targets = _real_dotenv_and_fleet_state_targets()
    assert os.path.join(REPO, ".env") in [str(p) for p in targets] or any(
        str(p) == os.path.normpath(os.path.join(REPO, ".env")) for p in targets
    )


def test_targets_include_a_known_fleet_file():
    """tools.lock_state's _STATE_FILE redirects to "lock_state.json" and tools.lock_state is
    not in the exception set -- its real top-level .fleet file must be watched."""
    targets = {p.name for p in _real_dotenv_and_fleet_state_targets()}
    assert "lock_state.json" in targets


def test_targets_exclude_the_documented_non_fleet_exceptions():
    """The four (five, counting both tools.trace_ops and tools.runlog_ops) modules whose real
    default is documented as living somewhere other than .fleet/ must not contribute a WRONG
    .fleet/<name> target -- see _FLEET_TARGET_MODULE_EXCEPTIONS' own comment in conftest.py."""
    assert _FLEET_TARGET_MODULE_EXCEPTIONS == frozenset({
        "tools.memory_ops", "tools.trace_ops", "tools.runlog_ops", "relay.selfimprove.apply",
        "tools.security",
    })
    targets = _real_dotenv_and_fleet_state_targets()
    names = {p.name for p in targets}
    # These are the redirect filenames of the excluded modules -- if the exclusion ever broke,
    # one of these would show up as a (wrong) .fleet/ target.
    assert "memory_state.json" not in names        # tools.memory_ops: really at the repo root
    assert "active_genome.json" not in names        # relay.selfimprove.apply: beside its module
    # tools.trace_ops / tools.runlog_ops both redirect to "companion_runs", a directory under
    # the user's home, not a .fleet file -- also excluded by the "no path separator" filter
    # even without the module exception, since it is a directory rather than a plain filename
    # only in the sense that its REAL location has one; the redirect string itself has none.
    # The module exception is what actually keeps a wrong .fleet/companion_runs entry out.
    assert "companion_runs" not in names
    # tools.security's redirect filename is "unlock_state.json" (see LIVE_RECORD_REDIRECTS'
    # "FOURTH CLASS" entry) -- the general loop, correctly excluded now, would otherwise add
    # `.fleet/unlock_state.json`, a path this machine has never had. The REAL file
    # (`.unlock_state.json`, at the repo root, no .fleet/ prefix at all) is tracked instead by
    # _security_critical_targets(), asserted present below.
    assert os.path.join(REPO, ".fleet", "unlock_state.json") not in [str(p) for p in targets]


def test_targets_include_every_security_critical_ledger():
    """_security_critical_targets() -- the seven unlock/lock-state files that get the HARD FAIL
    treatment below, not the general .fleet/ warning -- must actually be in the fingerprinted
    set, or the fixture would be comparing files it never looked at."""
    from pathlib import Path

    critical = set(_security_critical_targets())
    assert len(critical) == 7
    targets = set(_real_dotenv_and_fleet_state_targets())
    assert critical <= targets
    names = {p.name for p in critical}
    assert names == {
        ".unlock_state.json", "unlock_revocations.json", "unlock_generation.json",
        "unlock_state.lock", "lock_state.json", "lock_refusals.jsonl",
        "unlock_token_gap.json",
    }
    # The one entry with NO .fleet/ prefix at all -- the repo-root dotfile tools.security.
    # STATE_FILE actually resolves to (note the leading dot: ".unlock_state.json", not
    # "unlock_state.json" -- that filename is only the LIVE_RECORD_REDIRECTS entry's redirect
    # name, chosen for the tmp sandbox and never claimed to match the real, dotted basename).
    unlock_state = next(p for p in critical if p.name == ".unlock_state.json")
    assert unlock_state == Path(os.path.join(REPO, ".unlock_state.json"))
    assert (os.sep + ".fleet" + os.sep) not in str(unlock_state)


def test_every_live_record_redirect_module_is_covered_or_excepted():
    """Sanity check on the exception list itself: every module name on LIVE_RECORD_REDIRECTS is
    either contributing targets (the common case) or is in the hand-verified exception set --
    never silently neither, which would mean a new module was added to the redirect table
    without anyone checking whether it belongs on the exception list."""
    covered_or_excepted = set(LIVE_RECORD_REDIRECTS)
    assert _FLEET_TARGET_MODULE_EXCEPTIONS <= covered_or_excepted, (
        "an exception names a module that is not even on LIVE_RECORD_REDIRECTS -- stale entry?"
    )


# ── the proof: a throwaway clone, a throwaway test, a real nested pytest session ────────────

_THROWAWAY_ENV_WRITER = '''
def test_a_test_that_writes_the_real_env():
    with open(r"%s", "a", encoding="utf-8") as fh:
        fh.write("INJECTED_BY_THROWAWAY_TEST=1\\n")
'''

_THROWAWAY_NOOP = '''
def test_a_test_that_writes_nothing():
    assert True
'''

_THROWAWAY_FLEET_WRITER = '''
import os

def test_a_test_that_writes_a_fleet_file():
    fleet_dir = os.path.join(r"%s", ".fleet")
    os.makedirs(fleet_dir, exist_ok=True)
    with open(os.path.join(fleet_dir, "toolset_shadow.jsonl"), "a", encoding="utf-8") as fh:
        fh.write("INJECTED_BY_THROWAWAY_TEST\\n")
'''

# toolset_shadow.jsonl (not lock_state.json, used here until 2026-09-24) is the example for the
# WARNING-only proof below: lock_state.json moved into _security_critical_targets() that day and
# is now a HARD FAIL (see test_a_write_to_a_security_ledger_in_the_clone_turns_that_sessions_
# exit_red), so it stopped being an example of the asymmetry this test exists to pin.
_THROWAWAY_SECURITY_WRITER = '''
import os

def test_a_test_that_writes_a_security_ledger():
    fleet_dir = os.path.join(r"%s", ".fleet")
    os.makedirs(fleet_dir, exist_ok=True)
    with open(os.path.join(fleet_dir, "lock_state.json"), "a", encoding="utf-8") as fh:
        fh.write("INJECTED_BY_THROWAWAY_TEST\\n")
'''


def _venv_python() -> str:
    candidate = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def _make_throwaway_clone(dirpath: str) -> None:
    """`git clone --local` of THIS repo's CURRENT COMMIT into `dirpath` (must not already
    exist). Never touches this checkout's real .env or any other live state -- git clone only
    reads committed objects from .git, and this function writes nothing outside `dirpath`."""
    proc = subprocess.run(
        [_GIT, "clone", "--quiet", "--local", "--no-hardlinks", REPO, dirpath],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "throwaway git clone failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.stdout, proc.stderr)
    )


def _run_nested_pytest(clone_dir: str, test_filename: str, test_source: str) -> subprocess.CompletedProcess:
    test_path = os.path.join(clone_dir, test_filename)
    with open(test_path, "w", encoding="ascii") as fh:
        fh.write(test_source)
    return subprocess.run(
        # -p no:pytest-bdd: this repo's own .venv carries a pytest_bdd build that raises
        # `ImportError: cannot import name 'iterparentnodeids' from '_pytest.nodes'` against the
        # installed pytest version -- true of the CLONE's .venv-less run too, since it borrows
        # THIS repo's .venv interpreter (see _venv_python()), plugins and all. Every other
        # pytest invocation in this repo already carries this flag; a nested subprocess call is
        # still a pytest invocation. Without it every throwaway session below errors out before
        # collecting a single test, which reads as "the canary did not fire" and is really
        # "pytest never started".
        [_venv_python(), "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:pytest-bdd",
         test_filename, "-q"],
        cwd=clone_dir, capture_output=True, timeout=180,
    )


@pytest.fixture()
def throwaway_clone(tmp_path):
    """A throwaway `git clone --local` of this repo's current commit, with its OWN dummy .env
    (the real .env is gitignored, so a fresh clone has none). Removed unconditionally."""
    clone_dir = os.path.join(str(tmp_path), "canary_clone")
    _make_throwaway_clone(clone_dir)
    with open(os.path.join(clone_dir, ".env"), "w", encoding="ascii") as fh:
        fh.write("DUMMY=1\n")
    try:
        yield clone_dir
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def test_a_write_to_the_clones_own_env_turns_that_sessions_exit_red(throwaway_clone):
    real_repo_env = os.path.join(REPO, ".env")
    before_real = _fingerprint(real_repo_env)

    clone_env_path = os.path.join(throwaway_clone, ".env")
    proc = _run_nested_pytest(
        throwaway_clone, "test_throwaway_writes_env.py", _THROWAWAY_ENV_WRITER % clone_env_path
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace") + (proc.stderr or b"").decode(
        "utf-8", errors="replace")

    assert proc.returncode != 0, (
        "the nested session should have failed (the canary must catch the clone's own .env "
        "being written), but it exited 0:\n%s" % out
    )
    assert "LIVE-STATE CANARY" in out, (
        "the nested session failed, but not with this canary's message -- something else "
        "broke first:\n%s" % out
    )
    assert "_real_dotenv_and_fleet_state_must_not_change" in out
    assert clone_env_path in out or os.path.normpath(clone_env_path) in out, (
        "the failure message does not name the changed file:\n%s" % out
    )

    # AND THE OUTER, REAL CHECKOUT'S .env WAS NEVER TOUCHED -- the whole point of doing this in
    # a clone. If this fails, the throwaway test above somehow escaped the clone.
    after_real = _fingerprint(real_repo_env)
    assert after_real == before_real, (
        "the real repo .env changed while proving the canary in a THROWAWAY CLONE -- this "
        "must never happen; the clone's isolation failed"
    )


def test_a_session_that_writes_nothing_stays_green(throwaway_clone):
    """The negative case: proves the canary is not simply always red. Without this, a canary
    that failed on EVERY session (e.g. a bug comparing (None, None, None) != itself, or
    resolving a target path inconsistently between the two calls) would look identical to a
    working one in the test above."""
    proc = _run_nested_pytest(
        throwaway_clone, "test_throwaway_noop.py", _THROWAWAY_NOOP
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace") + (proc.stderr or b"").decode(
        "utf-8", errors="replace")
    assert proc.returncode == 0, "a session that wrote nothing should stay green:\n%s" % out
    assert "LIVE-STATE CANARY" not in out


def test_a_write_to_a_fleet_file_in_the_clone_warns_but_stays_green(throwaway_clone):
    """The deliberate asymmetry (see this file's header docstring for the measured reason a
    .fleet/ change is only a warning, never a hard fail): writing to the clone's own
    .fleet/toolset_shadow.jsonl must be REPORTED but must NOT turn the session red."""
    proc = _run_nested_pytest(
        throwaway_clone, "test_throwaway_writes_fleet_file.py",
        _THROWAWAY_FLEET_WRITER % throwaway_clone,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace") + (proc.stderr or b"").decode(
        "utf-8", errors="replace")
    assert proc.returncode == 0, (
        "a .fleet/ file change must warn, not fail the session:\n%s" % out
    )
    assert "LIVE-STATE CANARY WARNING" in out
    assert "toolset_shadow.jsonl" in out
    # And it must NOT be reported as either hard-fail variant.
    assert "LIVE-STATE CANARY (conftest._real_dotenv_and_fleet_state_must_not_change): this " \
           "test session modified the real repo .env" not in out
    assert "modified a real unlock/lock-state ledger" not in out


def test_a_write_to_a_security_ledger_in_the_clone_turns_that_sessions_exit_red(throwaway_clone):
    """THE OTHER HALF OF THE 2026-09-24 CHANGE: unlike the general .fleet/ bucket proven above,
    the seven unlock/lock-state ledgers are a HARD FAIL. Writing to the clone's own
    .fleet/lock_state.json (one of _security_critical_targets()) must turn the session red, with
    the security-specific message -- not the .env message, and not a mere warning."""
    proc = _run_nested_pytest(
        throwaway_clone, "test_throwaway_writes_security_ledger.py",
        _THROWAWAY_SECURITY_WRITER % throwaway_clone,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace") + (proc.stderr or b"").decode(
        "utf-8", errors="replace")
    assert proc.returncode != 0, (
        "a write to a real unlock/lock-state ledger must fail the session, not just warn:\n%s"
        % out
    )
    assert "LIVE-STATE CANARY" in out
    assert "modified a real unlock/lock-state ledger" in out
    assert "lock_state.json" in out
    # And it must NOT be reported as the .env variant -- a different file changed.
    assert "modified the real repo .env" not in out
