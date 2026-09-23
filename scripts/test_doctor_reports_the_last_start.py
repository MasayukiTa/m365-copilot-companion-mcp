# -*- coding: utf-8 -*-
r"""doctor.ps1 surfaces the last BACKGROUND start_all.ps1 run, and stops flagging a generated
Dev Tunnel name as "identifying".

BACKGROUND (see the newpc_fix_common.md task this executes). The daily launchers
(start_all.bat, the Desktop icon, the Startup .lnk / logon Task) all run start_all.ps1 through
start_all_hidden.vbs / start_background_hidden.vbs -- window 0, exit code unread -- so a
startup failure there produced nothing anyone would see until the chat window quietly stopped
answering. Commit 1f4588a made start_all.ps1 rewrite .setup\logs\start_all_summary.txt on
EVERY run (clean or not) with its failure list; this repo's doctor.ps1 previously never read
it. doctor.ps1's own section "# 7. Last background start" (Get-LastStartSummaryDoctor,
Test-LastStartSummaryStale, and the Check calls that use them) is what this test exercises.

Separately, commit d4d2c33 taught setup_devtunnel.ps1's Test-IdentifyingTunnelName to exempt a
name IT GENERATED (Test-GeneratedTunnelName) from the USERNAME-substring check -- without it, a
user named e.g. "pan" or "com" had the generated name "m365-copilot-companion-<hex>" flagged as
identifying on every run, purely because their username happens to occur inside the fixed
default name or its hex suffix (D29). doctor.ps1 carries its OWN copy of
Test-IdentifyingTunnelName (the header comment above it says to keep both, and bootstrap.py's,
in sync by hand) and had not received the same exemption; this test covers the mirrored
Test-GeneratedTunnelNameDoctor added alongside it.

HOW THIS RUNS WITHOUT A LIVE STACK. Nothing here dot-sources doctor.ps1 (it has top-level
side effects: it reads the real repo's .env, calls the real devtunnel CLI, hits
127.0.0.1:8000, etc.). Instead, per this repo's own convention (see
scripts/test_a_silent_death_leaves_its_exit_code.py and
scripts/test_setup_devtunnel_access_and_identity.py), the specific functions and the
"# 7. Last background start" block are extracted from the live .ps1 TEXT by balanced-brace /
marker slicing and run standalone in an isolated PowerShell process, with Add-Result and Check
(also extracted from the same file, so this cannot drift from what they actually do) supplying
just enough scaffolding to observe $script:results. No devtunnel CLI, no Edge, no server: the
block's own network call (server_pid lookup on 127.0.0.1:8000 for the staleness check) is left
in place and simply fails fast (nothing listens there in a test process), which is the same
"no live services" path doctor.ps1 takes on a machine that has not been started yet.

Windows-only: doctor.ps1 is Windows PowerShell (WMI CIM queries, devtunnel.exe, Win32-only
helpers elsewhere in the file), matching every other *.ps1 test in this directory.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402  (see: repository ratchet against text=True)

DOCTOR_PS1 = os.path.join(REPO, "scripts", "doctor.ps1")

_POWERSHELL = (
    shutil.which("powershell")
    or shutil.which("powershell.exe")
    or (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe") else None)
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _POWERSHELL,
    reason="doctor.ps1 and this test are Windows/PowerShell-only (os.name=%r, powershell found=%r)"
           % (os.name, bool(_POWERSHELL)),
)


# ── extracting from the live .ps1, without dot-sourcing it ─────────────────────────────────

def _extract_braced_block(text: str, start_marker: str) -> str:
    """The balanced-brace block starting at the first '{' at/after `start_marker`.

    Not PowerShell-aware (does not know about strings/here-strings); checked by hand that none
    of the functions extracted below have an unbalanced brace inside a string literal.
    """
    idx = text.index(start_marker)
    brace_start = text.index("{", idx)
    depth = 0
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting block at %r" % (start_marker,))


def _extract_var_line(text: str, var_name: str) -> str:
    """The single `$var_name = ...` assignment line (module-level constant), verbatim
    including any trailing comment. Used for the constants Test-IdentifyingTunnelName /
    Test-GeneratedTunnelNameDoctor close over ($DOCTOR_DEFAULT_NAME, $TOKEN_SHA256,
    $FULLNAME_SHA256) that live outside the function bodies the brace-extractor grabs -- these
    must come from the live file too, not be retyped here, or the test could pass against
    values the source no longer has."""
    marker = "$" + var_name + " ="
    idx = text.index(marker)
    end = text.index("\n", idx)
    return text[idx:end]


@pytest.fixture(scope="module")
def doctor_source() -> str:
    with open(DOCTOR_PS1, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def last_start_functions(doctor_source: str) -> str:
    """The two PURE helpers: parsing the summary file, and the staleness decision."""
    return (_extract_braced_block(doctor_source, "function Get-LastStartSummaryDoctor") + "\n\n" +
            _extract_braced_block(doctor_source, "function Test-LastStartSummaryStale"))


@pytest.fixture(scope="module")
def identifying_name_functions(doctor_source: str) -> str:
    # Constants the functions below close over, plus the functions themselves -- both must
    # come from the live file (see _extract_var_line's docstring).
    return (_extract_var_line(doctor_source, "DOCTOR_DEFAULT_NAME") + "\n" +
            _extract_var_line(doctor_source, "TOKEN_SHA256") + "\n" +
            _extract_var_line(doctor_source, "FULLNAME_SHA256") + "\n\n" +
            _extract_braced_block(doctor_source, "function Get-Sha256HexDoctor") + "\n\n" +
            _extract_braced_block(doctor_source, "function Test-GeneratedTunnelNameDoctor") + "\n\n" +
            _extract_braced_block(doctor_source, "function Test-IdentifyingTunnelName"))


@pytest.fixture(scope="module")
def last_start_block(doctor_source: str) -> str:
    """Section 7 top-level code (not a function): the wiring between the two helpers above and
    Check/Add-Result, exactly as doctor.ps1 runs it."""
    start = doctor_source.index("# 7. Last background start (.setup\\logs\\start_all_summary.txt).")
    end = doctor_source.index('Write-Host ""', start)
    return doctor_source[start:end]


def _run_ps(tmp_path, body: str, timeout: int = 60) -> str:
    p = tmp_path / ("driver_%d.ps1" % len(list(tmp_path.glob("driver_*.ps1"))))
    p.write_text(body, encoding="utf-8")
    proc = childproc.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
        timeout=timeout, creationflags=childproc.headless_creationflags())
    assert proc.returncode == 0, (
        "driver powershell exited %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout


# ── Get-LastStartSummaryDoctor: parsing the file start_all.ps1 writes ───────────────────────

_FMT_HEADER = '''$out = [ordered]@{}
function Emit([string]$name, $value) { $out[$name] = $value }
'''

_FMT_FOOTER = '''
$out | ConvertTo-Json -Depth 6 -Compress
'''


def test_missing_file_returns_null(tmp_path, last_start_functions):
    missing = tmp_path / "does-not-exist.txt"
    body = last_start_functions + _FMT_HEADER + (
        "Emit 'r' (Get-LastStartSummaryDoctor '%s')\n" % missing) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is None, out


def test_empty_file_returns_null(tmp_path, last_start_functions):
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    body = last_start_functions + _FMT_HEADER + (
        "Emit 'r' (Get-LastStartSummaryDoctor '%s')\n" % p) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is None, out


def test_a_real_summary_is_parsed_field_for_field(tmp_path, last_start_functions):
    p = tmp_path / "start_all_summary.txt"
    p.write_bytes((
        "failures=2\n"
        "when=2026-09-20 08:15:30\n"
        "mode=background (-NoUi)\n"
        "- devtunnel is signed out, so the tunnel is not hosted: run 'devtunnel user login', then start again\n"
        "- CopilotChat: ui\\CopilotChat.exe is missing or empty and was not rebuilt -- run powershell -File ui\\rebuild_ui.ps1\n"
        "fix: run doctor.bat for the specific fix for each line\n"
    ).encode("utf-8"))
    body = last_start_functions + _FMT_HEADER + (
        "$r = Get-LastStartSummaryDoctor '%s'\n"
        "Emit 'failures' $r.Failures\n"
        "Emit 'when' ($r.When.ToString('yyyy-MM-dd HH:mm:ss'))\n"
        "Emit 'mode' $r.Mode\n"
        "Emit 'lines' @($r.Lines)\n" % p) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["failures"] == 2
    assert out["when"] == "2026-09-20 08:15:30"
    assert out["mode"] == "background (-NoUi)"
    assert out["lines"] == [
        "devtunnel is signed out, so the tunnel is not hosted: run 'devtunnel user login', then start again",
        "CopilotChat: ui\\CopilotChat.exe is missing or empty and was not rebuilt -- run powershell -File ui\\rebuild_ui.ps1",
    ]
    # The literal "fix: ..." trailer line is NOT one of the "- " items -- it must not appear
    # among the per-failure texts doctor would report.
    assert not any(ln.startswith("fix:") for ln in out["lines"])


def test_a_clean_run_parses_as_zero_failures_and_no_lines(tmp_path, last_start_functions):
    p = tmp_path / "start_all_summary.txt"
    p.write_bytes("failures=0\nwhen=2026-09-24 07:00:00\nmode=full\n".encode("utf-8"))
    body = last_start_functions + _FMT_HEADER + (
        "$r = Get-LastStartSummaryDoctor '%s'\n"
        "Emit 'failures' $r.Failures\n"
        "Emit 'lines' @($r.Lines)\n" % p) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["failures"] == 0
    assert out["lines"] == []


def test_an_unparsable_when_line_leaves_when_null_but_still_reads_everything_else(tmp_path, last_start_functions):
    p = tmp_path / "start_all_summary.txt"
    p.write_bytes("failures=1\nwhen=not-a-date\nmode=full\n- something broke\n".encode("utf-8"))
    body = last_start_functions + _FMT_HEADER + (
        "$r = Get-LastStartSummaryDoctor '%s'\n"
        "Emit 'when_is_null' ($null -eq $r.When)\n"
        "Emit 'failures' $r.Failures\n"
        "Emit 'lines' @($r.Lines)\n"
        "Emit 'filetime_is_null' ($null -eq $r.FileTime)\n" % p) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["when_is_null"] is True
    assert out["failures"] == 1
    assert out["lines"] == ["something broke"]
    # FileTime is the staleness fallback precisely for this case -- it must still be set.
    assert out["filetime_is_null"] is False


# ── Test-LastStartSummaryStale: the staleness decision ──────────────────────────────────────

@pytest.mark.parametrize("summary_offset_h,server_offset_h,expected", [
    (-1, None, False),      # an hour old, no server info -> not stale
    (-25, None, True),      # more than 24h old -> stale regardless of the server
    (-1, 0, True),          # server started AFTER the recorded start -> stale (predates the boot)
    (-1, -5, False),        # server has been up longer than the record is old -> not stale
    (0, None, False),       # exactly now -> not stale
])
def test_staleness_matrix(tmp_path, last_start_functions, summary_offset_h, server_offset_h, expected):
    body = last_start_functions + _FMT_HEADER + '''
$now = Get-Date "2026-09-24 12:00:00"
$summaryTime = $now.AddHours(%s)
%s
$r = Test-LastStartSummaryStale $summaryTime $serverTime $now
Emit "r" $r
''' % (
        summary_offset_h,
        ("$serverTime = $now.AddHours(%s)" % server_offset_h) if server_offset_h is not None
        else "$serverTime = $null",
    ) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is expected, out


def test_a_null_summary_time_is_never_stale(tmp_path, last_start_functions):
    body = last_start_functions + _FMT_HEADER + '''
$now = Get-Date
Emit "r" (Test-LastStartSummaryStale $null $now $now)
''' + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is False, out


# ── the "# 7." block itself: wired into Check/Add-Result the way doctor.ps1 really runs it ──

def _block_driver(add_result_fn: str, check_fn: str, block: str, repo_dir) -> str:
    # `block` (the "# 7." section, sliced from the live file) ALREADY contains the
    # Get-LastStartSummaryDoctor / Test-LastStartSummaryStale function definitions -- it is
    # the section header through to (not including) the next "Write-Host """ -- so only
    # Add-Result and Check (defined elsewhere in the file) need supplying here.
    # Real Write-Host (not the -Json shadow) so failures are visible in the driver's own
    # stdout/stderr if something throws; only $script:results is asserted on.
    return (
        add_result_fn + "\n\n" + check_fn + "\n\n" +
        "$script:results = @(); $script:ok = 0; $script:bad = 0; $script:warn = 0; $script:unknown = 0\n"
        "$repo = '%s'\n" % repo_dir +
        block + "\n" +
        "$script:results | ConvertTo-Json -Depth 6 -Compress\n"
    )


