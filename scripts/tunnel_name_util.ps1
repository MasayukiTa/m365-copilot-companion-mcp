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

function Test-GeneratedTunnelName {
    # A name THIS REPOSITORY GENERATES: the default tunnel name, optionally followed by a hex
    # machine suffix (8 = the current scheme, 6 = the legacy setup_devtunnel.ps1 one -- see
    # bootstrap.py's _legacy_machine_suffix). Such a name carries nothing user- or
    # folder-derived -- the suffix is a hash of COMPUTERNAME/USERNAME, not either value
    # verbatim -- which matters twice: it cannot be identifying on its own (D29 below), and
    # its suffix says which machine made it even when .env has no separate host stamp (D7).
    #
    # THIRD COPY, CONSOLIDATED HERE 2026-09-24. This used to be Test-GeneratedTunnelName in
    # setup_devtunnel.ps1 and, byte-for-byte except its name, Test-GeneratedTunnelNameDoctor in
    # doctor.ps1 -- both hand-kept in sync with each other AND with bootstrap.py's
    # _is_generated_tunnel_name. Only the two PowerShell copies move here: bootstrap.py's
    # Python version stays where it is (a different language cannot dot-source this file), and
    # Test-IdentifyingTunnelName -- the larger function that CALLS this one, and that also
    # carries the SHA-256 leaked-token blocklist -- stays duplicated between
    # setup_devtunnel.ps1 and doctor.ps1 on purpose: those two copies differ in which local
    # variable holds the repo root ($root vs $repo), and merging a blocklist-bearing function
    # into a "no top-level side effects, trivially dot-sourceable" utility file is a bigger
    # change than this task asked for. Only the WHY-D29 rule -- "a generated name is exempt
    # from the user/folder substring check" -- had drifted into three separate copies of the
    # SAME regex, which is what this consolidation fixes.
    #
    # WHY D29 EXISTS AT ALL: without this exemption, Test-IdentifyingTunnelName's substring
    # check fired on a generated name whenever USERNAME happened to occur inside
    # "m365-copilot-companion-<hex>" (a user named "pan", "com", "on", or a hex-only name
    # landing inside the suffix) -- the generated name was then thrown away, regenerated as the
    # exact same name, and "The PUBLIC URL will change" printed on every run while nothing
    # changed.
    param(
        [string]$Name,
        # Matches setup_devtunnel.ps1's $DEFAULT_NAME / doctor.ps1's (now-removed)
        # $DOCTOR_DEFAULT_NAME / bootstrap.py's DEFAULT_TUNNEL_NAME -- all three have always
        # been the literal string below; a caller with a different default may still pass one.
        [string]$DefaultName = "m365-copilot-companion"
    )
    if ([string]::IsNullOrWhiteSpace($Name)) { return $false }
    return ($Name.Trim().ToLowerInvariant() -match
        ('^' + [regex]::Escape($DefaultName) + '(-[0-9a-f]{6}|-[0-9a-f]{8}){0,2}$'))
}
