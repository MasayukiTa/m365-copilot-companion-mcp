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
    # Python version stays where it is (a different language cannot dot-source this file).
    # Test-IdentifyingTunnelName -- the larger function that CALLS this one, and that also
    # carries the SHA-256 leaked-token blocklist -- followed later the same day: setup_devtunnel.ps1's
    # copy moved into this file (below) so heal_tunnel.ps1 could apply it too. doctor.ps1 still
    # defines its own copy AFTER dot-sourcing this file (it reads $repo, not $root), so doctor's
    # definition is the one doctor runs.
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

# =============================================================================================
# MOVED HERE FROM setup_devtunnel.ps1 (2026-09-24) so heal_tunnel.ps1 applies the SAME rules.
#
# heal_tunnel.ps1 runs on every start_all and kept a .env tunnel name it owned -- or switched to
# the account's FIRST owned tunnel -- with no "is another machine hosting it" check, no privacy
# guard and no "did this .env come from another machine" check: the likeliest way a second PC
# with a copied .env hosted this PC's tunnel on 2026-09-24. setup_devtunnel.ps1 had all three,
# defined inline in a script that cannot be dot-sourced (it does its work at top level). They
# now live here, once; setup_devtunnel.ps1 and heal_tunnel.ps1 both dot-source this file.
# Functions that read the repo root use the CALLER's $root (both callers define it).
# =============================================================================================

# --- ONE MACHINE IDENTITY, THE SAME ONE bootstrap.py USES (D22) -------------------------------
#   host   = platform.node() == [Net.Dns]::GetHostName(), lowercased
#   user   = getpass.getuser() == first non-empty of LOGNAME, USER, LNAME, USERNAME
#   suffix = sha256(lower(host + "|" + user)) hex, first 8
# The OLD identity (Get-LegacyHost / Get-LegacyMachineSuffix: %COMPUTERNAME%, SHA1[:6]) is still
# recognised so an install made before D22 keeps its URL.
function Get-ThisHost {
    $h = ""
    try { $h = [System.Net.Dns]::GetHostName() } catch { $h = "" }
    if ([string]::IsNullOrWhiteSpace($h)) { $h = "$env:COMPUTERNAME" }
    return $h.Trim().ToLowerInvariant()
}
function Get-LegacyHost {
    return ("$env:COMPUTERNAME").Trim().ToLowerInvariant()
}
function Get-ThisUser {
    foreach ($v in @($env:LOGNAME, $env:USER, $env:LNAME, $env:USERNAME)) {
        if ($v) { return $v }
    }
    return ""
}
function Get-MachineSuffix([string]$node = $null, [string]$user = $null) {
    if (-not $node) { $node = "" }
    if (-not $user) { $user = "" }
    if ($PSBoundParameters.Count -eq 0) {
        # bootstrap.py hashes platform.node() as returned (case preserved) and lowercases the
        # whole seed afterwards; Get-ThisHost is already lowercased, which is the same result.
        $node = Get-ThisHost
        $user = Get-ThisUser
    }
    $seed = ("$node|$user").ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($seed))
    } finally {
        $sha.Dispose()
    }
    $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    return $hex.Substring(0, 8)
}
function Get-LegacyMachineSuffix {
    $seed = "$env:COMPUTERNAME|$env:USERNAME"
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $bytes = $sha1.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($seed))
    } finally {
        $sha1.Dispose()
    }
    $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    return $hex.Substring(0, 6)
}
function Get-OwnDefaultTunnelNames([string]$DefaultName = "m365-copilot-companion") {
    # This machine's own generated names: the current scheme first, then the pre-D22 one.
    return @(("$DefaultName-" + (Get-MachineSuffix)), ("$DefaultName-" + (Get-LegacyMachineSuffix)))
}