@pytest.fixture(scope="module")
def add_result_fn(doctor_source: str) -> str:
    return _extract_braced_block(doctor_source, "function Add-Result")


@pytest.fixture(scope="module")
def check_fn(doctor_source: str) -> str:
    return _extract_braced_block(doctor_source, "function Check(")


def _results(tmp_path, add_result_fn, check_fn, last_start_block, repo_dir):
    body = _block_driver(add_result_fn, check_fn, last_start_block, repo_dir)
    out = _run_ps(tmp_path, body)
    # ConvertTo-Json -Compress emits ONE line for an array with >1 items, but a single-item
    # array collapses to a bare object (same PowerShell quirk the -Json mode itself works
    # around, see doctor.ps1's own comment above its final ConvertTo-Json call) -- normalise here.
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    parsed = json.loads(lines[-1])
    return parsed if isinstance(parsed, list) else [parsed]


def test_block_reports_info_when_no_summary_was_ever_recorded(
        tmp_path, add_result_fn, check_fn, last_start_block):
    repo_dir = tmp_path / "repo_missing"
    repo_dir.mkdir()
    results = _results(tmp_path, add_result_fn, check_fn, last_start_block, repo_dir)
    assert len(results) == 1
    r = results[0]
    assert r["id"] == "last_start"
    assert r["info"] is True
    assert r["ok"] is False
    assert "no background start has been recorded" in r["fix"]


