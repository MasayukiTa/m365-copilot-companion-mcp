"""Static self-check for the Windows Sandbox install-path harness (scripts/sandbox/).

Run:  python scripts/sandbox/check_harness.py      (exit 0 = ok)

Checks what makes a sandbox result acceptable or not, without starting a sandbox:

* both .ps1 files are ASCII only (Windows PowerShell 5.1 reads BOM-less scripts in the ANSI
  code page, so one non-ASCII byte can change what the script does);
* both parse (on Windows, through powershell.exe's own parser);
* the in-sandbox driver launches nothing from the product tree except quickstart.bat and
  start_all.bat -- no scripts\\*.ps1 / *.py is invoked, so no result can rest on a script a
  person never runs;
* the host launcher builds the source from `git archive HEAD` and refuses local state.

Deliberately not named test_*.py: it is not part of the hermetic CI suite (it needs nothing
from the product and changes nothing in it); it is a guard for this harness.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "sandbox_driver.ps1")
HOST = os.path.join(HERE, "run_sandbox.ps1")


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def check() -> list:
    problems = []
    for path in (DRIVER, HOST):
        data = _read(path)
        bad = [i for i, b in enumerate(data) if b > 127]
        if bad:
            problems.append("%s: non-ASCII byte at offset %d" % (os.path.basename(path), bad[0]))

    driver = _read(DRIVER).decode("ascii", "replace")
    code = "\n".join(l for l in driver.splitlines() if not l.lstrip().startswith("#"))
    # Anything from the product tree that is executed, rather than read, goes through a path
    # joined onto $App. Only the two .bat files may be.
    launched = set(re.findall(r'Join-Path \$App "([^"]+\.(?:bat|ps1|py|cmd|vbs))"', code))
    extra = sorted(launched - {"quickstart.bat", "start_all.bat"})
    if extra:
        problems.append("sandbox_driver.ps1 references product scripts other than the two .bat "
                        "files: %s" % ", ".join(extra))
    if re.search(r"-File\s+\(?\s*(?:Join-Path\s+)?\$App", code):
        problems.append("sandbox_driver.ps1 runs a product .ps1 with -File")

    host = _read(HOST).decode("ascii", "replace")
    if "archive" not in host or "HEAD" not in host:
        problems.append("run_sandbox.ps1 no longer builds the source from `git archive HEAD`")
    for name in (".env", ".setup", ".fleet", ".venv"):
        if '"%s"' % name not in host:
            problems.append("run_sandbox.ps1 no longer refuses %s in the extracted tree" % name)

    ps = shutil.which("powershell")
    if ps and os.name == "nt":
        for path in (DRIVER, HOST):
            cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   "'%s',[ref]$null,[ref]$e); if ($e.Count) { $e | %% { $_.Message + ' @' + "
                   "$_.Extent.StartLineNumber }; exit 1 }" % path.replace("'", "''"))
            r = subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True)
            if r.returncode != 0:
                problems.append("%s does not parse: %s" % (
                    os.path.basename(path), r.stdout.decode("utf-8", "replace").strip()))
    return problems


def main() -> int:
    problems = check()
    for p in problems:
        print("FAIL: " + p)
    if not problems:
        print("ok: sandbox harness checks passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
