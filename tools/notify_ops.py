import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

POWERSHELL_TIMEOUT = 20


def notify_approval_gate(title: str, body: str, gate_path: str | Path) -> str:
    """Show the normal toast and open FleetCockpit's actionable gate prompt.

    The prompt runs in a separate GUI process, so a long-running worker is never
    blocked while the user decides.  It also provides the live confirmation,
    auto, and bypass policy controls.
    """
    toast_result = notify_desktop(title, body) or "[notification handler returned no status]"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return toast_result + "; approval prompt suppressed under pytest"

    try:
        gate = Path(gate_path).expanduser().resolve()
        if gate.suffix.lower() != ".json" or not gate.name.lower().startswith("gate_"):
            return toast_result + "; approval prompt rejected invalid gate path"
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
    """True iff a cockpit process is up. Split out so a test can answer it without a machine.

    Kept separate because the two halves of the guard fail differently and have to be testable
    apart: the cooldown is arithmetic, this reads the world. A test of the cooldown that also
    consults the real process list passes or fails on whether the operator happens to have the
    dashboard open, which is not what it is asking.
    """
    try:
        import psutil
        want = COCKPIT.name.lower()
        return any((p.info.get("name") or "").lower() == want
                   for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def _dashboard_already_up() -> str:
    """Why another dashboard must not be opened right now, or "" if one may be.

    Two independent reasons, because either alone leaks. A cooldown does not notice a window
    the operator left open from yesterday; a running-process check does not stop a burst that
    all fires before the first process appears in the list.
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
        r = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=POWERSHELL_TIMEOUT,
        )
        if r.returncode != 0:
            return f"[notify_desktop error: PowerShell exit {r.returncode}\n{r.stderr.strip()}]"
        return f"Notification sent: {title}"
    except subprocess.TimeoutExpired:
        return f"[notify_desktop timeout after {POWERSHELL_TIMEOUT}s]"
    except Exception as e:
        return f"[notify_desktop error: {type(e).__name__}: {e}]"