def test_block_reports_one_fail_per_recorded_failure_with_start_alls_own_text(
        tmp_path, add_result_fn, check_fn, last_start_block):
    repo_dir = tmp_path / "repo_failed"
    logs = repo_dir / ".setup" / "logs"
    logs.mkdir(parents=True)
    when = "2026-09-24 06:00:00"  # recent -> not stale
    text = (
        "failures=2\n"
        "when=%s\n"
        "mode=background (-NoUi)\n"
        "- devtunnel is signed out, so the tunnel is not hosted: run 'devtunnel user login', then start again\n"
        "- bridge: :8765 held by pid 4242 and not serving /conv\n"
    ) % when
    (logs / "start_all_summary.txt").write_bytes(text.encode("utf-8"))
    results = _results(tmp_path, add_result_fn, check_fn, last_start_block, repo_dir)
    assert [r["id"] for r in results] == ["last_start_1", "last_start_2"]
    for r in results:
        assert r["ok"] is False
        assert r["info"] is False
        assert when in r["name"]
        assert "background (-NoUi)" in r["name"]
    assert results[0]["fix"] == (
        "devtunnel is signed out, so the tunnel is not hosted: run 'devtunnel user login', then start again")
    assert results[1]["fix"] == "bridge: :8765 held by pid 4242 and not serving /conv"
    assert "may be stale" not in results[0]["name"]


