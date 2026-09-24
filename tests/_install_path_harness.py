# -*- coding: utf-8 -*-
"""Shared harness for the install-path tests (new-PC review, 2026-09-24).

Everything here builds THROWAWAY trees under a temp directory and runs the real .bat / .ps1
files from this checkout inside them, with stubs standing in for the things that must never
be touched from a test on the owner's machine (the network, devtunnel, a real uv download).
Nothing here reads or writes the repository's own .env, .venv or .setup.

Not a test module (no test_ prefix); imported by the test_install_path_*.py files.
ASCII / ENGLISH ONLY.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import childproc  # noqa: E402

SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = shutil.which("powershell") or os.path.join(
    SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
CSC = os.path.join(SYSROOT, "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")

#: Windows PowerShell 5.1's own documented default PSModulePath (user, all-users, system).
#: run_ps() below sets this for every "powershell" (5.1) child it spawns instead of letting it
#: inherit ours. This test process is usually itself a child of a PowerShell 7 (pwsh) shell --
#: that is GitHub Actions windows-latest's default shell for a `run:` step, and this repo's
#: pytest invocation runs under it -- and pwsh's PSModulePath (its own Modules dir first) makes
#: 5.1 resolve a cmdlet like Get-AuthenticodeSignature to pwsh 7's own Microsoft.PowerShell.
#: Security module (built for .NET (Core), not the .NET Framework CLR 5.1 runs on) and fail to
#: load it: "the 'Get-AuthenticodeSignature' command was found in the module ..., but the
#: module could not be loaded." Measured in CI, 2026-09-24: this took down
#: scripts/test_setup_devtunnel_signature.py entirely, since every one of its cases goes
#: through run_ps(). It is the same defect setup.bat/quickstart.bat now guard against for real
#: users of a pwsh terminal (a3415bf) -- this is that same fix for the test's own child.
_PS51_DEFAULT_MODULE_PATH = os.pathsep.join([
    os.path.join(os.environ.get("UserProfile", ""), "Documents", "WindowsPowerShell", "Modules"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "WindowsPowerShell", "Modules"),
    os.path.join(SYSROOT, "System32", "WindowsPowerShell", "v1.0", "Modules"),
])

IS_WINDOWS = os.name == "nt"


# ---- PowerShell function extraction --------------------------------------------------------

def extract_ps_function(text: str, name: str) -> str:
    """`function <name> { ... }` by balanced braces (not PowerShell-aware; the functions
    extracted from this project's scripts keep their braces balanced inside strings)."""
    marker = "function " + name
    idx = text.index(marker)
    start = text.index("{", idx)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[idx:i + 1]
    raise AssertionError("unbalanced braces extracting %s" % name)


def run_ps(script_text: str, tmpdir: Path, timeout: int = 120, env=None):
    """Write `script_text` to a temp .ps1 (UTF-8 with BOM, so PS 5.1 reads it as UTF-8) and run
    it with -File. Returns the CompletedProcess (stdout/stderr decoded).

    PSModulePath is reset to Windows PowerShell 5.1's own default (see
    `_PS51_DEFAULT_MODULE_PATH` above) unless the caller's `env` already sets it -- this
    process itself usually runs under pwsh (PowerShell 7), whose PSModulePath would otherwise
    leak into the 5.1 child unchanged and break command auto-load for anything sharing a module
    name with a pwsh-7-only module (Get-AuthenticodeSignature, measured 2026-09-24)."""
    p = Path(tmpdir) / ("drv_%d.ps1" % (abs(hash(script_text)) % 10 ** 8))
    p.write_bytes(b"\xef\xbb\xbf" + script_text.encode("utf-8"))
    # `env` may be None (inherit ours -- which already HAS a PSModulePath, so a plain
    # setdefault would keep the polluted one) or a caller-supplied dict that intentionally
    # sets its own. Either way, override to the 5.1 default UNLESS the caller's dict already
    # named PSModulePath explicitly -- that is the one way a caller opts out of this.
    #
    # os.environ on Windows keeps whatever CASE the process actually inherited the name in
    # (measured here: "PSMODULEPATH", all upper) -- dict(os.environ) carries that same key
    # verbatim, so assigning child_env["PSModulePath"] (mixed case) adds a SECOND, DIFFERENTLY
    # -CASED entry instead of replacing it. subprocess/CreateProcess then hands the child BOTH,
    # and the child's own (case-insensitive but first-match) lookup can still see the original,
    # polluted one -- silently undoing this override. Strip every existing spelling first.
    #
    # The caller's own value is read BEFORE the strip: the first version stripped every
    # spelling and then only re-added the default, so a caller that named PSModulePath -- the
    # documented opt-out -- had it deleted and the child ran with no module path at all.
    child_env = dict(os.environ) if env is None else dict(env)
    caller_value = None
    if env is not None:
        for k, v in env.items():
            if k.upper() == "PSMODULEPATH":
                caller_value = v
    for _existing in [k for k in child_env if k.upper() == "PSMODULEPATH"]:
        del child_env[_existing]
    child_env["PSModulePath"] = (_PS51_DEFAULT_MODULE_PATH if caller_value is None
                                 else caller_value)
    return childproc.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                         timeout=timeout, env=child_env, cwd=str(tmpdir))


def ps_functions(rel: str, *names: str) -> str:
    src = (REPO / rel).read_text(encoding="utf-8-sig")
    return "\n\n".join(extract_ps_function(src, n) for n in names)


# ---- batch-file runs -----------------------------------------------------------------------

