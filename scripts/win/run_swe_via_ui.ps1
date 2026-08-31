# Drive a whole SWE-bench slice through the COCKPIT, start to finish, without stopping.
#
# WHY THROUGH THE UI. A run driven straight into the fleet proves the fleet works; it does not
# prove the cockpit hands it the same thing, and the gap between those two has bitten here --
# the back end was correct while the surface was full of errors, and "the tests pass" was true
# of a path nobody uses. submit_via_ui.ps1 says the same in its own header.
#
# WHY ONE SCRIPT FOR THE WHOLE SLICE. The previous plan staged one batch, submitted it, and
# reported -- which makes a stopping point per batch, and five batches means five chances to
# stop. This runs every batch to completion on its own.
#
# WHAT IT DOES PER BATCH: stage worktrees -> serialise the goals to one JSON line each (the
# cockpit splits its input on newlines, so a multi-line problem statement would otherwise
# become dozens of fragments) -> submit through the cockpit -> wait for the run to go idle ->
# capture the diffs.

[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice40_fresh.json",
    [int]$BatchSize = 8,
    [int]$PerBatchTimeoutSec = 3600,
    [string]$Preds = ".fleet/swe/ui_preds.json",
    [string]$Log = ".fleet/swe/ui_run.log",
    # RESUME, because a run that died at batch 1's capture should not re-solve batch 1. The
    # accumulator is only cleared when starting from the beginning; resuming keeps what is
    # already captured, which is the point of resuming.
    [int]$StartBatch = 1,
    # HOW MANY TIMES AN UNCOVERED INSTANCE IS RE-WORKED. One pass through the batches is
    # not a run: a batch can fail to submit, or submit and never start, and one pass leaves
    # those instances silently unanswered.
    [int]$MaxRounds = 3,
    # Below this, the Go module cache is cleared between batches. It is the largest
    # regenerable thing a run leaves behind on a box with single-digit GB free.
    [double]$GoCacheFloorGb = 4.0
)

$ErrorActionPreference = "Stop"
# The repository is found from this script's own location, not written in: a path in a
# tracked file names the machine it was written on.
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format HH:mm:ss), $m
    $line | Tee-Object -FilePath $Log -Append | Out-Host
}

# A FRESH ACCUMULATOR. Keeping the old one lets an interrupted run's patches be graded as this
# run's, which is the shortest path from this harness to a number nobody produced.
if ($StartBatch -le 1 -and (Test-Path $Preds)) {
    Move-Item -LiteralPath $Preds -Destination "$Preds.prev" -Force
}
# NO BOM. PowerShell 5.1's `-Encoding utf8` writes one, and the Python that reads this file
# next raises "Unexpected UTF-8 BOM" -- which stopped a run dead at its first capture. This
# repository already holds that rule for .env files; it is the same trap in a new file.
if ($StartBatch -le 1) {
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $Preds), "[]",
                                   (New-Object System.Text.UTF8Encoding($false)))
}
if ($StartBatch -le 1) {
    if (Test-Path ".fleet/swe/pro_wt_map.json") { Remove-Item ".fleet/swe/pro_wt_map.json" -Force }
    if (Test-Path ".fleet/swe/work") { Remove-Item ".fleet/swe/work" -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-Item $Log -Force -ErrorAction SilentlyContinue
}

# FAN-OUT MUST BE OFF, AND THE SCRIPT MAKES SURE RATHER THAN ASSUMING.
#
# The cockpit keeps `fanout` in its settings file, so a run inherits whatever the last person
# left it as. Measured: it was on, and eight SWE instances became FORTY-EIGHT workers -- each
# instance split into subtasks that then edit THE SAME worktree, which is the one arrangement
# the split job explicitly forbids. Every parent ended FANOUT, so the capture step would have
# read whatever several children had done to one checkout and called it that instance's patch.
#
# A SWE instance is one bug fix. It is not a divisible campaign, and no setting a person left
# behind should be able to make it into one.
$settings = Join-Path $env:APPDATA "copilot-bridge\settings.txt"
if (Test-Path $settings) {
    $body = Get-Content -LiteralPath $settings -Encoding UTF8
    if ($body -match '^fanout=on') {
        Say "fanout was ON in the cockpit settings -- turning it off for this run"
        ($body -replace '^fanout=on', 'fanout=off') |
            Set-Content -LiteralPath $settings -Encoding UTF8
    }
} else {
    Say "WARNING: cockpit settings not found; cannot confirm fanout is off"
}

$ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); print('\n'.join(sorted(x['instance_id'] for x in r)))" $SliceFile
$ids = @($ids -split "`n" | Where-Object { $_.Trim() })
Say ("START ui-run: {0} instances, batch={1}, slice={2}" -f $ids.Count, $BatchSize, $SliceFile)

