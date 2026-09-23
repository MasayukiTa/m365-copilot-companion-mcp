# preflight_policy.ps1 -- can this machine run this project's PowerShell and VBScript at all?
#
# WHY (D18 in the new-PC install review, 2026-09-24). Every launcher here runs
# `powershell -ExecutionPolicy Bypass -File ...` or `wscript ...vbs`, and four things a managed
# PC commonly has defeat that without a word:
#   * a Group-Policy execution policy (MachinePolicy / UserPolicy scope). It OVERRIDES the
#     -ExecutionPolicy Bypass on the command line, so AllSigned or Restricted refuses every
#     script here, and RemoteSigned refuses every script carrying...
#   * ...Mark-of-the-Web: the Zone.Identifier stream Windows puts on every file extracted from
#     a downloaded ZIP (the README tells people to use GitHub's "Download ZIP"). It also makes
#     wscript ask "Do you want to run this file?" in front of the hidden launcher;
#   * Constrained Language Mode (AppLocker / WDAC application control): scripts run, but the
#     supervisor's named mutex and the Windows Forms dialogs are refused at runtime;
#   * Windows Script Host disabled: start_all.bat, the Desktop launcher and logon autostart all
#     go through wscript and simply do nothing.
# None of these was detected anywhere (git grep Unblock-File|MachinePolicy|LanguageMode: none),
# so an install on such a PC failed later, elsewhere, and looked like something else.
#
# HOW IT IS RUN, AND WHY THAT WAY. setup.bat runs this file through Invoke-Expression from a
# -Command string, not with -File: an execution policy governs script FILES, so a -File launch
# of the very script meant to diagnose a blocked -File launch would be blocked by it. From
# inside, it then runs a real `-File` probe (this same file with -ProbeOnly), which is the
# ground truth: exactly the launch every other script here depends on.
#
#   powershell -NoProfile -Command "$PreflightRoot = (Get-Location).Path; iex (Get-Content -Raw -LiteralPath 'scripts\preflight_policy.ps1')"
#
# Only cmdlets, string operators and hashtables are used, so it runs unchanged in Constrained
# Language Mode -- the mode it has to be able to report.
#
# EXIT CODES (setup.bat branches on them):
#   0  nothing in the way
#   3  files carry Mark-of-the-Web but scripts still run here -> OFFER to unblock
#   4  scripts are REFUSED because of Mark-of-the-Web -> unblocking is required
#   1  blocked by something setup cannot fix (policy / language mode) -> stop, message says why
#
# ASCII / ENGLISH ONLY.

function Get-PolicyFindings {
    # PURE: every input is a value, so the decision is testable without the machine state.
    param(
        [bool]$ProbeRan,             # the -File probe executed at all
        [string]$ProbeLanguage,      # LanguageMode the probe reported ('' when it did not run)
        [string]$ProbeError,         # what powershell printed when the probe was refused
        [string]$MachinePolicy,      # Get-ExecutionPolicy -Scope MachinePolicy
        [string]$UserPolicy,         # Get-ExecutionPolicy -Scope UserPolicy
        [int]$MotwCount,             # script files carrying a Zone.Identifier stream
        [bool]$WshEnabled,
        [string]$Root
    )
    $findings = @()
    $gpo = ''
    $gpoScope = ''
    foreach ($pair in @(@('MachinePolicy', $MachinePolicy), @('UserPolicy', $UserPolicy))) {
        $v = [string]$pair[1]
        if ($v -and $v -ne 'Undefined' -and -not $gpo) { $gpo = $v; $gpoScope = $pair[0] }
    }
    $unblockCmd = "powershell -NoProfile -Command ""Get-ChildItem -LiteralPath '" + $Root + "' -Recurse -File | Unblock-File"""

    if (-not $ProbeRan) {
        if ($MotwCount -gt 0 -and ($gpo -eq 'RemoteSigned' -or $ProbeError -match 'not digitally signed|cannot be loaded')) {
            $findings += @{ Id = 'motw-blocking'; Code = 4; Lines = @(
                "BLOCKED: Windows refuses to run this project's PowerShell scripts, because the",
                "files carry the 'downloaded from the internet' mark (Mark-of-the-Web, $MotwCount script file(s))",
                "and this PC's policy ($(if ($gpo) { "Group Policy $gpoScope = $gpo" } else { 'execution policy' })) refuses marked scripts.",
                "NEXT STEP: let setup remove the mark from this folder's files (answer Y below), or run:",
                "    $unblockCmd",
                "and then run setup.bat again.") }
        } elseif ($gpo -eq 'AllSigned' -or $gpo -eq 'Restricted') {
            $findings += @{ Id = 'gpo-policy'; Code = 1; Lines = @(
                "BLOCKED: Group Policy ($gpoScope) sets the PowerShell execution policy to $gpo.",
                "A Group-Policy setting overrides '-ExecutionPolicy Bypass', and this project's",
                "scripts are not signed, so none of them can run on this PC.",
                "NEXT STEP: ask your IT department to allow local scripts for your account (the",
                "policy 'Turn on Script Execution' set to 'Allow local scripts and remote signed",
                "scripts'), or to exempt this folder: $Root",
                "Then run setup.bat again.") }
        } else {
            $err = ($ProbeError -replace '\s+', ' ').Trim()
            if ($err.Length -gt 300) { $err = $err.Substring(0, 300) + '...' }
            $findings += @{ Id = 'probe-refused'; Code = 1; Lines = @(
                "BLOCKED: a test PowerShell script in this folder could not be run.",
                "PowerShell said: $err",
                "NEXT STEP: send that line to your IT department -- this project runs its setup",
                "with 'powershell -ExecutionPolicy Bypass -File <script>' from $Root",
                "and needs that to be allowed. Then run setup.bat again.") }
        }
    } elseif ($ProbeLanguage -and $ProbeLanguage -ne 'FullLanguage') {
        $findings += @{ Id = 'clm'; Code = 1; Lines = @(
            "BLOCKED: PowerShell runs this project's scripts in $ProbeLanguage mode.",
            "That is an application-control policy (AppLocker or WDAC). Scripts start, but the",
            "background supervisor cannot create its lock and the setup dialogs cannot load, so",
            "the install would fail later with errors that point elsewhere.",
            "NEXT STEP: ask your IT department to allow PowerShell scripts under",
            "    $Root",
            "(an AppLocker/WDAC path or publisher rule), then run setup.bat again.",
            "To try anyway, set SETUP_IGNORE_POLICY=1 in this window and re-run.") }
    } elseif ($MotwCount -gt 0) {
        $findings += @{ Id = 'motw-present'; Code = 3; Lines = @(
            "NOTE: $MotwCount script file(s) here carry the 'downloaded from the internet' mark",
            "(Mark-of-the-Web). PowerShell accepts them on this PC today, but Windows may ask",
            "'Do you want to run this file?' in front of the hidden launcher, and a later policy",
            "change would block them. Removing the mark is safe for files you chose to run:",
            "    $unblockCmd") }
    }

    if (-not $WshEnabled) {
        $findings += @{ Id = 'wsh-disabled'; Code = 0; Lines = @(
            "WARNING: Windows Script Host (wscript) is disabled on this PC. start_all.bat, the",
            "Desktop launcher and logon autostart all start through it, so they will do NOTHING",
            "(no window, no error). Setup itself does not need it and continues.",
            "NEXT STEP: ask IT to enable Windows Script Host for your account, or start the",
            "stack directly with:",
            "    powershell -NoProfile -ExecutionPolicy Bypass -File ""$Root\scripts\start_all.ps1""") }
    }
    return ,$findings
}

