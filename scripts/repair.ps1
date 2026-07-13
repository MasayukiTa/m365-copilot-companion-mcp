# =============================================================================
#  repair.ps1 -- situation-aware repair dispatcher for the m365-copilot-companion-mcp
#  stack. DETECTION stays single-source in scripts\doctor.ps1 (read-only); this
#  script only ACTS, and only on what doctor actually reports as broken -- it never
#  runs a blind "fix everything" pass.
#
#  Flow: run doctor.ps1 -Json (or read a supplied mock JSON -- see -JsonInput /
#  -MockJson below), parse the per-check results, and for each FAILING check (not
#  OK, not [SKIP]'d by the tunnel chain, not an info-only line) -- IN DOCTOR'S OWN
#  EMITTED ORDER, i.e. its dependency order -- look up a repair in the REGISTRY
#  below and act according to its TIER:
#
#    Tier A  = auto-run.       Safe, idempotent, no side effect beyond "start
#              something that should already be running". Runs without asking.
#    Tier B  = confirm first.  Has a real side effect a person should approve
#              (installs software, recreates a tunnel/URL, closes UI windows,
#              rebuilds an exe). Prompts Y/N unless -Yes or -Auto is given.
#    Tier C  = human-only.     Needs an interactive sign-in, a value only the
#              human has (an agent URL), or a decision (matching a Bearer in an
#              external system). NEVER attempted here -- always just printed as
#              the exact manual step. This mirrors the project's existing "Fix"
#              principle from doctor.ps1: only auto-fix what can be fixed
#              reliably; human-only steps are surfaced, never silently attempted.
#
#  Several failing ids can map to the SAME underlying repair command (e.g.
#  server_up and tunnel_serving are both cleared by starting the stack; ui_
#  copilotchat and ui_fleetcockpit are both cleared by one UI rebuild) -- those
#  are DEDUPED so the command only runs ONCE per pass, keyed on the repair's
#  registry key, never on the check id.
#
#  After each pass that actually ran a Tier A/B repair, doctor is re-invoked to
#  see what got cleared (an upstream fix like starting the server often clears
#  downstream fails for free) and to discover newly-unblocked checks (e.g. once
#  tunnel_cli is fixed, the previously-[SKIP]'d tunnel_login/tunnel_exists/
#  tunnel_serving checks actually run next time). The loop is capped (4 passes)
#  so it can never spin, and it stops escalating the moment it hits a Tier C
#  blocker or a declined Tier B -- past a human-required step there is nothing
#  more this script can safely do, so it reports that clearly instead of retrying.
#
# USAGE
#   powershell -File scripts\repair.ps1                 # interactive: A auto, B prompts Y/N, C printed
#   powershell -File scripts\repair.ps1 -Auto            # Tier A only; Tier B skipped (needs a human); Tier C printed
#   powershell -File scripts\repair.ps1 -Auto -Yes       # Tier A + Tier B both auto-confirmed; Tier C printed
#   powershell -File scripts\repair.ps1 -DryRun          # preview ONLY -- nothing is ever executed
#   powershell -File scripts\repair.ps1 -DryRun -MockJson path\to\fake_doctor_results.json
#                                                         # drive the dispatcher from a canned JSON array
#                                                         # instead of the live doctor -- this is also the
#                                                         # seam a cockpit UI or an automated test uses to
#                                                         # exercise the tier/dedupe logic without touching
#                                                         # the real machine.
#
# FLAGS
#   -DryRun    : never execute anything. For every failing check, print what WOULD
#                run (tier + exact command, or the human step). This is both the
#                safe preview AND the primary way to test this script. Always a
#                SINGLE pass (no re-check loop -- nothing changed, so there is
#                nothing new to discover).
#   -Auto      : run Tier A automatically; SKIP Tier B (prints "needs confirmation");
#                print Tier C human steps. For an unattended "fix what's safe" run.
#   -Yes       : auto-confirm Tier B (answers Y without prompting). Has no effect
#                without -Auto or in plain interactive mode it simply removes the
#                Y/N prompt for Tier B items.
#   -JsonInput / -MockJson (same parameter, two names) : path to a JSON array
#                (the same shape doctor.ps1 -Json emits) to use INSTEAD OF running
#                the real doctor.ps1. When set, the real doctor is never invoked,
#                and the run is always exactly one pass (a canned snapshot cannot
#                be "re-checked" -- there is no live system behind it to change).
#   -ResultJson : in addition to the normal human-readable output on every other
#                line, emit ONE compact JSON object as the LAST stdout line:
#                { autofixed: [{id,note}], confirmNeeded: [{id,note}],
#                  humanSteps: [{id,step}], finalOk: <int>, finalBad: <int> }
#                so a caller (e.g. a cockpit UI) can read just that last line to
#                learn what got fixed, what still needs confirmation, and what
#                manual steps remain, without scraping the colored text above it.
#
# SAFETY
#   - repair.ps1 is the ONLY script here that mutates anything; doctor.ps1 never
#     does. Every action is a single, previously-documented command (the same one
#     doctor's own "fix:" text already names, or the project's own launch scripts) --
#     nothing is chained, and nothing destructive is ever run.
#   - Every external call is best-effort: a failed repair prints a clear FAIL line
#     and moves on; it never throws the whole dispatcher over.
#   - ASCII / ENGLISH ONLY (cmd/console safe).
# =============================================================================
param(
    [switch]$DryRun,
    [switch]$Auto,
    [switch]$Yes,
    [Parameter()]
    [Alias('MockJson')]
    [string]$JsonInput,
    [switch]$ResultJson
)