# WHY THIS LOOPS OVER ROUNDS AND NOT JUST OVER BATCHES.
#
# The previous shape was one pass of five batches, and it caught a failed submit, logged it,
# and carried on into the wait and the capture. Measured, 2026-08-29: batches 3, 4 and 5 all
# threw out of SendKeys, so nothing was ever submitted for them; the driver then waited the
# full per-batch hour three times over for a fleet that had never started, captured empty
# diffs from worktrees nobody had touched, and finished with
#     DONE ui-run: 40 predictions
# for a slice in which 22 instances had never been sent anywhere. Three hours of the wall
# clock went into those waits and the run's own report concealed it.
#
# Two rules come out of that, and they are the whole of this rewrite:
#   1. A batch that was never submitted is not waited for and is not captured. Capturing it
#      is what turned "no work was done" into a row in the predictions file.
#   2. The run's finish condition is coverage, not row count. It re-works whatever is still
#      uncovered until it is covered or the rounds run out, and it says so either way.
$idsFile = ".fleet/swe/ui_all_ids.txt"
Set-Content -Path $idsFile -Value ($ids -join "`n") -Encoding ascii   # ascii: utf8 here means BOM

# WHAT IS PENDING IS WHAT IS UNCOVERED, NOT WHAT COMES AFTER SOME BATCH NUMBER.
# Resuming by batch index assumes every earlier batch succeeded, and the run this replaces
# is the counter-example: batches 3-5 sat in the completed range having answered nothing.
$pending = @(& $py bench/ui_missing_ids.py $idsFile $Preds | Where-Object { $_.Trim() })
Say ("pending at start: {0} of {1} uncovered" -f $pending.Count, $ids.Count)
$submitFails = 0

