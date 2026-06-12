"""Compile both WPF C# sources to TEMP exes (does not touch the running CopilotChat.exe /
FleetCockpit.exe) to verify they build cleanly. Reports csc errors verbatim."""
import os, subprocess

FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
WPF = os.path.join(FW, "WPF")
UI = r"C:\Users\USER\companion-mcp\ui"
OUT = os.path.join(UI, "_buildcheck")
os.makedirs(OUT, exist_ok=True)

refs = [os.path.join(WPF, "PresentationFramework.dll"),
        os.path.join(WPF, "PresentationCore.dll"),
        os.path.join(WPF, "WindowsBase.dll"),
        os.path.join(FW, "System.Xaml.dll"),
        os.path.join(FW, "System.Web.Extensions.dll"),
        os.path.join(FW, "System.Windows.Forms.dll")]

targets = [("CopilotChat", ["CopilotChat.cs", "Markdown.cs"]),
           ("FleetCockpit", ["FleetCockpit.cs"])]

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