def crlf_copy(src: Path, dst: Path) -> None:
    """Copy a text file with CRLF endings: what a checkout produces under .gitattributes, and
    what cmd needs for reliable label lookup."""
    data = Path(src).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def minimal_path(*extra_dirs) -> str:
    """A PATH with the system tools cmd scripts need and NO Python, py launcher or uv."""
    base = [os.path.join(SYSROOT, "System32"), os.path.join(SYSROOT, "System32", "Wbem"),
            os.path.join(SYSROOT, "System32", "WindowsPowerShell", "v1.0")]
    return os.pathsep.join([str(d) for d in extra_dirs] + base)


def clean_env(tree: Path, path: str, **extra) -> dict:
    """An environment that cannot reach the owner's profile-level tools: a fake USERPROFILE and
    LOCALAPPDATA (so no WinGet\\Links\\uv.exe or ~/.local/bin/uv.exe is found), no proxy unless
    the test sets one, and FROM_QUICKSTART so setup.bat does not pause."""
    prof = Path(tree).parent / "fakeprofile"
    (prof / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                                "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "MCP_TUNNEL_ALLOW_ANONYMOUS",
                                "SETUP_UNBLOCK", "SETUP_IGNORE_POLICY", "UV_INSTALLER_URL",
                                "SETUP_PREFER_UV",
                                # Set by some shells/harnesses; it stops cmd finding a bare
                                # `setup.bat` in the current directory. The scripts must not
                                # depend on that lookup, and the tests call them by full path.
                                "NODEFAULTCURRENTDIRECTORYINEXEPATH")}
    env.update({
        "PATH": path,
        "USERPROFILE": str(prof),
        "LOCALAPPDATA": str(prof / "AppData" / "Local"),
        "FROM_QUICKSTART": "1",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run_cmd(bat: str, tree: Path, env: dict, args=(), stdin: str = "", timeout: int = 300):
    return childproc.run(["cmd", "/c", str(Path(tree) / bat), *args], cwd=str(tree), env=env,
                         input=stdin, timeout=timeout)


# ---- stubs ---------------------------------------------------------------------------------

UV_STUB_CS = r"""
using System; using System.IO; using System.Diagnostics;
class UvStub {
    static int Main(string[] a) {
        string log = Environment.GetEnvironmentVariable("UV_STUB_LOG");
        if (!String.IsNullOrEmpty(log)) File.AppendAllText(log, String.Join(" ", a) + Environment.NewLine);
        if (a.Length > 0 && a[0] == "--version") { Console.WriteLine("uv 0.0.0-stub"); return 0; }
        if (a.Length > 1 && a[0] == "python" && a[1] == "install") return 0;
        if (a.Length > 0 && a[0] == "venv") {
            string py = Environment.GetEnvironmentVariable("UV_STUB_PYTHON");
            ProcessStartInfo psi = new ProcessStartInfo(py, "-m venv --without-pip .venv");
            psi.UseShellExecute = false;
            Process p = Process.Start(psi); p.WaitForExit(); return p.ExitCode;
        }
        return 2;
    }
}
"""

_UV_STUB_CACHE: dict = {}


def uv_stub_exe(workdir: Path) -> Path | None:
    """A real .exe standing in for uv (setup.bat tests for uv.exe by name and runs it).
    Logs its argv to %UV_STUB_LOG%; `venv` builds a pip-less venv with %UV_STUB_PYTHON%.
    None when csc.exe is absent."""
    if not os.path.isfile(CSC):
        return None
    if "exe" in _UV_STUB_CACHE and Path(_UV_STUB_CACHE["exe"]).is_file():
        return Path(_UV_STUB_CACHE["exe"])
    d = Path(tempfile.mkdtemp(prefix="uvstub_"))
    cs = d / "uvstub.cs"
    cs.write_text(UV_STUB_CS, encoding="ascii")
    exe = d / "uv.exe"
    r = childproc.run([CSC, "/nologo", "/out:" + str(exe), str(cs)], timeout=120)
    if r.returncode != 0 or not exe.is_file():
        raise AssertionError("could not build the uv stub: %s %s" % (r.stdout, r.stderr))
    _UV_STUB_CACHE["exe"] = str(exe)
    return exe


STUB_BOOTSTRAP = (
    "import sys\n"
    "print('STUB-BOOTSTRAP python=%d.%d exe=%s args=%s' % (sys.version_info[0], "
    "sys.version_info[1], sys.executable, sys.argv[1:]))\n"
    "sys.exit(0)\n"
)


def base_python() -> str:
    """A real Python >= 3.10 to build throwaway venvs with (the one running the tests)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def setup_tree(root: Path, name: str = "repo") -> Path:
    """A throwaway tree holding THIS checkout's setup.bat and the scripts it runs, with a stub
    bootstrap.py that only reports which interpreter launched it."""
    tree = Path(root) / name
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    crlf_copy(REPO / "setup.bat", tree / "setup.bat")
    for s in ("preflight_policy.ps1", "detect_proxy.ps1", "ca_bundle.ps1"):
        shutil.copyfile(REPO / "scripts" / s, tree / "scripts" / s)
    (tree / "scripts" / "bootstrap.py").write_text(STUB_BOOTSTRAP, encoding="ascii")
    return tree


def make_venv(tree: Path) -> Path:
    r = childproc.run([base_python(), "-m", "venv", "--without-pip", str(tree / ".venv")],
                      timeout=300)
    assert r.returncode == 0, r.stderr
    return tree / ".venv" / "Scripts" / "python.exe"