def test_block_reports_ok_with_no_problems_when_the_last_start_was_clean(
        tmp_path, add_result_fn, check_fn, last_start_block):
    repo_dir = tmp_path / "repo_clean"
    logs = repo_dir / ".setup" / "logs"
    logs.mkdir(parents=True)
    (logs / "start_all_summary.txt").write_bytes(
        b"failures=0\nwhen=2026-09-24 06:00:00\nmode=full\n")
    results = _results(tmp_path, add_result_fn, check_fn, last_start_block, repo_dir)
    assert len(results) == 1
    assert results[0]["id"] == "last_start"
    assert results[0]["ok"] is True
    assert results[0]["info"] is False


def test_block_flags_a_stale_record_even_when_it_was_clean(
        tmp_path, add_result_fn, check_fn, last_start_block):
    repo_dir = tmp_path / "repo_stale"
    logs = repo_dir / ".setup" / "logs"
    logs.mkdir(parents=True)
    (logs / "start_all_summary.txt").write_bytes(
        b"failures=0\nwhen=2026-09-20 06:00:00\nmode=full\n")  # far more than 24h before "now"
    results = _results(tmp_path, add_result_fn, check_fn, last_start_block, repo_dir)
    assert len(results) == 1
    assert results[0]["ok"] is True  # still OK -- zero failures were recorded
    assert "may be stale" in results[0]["name"]


# ── Test-IdentifyingTunnelName / Test-GeneratedTunnelNameDoctor: the D29 mirror ─────────────

def test_a_generated_name_is_not_flagged_even_when_username_is_a_substring(
        tmp_path, identifying_name_functions):
    """The exact D29 scenario: USERNAME 'pan' occurs inside 'm365-copilot-companion-<hex>'
    purely by coincidence (it is a substring of 'companion'), and the name was generated by
    this repo's own tooling, not typed by the user -- it must not be flagged."""
    body = identifying_name_functions + _FMT_HEADER + '''
$repo = "C:\\nonexistent-repo-dir-for-this-test"
$env:USERNAME = "pan"
Emit "r" (Test-IdentifyingTunnelName "m365-copilot-companion-12ab34cd")
''' + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is False, out


def test_a_custom_name_containing_the_username_is_still_flagged(tmp_path, identifying_name_functions):
    """The exemption must not swallow a REAL leak: a hand-picked name that happens to start
    with the username is exactly what this check exists to catch."""
    body = identifying_name_functions + _FMT_HEADER + '''
$repo = "C:\\nonexistent-repo-dir-for-this-test"
$env:USERNAME = "pan"
Emit "r" (Test-IdentifyingTunnelName "pan-personal-tunnel")
''' + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is True, out


@pytest.mark.parametrize("suffix_len", [6, 8])
def test_generated_name_detector_accepts_both_suffix_lengths(tmp_path, identifying_name_functions, suffix_len):
    body = identifying_name_functions + _FMT_HEADER + (
        'Emit "r" (Test-GeneratedTunnelNameDoctor "m365-copilot-companion-%s")\n'
        % ("a" * suffix_len)
    ) + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is True, out


def test_generated_name_detector_rejects_a_lookalike_with_extra_text(tmp_path, identifying_name_functions):
    body = identifying_name_functions + _FMT_HEADER + '''
Emit "r" (Test-GeneratedTunnelNameDoctor "m365-copilot-companion-pan-12ab34cd")
''' + _FMT_FOOTER
    out = json.loads(_run_ps(tmp_path, body))
    assert out["r"] is False, out
