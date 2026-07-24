# tunnel_name_util.ps1 -- shared PURE helpers for detecting "tunnel name drift":
# a supervisor.ps1 process hosting a different Dev Tunnel than the one currently
# named in .env's MCP_TUNNEL_NAME (the bug: an .env copied from another machine
# names a tunnel that machine's account owns; the supervisor starts hosting that
# borrowed tunnel; later heal_tunnel.ps1's self-heal repoints .env's
# MCP_TUNNEL_NAME to this account's own tunnel -- but the already-running
# supervisor keeps hosting the borrowed one, pollutes it, and this machine's own
# tunnel stays unhosted).
#
# start_all.ps1, supervisor.ps1, and doctor.ps1 ALL need the exact same
# "extract -TunnelName from a process command line" parse and the same
# ".cluster"-suffix-insensitive bare-name compare. Rather than copy that regex
# into three files (and risk them drifting out of sync with each other), it
# lives here ONCE and every caller dot-sources this file.
#
# This is also what makes it cleanly unit-testable: this file has NO top-level
# side effects (it only defines functions), so dot-sourcing it -- from a Pester
# test, or from any of the three real scripts -- never runs real process/.env
# I/O and needs no dot-source guard. (Contrast heal_tunnel.ps1, which DOES do
# real work at the bottom of the file and therefore needs its own
# `$MyInvocation.InvocationName -ne '.'` guard to be safely dot-sourceable.)
#
# ASCII / ENGLISH ONLY (cmd/console safe). No BOM.

function Get-SupervisorArgTunnel {
    # Extracts the value following "-TunnelName" from a process command line
    # string (e.g. Win32_Process.CommandLine for a running supervisor.ps1).
    # Returns "" when -TunnelName is absent, or the input is empty/$null.
    param([string]$CommandLine)
    if (-not $CommandLine) { return "" }
    $m = [regex]::Match($CommandLine, '-TunnelName\s+"?([^"\s]+)"?')
    if ($m.Success) { return $m.Groups[1].Value }
    return ""
}

function Get-BareTunnelName {
    # "name.cluster" -> "name" (lowercased). Also safe on an already-bare id
    # (no dot present) -- returns it lowercased, unchanged in shape. Mirrors
    # Get-BareTunnelId in heal_tunnel.ps1 (kept as a separate tiny function
    # here rather than shared with that file, to avoid a cross-dependency
    # between the two independent self-heal mechanisms; both are 1-line
    # regexes and easy to keep in sync by inspection).
    param([string]$Name)
    if (-not $Name) { return "" }
    return (($Name -split '\.')[0]).ToLowerInvariant()
}

function Test-SupervisorTunnelDrift {
    # PURE decision: has a RUNNING supervisor drifted from the tunnel .env
    # currently names? Conservative by design -- returns $true (drifted,
    # caller should restart it) ONLY when BOTH the running supervisor's
    # -TunnelName and the .env name are known (non-empty) and their BARE
    # forms differ. If either side is unknown/empty, there is no way to
    # positively tell drift occurred, so this returns $false (leave as-is)
    # rather than guess -- a false "drift" would restart a perfectly healthy
    # supervisor and briefly drop a live tunnel connection.
    param(
        [string]$RunningCommandLine,
        [string]$EnvTunnelName
    )
    $runBare = Get-BareTunnelName (Get-SupervisorArgTunnel $RunningCommandLine)
    $envBare = Get-BareTunnelName $EnvTunnelName
    if ([string]::IsNullOrEmpty($runBare) -or [string]::IsNullOrEmpty($envBare)) { return $false }
    return ($runBare -ne $envBare)
}

# =============================================================================
# Get-UpdateStrategy -- unrelated to tunnel drift, but colocated here for the
# same reason as the three functions above: start_all.ps1 already dot-sources
# this file (see its header comment), so adding one more side-effect-free pure
# function here needs no new dot-source line anywhere. It backs
# Check-ForUpdates's self-update logic in start_all.ps1.
#
# INCIDENT THIS SERVES: the project's upstream `main` was force-pushed (history
# rewritten to scrub bad commit metadata). Every existing local clone then has
# a DIVERGED local main relative to its upstream -- e.g. "behind 10, ahead 4",
# where the 4 "ahead" commits are just the OLD pre-rewrite versions of commits
# whose content already exists in the new upstream history. `git pull --ff-only`
# correctly refuses to touch that (it must never silently merge/rebase over a
# user's own work) -- but that leaves the user stuck forever with a perpetual
# "could not update" dialog and no path forward. Get-UpdateStrategy tells the
# caller which of the two very different recovery flows applies.
# =============================================================================
function Get-UpdateStrategy {
    # PURE decision, no I/O. Given how the local checkout compares to its
    # upstream, decides which update strategy Check-ForUpdates should use.
    # Total and conservative: for ANY input (including negative/bogus counts
    # from an upstream parse failure) this returns one of the four known
    # strings below -- it never throws.
    #
    #   'up-to-date'         Behind -le 0 -- nothing to do.
    #   'fast-forward'       Behind -gt 0 and CanFastForward -- today's plain
    #                        `git pull --ff-only` path; unchanged behavior.
    #   'rewritten-upstream' Behind -gt 0, NOT fast-forward-able, and
    #                        Ahead -gt 0 -- exactly the shape a force-pushed
    #                        history rewrite produces (see header above).
    #   'diverged-unknown'   Behind -gt 0, NOT fast-forward-able, and
    #                        Ahead -le 0 -- a divergence that does not match
    #                        the known rewrite signature (e.g. Ahead itself
    #                        could not be determined). Handled the same way
    #                        as 'rewritten-upstream' by the caller today, but
    #                        kept as its own label so it can be told apart in
    #                        logs/tests if it ever needs different handling.
    param(
        [int]$Behind,
        [int]$Ahead,
        [bool]$CanFastForward
    )
    if ($Behind -le 0) { return 'up-to-date' }
    if ($CanFastForward) { return 'fast-forward' }
    if ($Ahead -gt 0) { return 'rewritten-upstream' }
    return 'diverged-unknown'
}
