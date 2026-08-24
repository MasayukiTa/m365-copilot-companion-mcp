# env_defaults.ps1 -- backfill and upgrade the safe defaults in a .env, without ever
# overwriting a choice the user made.
#
# Split out of start_all.ps1 so it can be exercised on a throwaway .env. The logic it holds is
# the kind that is only ever wrong on someone else's machine: it decides whether a value in a
# config file belongs to the user or to us, and getting that backwards either freezes every
# installation on an old default or silently discards a deliberate setting.
#
# Dot-source to use:  . scripts\win\env_defaults.ps1 ; Ensure-EnvDefaults -Root <dir>
#
# ASCII / ENGLISH ONLY.

function Ensure-EnvDefaults {
    param([string]$Root = $root)
    # Release upgrades must not overwrite a user's .env, but existing installations need new
    # safe defaults.
    #
    # APPENDING ONLY ABSENT KEYS MEANT A DEFAULT COULD NEVER BE CHANGED AGAIN. Once an older
    # release had written MCP_REVIEW_P2C=0 as its default, the key existed, so every later
    # release skipped it and that machine stayed on the old value forever -- reported as
    # "start_all doesn't rewrite parts of the env on other people's machines", and it was
    # exactly right. The cause is that the file cannot say whether a value is there because
    # the USER chose it or because an EARLIER RUN OF THIS FUNCTION wrote it, and without that
    # distinction the only safe move is to never touch anything.
    #
    # So this records what it wrote, next to the file, and consults that record next time:
    #   * key absent from .env            -> write the current default, and remember writing it
    #   * present, equal to what we wrote -> ours to update; write the new default, remember it
    #   * present, different              -> the user changed it. Never touched again.
    # A machine with no record behaves exactly as before: everything present is assumed to be
    # the user's, which is the safe reading when we genuinely cannot tell.
    #
    # MCP_FLEET_SOCKET IS DELIBERATELY NOT IN THIS LIST, and should not be added. Unset means
    # ON, so writing "1" would pin every installation to today's answer and take the code's
    # default away from the next person who wants to change it. Note the asymmetry while you
    # are here: an explicit BLANK means OFF, because scripts disable the route by assigning an
    # empty string, so "MCP_FLEET_SOCKET=" in a .env is a switched-off route and not a blank.
    try {
        $path = Join-Path $Root ".env"
        # quickstart writes .env; if it has not run yet there is nothing to backfill, and the
        # process runs on the code's own defaults, which are the same values written here.
        if (-not (Test-Path $path)) { return }
        $text = [System.IO.File]::ReadAllText($path)
        $recordPath = Join-Path $Root ".env.defaults.json"
        $written = @{}
        if (Test-Path $recordPath) {
            try {
                $obj = Get-Content $recordPath -Raw | ConvertFrom-Json
                foreach ($p in $obj.PSObject.Properties) { $written[$p.Name] = [string]$p.Value }
            } catch { $written = @{} }
        }
        $defaults = [ordered]@{
            TASK_JOB_APPROVAL_MODE = "default"
            MCP_REVIEW_P2C = "0"
            MCP_EXECUTION_PROFILES = "0"
            MCP_DEEP_REVIEW_TRANSPORT = "auto"
            MCP_LOCAL_REVIEW_MAX_CONCURRENT = "2"
            MCP_LOCAL_ROTATE_AFTER_TURNS = "3"
            MCP_LOCAL_EDGE_MB_LIMIT = "1400"
        }
        $changed = $false
        foreach ($entry in $defaults.GetEnumerator()) {
            $key = $entry.Key
            $want = [string]$entry.Value
            $line = '(?m)^([ 	]*)' + [regex]::Escape($key) + '[ 	]*=(.*)$'
            $m = [regex]::Match($text, $line)
            if (-not $m.Success) {
                if ($text.Length -gt 0 -and -not ($text.EndsWith("`n") -or $text.EndsWith("`r"))) {
                    $text += "`r`n"
                }
                $text += $key + "=" + $want + "`r`n"
                $written[$key] = $want
                $changed = $true
                continue
            }
            $current = $m.Groups[2].Value.Trim()
            if ($current -eq $want) { $written[$key] = $want; continue }
            # Present and different. Ours to update only if it still holds what WE last wrote.
            if ($written.ContainsKey($key) -and $written[$key] -eq $current) {
                $text = $text.Remove($m.Index, $m.Length).Insert($m.Index, $key + "=" + $want)
                $written[$key] = $want
                $changed = $true
            }
            # else: the user chose this value. Leave it alone, and do not claim it later.
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        if ($changed) { [System.IO.File]::WriteAllText($path, $text, $utf8NoBom) }
        # The record is rewritten even when the file was not, so a value the user has since
        # matched to our default by hand is not mistaken for ours on some later upgrade.
        [System.IO.File]::WriteAllText(
            $recordPath, ($written | ConvertTo-Json -Compress), $utf8NoBom)
    } catch {
        # A default-backfill failure must never prevent daily startup.
    }
}
