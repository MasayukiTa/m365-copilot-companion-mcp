# -*- coding: utf-8 -*-
r"""First-run bootstrap on a machine behind a TLS-intercepting proxy.

THE OBSERVED FAILURE, on a fresh machine with no Python:

    Using uv at ".setup\bin\uv.exe" to provision Python and .venv (no admin) ...
    error: Failed to install cpython-3.12.14-windows-x86_64-none
      Caused by: invalid peer certificate: UnknownIssuer

uv is a Rust binary carrying its OWN root certificates (rustls / webpki-roots); it does not
read the Windows certificate store, where the intercepting proxy's CA lives. The tell is that
PowerShell's Invoke-RestMethod SUCCEEDS on the same network in the same script -- so uv
installs fine and then cannot fetch a Python.

This repository already knew about the interception: scripts/setup.ps1 works around it for pip
with --trusted-host. The newer uv path never inherited that knowledge -- a guard that exists on
one path and not the one being taken.

EXPORTING BEATS DISABLING. --trusted-host turns verification OFF for those hosts. Exporting the
roots this machine already trusts keeps verification ON and helps every tool that reads a PEM.
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = io.open(os.path.join(REPO, "setup.bat"), encoding="ascii").read()
PS1 = io.open(os.path.join(REPO, "scripts", "ca_bundle.ps1"), encoding="utf-8").read()


# -- the .bat rules this repository already has ---------------------------------------------

def test_setup_bat_is_ascii_only():
    """A cp932 console mangles non-ASCII in a .bat and it has broken things here before."""
    raw = io.open(os.path.join(REPO, "setup.bat"), "rb").read()
    assert not [b for b in raw if b > 127]


# -- the fix ---------------------------------------------------------------------------------

def test_uv_is_told_to_use_the_platform_certificates():
    """UV_NATIVE_TLS makes uv read the Windows store instead of its bundled roots. Set before
    uv runs, and on BOTH branches -- the export can fail and uv must still get the flag."""
    assert BAT.count("UV_NATIVE_TLS=1") >= 2


def test_the_bundle_is_exported_before_uv_is_invoked():
    assert BAT.index("ca_bundle.ps1") < BAT.index('"!UVEXE!" python install')


def test_every_fetcher_that_reads_a_pem_is_pointed_at_it():
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        assert var in BAT, var


def test_an_operators_existing_setting_is_never_clobbered():
    """SSL_CERT_FILE is load-bearing elsewhere on this machine -- one tool needs it set and
    another needs it unset. Overwriting a value the operator chose would break something that
    was working."""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        assert re.search(r"if not defined %s set" % var, BAT), var


def test_the_python_install_exit_code_is_checked():
    """It was not. A failed download fell straight through to `uv venv`, which failed for the
    same reason, and the operator was shown one message about the second failure and none
    about the first."""
    i = BAT.index('"!UVEXE!" python install')
    assert "if errorlevel 1" in BAT[i:i + 400]


# -- what the operator is told ----------------------------------------------------------------

def test_the_failure_names_the_certificate_cause():
    assert "UnknownIssuer" in BAT
    assert "inspects TLS" in BAT or "intercept" in BAT.lower()


def test_it_no_longer_suggests_a_command_that_cannot_work():
    """The old message said to run `uv venv .venv` by hand. That fails identically -- it still
    has to download a Python over the same connection. An instruction that cannot work costs
    the operator another cycle to discover it does nothing."""
    assert "will NOT help" in BAT


def test_the_manual_route_is_documented():
    """Reading the Windows stores covers the normal case and is not guaranteed: a machine that
    is not domain-joined, or a CA distributed another way, can leave nothing to find."""
    assert "ca-extra.pem" in BAT
    assert "ca-extra.pem" in PS1


# -- the exporter -------------------------------------------------------------------------------

def test_it_reads_both_scopes_and_both_kinds_of_store():
    r"""A proxy chain is commonly a root in LocalMachine\Root with an issuing intermediate in
    LocalMachine\CA. Exporting only roots produces a bundle that cannot complete the chain and
    fails identically to having none."""
    for store in ("LocalMachine\Root", "LocalMachine\CA",
                  "CurrentUser\Root", "CurrentUser\CA"):
        assert store in PS1, store


def test_it_refuses_to_write_a_bundle_that_would_reject_everything():
    """A near-empty PEM is accepted by every consumer and then rejects every certificate,
    which reads as a network fault rather than as a broken bundle."""
    assert "refusing to write a bundle" in PS1


def test_expired_roots_are_skipped():
    assert "NotAfter" in PS1


def test_a_der_certificate_is_accepted_not_rejected():
    """Handing someone a file back and telling them it is the wrong encoding is not help."""
    assert "X509Certificate2" in PS1


def test_the_bundle_cannot_be_committed():
    """It contains certificate subjects, which name the organisation."""
    ignore = io.open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read()
    assert ".setup/" in ignore


def test_nothing_in_the_exporter_names_an_organisation():
    """Discovered, not configured: it reads whatever the machine trusts, so it also works on a
    machine with no interception."""
    # VENDOR AND ORGANISATION NAMES, not the generic adjective. The first version of this
    # test matched "corp", which fired on the word "corporate" in a comment -- a description of
    # the situation, not a name. A check that cannot tell a category from an identifier flags
    # the documentation and misses the leak.
    low = PS1.lower()
    for word in ("zscaler", "bluecoat", "netskope", "forcepoint", "paloalto", "fortinet"):
        assert word not in low, word
    # nothing that looks like a specific host or an internal domain
    assert not re.search(r"https?://(?!learn\.microsoft|www\.python)", low)


def test_the_certificates_are_set_up_for_every_route_not_just_uv():
    """A machine that already has Python skips the uv branch entirely. Leaving the bundle
    inside that branch would be the same 'guard on one path only' shape as the original
    defect -- pip has --trusted-host, but anything else bootstrap.py fetches would not."""
    tls = BAT.index("ca_bundle.ps1")
    first_route = BAT.index('if exist ".venv\Scripts\python.exe"')
    assert tls < first_route, "the TLS setup runs after a route can already have been taken"


def test_it_still_runs_before_uv():
    assert BAT.index("ca_bundle.ps1") < BAT.index('"!UVEXE!" python install')


# -- the failure that surfaced once TLS was fixed --------------------------------------------

def test_the_venv_is_created_with_pip():
    """`uv venv` does NOT seed pip. Without --seed it produces a perfectly good venv with no
    pip, the bootstrap's health probe reads that as "broken", tries to DELETE it, fails with
    WinError 5, and tells the operator to remove .venv by hand -- for a venv that was
    working."""
    assert "venv --seed .venv" in BAT


def test_both_spellings_of_the_system_certs_flag_are_set():
    """Newer uv deprecates UV_NATIVE_TLS in favour of UV_SYSTEM_CERTS and warns about it;
    older uv does not know the new name. Setting both keeps either version working."""
    assert BAT.count("UV_SYSTEM_CERTS=1") >= 2
    assert BAT.count("UV_NATIVE_TLS=1") >= 2


def test_a_pip_less_venv_is_repaired_rather_than_destroyed():
    """Destroying a working venv over a missing package is disproportionate, and it is what
    ended the run on a fresh machine. Healthy means the interpreter RUNS."""
    src = io.open(os.path.join(REPO, "scripts", "bootstrap.py"), encoding="utf-8").read()
    assert "_seed_pip" in src and "ensurepip" in src
    i = src.index("def _venv_is_healthy")
    body = src[i:i + 1400]
    assert "_venv_runs()" in body, "health is no longer decided by whether the interpreter runs"
    assert "_seed_pip()" in body, "a pip-less venv is no longer repaired"


def test_removing_a_venv_survives_read_only_files():
    """The delete failed with WinError 5. Windows marks files read-only and rmtree cannot
    unlink those -- the same defect already fixed once in the benchmark's worktree cleanup."""
    src = io.open(os.path.join(REPO, "scripts", "bootstrap.py"), encoding="utf-8").read()
    i = src.index('shutil.rmtree(ROOT / ".venv"')
    assert "onerror=" in src[i:i + 120]
    assert "S_IWRITE" in src