for ($round = 1; $round -le $MaxRounds; $round++) {
    if ($pending.Count -eq 0) { break }
    $batches = [math]::Ceiling($pending.Count / $BatchSize)
    Say ("=== round {0}/{1}: {2} instances, {3} batches ===" -f $round, $MaxRounds, $pending.Count, $batches)

    for ($b = 0; $b -lt $batches; $b++) {
        # Checked HERE, between batches, so a freeze never interrupts work already in flight:
        # the batch that is running finishes and is captured, and the next one does not start.
        if (Test-Path ".fleet/swe/FREEZE_LOCAL") {
            Say "FREEZE_LOCAL present; not starting another batch on this machine"
            Say "UI_RUN_FROZEN"
            exit 0
        }
        $slice = @($pending[($b * $BatchSize)..([math]::Min(($b + 1) * $BatchSize, $pending.Count) - 1)])
        Say ("--- r{0} batch {1}/{2}: {3} instances ---" -f $round, ($b + 1), $batches, $slice.Count)

        # A RUN ALREADY IN FLIGHT IS NOT A BROKEN COCKPIT.
        #
        # submit_via_ui.ps1 refuses while a run is live, because Ctrl+Enter steers rather than
        # starts. The driver read that refusal as a submit failure, gave up after three
        # attempts, and after two batches exited UI_RUN_BLOCKED "so the cockpit can be
        # repaired" -- with nothing wrong with the cockpit. Measured: a previous run's fleet
        # outlived its own driver by forty minutes, and the next run declined to start for
        # the whole of it and then quit. Waiting is the entire fix, and a bounded wait cannot
        # become the hang it replaces.
        $idleBy = (Get-Date).AddMinutes(90)
        while ((Get-Date) -lt $idleBy) {
            $live = & $py -c "import json,io,os; p='.fleet/status.json'; d=json.load(io.open(p,encoding='utf-8-sig')) if os.path.exists(p) else {}; print('1' if d.get('running') else '0')"
            if ($live -ne "1") { break }
            Say "a run is still in flight; waiting before submitting"
            Start-Sleep -Seconds 60
        }


        # THE AGENT PAGE MUST BE READY BEFORE ANYTHING IS SUBMITTED -- AND AFTER THE WAIT.
        #
        # This sat above the "is a run still in flight" wait, so it navigated and could surface
        # :9222 while ANOTHER run still owned that browser. Repairing one run by disturbing
        # another is not a repair. It belongs here, once this run is the only one.
        #
        # AND ANYTHING THAT IS NOT READY STOPS THE BATCH. The first version blocked only on
        # needs_signin, so "loading", "redirect" and "unknown" all fell through to submission --
        # which is exactly the state that produced two runs of workers with no tools writing
        # patches from memory. There is no reading of "not ready" that makes submitting safe.
        $agentUrl = ((Get-Content .env | Where-Object { $_ -match '^MCP_FLEET_AGENT_URL=' }) -replace '^MCP_FLEET_AGENT_URL=','').Trim()
        if ($agentUrl) {
            $cls = & $py -c "import sys;from relay import edge_auth;print(edge_auth.ensure_ready(sys.argv[1]))" $agentUrl
            Say ("agent page: {0}" -f $cls)
            if ($cls -ne "ready") {
                if ($cls -eq "needs_signin") {
                    Say "the agent page needs a human sign-in; the window has been surfaced"
                } else {
                    Say ("the agent page is '{0}', not ready" -f $cls)
                }
                Say "not submitting a batch that cannot call tools"
                Say "UI_RUN_BLOCKED"
                exit 4
            }
        }

        $env:SWE_SLICE_FILE = $SliceFile
        $goalsFile = ".fleet/swe/ui_batch.jsonl"
        & $py bench/pro_stage_goals.py --ids ($slice -join ",") --out $goalsFile 2>&1 |
            Select-Object -Last 1 | ForEach-Object { Say ("stage: " + $_) }

        $uiFile = ".fleet/swe/ui_batch_lines.txt"
        $lineCount = & $py -c "import sys; sys.path.insert(0,'.'); from bench.ui_goal_lines import write_ui_file; print(write_ui_file(sys.argv[1], sys.argv[2]))" $goalsFile $uiFile
        Say ("ui lines: {0}" -f $lineCount)

        # SUBMIT, WITH RETRIES, AND THE OUTCOME DECIDES WHETHER THE REST OF THE BATCH RUNS.
        # SendKeys' journal hook fails when the foreground application is not pumping
        # messages, which is a transient condition and worth another attempt -- but a batch
        # that never got submitted must not fall through to the wait.
        # SUCCESS IS THE EXIT CODE AND THE MARKER, NOT THE ABSENCE OF STDERR.
        #
        # This read `... 2>&1 | Select-Object -Last 3` under $ErrorActionPreference = "Stop".
        # In PowerShell 5.1, merging a native command's stderr into the pipeline wraps each
        # line in an ErrorRecord, which that preference turns into a TERMINATING error -- so
        # the moment submit_via_ui.ps1 wrote its diagnostics to stderr (they were moved there
        # deliberately, because on stdout they became the function's return value), every
        # SUCCESSFUL submit threw. Measured: three attempts, three "FAILED", and the reported
        # reason was the progress line "invoking button" -- the last thing the child printed
        # before succeeding. The batch was submitted and the driver gave up on it.
        $submitted = $false
        for ($try = 1; $try -le 3 -and -not $submitted; $try++) {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $out = & powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\submit_via_ui.ps1 -GoalFile $uiFile 2>&1 | Out-String
            $rc = $LASTEXITCODE
            $ErrorActionPreference = $prevEap
            foreach ($l in (($out -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 3)) {
                Say ("submit: " + $l)
            }
            if ($rc -eq 0 -and $out -match "submitted:") {
                $submitted = $true
            } else {
                Say ("submit attempt {0}/3 FAILED (exit {1}, marker {2})" -f $try, $rc,
                     $(if ($out -match "submitted:") { "present" } else { "absent" }))
                Start-Sleep -Seconds 20
            }
        }
        if (-not $submitted) {
            Say "submit gave up after 3 attempts; NOT waiting and NOT capturing this batch"
            # AND STOP DRIVING, because the next batch will fail the same way.
            #
            # Measured 2026-08-29: with the cockpit unreachable, this loop ran three rounds
            # of two batches in four minutes, failed all six, and exited "incomplete" --
            # then the supervisor started another driver to do it again, eight times over.
            # Repairing the cockpit is the supervisor's job and it cannot do it while a
            # driver is churning, so hand control back after the second failure in a row.
            $submitFails++
            if ($submitFails -ge 2) {
                Say "two batches in a row could not be submitted; exiting so the cockpit can be repaired"
                Say "UI_RUN_BLOCKED"
                exit 3
            }
            continue
        }
        $submitFails = 0

        $t0 = Get-Date
        $sawRunning = $false
        while (((Get-Date) - $t0).TotalSeconds -lt $PerBatchTimeoutSec) {
            Start-Sleep -Seconds 15
            $running = & $py -c "import json,io,os; p='.fleet/status.json'; d=json.load(io.open(p,encoding='utf-8')) if os.path.exists(p) else {}; print('1' if d.get('running') else '0')"
            if ($running -eq "1") { $sawRunning = $true }
            elseif ($sawRunning) { Say "batch fleet went idle"; break }
        }
        if (-not $sawRunning) {
            # The submit reported success and the fleet still never ran. Capturing here is
            # what manufactured empty predictions and let the run call them answers.
            Say "the fleet never reported running; NOT capturing this batch"
            continue
        }

        try {
            & $py bench/pro_capture.py --preds $Preds 2>&1 |
                Select-Object -Last 2 | ForEach-Object { Say ("capture: " + $_) }
        } catch {
            Say ("capture FAILED for this batch: " + $_.Exception.Message)
        }

        # REAP WHAT THE FINISHED WORKERS LEFT RUNNING.
        #
        # Seventeen npx processes survived their runs by up to fourteen hours on 2026-08-30.
        # They held 672 MB, and worse they held the worktree files open: `git worktree remove`
        # failed, the capture left husks that resolve to the harness's own repository, and the
        # free-disk figure this driver admits work against was wrong by six checkouts.
        #
        # Between batches, when nothing of ours should still be building.
        & $py -c "import sys; sys.path.insert(0,'.'); from relay.orphan_reaper import reap; import json; r=reap(min_age_s=1800, dry_run=False); print('reaped %d orphan(s) from %s' % (len(r['killed']), r['work_root']))" 2>&1 |
            ForEach-Object { Say ("reaper: " + $_) }

        # THE GO MODULE CACHE IS THE BIGGEST THING THIS RUN CREATES, AND IT IS REGENERABLE.
        #
        # Deleting a Go toolchain cache by hand to make room, mid-run, did not work: a worker
        # that needs Go and cannot find it installs Go -- measured 2026-08-30 07:08, a worker
        # ran `winget install --id GoLang.Go --accept-package-agreements` on this machine --
        # and then refills the module cache anyway. 4.2 GB freed came back as 2.9 GB in
        # ~\go plus a system-wide install. Freeing it by hand moves the problem; freeing it
        # BETWEEN BATCHES, when no worker is building, actually holds.
        #
        # `go clean -modcache` and not a tree walk: module cache entries are read-only and
        # rmtree leaves most of them behind.
        $freeGb = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
        if ($freeGb -lt $GoCacheFloorGb) {
            $goExe = "C:\Program Files\Goin\go.exe"
            if (Test-Path $goExe) {
                Say ("free disk {0} GB below {1}; clearing the Go module cache between batches" -f $freeGb, $GoCacheFloorGb)
                & $goExe clean -modcache 2>&1 | Select-Object -Last 1 | ForEach-Object { Say ("go clean: " + $_) }
                & $goExe clean -cache 2>&1 | Select-Object -Last 1 | ForEach-Object { Say ("go clean: " + $_) }
                Say ("free disk now {0} GB" -f [math]::Round((Get-PSDrive C).Free / 1GB, 2))
            } else {
                Say ("free disk {0} GB below {1} and no go.exe to clean with" -f $freeGb, $GoCacheFloorGb)
            }
        }
    }

    $pending = @(& $py bench/ui_missing_ids.py $idsFile $Preds | Where-Object { $_.Trim() })
    Say ("round {0} end: {1} still uncovered" -f $round, $pending.Count)
}

$covered = $ids.Count - $pending.Count
Say ("ui-run finished: {0}/{1} instances have a usable patch" -f $covered, $ids.Count)
if ($pending.Count -gt 0) {
    Say ("STILL UNCOVERED ({0}): {1}" -f $pending.Count, (($pending | Select-Object -First 12) -join " "))
    Say "UI_RUN_INCOMPLETE"
} else {
    Say "UI_RUN_COMPLETE"
}
