# wsh_vbs_check.ps1 -- "will wscript.exe run a .vbs on this machine at all?"
#
# SPLIT OUT of preflight_policy.ps1 (2026-09-25, scripts/test_start_all_ten_clicks.py). start_
# all.bat calls this -- via -CheckWshOnly, see below -- on EVERY run, before the single-instance
# lock and the leave/wait/run decision even happen, to pick which of two launch mechanisms to
# use (the hidden wscript.exe path, or the direct-PowerShell fallback). Ten start_all.bat clicks
# at once used to mean ten concurrent `powershell -File preflight_policy.ps1 -CheckWshOnly`
# cold-starts, EACH parsing that file's ~300 lines (Get-PolicyFindings, Get-MotwCount, the whole
# setup.bat-only policy-report machinery this check never uses) before ever reaching the two
# functions it actually needed. Measured: that parse-and-registry-probe cost, times ten at once,
# was real CPU/IO contention that pushed an unrelated LEAVING copy's own internal timing past
# LEAVE_BOUND_SEC (scripts/test_start_all_ten_clicks.py) even though that copy never calls this
# script itself. A first attempt fixed this by caching the answer to a temp file instead --
# measured WORSE on the actual CI runner (leaver_max_s regressed further, 3.86-4.42s), most
# likely the cache file's own I/O (write + Get-Item + Get-Content, ten times, contending on one
# path) costing more than it saved on that runner. This file is the other fix: make the ten
# concurrent processes have less to parse and less to do, rather than trying to make nine of
# them do nothing. preflight_policy.ps1 (setup.bat's own, much less frequent, full policy check)
# dot-sources this file so the registry paths stay in exactly one place, as before.
#
# PREFLIGHT_TEST_WSH_ENABLED / PREFLIGHT_TEST_VBS_ENGINE (tests only): force the answer so a
# test can exercise both branches without touching the machine's real registry -- same
# convention preflight_policy.ps1 always used, unchanged by the split.

function Test-WshEnabled {
    # Enabled=0 (string or DWORD) under either hive disables wscript for this user.
    if ($env:PREFLIGHT_TEST_WSH_ENABLED -eq '0') { return $false }
    if ($env:PREFLIGHT_TEST_WSH_ENABLED -eq '1') { return $true }
    foreach ($k in @('HKLM:\SOFTWARE\Microsoft\Windows Script Host\Settings',
                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Script Host\Settings',
                     'HKCU:\SOFTWARE\Microsoft\Windows Script Host\Settings')) {
        $p = Get-ItemProperty -LiteralPath $k -Name Enabled -ErrorAction SilentlyContinue
        if ($p -and ([string]$p.Enabled).Trim() -eq '0') { return $false }
    }
    return $true
}

function Test-VbsEngineAvailable {
    # START-?? (new-PC review, 2026-09-24): wscript.exe itself can start fine, with WSH fully
    # "Enabled", and still have NOTHING able to run a .vbs -- some Windows 11 builds ship with
    # the VBScript engine removed/deprecated (an optional Windows feature) while leaving the
    # WSH Enabled registry key untouched. In that state wscript.exe opens a MODAL dialog
    # ("script engine ... not registered" / Windows Script Host cannot find a script engine for
    # ".vbs") and BLOCKS until someone clicks OK -- one modal per double-click of start_all.bat,
    # never an error code, never anything on the WSH Enabled key Test-WshEnabled reads.
    #
    # Checked the same way Windows itself resolves a .vbs double-click, entirely through the
    # registry (COM class lookup, no process, no UI, cannot show a dialog): .vbs's ProgID ->
    # that ProgID's ScriptEngine name (defaults to "VBScript" when the key is absent, as stock
    # Windows leaves it) -> that engine name's CLSID -> that CLSID's InprocServer32 DLL path ->
    # the DLL file actually exists on disk. Any missing link means nothing will run a .vbs.
    #
    # PREFLIGHT_TEST_VBS_ENGINE (tests only): forces the answer, same convention as
    # PREFLIGHT_TEST_WSH_ENABLED, so a test can exercise both branches without depending on
    # whether the machine running the test happens to have the engine installed.
    if ($env:PREFLIGHT_TEST_VBS_ENGINE -eq '0') { return $false }
    if ($env:PREFLIGHT_TEST_VBS_ENGINE -eq '1') { return $true }
    try {
        $ext = Get-Item -LiteralPath 'Registry::HKEY_CLASSES_ROOT\.vbs' -ErrorAction Stop
        $progId = [string]$ext.GetValue('')
        if (-not $progId) { return $false }
        $engine = 'VBScript'
        $engineKey = Get-Item -LiteralPath ('Registry::HKEY_CLASSES_ROOT\' + $progId + '\ScriptEngine') -ErrorAction SilentlyContinue
        if ($engineKey) {
            $named = [string]$engineKey.GetValue('')
            if ($named) { $engine = $named }
        }
        $clsidKey = Get-Item -LiteralPath ('Registry::HKEY_CLASSES_ROOT\' + $engine + '\CLSID') -ErrorAction SilentlyContinue
        if (-not $clsidKey) { return $false }
        $clsid = [string]$clsidKey.GetValue('')
        if (-not $clsid) { return $false }
        $dllKey = Get-Item -LiteralPath ('Registry::HKEY_CLASSES_ROOT\CLSID\' + $clsid + '\InprocServer32') -ErrorAction SilentlyContinue
        if (-not $dllKey) { return $false }
        $dll = ([string]$dllKey.GetValue('')).Trim('"')
        if (-not $dll) { return $false }
        $dll = [System.Environment]::ExpandEnvironmentVariables($dll)
        return (Test-Path -LiteralPath $dll -PathType Leaf)
    } catch { return $false }
}

function Test-CanRunVbs {
    # The one question every caller actually has: will `wscript.exe foo.vbs` do the intended
    # thing without popping a modal or silently no-op'ing? Both halves must hold.
    return (Test-WshEnabled) -and (Test-VbsEngineAvailable)
}

# ---- entry point (only when run directly, e.g. `-File wsh_vbs_check.ps1 -CheckWshOnly` from
# start_all.bat) -- SKIPPED when dot-sourced (preflight_policy.ps1 dot-sources this file with no
# arguments of its own, so $args is empty in that context regardless of what preflight_policy.ps1
# itself was called with). ----
if ($args -contains '-CheckWshOnly') {
    # START-16, 2026-09-24: start_all.bat (the DAILY launcher, run long after setup.bat's own
    # preflight already warned-and-continued past a disabled WSH) calls this on every run to
    # decide whether wscript.exe's hidden launcher can be trusted at all. The output name
    # (WSH-ENABLED) is the established contract every caller (start_all.bat,
    # make_desktop_shortcut.ps1, register-supervisor.ps1) greps for; it also covers a missing
    # VBScript engine (Test-CanRunVbs), a second, different way wscript can fail to do anything.
    Write-Output ('WSH-ENABLED=' + [int](Test-CanRunVbs))
    exit 0
}
