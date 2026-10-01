# Cross-process ownership for the one shared FleetCockpit composer.
#
# A persistent file NAME is harmless. Ownership is the Windows file HANDLE opened with
# FileShare.None. If a submitter crashes, the OS closes the handle and the next submitter can
# acquire the same path immediately; there is no stale-lock cleanup protocol to get wrong.

function Enter-GuiSubmitLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$RepoRoot,
        [int]$TimeoutSeconds = 90
    )

    $stateDir = Join-Path ([IO.Path]::GetFullPath($RepoRoot)) '.fleet'
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    $lockPath = Join-Path $stateDir 'gui_submit.lock'
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
    $lastError = $null

    while ((Get-Date) -lt $deadline) {
        try {
            $stream = New-Object System.IO.FileStream(
                $lockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None)
            try {
                $owner = "pid={0}; acquired_utc={1:o}`n" -f $PID, [DateTime]::UtcNow
                $bytes = [Text.Encoding]::UTF8.GetBytes($owner)
                $stream.SetLength(0)
                $stream.Position = 0
                $stream.Write($bytes, 0, $bytes.Length)
                $stream.Flush($true)
            } catch {
                $stream.Dispose()
                throw
            }
            return [PSCustomObject]@{
                Stream = $stream
                Path = $lockPath
                Pid = $PID
            }
        } catch [System.IO.IOException] {
            $lastError = $_.Exception
            Start-Sleep -Milliseconds 100
        }
    }

    $detail = if ($lastError) { $lastError.Message } else { 'lock remained busy' }
    throw ("another GUI submitter owns FleetCockpit goalInput; timed out after {0}s waiting for {1}: {2}" -f
           [Math]::Max(1, $TimeoutSeconds), $lockPath, $detail)
}

function Exit-GuiSubmitLock {
    [CmdletBinding()]
    param($Lease)
    if ($null -eq $Lease) { return }
    try {
        if ($null -ne $Lease.Stream) { $Lease.Stream.Dispose() }
    } catch { }
}
