import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

POWERSHELL_TIMEOUT = 20


def _user_profile() -> str:
    """%USERPROFILE%, the same folder Environment.SpecialFolder.UserProfile resolves to on
    the .NET side. A thin wrapper only so tests have one place to monkeypatch it."""
    return os.path.expanduser("~")


def resolve_gate_directory(file_base: str = "", file_gate_dir: str = "") -> str:
    """Mirror ui/FleetCockpit.cs `CockpitWindow.ResolveGateDirectory`, in the SAME order,
    so this launcher can ask the cockpit's own question before spawning it: "will the
    process I am about to start be able to find this gate file at all?"

    THIS MUST STAY IN LOCKSTEP WITH THE C# METHOD OF THE SAME NAME. It exists because the
    two were drifting: gate writers (relay/skills.py, tools/gate_ops.py, tools/contract_gate.py,
    tools/task_router.py) each pick their own gate directory -- often a temp dir with
    MCP_SKILLS_GATE_DIR or SkillStore(gate_dir=...), neither of which the cockpit's
    ApprovalPromptWindow has ever heard of -- while the WINDOW that is supposed to display the
    gate resolves its own directory independently in FleetCockpit.cs. When the two disagreed,
    the window still opened, immediately refused the gate as "not in the gate directory", and
    closed -- a window whose only purpose was to report that it had failed. Diagnosed from a
    real MessageBox naming `...\\tierbench_1e_zcl1p\\gates` as the gate's folder and
    `C:\\Users\\<user>\\.companion_gates` as the one the prompt would accept.

    `file_base` / `file_gate_dir` exist only so this signature matches the C# one exactly
    (a settings-file fallback for MCP_ALLOWED_BASE / MCP_GATE_DIR); the ApprovalPromptWindow
    constructor -- the ONLY caller that matters for --approval-gate -- calls
    `ResolveGateDirectory("", "")`, i.e. it never reads that fallback, only the process
    environment the child inherits from this launcher. Callers here should do the same.
    """
    # MCP_GATE_DIR FIRST -- same order as the C# side, and for the same reason: it is what a
    # test suite (or a benchmark script) sets to steer its gates away from the operator's queue.
    over = os.environ.get("MCP_GATE_DIR") or ""
    if not over.strip():
        over = file_gate_dir or ""
    over = over.strip().strip('"')
    if over:
        return os.path.abspath(over)

    raw = os.environ.get("MCP_ALLOWED_BASE") or ""
    if not raw.strip():
        raw = file_base or ""
    raw = raw.strip()
    if not raw or raw == "*":
        base_path = _user_profile()
    else:
        # Path.PathSeparator is ';' on Windows -- MCP_ALLOWED_BASE may list several roots,
        # and only the first one is the gate root, exactly as the C# side takes roots[0].
        roots = raw.split(os.pathsep)
        base_path = roots[0].strip().strip('"') if roots else ""
        if base_path == "~":
            base_path = _user_profile()
        elif base_path.startswith("~\\") or base_path.startswith("~/"):
            base_path = os.path.join(_user_profile(), base_path[2:])
        if len(base_path) == 2 and base_path[1] == ":":
            base_path += os.sep
        if not base_path:
            base_path = _user_profile()
    # Path.GetFullPath does NOT expand an 8.3 short name, and neither does os.path.abspath --
    # both just normalise separators and "..". That asymmetry is exactly why the membership
    # check below asks the filesystem instead of comparing this string to another one.
    return os.path.join(os.path.abspath(base_path), ".companion_gates")


