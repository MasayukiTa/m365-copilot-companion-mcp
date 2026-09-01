# prune_edge_cache.ps1 -- delete the regenerable caches of ONE Edge profile directory.
#
# ASCII / ENGLISH ONLY.
#
# WHY THIS IS ITS OWN FILE. It began inline in start_companion_edge.ps1, where the only way to
# exercise it was to launch a browser -- and a browser rewrites its own profile on startup, so
# the first run of it could not distinguish "the prune deleted Local Storage" from "Edge
# rebuilt Local Storage because the seeded file was not valid leveldb". A deletion routine
# whose blast radius cannot be observed without a process that also writes to the target is
# not a routine anyone can vouch for. Here it runs against a directory, touches nothing else,
# and prints what it did, so a test can seed a profile and check exactly what survived.
#
# THE RULE: only directories whose OWN NAME is a cache name. Not a path substring -- a profile
# living under a folder containing "cache" would otherwise be deleted wholesale, taking the
# sign-in with it. Cookies, Local Storage, Preferences and everything else stay.

param(
    [Parameter(Mandatory = $true)][string]$ProfileDir,
    # The ceiling, in megabytes. Under it, nothing is deleted at all: these caches exist to
    # stop the browser re-fetching the same assets, and emptying one that is already small
    # buys nothing and costs a slower first navigation.
    [int]$CapMB = 2,
    [switch]$DryRun
)

$cacheDirNames = @("Cache","Code Cache","GPUCache","DawnCache","ShaderCache","GrShaderCache",
                   "Service Worker","CacheStorage","extensions_crx_cache","component_crx_cache")

if (-not (Test-Path $ProfileDir)) { Write-Output "absent: $ProfileDir"; exit 0 }

function Get-CacheDirs($root) {
    Get-ChildItem $root -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $cacheDirNames -contains $_.Name }
}

$held = 0
foreach ($d in (Get-CacheDirs $ProfileDir)) {
    $held += (Get-ChildItem $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
              Measure-Object Length -Sum).Sum
}
$heldMB = [math]::Round($held / 1MB, 1)

if ($heldMB -le $CapMB) {
    Write-Output ("under cap: {0} MB <= {1} MB" -f $heldMB, $CapMB)
    exit 0
}

if ($DryRun) {
    Write-Output ("would prune: {0} MB over the {1} MB ceiling" -f $heldMB, $CapMB)
    exit 0
}

Write-Output ("cache {0:N0} MB is over the {1} MB ceiling; pruning before launch" -f $heldMB, $CapMB)
foreach ($d in (Get-CacheDirs $ProfileDir)) {
    Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

$after = 0
foreach ($d in (Get-CacheDirs $ProfileDir)) {
    $after += (Get-ChildItem $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
               Measure-Object Length -Sum).Sum
}
Write-Output ("pruned: {0} MB -> {1} MB" -f $heldMB, [math]::Round($after / 1MB, 1))