# --- privacy guard ----------------------------------------------------------------------------
# Some tunnel names leak an identifying (organization/user) token to the GLOBAL devtunnels.ms
# namespace. The two SHA-256 values are a blocklist of the specific leaked token and full tunnel
# name seen in the wild -- the plaintext is never written here, only its hash. Hashing is over
# the UTF-8 bytes of the lowercased input, hex-encoded lowercase. bootstrap.py and doctor.ps1
# carry the same two values -- keep them in sync.
function Get-Sha256Hex([string]$s) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    } finally {
        $sha256.Dispose()
    }
    return (-join ($bytes | ForEach-Object { $_.ToString("x2") }))
}
# $true if the name (or one of its hyphen/underscore/dot/space-separated tokens) is identifying;
# $false otherwise, including an empty name (nothing to leak).
function Test-IdentifyingTunnelName([string]$name) {
    $TOKEN_SHA256 = "2a0341296bb96dc7d205036f9f693427809772f6136a46f58b04a1c492de9e04"  # gitleaks:allow
    $FULLNAME_SHA256 = "5ba174b8e87faf4e8106e36a7cf5a901bbec3435d01fbd56914c2b0346858261"  # gitleaks:allow
    if ([string]::IsNullOrWhiteSpace($name)) { return $false }
    $lower = $name.ToLowerInvariant()

    # 1. Whole-name blocklist hash match.
    if ((Get-Sha256Hex $lower) -eq $FULLNAME_SHA256) { return $true }

    # 2. Per-token blocklist hash match.
    $tokens = @($lower -split '[^a-z0-9]+' | Where-Object { $_ })
    foreach ($t in $tokens) {
        if ((Get-Sha256Hex $t) -eq $TOKEN_SHA256) { return $true }
    }

    # 3. Generic runtime checks -- folder-derived / user-derived names on any machine. NOT FOR A
    #    NAME THIS REPOSITORY GENERATED (D29): the substring test fired on those whenever
    #    USERNAME occurred inside "m365-copilot-companion-<hex>".
    if (Test-GeneratedTunnelName $name) { return $false }
    $repoLeaf = ""
    if ($root) { $repoLeaf = (Split-Path -Leaf $root).ToLowerInvariant() }
    $userName = ("$env:USERNAME").ToLowerInvariant()
    foreach ($t in $tokens) {
        if (($repoLeaf -and $t -eq $repoLeaf) -or ($userName -and $t -eq $userName)) { return $true }
    }
    if (($repoLeaf -and $lower.Contains($repoLeaf)) -or ($userName -and $lower.Contains($userName))) { return $true }

    return $false
}

# --- NEVER ADOPT A TUNNEL ANOTHER MACHINE IS HOSTING RIGHT NOW (57ad0d1) -----------------------
# With one Microsoft account signed in on two PCs every tunnel of either PC is in the account's
# list. A tunnel `devtunnel show` reports as hosted (Host connections >= 1) while no devtunnel
# host on THIS machine hosts it belongs, right now, to another machine. A count that cannot be
# read is not evidence of anything. supervisor.ps1 carries the same command-line rule;
# scripts/test_tunnel_served_by_another_pc.py runs both.
function Get-HostConnectionsFromShow([string[]]$showOutput) {
    # PURE. The "Host connections : N" count from `devtunnel show`, or $null.
    foreach ($l in @($showOutput)) {
        if ($l -match '(?i)^\s*Host connections\s*:\s*(\d+)') { return [int]$matches[1] }
    }
    return $null
}
function Test-IsTunnelHostCommandLine([string]$CommandLine, [string]$Name) {
    # PURE. `devtunnel host <name>` for this tunnel (bare id, with or without ".<cluster>").
    if (-not $CommandLine -or -not $Name) { return $false }
    if ($CommandLine -notmatch '(?i)devtunnel(\.exe|\.cmd)?"?\s+host\s') { return $false }
    $bare = (($Name -split '\.')[0]).ToLowerInvariant()
    if (-not $bare) { return $false }
    return ($CommandLine -match ('(?i)\shost\s+"?' + [regex]::Escape($bare) + '(\.[a-z0-9]+)?("|\s|$)'))
}
function Test-ThisMachineHostsTunnel([string]$name) {
    $hosts = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
               Where-Object { Test-IsTunnelHostCommandLine ([string]$_.CommandLine) $name })
    return ($hosts.Count -gt 0)
}

