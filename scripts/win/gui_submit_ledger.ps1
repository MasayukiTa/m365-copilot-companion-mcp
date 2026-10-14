# Idempotency record for GUI submissions (dot-sourced by submit_via_ui.ps1). ASCII only.
#
# WHY. The cockpit writes one durable add_goal command per button press, and the runner admits
# identical text under a new command as a NEW task. A caller that retried after a "failure" that
# happened AFTER the button was invoked therefore duplicated work (2026-10-14: 3 goals became 5
# workers, five commands ~1 minute apart, all acked applied). The only safe retry rule is: once a
# goal set has been handed to the button, never hand it over again unless the operator says so.
#
# Callers hold gui_submit.lock around every use, so the read-modify-write below is serialized.

function Get-GoalSetKey {
    param([string[]]$Goals)
    $joined = (@($Goals | ForEach-Object { $_.Trim() }) -join "`n")
    $sha = [System.Security.Cryptography.SHA1]::Create()
    $bytes = $sha.ComputeHash((New-Object System.Text.UTF8Encoding($false)).GetBytes($joined))
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16)
}

function Read-SubmissionRecords {
    param([string]$Path)
    $out = @{}
    try {
        if (Test-Path -LiteralPath $Path) {
            $obj = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($p in $obj.PSObject.Properties) { $out[$p.Name] = [double]$p.Value }
        }
    } catch { }
    return $out
}

# Returns the unix time the goal set was invoked, or 0 when it was not invoked within the window.
function Get-RecentInvocation {
    param([string]$Path, [string]$Key, [int]$WindowSeconds = 21600)
    $recs = Read-SubmissionRecords -Path $Path
    if (-not $recs.ContainsKey($Key)) { return 0 }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if (($now - $recs[$Key]) -gt $WindowSeconds) { return 0 }
    return $recs[$Key]
}

function Add-SubmissionRecord {
    param([string]$Path, [string]$Key, [int]$WindowSeconds = 21600)
    $recs = Read-SubmissionRecords -Path $Path
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $keep = [ordered]@{}
    foreach ($k in $recs.Keys) { if (($now - $recs[$k]) -le $WindowSeconds) { $keep[$k] = $recs[$k] } }
    $keep[$Key] = $now
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($tmp, ($keep | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Remove-SubmissionRecord {
    param([string]$Path, [string]$Key)
    $recs = Read-SubmissionRecords -Path $Path
    if (-not $recs.ContainsKey($Key)) { return }
    $keep = [ordered]@{}
    foreach ($k in $recs.Keys) { if ($k -ne $Key) { $keep[$k] = $recs[$k] } }
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($tmp, ($keep | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