def gate_is_reachable(gate_path: str | Path) -> Tuple[bool, str, str, str]:
    """Would ui/FleetCockpit.cs's ApprovalPromptWindow actually accept this gate file?

    Mirrors the check in the ApprovalPromptWindow constructor (ui/FleetCockpit.cs, around the
    `_gateDir = Path.GetDirectoryName(full)` / `Directory.GetFiles(allowed, ...)` lines): the
    filename must look like a gate (`gate_*.json`), and the file must actually be found inside
    `resolve_gate_directory()`'s answer.

    Returns (ok, gate_dir, allowed_dir, reason). `reason` is "" when ok is True, else one of a
    few short machine-stable strings a caller can match on or simply display.

    FILESYSTEM IDENTITY, NOT STRING SPELLING. A gate directory under %TEMP% can be handed back
    in its 8.3 short form by one caller while `resolve_gate_directory()` is spelled long (or the
    reverse) -- comparing the two directory strings failed exactly there in the C# window before
    it was rewritten to enumerate the target directory instead of trusting a string compare (see
    the comment at FleetCockpit.cs ApprovalPromptWindow, "ASK THE FILESYSTEM, DO NOT COMPARE
    SPELLINGS"). `os.path.samefile` and directory listing both ask Windows the same question the
    fix there asks: is this the same directory, whatever either side happened to call it.
    """
    full = os.path.abspath(str(gate_path))
    gate_dir = os.path.dirname(full)
    name = os.path.basename(full)
    allowed = resolve_gate_directory()

    if not name.lower().startswith("gate_") or os.path.splitext(name)[1].lower() != ".json":
        return False, gate_dir, allowed, "invalid gate filename"

    if not os.path.isdir(allowed):
        return False, gate_dir, allowed, "prompt directory does not exist"

    try:
        if os.path.isdir(gate_dir) and os.path.samefile(gate_dir, allowed):
            if os.path.isfile(full):
                return True, gate_dir, allowed, ""
            return False, gate_dir, allowed, "gate file does not exist"
    except OSError:
        pass

    # Fall back to exactly what the C# side does: list the ALLOWED directory for the exact
    # file name, rather than trust any directory-path comparison at all.
    try:
        entries = os.listdir(allowed)
    except OSError:
        return False, gate_dir, allowed, "prompt directory is not listable"
    if any(entry.lower() == name.lower() for entry in entries):
        return True, gate_dir, allowed, ""
    return False, gate_dir, allowed, "outside the prompt's directory"