# -- the window that vanishes ------------------------------------------------------------------

def _boot():
    return io.open(os.path.join(REPO, "scripts", "bootstrap.py"), encoding="utf-8").read()


def test_everything_printed_is_also_written_to_a_transcript():
    """An operator on a fresh machine reported "the command prompt fell over and re-running
    still dies partway", and there was nothing to read -- the window was gone. Every exit path
    in quickstart.bat and setup.bat pauses, so a clean failure HOLDS the window; what they saw
    was an abnormal termination, which is exactly the case where the on-screen output is the
    only record and is lost. Two rounds were then spent guessing."""
    src = _boot()
    i = src.index("def log(msg: str)")
    assert "_transcribe(msg)" in src[i:i + 900]
    assert "TRANSCRIPT" in src


def test_an_unhandled_crash_reaches_the_transcript():
    """ActionNeeded and StepError already go through log(). A crash does not -- and a crash is
    precisely what leaves someone saying "it fell over" with no evidence."""
    src = _boot()
    assert "sys.excepthook" in src
    assert "UNHANDLED" in src
    assert "format_exception" in src


def test_the_transcript_path_is_announced_before_anything_can_fail():
    """Printed at the end, a crash never reaches it."""
    src = _boot()
    i = src.index('if __name__ == "__main__"')
    tail = src[i:]
    assert tail.index("Transcript:") < tail.index("sys.exit(main())")


def test_the_transcript_cannot_kill_the_run_it_records():
    """A logging call has taken a run down in this project before -- a cp932 console and a
    print() that raised UnicodeEncodeError, eight minutes into a forty-instance benchmark."""
    src = _boot()
    i = src.index("def _transcribe")
    body = src[i:i + 700]
    assert "except Exception:" in body and "pass" in body


def test_the_console_write_is_encode_safe():
    """The transcript is written whatever the console can display, so a character the console
    cannot show is still recorded rather than ending the run."""
    src = _boot()
    i = src.index("def log(msg: str)")
    body = src[i:i + 900]
    assert "replace" in body and "sys.stdout" in body
