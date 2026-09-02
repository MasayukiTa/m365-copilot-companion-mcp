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
    src = _src()
    i = src.index("curl.exe")
    window = src[i:i + 600]
    assert " -L " in window or window.count("-L") >= 1, "curl is not following the aka.ms redirect"