# --- A .env CARRIED FROM ANOTHER MACHINE (D7) ------------------------------------------------
# "Provably" foreign = a host stamp (MCP_TUNNEL_HOST) naming a different machine, or -- with no
# stamp -- a generated default name whose hash suffix is not this machine's. A .env with no stamp
# and a custom name is NOT provably foreign: dropping a name this machine really owns would
# change a working URL. bootstrap.py _foreign_env_reason is the Python side of the same rule.
function Get-EnvTunnelProvenance {
    # PURE when every identity parameter is passed; omitted ones are read from this machine.
    # Returns @{ ForeignReason = "" | why; StampIsMine = bool }.
    param([string]$RecordedHost = "", [string]$RecordedName = "",
          [string]$ThisHost = $null, [string]$LegacyHost = $null,
          [string]$MachineSuffix = $null, [string]$LegacyMachineSuffix = $null,
          [string]$DefaultName = "m365-copilot-companion")
    if (-not $ThisHost) { $ThisHost = Get-ThisHost }
    if (-not $LegacyHost) { $LegacyHost = Get-LegacyHost }
    $recorded = ("" + $RecordedHost).Trim().ToLowerInvariant()
    $stampIsMine = [bool]($recorded -and (($recorded -eq $ThisHost) -or ($recorded -eq $LegacyHost)))
    $why = ""
    if ($recorded -and -not $stampIsMine) {
        $why = "its tunnel was recorded on '$recorded', not this machine ('$ThisHost')"
    } elseif (-not $recorded -and $RecordedName -and (Test-GeneratedTunnelName $RecordedName $DefaultName)) {
        $sfxPart = $RecordedName.Trim().ToLowerInvariant().Substring($DefaultName.Length)
        if ($sfxPart -match '^-([0-9a-f]+)') {
            $sfx = $matches[1]
            if (-not $MachineSuffix) { $MachineSuffix = Get-MachineSuffix }
            if (-not $LegacyMachineSuffix) { $LegacyMachineSuffix = Get-LegacyMachineSuffix }
            if (($sfx -ne $MachineSuffix) -and ($sfx -ne $LegacyMachineSuffix)) {
                $why = "its tunnel name '$RecordedName' was generated on another machine (the suffix is not this machine's)"
            }
        }
    }
    return [PSCustomObject]@{ ForeignReason = $why; StampIsMine = $stampIsMine }
}
# Which MCP_TUNNEL_* keys cannot move to a new machine is decided by tools/env_portability.py
# (merge_for_new_machine), not here -- one copy of the rules.
function Get-PythonForHelpers {
    # `return ,@(...)`: a one-element array returned plainly is unrolled to its string, and
    # $py[0] is then the first CHARACTER of the path ("C").
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return ,@($venvPy) }
    # The classifier is stdlib-only, so any Python 3 will do when .venv does not exist yet.
    $c = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return ,@($c.Source) }
    $c = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return ,@($c.Source, "-3") }
    return $null
}
# The MCP_TUNNEL_* keys of $envFile that merge_for_new_machine drops on a move, or $null when the
# classifier could not be asked (no Python, or it did not answer in its documented shape).
function Get-MachineBoundTunnelKeys([string]$envFile) {
    $py = Get-PythonForHelpers
    if (-not $py) { return $null }
    $classifier = Join-Path $root "tools\env_portability.py"
    if (-not (Test-Path $classifier)) { return $null }
    $pyExe = $py[0]
    $pyArgs = @()
    if ($py.Count -gt 1) { $pyArgs = @($py[1..($py.Count - 1)]) }
    $out = @(& $pyExe @pyArgs $classifier machine-bound $envFile 2>$null)
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not ($out | Where-Object { $_ -match '^done:\d+$' })) { return $null }
    $keys = @($out | ForEach-Object { if ($_ -match '^dropped:(\S+)$') { $matches[1] } } |
              Where-Object { $_ -like "MCP_TUNNEL_*" })
    return ,$keys
}
function ConvertTo-EnvLinesWithKeysAside([string[]]$Lines, [string[]]$Keys, [string]$Note) {
    # PURE. Each KEY=... line of $Keys becomes "# <note>" + "# <line>": readable, not in effect.
    $out = @()
    foreach ($ln in @($Lines)) {
        $k = ""
        if ($ln -match '^([A-Za-z_][A-Za-z0-9_]*)=') { $k = $matches[1] }
        if ($k -and (@($Keys) -contains $k)) {
            $out += ("# " + $Note)
            $out += ("# " + $ln)
        } else {
            $out += $ln
        }
    }
    return ,$out
}
