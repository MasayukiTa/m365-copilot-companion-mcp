"""Compile both WPF C# sources to TEMP exes (does not touch the running CopilotChat.exe /
FleetCockpit.exe) to verify they build cleanly. Reports csc errors verbatim.

Also importable: `targets_from_rebuild_script()` and `build(name, sources, out_dir)` are what
ui/test_both_windows_can_be_constructed.py uses to produce the same two exes in a temp dir,
so there is one parser of rebuild_ui.ps1's Build lines and one csc command line, not two."""
import os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import childproc  # noqa: E402

FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
WPF = os.path.join(FW, "WPF")
UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
OUT = os.path.join(UI, "_buildcheck")

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


targets_from_rebuild_script = _targets_from_rebuild_script


def build(name, srcs, out_dir, timeout=300):
    """csc one target into `out_dir`/<name>.exe. Returns the CompletedProcess (decoded).

    The same references for every target, and app.manifest when it is there -- as
    rebuild_ui.ps1 embeds it. A caller that RUNS the exe (not just compiles it) needs the
    manifest: it is what declares the DPI awareness the window was written against."""
    cmd = [CSC, "/nologo", "/target:winexe", "/out:" + os.path.join(out_dir, name + ".exe")]
    manifest = os.path.join(UI, "app.manifest")
    if os.path.isfile(manifest):
        cmd.append("/win32manifest:" + manifest)
    cmd += ["/r:" + r for r in refs]
    cmd += [os.path.join(UI, s) for s in srcs]
    # `text=True, errors="replace"` decoded csc with the local code page and the error line came
    # back as mojibake on this machine -- "error CS0103: 蜷榊燕 'FleetCommands' ..." -- which is
    # the whole content of the report. A check whose finding cannot be read has not reported it.
    return childproc.run(cmd, timeout=timeout)


def main():
    # BEFORE ANY PROGRESS OUTPUT. csc.exe missing (no .NET Framework 4.x, or a machine where it
    # lives somewhere other than this hardcoded path) used to reach childproc.run -> subprocess.run
    # unchecked, and FileNotFoundError propagated as a bare traceback -- AFTER the "target ..."
    # lines below had already printed, which reads as progress toward a build that never started.
    if not os.path.isfile(CSC):
        raise SystemExit(
            "csc.exe not found at %s: .NET Framework 4.x is required "
            "(Windows Features > .NET Framework 4.8 Advanced Services)" % CSC)
    os.makedirs(OUT, exist_ok=True)
    targets = _targets_from_rebuild_script()
    for _name, _srcs in targets:
        print("target", _name, "=", ", ".join(_srcs))

    failed = []
    for name, srcs in targets:
        r = build(name, srcs, OUT)
        print("=" * 60)
        print(name, "rc=", r.returncode)
        out = (r.stdout or "") + (r.stderr or "")
        errs = [l for l in out.splitlines() if "error" in l.lower() or "warning CS" in l]
        if errs:
            for l in errs[:25]:
                print("  ", l.strip())
        else:
            print("   clean (no errors/warnings)")
        if r.returncode != 0:
            failed.append(name)

    # AND IT HAS TO BE ABLE TO FAIL. This printed "rc= 1" and then exited 0, so every caller -- a
    # shell, a CI step, ui/_buildcheck.bat, a person reading $? -- saw a pass. A check that reports
    # a break only in prose is a check nobody can wire up, and on 2026-09-22 this one would have
    # caught ui/FleetCommands.cs missing from a build list before CI did, had anyone been able to
    # read its verdict. Printing is not reporting.
    if failed:
        raise SystemExit("UI BUILD CHECK FAILED: " + ", ".join(failed))
    print("=" * 60)
    print("all %d target(s) built" % len(targets))


if __name__ == "__main__":
    main()
