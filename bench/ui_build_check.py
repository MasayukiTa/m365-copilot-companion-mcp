"""Compile both WPF C# sources to TEMP exes (does not touch the running CopilotChat.exe /
FleetCockpit.exe) to verify they build cleanly. Reports csc errors verbatim."""
import os, re, subprocess

FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
WPF = os.path.join(FW, "WPF")
UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
OUT = os.path.join(UI, "_buildcheck")
os.makedirs(OUT, exist_ok=True)

refs = [os.path.join(WPF, "PresentationFramework.dll"),
        os.path.join(WPF, "PresentationCore.dll"),
        os.path.join(WPF, "WindowsBase.dll"),
        os.path.join(FW, "System.Xaml.dll"),
        os.path.join(FW, "System.Web.Extensions.dll"),
        os.path.join(FW, "System.Windows.Forms.dll")]

# READ FROM THE REAL BUILD, NOT COPIED FROM IT. This list used to be written out here, and it
# had drifted: it compiled CopilotChat without Theme.cs and FleetCockpit without either
# SelfImproveDashboard.cs or Theme.cs -- so the check could report a clean build of something
# nobody ships, and a break in the two omitted files would not have shown up here at all. The
# same omission-by-hand has broken this project's UI before. rebuild_ui.ps1 is what actually
# produces the binaries, so its Build lines are the source of truth; parse them.
def _targets_from_rebuild_script():
    path = os.path.join(UI, "rebuild_ui.ps1")
    found = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r'\s*Build\s+"([A-Za-z0-9_]+)"\s+@\((.*)\)\s*$', line)
        if m:
            found.append((m.group(1), re.findall(r'"([^"]+\.cs)"', m.group(2))))
    if not found:
        raise SystemExit("ui_build_check: no Build lines found in rebuild_ui.ps1 -- refusing to "
                         "check a source list I cannot read, because passing here would mean "
                         "nothing")
    return found


targets = _targets_from_rebuild_script()
for _name, _srcs in targets:
    print("target", _name, "=", ", ".join(_srcs))

for name, srcs in targets:
    cmd = [CSC, "/nologo", "/target:winexe", "/out:" + os.path.join(OUT, name + ".exe")]
    cmd += ["/r:" + r for r in refs]
    cmd += [os.path.join(UI, s) for s in srcs]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    print("=" * 60)
    print(name, "rc=", r.returncode)
    out = (r.stdout or "") + (r.stderr or "")
    errs = [l for l in out.splitlines() if "error" in l.lower() or "warning CS" in l]
    if errs:
        for l in errs[:25]:
            print("  ", l.strip())
    else:
        print("   clean (no errors/warnings)")