$ErrorActionPreference = "Continue"
# This script lives in <repo>\scripts; doctor.ps1 is its sibling, and $repo (the
# REPO ROOT, one level up) is where every registry command below is anchored.
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir
$doctorPs1 = Join-Path $scriptDir "doctor.ps1"

# -----------------------------------------------------------------------------
# REGISTRY: id -> { Tier; Key (dedupe bucket); Cmd (what to run, Tier A/B only);
#                    Human (the manual step text, Tier C only); Note }
# Tier A/B commands are plain command lines run via Invoke-Expression -- they are
# NOT user input, they are fixed strings authored right here, one per registry
# row, so there is no injection surface.
# -----------------------------------------------------------------------------
$stackStartCmd     = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\start_all.ps1`" -NoUi -NoSplash"
$edgeCompanionCmd   = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\start_companion_edge.ps1`""
$edgeBridgeCmd      = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\start_bridge.ps1`" -Keepalive"
$tunnelCliCmd       = "winget install Microsoft.devtunnel"
$tunnelSetupCmd     = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\setup_devtunnel.ps1`""
$tunnelHealCmd      = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\heal_tunnel.ps1`""
$uiRebuildCmd       = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$repo\ui\rebuild_ui.ps1`" -NoLaunch"

$Registry = @{
    server_up       = @{ Tier = 'A'; Key = 'stack_start';    Cmd = $stackStartCmd;   Note = 'starts the MCP server (via supervisor.ps1, hosted by start_all.ps1)' }
    tunnel_serving  = @{ Tier = 'A'; Key = 'stack_start';    Cmd = $stackStartCmd;   Note = 'same stack start as server_up -- the supervisor also hosts the Dev Tunnel' }
    tunnel_owned    = @{ Tier = 'A'; Key = 'tunnel_heal';    Cmd = $tunnelHealCmd;   Note = 'repoints MCP_TUNNEL_NAME to a tunnel this account owns (URL-preserving when possible; safe to auto-run)' }
    edge_companion  = @{ Tier = 'A'; Key = 'edge_companion'; Cmd = $edgeCompanionCmd; Note = 'launches the dedicated companion Edge (:9222)' }
    edge_bridge     = @{ Tier = 'A'; Key = 'edge_bridge';    Cmd = $edgeBridgeCmd;   Note = 'optional -- only run because edge_bridge actually failed' }

    tunnel_cli      = @{ Tier = 'B'; Key = 'tunnel_cli_install'; Cmd = $tunnelCliCmd;   Note = 'installs software (devtunnel CLI) -- confirm first' }
    tunnel_exists   = @{ Tier = 'B'; Key = 'tunnel_setup';       Cmd = $tunnelSetupCmd; Note = 're-creates the tunnel -- changes the public URL; the Copilot Studio connector may need updating' }
    tunnel_name_private = @{ Tier = 'B'; Key = 'tunnel_rename';  Cmd = $tunnelSetupCmd; Note = 'recreates the tunnel under a private name -- changes the public URL; the Copilot Studio connector will need the new URL afterward' }
    ui_copilotchat  = @{ Tier = 'B'; Key = 'ui_rebuild';         Cmd = $uiRebuildCmd;   Note = 'closes and rebuilds both UI windows' }
    ui_fleetcockpit = @{ Tier = 'B'; Key = 'ui_rebuild';         Cmd = $uiRebuildCmd;   Note = 'same rebuild as ui_copilotchat -- covers both apps' }

    tunnel_login    = @{ Tier = 'C'; Human = "Run:  devtunnel login   (opens a browser; the supervisor cannot host the tunnel until the CLI is logged in). Then re-run this." }
    m365_signin     = @{ Tier = 'C'; Human = "Run:  powershell -File scripts\start_companion_edge.ps1 -Foreground   then complete the M365 (Entra ID) sign-in in the window that appears. It persists across restarts." }
    env_api_key     = @{ Tier = 'C'; Human = "No .env / Bearer. Run quickstart.bat (creates .env with a fresh Bearer + unlock password)." }
    agent_url       = @{ Tier = 'C'; Human = "Paste the Copilot Studio agent URL: double-click configure_env.bat (README STEP 4)." }
    auth_bearer     = @{ Tier = 'C'; Human = "Bearer rejected: the 'Bearer <MCP_API_KEY>' configured in Copilot Studio must match .env exactly." }

    dotnet_csc      = @{ Tier = 'INFO' }
}

