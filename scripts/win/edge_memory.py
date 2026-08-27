"""How much RAM a managed browser actually costs, measured the way Task Manager measures it.

WHAT WAS WRONG, AND FOR HOW LONG. Every memory figure this repository produced on 2026-08-27 --
the capture page's "610 MB", the lean trial's rises, the checkpoint's per-profile line -- was
the sum of `Win32_Process.WorkingSetSize` across the browser's processes. That counter includes
SHARED pages, and a Chromium browser is fifteen processes sharing one binary and one set of
mapped libraries, so the shared part was counted fifteen times.

    companion Edge     122 MB private working set      reported as 295 MB      2.4x
    bridge Edge         34 MB private working set      reported as  97 MB      2.9x

The 122 matches what Task Manager shows for the same browser, which is the number anybody
looking at the machine sees.

WHAT SURVIVES THE CORRECTION AND WHAT DOES NOT. A comparison between two arms measured the same
way is still a comparison: the lean trial's 605.8 against 266.7 was a real difference, and the
capture-frequency finding -- 3.8 captures a minute -- never depended on the memory metric at
all. What does not survive is any ABSOLUTE claim. "The page costs 610 MB" was never true;
divide by about 2.4.

WHY NOT PrivateMemorySize64, WHICH IS EASIER TO GET. That is committed private VIRTUAL memory,
which can be several times the resident amount -- the same browsers report 352 MB and 298 MB by
that measure. It answers "how much address space", not "how much RAM".

`Win32_PerfRawData_PerfProc_Process.WorkingSetPrivate` is the resident, private, per-process
figure, and it joins to Win32_Process on IDProcess so each byte can be attributed to a profile.
"""
from __future__ import annotations

import json
import subprocess

#: One PowerShell call for both classes, joined on the process id. Two calls would sample the
#: machine at two different moments and attribute a process that started in between to nothing.
_PS = r"""
$proc = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Select-Object ProcessId, CommandLine
$perf = Get-CimInstance Win32_PerfRawData_PerfProc_Process -Filter "Name like 'msedge%'" |
        Select-Object IDProcess, WorkingSetPrivate
$priv = @{}
foreach ($p in $perf) { $priv[[int]$p.IDProcess] = [double]$p.WorkingSetPrivate }
$out = @()
foreach ($r in $proc) {
    $k = [int]$r.ProcessId
    $out += [pscustomobject]@{
        pid = $k
        cmd = $r.CommandLine
        priv = $(if ($priv.ContainsKey($k)) { $priv[$k] } else { 0 })
    }
}
$out | ConvertTo-Json -Compress -Depth 3
"""


def _rows(timeout=60):
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", _PS],
                             capture_output=True, text=True, timeout=timeout).stdout
        data = json.loads(raw) if (raw or "").strip() else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def private_mb(profile="copilot-companion-edge", timeout=60):
    """Resident private memory of one managed profile's browser, in MB, or None.

    None means the machine could not be asked -- which is not the same as zero, and a caller
    that treats it as zero reports a browser that vanished.
    """
    rows = _rows(timeout)
    if not rows:
        return None
    total = sum(float(r.get("priv") or 0) for r in rows
                if profile in (r.get("cmd") or ""))
    return round(total / 1048576.0, 1)


def per_process(profile="copilot-companion-edge", timeout=60):
    """{process kind: MB} for one profile, so a total can be read rather than guessed at.

    The kinds are Chromium's own: the browser process, the GPU process, one renderer per site,
    and the utility processes. Which of them are optional is a question about launch flags, and
    it cannot be asked at all without seeing them separately.
    """
    import re

    out = {}
    for r in _rows(timeout):
        cmd = r.get("cmd") or ""
        if profile not in cmd:
            continue
        kind = "browser"
        m = re.search(r"--type=([a-zA-Z-]+)", cmd)
        if m:
            kind = m.group(1)
        sub = re.search(r"--utility-sub-type=\S*?([A-Za-z]+)Service", cmd)
        if sub:
            kind = "utility:" + sub.group(1)
        out[kind] = round(out.get(kind, 0.0) + float(r.get("priv") or 0) / 1048576.0, 1)
    return out


if __name__ == "__main__":
    for prof in ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge"):
        total = private_mb(prof)
        if not total:
            continue
        print("%-24s %7.1f MB" % (prof, total))
        for kind, mb in sorted(per_process(prof).items(), key=lambda kv: -kv[1]):
            print("      %-22s %6.1f" % (kind, mb))
