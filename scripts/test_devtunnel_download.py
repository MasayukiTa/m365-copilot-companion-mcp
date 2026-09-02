"""The devtunnel download must be able to give up, and must not look dead while it works.

STEP 4/7 was reported stuck on a fresh machine with the last line
"winget unavailable/failed -> DIRECT DOWNLOAD (no winget needed)..." and nothing after it.

Three faults, and the third is the one that made it unreportable:

  1. `-TimeoutSec` on Invoke-WebRequest is a PER-READ timeout, not a total cap. A connection
     that is slow but alive never trips it, so the step could hang indefinitely while a timeout
     was apparently protecting it.
  2. $ProgressPreference was unset, and PowerShell 5.1 draws a progress bar per chunk for
     -OutFile -- the documented 10-50x slowdown on Invoke-WebRequest.
  3. Nothing was printed between the banner and success, so "slow" and "dead" were
     indistinguishable to the person watching.

curl.exe (Windows 10 1803+) is tried first: --max-time is a real total cap, it follows the
aka.ms redirect and uses the system proxy. Measured on the corporate link: 22.7 MB in 27.2s,
first two bytes MZ.
"""
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, "scripts", "setup_devtunnel.ps1")


def _src():
    return io.open(PS1, encoding="utf-8-sig").read()


def test_the_download_has_a_total_time_cap():
    # The property that was missing. -TimeoutSec is per-read; --max-time is the whole transfer.
    src = _src()
    assert "--max-time" in src, "no total cap on the download; -TimeoutSec alone cannot end a hang"
    m = re.search(r"--max-time\s+(\d+)", src)
    assert m and int(m.group(1)) <= 600, "the cap is too long to help someone watching a stuck screen"


def test_curl_is_tried_before_invoke_webrequest():
    src = _src()
    assert "curl.exe" in src
    assert src.index("curl.exe") < src.index("Invoke-WebRequest"), (
        "Invoke-WebRequest runs first, so the slow path is still the default one")


def test_the_progress_bar_is_disabled_around_the_download():
    # This is a speed fix, not cosmetics: the per-chunk progress render is the 10-50x penalty.
    src = _src()
    assert "$ProgressPreference = 'SilentlyContinue'" in src


def test_the_progress_preference_is_restored():
    # Leaving it off would silently change every later command in the same session.
    src = _src()
    assert "$prevProgress" in src and "finally" in src, (
        "ProgressPreference is not restored; a failure mid-download would leak the setting")


def test_it_says_something_while_it_works():
    # "slow" and "dead" must not look the same. The operator reported it as stuck precisely
    # because there was nothing between the banner and success.
    src = _src()
    assert "downloading devtunnel" in src
    assert re.search(r"downloaded .*MB", src), "no completion line with a size"


def test_it_tells_the_operator_how_long_to_wait():
    src = _src()
    assert re.search(r"more than ~?\d+ minutes", src), (
        "nothing tells the person watching when to stop waiting")


def test_a_failed_download_still_refuses_a_non_binary():
    # Unchanged property, re-checked because the download path was rewritten: a proxy block page
    # arrives as HTTP 200 with HTML, and renaming that to devtunnel.exe is how a machine ends up
    # with a PATH entry pointing at a login page.
    src = _src()
    assert "0x4D" in src and "0x5A" in src, "the MZ check on the payload is gone"


def test_curl_follows_redirects():
    # The URL is an aka.ms shortlink; without -L curl saves the redirect stub, which would then
    # fail the MZ check and look like a corporate block.
    # THE INVOCATION LINE, not a window after the first mention of curl. The first version
    # searched 600 characters after "curl.exe", which a comment above the call pushed the real
    # command out of -- the test failed while the code was correct, which is its own defect.
    src = _src()
    calls = [ln for ln in src.splitlines() if "& $curl " in ln]
    assert calls, "no invocation of $curl found"
    assert all("-L" in ln for ln in calls), (
        "curl is invoked without -L, so the aka.ms redirect stub is saved instead of the binary "
        "and the MZ check then reports it as a block: %s" % calls)


# ---------------------------------------------------------- failing with evidence


def test_the_failure_reports_what_each_attempt_said():
    # It used to print "Your company network blocked this download" for EVERY cause, discarding
    # curl's exit code and Invoke-WebRequest's exception. A certificate error, a 407, a DNS
    # failure and a real block were reported identically -- and the operator was told to ask IT
    # for a file, which is the wrong remedy for most of them and costs somebody else a day.
    src = _src()
    assert "$dlWhy" in src, "nothing collects why the attempts failed"
    assert "What each attempt reported" in src, "the failure does not print its evidence"


def test_the_diagnosis_is_not_a_single_assertion():
    src = _src()
    for cause in ("Proxy Authentication", "could not resolve", "certificate"):
        assert cause in src, "no branch for %s; the message would assert one cause for all" % cause


def test_it_no_longer_asserts_a_block_unconditionally():
    src = _src()
    i = src.index("ERROR: could not download devtunnel")
    tail = src[i:i + 3000]
    # The phrase may still appear as one branch among several, but it must not be the only path.
    assert tail.count("elseif") >= 3, "the diagnosis is still effectively unconditional"


def test_the_timeout_branch_is_not_matched_by_any_stray_28():
    # `-match '28'` hits a byte count, a port, or a timestamp. Anchored to curl's own form.
    src = _src()
    assert "-match 'timed out|Timeout|28'" not in src, "bare 28 still matches any number"
    assert "curl exit 28" in src or r"curl: \(28\)" in src


def test_a_third_transport_is_tried_before_giving_up():
    # BITS reads the WinHTTP proxy configuration, which is where a managed machine's proxy
    # actually lives; .NET and curl read different places.
    src = _src()
    assert "Start-BitsTransfer" in src


def test_the_manual_way_out_names_the_url():
    # Telling somebody to "download devtunnel" without the URL makes them search for it.
    src = _src()
    i = src.index("MANUAL WAY OUT")
    assert "$dlUri" in src[i:i + 800]