# -----------------------------------------------------------------------------
# Doctor invocation / mock input
# -----------------------------------------------------------------------------
function Get-DoctorResults {
    if ($JsonInput) {
        if (-not (Test-Path $JsonInput)) {
            Write-Host "ERROR: -JsonInput/-MockJson file not found: $JsonInput" -ForegroundColor Red
            return $null
        }
        try {
            $raw = Get-Content -Path $JsonInput -Raw
            $parsed = $raw | ConvertFrom-Json
            # ConvertFrom-Json can hand back a single object (not an array) if the
            # file holds exactly one check -- normalize so callers can always @() it.
            return @($parsed)
        } catch {
            Write-Host "ERROR: could not parse -JsonInput/-MockJson as JSON: $($_.Exception.Message)" -ForegroundColor Red
            return $null
        }
    }
    if (-not (Test-Path $doctorPs1)) {
        Write-Host "ERROR: doctor.ps1 not found at $doctorPs1" -ForegroundColor Red
        return $null
    }
    try {
        $raw = & powershell -NoProfile -ExecutionPolicy Bypass -File $doctorPs1 -Json 2>$null
        # -Json mode emits exactly one line; if $raw came back as an array of lines
        # (rare, but be defensive), take the last non-empty one.
        $line = $raw
        if ($raw -is [array]) { $line = ($raw | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1) }
        if (-not $line) {
            Write-Host "ERROR: doctor.ps1 -Json produced no output." -ForegroundColor Red
            return $null
        }
        $parsed = $line | ConvertFrom-Json
        return @($parsed)
    } catch {
        Write-Host "ERROR: failed to run/parse doctor.ps1 -Json: $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }
}

# -----------------------------------------------------------------------------
# Execute one registry command (Tier A/B only). Best-effort: never throws.
# -----------------------------------------------------------------------------
function Invoke-RepairCommand([string]$cmd) {
    try {
        Invoke-Expression $cmd
        return ($LASTEXITCODE -eq $null) -or ($LASTEXITCODE -eq 0)
    } catch {
        Write-Host ("      FAILED: " + $_.Exception.Message) -ForegroundColor Red
        return $false
    }
}

# -----------------------------------------------------------------------------
# One pass over the currently-failing checks (already filtered: ok=false,
# skipped=false, info=false), in the order doctor emitted them. Returns a summary
# object describing what happened, for the loop driver and the final report.
# -----------------------------------------------------------------------------
function Invoke-RepairPass([array]$failing) {
    $ranKeys      = @{}   # dedupe: registry Key -> already run this pass
    $autoFixed    = @()   # ids actually repaired (Tier A or confirmed Tier B)
    $needsHuman   = @()   # Tier C ids (human_step text)
    $declined     = @()   # Tier B ids the user said no to (or -Auto skipped)
    $noRegistry   = @()   # ids with no registry entry at all
    $wouldFix     = @()   # Tier A/B ids identified under -DryRun (nothing actually ran) --
                           # only consumed by -ResultJson so a -DryRun test run still shows
                           # what the dispatcher WOULD do, without claiming it ran anything
    $anyActionRan = $false

    foreach ($chk in $failing) {
        $id = $chk.id
        if (-not $Registry.ContainsKey($id)) {
            Write-Host ("  [ ?  ] no automatic repair for " + $id + "; see: " + $chk.fix) -ForegroundColor Yellow
            $noRegistry += [PSCustomObject]@{ id = $id; fix = $chk.fix }
            continue
        }
        $entry = $Registry[$id]

        if ($entry.Tier -eq 'INFO') {
            # Informational only (dotnet_csc) -- never acted on, even if it reports fail.
            continue
        }

        if ($entry.Tier -eq 'C') {
            Write-Host ""
            Write-Host ("  [TIER C -- human only] " + $id) -ForegroundColor Cyan
            Write-Host ("      " + $entry.Human) -ForegroundColor Yellow
            $needsHuman += [PSCustomObject]@{ id = $id; step = $entry.Human }
            continue
        }

        # Tier A or B from here on -- dedupe on the registry Key, not the check id.
        $key = $entry.Key
        if ($ranKeys.ContainsKey($key)) {
            Write-Host ("  [ =  ] " + $id + " -- covered by the '" + $key + "' repair already run this pass") -ForegroundColor DarkGray
            # If that shared repair actually ran, count this id as fixed-by-association.
            if ($ranKeys[$key]) { $autoFixed += $id }
            continue
        }

        if ($entry.Tier -eq 'A') {
            Write-Host ""
            Write-Host ("  [TIER A -- auto] " + $id) -ForegroundColor Green
            Write-Host ("      cmd: " + $entry.Cmd) -ForegroundColor Yellow
            if ($DryRun) {
                Write-Host "      DRY RUN -- not executed." -ForegroundColor DarkGray
                $ranKeys[$key] = $false
                $wouldFix += $id
                continue
            }
            $ok = Invoke-RepairCommand $entry.Cmd
            Write-Host ("      " + $(if ($ok) { "OK -- command completed" } else { "FAILED -- see output above" })) -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
            $ranKeys[$key] = $ok
            if ($ok) { $autoFixed += $id; $anyActionRan = $true }
            continue
        }

        if ($entry.Tier -eq 'B') {
            Write-Host ""
            Write-Host ("  [TIER B -- confirm] " + $id) -ForegroundColor Magenta
            Write-Host ("      cmd:  " + $entry.Cmd) -ForegroundColor Yellow
            Write-Host ("      note: " + $entry.Note) -ForegroundColor DarkGray
            if ($DryRun) {
                Write-Host "      DRY RUN -- would prompt for confirmation, not executed." -ForegroundColor DarkGray
                $ranKeys[$key] = $false
                $wouldFix += $id
                continue
            }
            if ($Auto -and -not $Yes) {
                Write-Host "      skipped (needs confirmation; re-run without -Auto, or add -Yes, to apply this)" -ForegroundColor Yellow
                $declined += [PSCustomObject]@{ id = $id; cmd = $entry.Cmd }
                $ranKeys[$key] = $false
                continue
            }
            $confirmed = $Yes.IsPresent
            if (-not $confirmed) {
                $answer = Read-Host "      Run this now? (Y/N)"
                $confirmed = ($answer -match '^(y|yes)$')
            }
            if (-not $confirmed) {
                Write-Host "      declined." -ForegroundColor Yellow
                $declined += [PSCustomObject]@{ id = $id; cmd = $entry.Cmd }
                $ranKeys[$key] = $false
                continue
            }
            $ok = Invoke-RepairCommand $entry.Cmd
            Write-Host ("      " + $(if ($ok) { "OK -- command completed" } else { "FAILED -- see output above" })) -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
            $ranKeys[$key] = $ok
            if ($ok) { $autoFixed += $id; $anyActionRan = $true }
            continue
        }
    }

    return [PSCustomObject]@{
        AutoFixed    = $autoFixed
        NeedsHuman   = $needsHuman
        Declined     = $declined
        NoRegistry   = $noRegistry
        WouldFix     = $wouldFix
        AnyActionRan = $anyActionRan
    }
}

# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "m365-copilot-companion-mcp  --  repair dispatcher" -ForegroundColor Cyan
Write-Host "==================================================="
if ($JsonInput) {
    Write-Host ("Mock input mode: reading doctor results from " + $JsonInput + " (real doctor.ps1 is NOT invoked; single pass only).") -ForegroundColor DarkGray
}
if ($DryRun) {
    Write-Host "DRY RUN: nothing will be executed. This is a preview only." -ForegroundColor Yellow
}
Write-Host ""

$maxPasses = 4
$pass = 0
$allHuman = @()
$allDeclined = @()
$allNoRegistry = @()
$allFixed = @()
$allWouldFix = @()
$lastResults = $null

while ($true) {
    $pass++
    $results = Get-DoctorResults
    if ($null -eq $results) {
        Write-Host "Could not obtain doctor results -- aborting." -ForegroundColor Red
        exit 1
    }
    $lastResults = $results

    $failing = @($results | Where-Object { -not $_.ok -and -not $_.skipped -and -not $_.info })

    Write-Host ("--- pass $pass of $maxPasses : " + $failing.Count + " failing check(s) ---") -ForegroundColor Cyan

    if ($failing.Count -eq 0) {
        Write-Host "Nothing to repair -- all green." -ForegroundColor Green
        break
    }

    $passResult = Invoke-RepairPass $failing
    $allHuman      += $passResult.NeedsHuman
    $allDeclined   += $passResult.Declined
    $allNoRegistry += $passResult.NoRegistry
    $allFixed      += $passResult.AutoFixed
    $allWouldFix   += $passResult.WouldFix

    # -DryRun and -JsonInput are both single-pass by design: -DryRun changes
    # nothing to re-check, and -JsonInput has no live system behind it to re-poll.
    if ($DryRun -or $JsonInput) { break }

    if (-not $passResult.AnyActionRan) {
        Write-Host ""
        Write-Host "No Tier A/B repair actually ran this pass (everything remaining needs a human) -- stopping." -ForegroundColor Yellow
        break
    }

    if ($pass -ge $maxPasses) {
        Write-Host ""
        Write-Host "Reached the re-check cap ($maxPasses passes) -- stopping." -ForegroundColor Yellow
        break
    }

    Write-Host ""
    Write-Host "Re-running doctor to pick up what that repair may have cleared..." -ForegroundColor DarkGray
    Write-Host ""
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "---------------------------------------------"
Write-Host "SUMMARY" -ForegroundColor Cyan
if ($allFixed.Count -gt 0) {
    Write-Host ("  Auto-fixed: " + (($allFixed | Select-Object -Unique) -join ", ")) -ForegroundColor Green
} else {
    Write-Host "  Auto-fixed: (none)" -ForegroundColor DarkGray
}
if ($allDeclined.Count -gt 0) {
    Write-Host "  Declined / skipped confirmation (re-run to apply):" -ForegroundColor Yellow
    foreach ($d in ($allDeclined | Select-Object -Unique -Property id, cmd)) {
        Write-Host ("    - " + $d.id + "  ->  " + $d.cmd)
    }
}
if ($allNoRegistry.Count -gt 0) {
    Write-Host "  No automatic repair available (manual, see doctor's fix text):" -ForegroundColor Yellow
    foreach ($n in ($allNoRegistry | Select-Object -Unique -Property id, fix)) {
        Write-Host ("    - " + $n.id + "  ->  " + $n.fix)
    }
}
if ($allHuman.Count -gt 0) {
    Write-Host "  Needs YOU (human-only step):" -ForegroundColor Yellow
    foreach ($h in ($allHuman | Select-Object -Unique -Property id, step)) {
        Write-Host ("    - " + $h.id + ":")
        Write-Host ("        " + $h.step)
    }
}
if ($lastResults) {
    $finalBad = @($lastResults | Where-Object { -not $_.ok -and -not $_.skipped -and -not $_.info }).Count
    $finalOk  = @($lastResults | Where-Object { $_.ok }).Count
    Write-Host ""
    Write-Host ("  Final doctor tally: " + $finalOk + " OK, " + $finalBad + " need attention.") -ForegroundColor $(if ($finalBad -eq 0) { "Green" } else { "Yellow" })
}
Write-Host ""

# -----------------------------------------------------------------------------
# -ResultJson: machine-readable summary for a caller (e.g. a cockpit UI) that
# only wants the outcome, not the colored text above. Emitted as the ABSOLUTE
# LAST stdout line -- nothing prints after this. Under -DryRun there is nothing
# "actually fixed" to report (see Invoke-RepairPass), so Tier A/B ids identified
# during the preview are folded into "autofixed" too (their note is suffixed to
# say so) -- this is what lets a -DryRun -MockJson test run exercise the full
# shape of this JSON without ever executing a repair command for real.
# -----------------------------------------------------------------------------
if ($ResultJson) {
    $autofixedIds = if ($DryRun) { @($allFixed) + @($allWouldFix) } else { @($allFixed) }
    $autofixedOut = @(
        $autofixedIds | Select-Object -Unique | ForEach-Object {
            $rid = $_
            $baseNote = if ($Registry.ContainsKey($rid)) { $Registry[$rid].Note } else { "" }
            $note = if ($DryRun) { ($baseNote + " (DRY RUN -- not executed)") } else { $baseNote }
            [PSCustomObject]@{ id = $rid; note = $note }
        }
    )
    $confirmNeededOut = @(
        $allDeclined | Select-Object -Unique -Property id | ForEach-Object {
            $rid = $_.id
            $note = if ($Registry.ContainsKey($rid)) { $Registry[$rid].Note } else { "" }
            [PSCustomObject]@{ id = $rid; note = $note }
        }
    )
    $humanStepsOut = @(
        $allHuman | Select-Object -Unique -Property id, step | ForEach-Object {
            [PSCustomObject]@{ id = $_.id; step = $_.step }
        }
    )
    $finalOkOut = 0
    $finalBadOut = 0
    if ($lastResults) {
        $finalOkOut  = @($lastResults | Where-Object { $_.ok }).Count
        $finalBadOut = @($lastResults | Where-Object { -not $_.ok -and -not $_.skipped -and -not $_.info }).Count
    }
    $resultObj = [PSCustomObject]@{
        autofixed     = $autofixedOut
        confirmNeeded = $confirmNeededOut
        humanSteps    = $humanStepsOut
        finalOk       = $finalOkOut
        finalBad      = $finalBadOut
    }
    Write-Output (ConvertTo-Json -InputObject $resultObj -Compress -Depth 5)
}