def notify_approval_gate(title: str, body: str, gate_path: str | Path) -> str:
    """Show the normal toast and open FleetCockpit's actionable gate prompt.

    The prompt runs in a separate GUI process, so a long-running worker is never
    blocked while the user decides.  It also provides the live confirmation,
    auto, and bypass policy controls.
    """
    # A TOAST THAT NO CLICK CAN ANSWER IS NOISE. The gate has to be one the approval prompt
    # can open (same question gate_is_reachable asks below); otherwise the person gets a
    # notification, clicks it, and nothing happens -- observed 2026-09-24 with gates written
    # into temp directories. Such a gate is not the operator's to answer, so it gets no toast.
    try:
        _reachable_for_toast = gate_is_reachable(Path(gate_path).expanduser().resolve())[0]
    except Exception:
        _reachable_for_toast = True   # cannot tell: notify rather than lose a real question
    if not _reachable_for_toast:
        toast_result = "[toast not shown: gate is outside the approval prompt's directory]"
    else:
        toast_result = notify_desktop(title, body) or "[notification handler returned no status]"
    # PYTEST_CURRENT_TEST IS NOT ENOUGH, AND THE GAP IS NOT SUBTLE. pytest sets it only while a
    # test FUNCTION runs; collection, session fixtures and teardown all run without it, and a
    # gate written in any of those phases opened a real window on the operator's desktop. It
    # could not even be answered: the skills store writes through MCP_SKILLS_GATE_DIR while this
    # prompt resolves MCP_GATE_DIR, which are the same directory in production and deliberately
    # different under test, so the window appeared solely to report that it had failed.
    # MCP_SUPPRESS_GUI is set by conftest at module scope and therefore covers every phase.
    if os.environ.get("MCP_SUPPRESS_GUI") == "1":
        return toast_result + "; approval prompt suppressed (MCP_SUPPRESS_GUI)"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return toast_result + "; approval prompt suppressed under pytest"

    try:
        gate = Path(gate_path).expanduser().resolve()
        if gate.suffix.lower() != ".json" or not gate.name.lower().startswith("gate_"):
            return toast_result + "; approval prompt rejected invalid gate path"
        # ASK THE SAME QUESTION THE COCKPIT WILL ASK, BEFORE SPAWNING IT. FleetCockpit.exe's
        # ApprovalPromptWindow refuses any gate it does not find inside
        # CockpitWindow.ResolveGateDirectory("", "")'s answer and then closes itself, having
        # painted nothing but a MessageBox that says so -- a window whose only purpose was to
        # report its own failure. A gate written by a caller with a non-default gate directory
        # (a benchmark script's SkillStore(gate_dir=...) or MCP_SKILLS_GATE_DIR, without also
        # setting MCP_GATE_DIR or MCP_SUPPRESS_GUI) hits this every time. gate_is_reachable
        # mirrors that same resolution in Python so THIS process -- which shares the same
        # environment the child inherits -- can tell in advance and simply not open the window.
        reachable, gate_dir, allowed_dir, reason = gate_is_reachable(gate)
        if not reachable:
            return (toast_result +
                    f"; approval prompt not opened: gate {gate_dir} is outside "
                    f"the prompt's directory {allowed_dir} ({reason})")
        cockpit = Path(__file__).resolve().parents[1] / "ui" / "FleetCockpit.exe"
        if not cockpit.is_file():
            return toast_result + "; approval prompt unavailable (FleetCockpit.exe not built)"
        subprocess.Popen(
            [str(cockpit), "--approval-gate", str(gate)],
            cwd=str(cockpit.parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return toast_result + "; actionable approval prompt opened"
    except Exception as e:
        return toast_result + f"; approval prompt error: {type(e).__name__}: {e}"


#: The built cockpit. A module constant rather than a path computed inside the function, so a
#: test can point it at a file that is not there -- the "never built" branch is the one that
#: runs on every host that only serves, and it had no test until it was made reachable.
COCKPIT = Path(__file__).resolve().parents[1] / "ui" / "FleetCockpit.exe"


#: When a dashboard was last opened from here, and how long before another may be.
#:
#: A NOTIFICATION MUST NOT BE ABLE TO SPAWN AN UNBOUNDED NUMBER OF WINDOWS.
#:
#: This opened one per notified event, with no cooldown and no check for a window already up.
#: Forty-two notifiable acts landed in one day -- twenty-three re-signings and nineteen
#: mismatches -- and the machine spent the afternoon opening forty-two copies of a WPF window
#: until Claude was killed and then the PC went down. Neither half was fatal alone: the acts
#: were too many, and a per-event window launcher survives only a workload that never bursts.
#:
#: The fix is here rather than at the caller because it is this function's promise that was
#: wrong. Every caller, present and future, gets the bound.
_DASHBOARD_LAST = [0.0]
DASHBOARD_COOLDOWN_S = 300.0


def cockpit_running() -> bool:
    """True iff the AUTHORITY dashboard is up. Split out so a test can answer it without a machine.

    Kept separate because the two halves of the guard fail differently and have to be testable
    apart: the cooldown is arithmetic, this reads the world. A test of the cooldown that also
    consults the real process list passes or fails on whether the operator happens to have the
    dashboard open, which is not what it is asking.

    THE PROCESS NAME IS NOT THE WINDOW. One executable serves three windows -- the ordinary
    cockpit, the approval prompt and this dashboard -- so matching the image name reported
    "already running" whenever the operator had the cockpit open, which is nearly always, and
    the re-signing notification then opened nothing at all. Measured: with the cockpit up, four
    consecutive calls were refused and no dashboard ever appeared. The argv is what distinguishes
    them, so the argv is what gets read.

    A process whose cmdline cannot be read (access denied on a foreign owner) is not counted:
    the harm of one extra window is small and bounded, and the harm of silently refusing to
    open the control is the failure this whole path exists to prevent.
    """
    try:
        import psutil
        want = COCKPIT.name.lower()
        for proc in psutil.process_iter(["name", "cmdline"]):
            if (proc.info.get("name") or "").lower() != want:
                continue
            argv = proc.info.get("cmdline") or []
            if any(str(a).lower() == "--authority" for a in argv[1:]):
                return True
        return False
    except Exception:
        return False


def _dashboard_already_up() -> str:
    """Why another dashboard must not be opened right now, or "" if one may be.

    Two independent reasons, because either alone leaks. A cooldown does not notice a window
    the operator left open from yesterday; a running-process check does not stop a burst that
    all fires before the first process appears in the list.

    NEITHER OF THESE IS THE LOAD-BEARING GUARD, and the file should not pretend otherwise. The
    cooldown lives in module state, so it only sees repeats inside ONE python process; the
    re-signings that produced 24 windows in a day were 24 separate CLI invocations, and this
    arithmetic was blind to every one of them. What actually bounds the count is the named
    mutex FleetCockpit.exe takes on --authority: it is cross-process, it cannot race, and a
    second launch raises the open window instead of adding one. These two remain because they
    are cheaper than spawning a process that will immediately exit -- an optimisation, not the
    safety property.
    """
    now = time.time()
    if now - _DASHBOARD_LAST[0] < DASHBOARD_COOLDOWN_S:
        return "opened %.0fs ago" % (now - _DASHBOARD_LAST[0])
    if cockpit_running():
        return "already running"
    return ""


def open_authority_dashboard() -> str:
    """Open the self-improvement dashboard's Authority view. Returns "" when it cannot.

    THE CONTROL, NOT A DESCRIPTION OF IT. The dashboard already computes the frozen-set
    comparison itself rather than believing python, shows the ledger, and carries the button
    that withdraws the last re-signing. A notification about such an act should land the person
    there. Before this it opened a text file of commands to paste into a terminal, which the
    operator summarised as "and then what am I supposed to do with it".

    Best effort and non-blocking: the prompt runs in its own process, and a missing build is a
    normal state on a machine where the UI was never compiled -- the caller keeps its written
    briefing as the fallback for exactly that.
    """
    # THE SAME GUARD AS notify_desktop, and it was missing here. That function is inert under
    # pytest by construction -- checked once, so no test file has to remember -- while this one
    # launches a window on the operator's actual desktop. Both are reached from pending._notify,
    # so the layer stopped half way across a single call: today only the local fixtures in two
    # test files keep it quiet, which is the fail-open shape of a hand-written allowlist.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    try:
        cockpit = COCKPIT
        if not cockpit.is_file():
            return ""
        blocked = _dashboard_already_up()
        if blocked:
            return ""
        subprocess.Popen([str(cockpit), "--authority"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _DASHBOARD_LAST[0] = time.time()
        return str(cockpit)
    except Exception:
        return ""


def notify_desktop(
    title: str,
    body: str,
    app_id: str = "m365-copilot-companion-mcp",
    icon_path: Optional[str] = None,
    launch: str = "",
) -> str:
    """Show a native Windows toast notification on the host PC.

    Useful when a long-running task finishes (pair with job_wait), or when the
    agent wants to surface an event to the user without depending on the chat
    UI being focused.

    Args:
        title: Toast title (bold heading).
        body: Toast body text.
        app_id: AppId string shown as the source. Defaults to m365-copilot-companion-mcp.
        icon_path: Optional file:// path to a PNG/JPG icon.
        launch: Optional URI opened when the toast is CLICKED -- a file:/// path, a folder,
            an http(s) URL. A notification that reports something a person must decide about
            and then does nothing when clicked is an alarm, not a control; this is what turns
            it back into one. Best effort: toast activation depends on the AppId being
            registered, so a click that does nothing is still possible and the body must
            therefore carry the instructions in its own right.

    NEWLINES SURVIVE. The body used to be interpolated into a PowerShell double-quoted string,
    which does not interpret backslash-n -- so a multi-line body arrived as literal "
"
    markers and rendered as garbage. It is passed base64-encoded now and decoded on the far
    side, which is also what makes quotes and non-ASCII safe.
    """
    # SYSTEMIC pytest guard (2026-07): this is the single real chokepoint that
    # actually shells out to PowerShell to raise an OS toast. Every notify path
    # in the codebase (default_notify, task_router, contract_gate, gate_ops,
    # main.py) ultimately calls THIS function. pytest sets PYTEST_CURRENT_TEST
    # in the environment for the duration of every test it runs, so checking it
    # here -- once -- makes every test, present and future, inert by
    # construction instead of relying on each test file remembering to mock.
    # Production runtime never has this var set, so behavior there is unchanged.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "[notify_desktop suppressed: running under pytest]"
    # PYTEST_CURRENT_TEST DOES NOT REACH A CHILD PROCESS STARTED OUTSIDE A TEST FUNCTION, and
    # the owner saw the result on 2026-09-24: a stack of "Skill approval needed" toasts from
    # test runs, each naming a gate in a pytest temp directory, none of which a click could
    # open. conftest sets MCP_SUPPRESS_GUI=1 at module scope, so every child inherits it --
    # the same switch notify_approval_gate already honoured for its window, now honoured for
    # the toast too.
    if os.environ.get("MCP_SUPPRESS_GUI") == "1":
        return "[notify_desktop suppressed: MCP_SUPPRESS_GUI]"
    try:
        if not title:
            return "[notify_desktop error: title is required]"

        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            return "[notify_desktop error: PowerShell not found on PATH]"

        import base64 as _b64

        def _b(text):
            """Base64 of the UTF-8 bytes. Nothing in the value can then reach the shell."""
            return json.dumps(_b64.b64encode(str(text).encode("utf-8")).decode("ascii"))

        safe_title = _b(title)
        safe_body = _b(body or "")
        safe_app = json.dumps(str(app_id), ensure_ascii=False)
        safe_launch = json.dumps(str(launch or ""), ensure_ascii=False)

        # Two-stage, resilient. Stage 1: a proper WinRT toast built from a
        # template (GetTemplateContent avoids `New-Object XmlDocument`, which is
        # not projected in Windows PowerShell and was the original failure).
        # Stage 2 (fallback): a tray balloon via System.Windows.Forms, which
        # reliably surfaces from a console process and lands in the Action Center
        # on Windows 10/11. One of these will fire on any normal interactive PC.
        ps_script = f"""
$ErrorActionPreference = 'Stop'
function Dec($s) {{ [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)) }}
$title  = Dec {safe_title}
$body   = Dec {safe_body}
$appId  = {safe_app}
$launch = {safe_launch}
$shown = $false
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $tt = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($tt)
    $texts = $xml.GetElementsByTagName('text')
    [void]$texts.Item(0).AppendChild($xml.CreateTextNode($title))
    [void]$texts.Item(1).AppendChild($xml.CreateTextNode($body))
    if ($launch) {{
        # CLICKING SHOULD DO SOMETHING. Protocol activation opens the URI; if the AppId is not
        # registered Windows ignores it, which is why the body still has to stand alone.
        $xml.DocumentElement.SetAttribute('launch', $launch)
        $xml.DocumentElement.SetAttribute('activationType', 'protocol')
    }}
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    $shown = $true
}} catch {{ }}
if (-not $shown) {{
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.BalloonTipTitle = $title
    $n.BalloonTipText = $body
    $n.Visible = $true
    $n.ShowBalloonTip(8000)
    Start-Sleep -Seconds 4
    $n.Dispose()
    $shown = $true
}}
if ($shown) {{ Write-Output 'OK' }} else {{ throw 'notification: no method succeeded' }}
"""
        from tools.childproc import run as _run_child
        r = _run_child(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=POWERSHELL_TIMEOUT,
        )
        if r.returncode != 0:
            return f"[notify_desktop error: PowerShell exit {r.returncode}\n{r.stderr.strip()}]"
        return f"Notification sent: {title}"
    except subprocess.TimeoutExpired:
        return f"[notify_desktop timeout after {POWERSHELL_TIMEOUT}s]"
    except Exception as e:
        return f"[notify_desktop error: {type(e).__name__}: {e}]"