function Get-PreflightExitCode {
    param($Findings)
    $codes = @($Findings | ForEach-Object { [int]$_.Code })
    if ($codes -contains 1) { return 1 }
    if ($codes -contains 4) { return 4 }
    if ($codes -contains 3) { return 3 }
    return 0
}

function Get-MotwCount {
    # Script files only: that is what a policy judges and what the launchers run. The whole
    # tree (thousands of .py files, and .venv) would be slow and changes no decision.
    param([string]$Root)
    $n = 0
    $files = @(Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match '^\.(bat|cmd|ps1|vbs)$' })
    $files += @(Get-ChildItem -LiteralPath (Join-Path $Root 'scripts') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match '^\.(bat|cmd|ps1|vbs)$' })
    foreach ($f in $files) {
        $s = Get-Item -LiteralPath $f.FullName -Stream 'Zone.Identifier' -ErrorAction SilentlyContinue
        if ($s) { $n++ }
    }
    return $n
}

function Test-WshEnabled {
    # Enabled=0 (string or DWORD) under either hive disables wscript for this user.
    # PREFLIGHT_TEST_WSH_ENABLED (tests only): forces the answer so a test can exercise both
    # branches without touching this machine's real Windows Script Host registry keys.
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

# ---- entry points --------------------------------------------------------------------------
if ($args -contains '-CheckWshOnly') {
    # START-16, 2026-09-24: start_all.bat (the DAILY launcher, run long after setup.bat's own
    # preflight already warned-and-continued past a disabled WSH) calls this on every run to
    # decide whether wscript.exe's hidden launcher can be trusted at all -- reusing this same
    # Test-WshEnabled check rather than a second copy of the registry paths.
    Write-Output ('WSH-ENABLED=' + [int](Test-WshEnabled))
    exit 0
}
if ($args -contains '-ProbeOnly') {
    # Reached only through a real -File launch: proves the launch works and says in which mode.
    Write-Output ('PROBE-LANGUAGE=' + $ExecutionContext.SessionState.LanguageMode)
    exit 0
}

if (-not $env:PREFLIGHT_NO_AUTORUN) {
    $root = $PreflightRoot
    if (-not $root) { $root = (Get-Location).Path }
    $self = Join-Path $root 'scripts\preflight_policy.ps1'
    $probeOut = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $self -ProbeOnly 2>&1 | ForEach-Object { [string]$_ })
    $probeText = $probeOut -join ' '
    $lang = ''
    if ($probeText -match 'PROBE-LANGUAGE=(\w+)') { $lang = $Matches[1] }
    $mp = [string](Get-ExecutionPolicy -Scope MachinePolicy)
    $up = [string](Get-ExecutionPolicy -Scope UserPolicy)
    $findings = Get-PolicyFindings -ProbeRan ([bool]$lang) -ProbeLanguage $lang -ProbeError $probeText `
        -MachinePolicy $mp -UserPolicy $up -MotwCount (Get-MotwCount $root) `
        -WshEnabled (Test-WshEnabled) -Root $root
    foreach ($f in $findings) {
        Write-Output ''
        foreach ($l in $f.Lines) { Write-Output ('  ' + $l) }
    }
    exit (Get-PreflightExitCode $findings)
}
