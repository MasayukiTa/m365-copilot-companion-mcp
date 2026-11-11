<#
.SYNOPSIS
  One tick of the Dev Tunnel token-cache safety net: back up a healthy cache, restore a zeroed one.

.DESCRIPTION
  Range (D) of the 2026-11-11/12 Dev Tunnel sign-in-loss work. The owner had to redo the
  sign-in four times in one night; the MSAL token cache
  %LOCALAPPDATA%\DevTunnels\devtunnels-tokens-microsoft went from 4182 B / 4214 B to 0 B with
  no sign-in window open, and a 0-byte cache reads as "logged out". This keeps a private copy
  of the cache while it is healthy and restores it when the live file is found at 0 B and no
  devtunnel login process is running -- so the persisted sign-in survives a zeroing with no
  human re-authenticating.

  ALL the policy lives in scripts/tunnel_token_backup.py (pure, cross-platform, tested). This
  wrapper only: measures whether a `devtunnel` login/sign-in process is running right now, and
  calls do_backup then do_restore through the repo's python. It performs no decision of its own
  and NEVER calls the devtunnel CLI (so it cannot itself truncate-then-write the cache). It
  does not touch a live tunnel host. Intended to be invoked once per supervisor health cycle;
  it is a no-op when the cache is healthy and present.

  SEPARATE from range (C): this wrapper does not change supervisor.ps1 and does not alert; it
  is the backup/restore mechanism only. The supervisor's 0-byte alerting and CLI-suppression
  are range (C). Running this wrapper is safe alongside them: it reads the cache, copies files,
  and returns one word per action on stdout for the caller to log.

.PARAMETER RepoRoot
  Repo root (where .venv and scripts\ live). Defaults to this script's parent's parent.

.PARAMETER Python
  The interpreter to run the pure module with. Defaults to <RepoRoot>\.venv\Scripts\python.exe.
#>
param(
    [string]$RepoRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "SilentlyContinue"
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
if (-not $Python) { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }

function Test-DevtunnelLoginRunning {
    # Is a `devtunnel` login / user-login / sign-in process running right now? Such a process
    # is itself truncate-then-writing the token cache, so a restore must wait for it. Matched on
    # the command line of any live devtunnel process doing a login/user operation. A reader/host
    # (`devtunnel host ...`) is NOT a login and does not block a restore.
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                ($_.CommandLine -match '(?i)devtunnel') -and
                ($_.CommandLine -match '(?i)\b(login|user\s+login|user\s+show|logout)\b')
            }
        return [bool]$procs
    } catch { return $false }
}

$loginRunning = Test-DevtunnelLoginRunning
$loginArg = if ($loginRunning) { "True" } else { "False" }

# Drive the pure module. It resolves the cache dir and backup dir from LOCALAPPDATA, backs up a
# healthy cache, then restores a zeroed one when no login is running. One word per action is
# printed for the caller to log. The python never calls the devtunnel CLI.
$code = @'
import sys
sys.path.insert(0, sys.argv[1])
import tunnel_token_backup as T
login_running = (sys.argv[2] == "True")
cache = T.default_cache_dir()
backup = T.default_backup_dir()
if not cache or not backup:
    print("skipped-no-localappdata")
    sys.exit(0)
b = T.do_backup(cache, backup)
r = T.do_restore(cache, backup, login_running=login_running)
print("backup=%s restore=%s" % (b, r))
'@

$scriptsDir = $PSScriptRoot
& $Python -c $code $scriptsDir $loginArg
