import json
import os
import shutil
import subprocess
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


def notify_desktop(
    title: str,
    body: str,
    app_id: str = "m365-copilot-companion-mcp",
    icon_path: Optional[str] = None,
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

        safe_title = json.dumps(str(title), ensure_ascii=False)
        safe_body = json.dumps(str(body or ""), ensure_ascii=False)
        safe_app = json.dumps(str(app_id), ensure_ascii=False)

        # Two-stage, resilient. Stage 1: a proper WinRT toast built from a
        # template (GetTemplateContent avoids `New-Object XmlDocument`, which is
        # not projected in Windows PowerShell and was the original failure).
        # Stage 2 (fallback): a tray balloon via System.Windows.Forms, which
        # reliably surfaces from a console process and lands in the Action Center
        # on Windows 10/11. One of these will fire on any normal interactive PC.
        ps_script = f"""
$ErrorActionPreference = 'Stop'
$title = {safe_title}
$body  = {safe_body}
$appId = {safe_app}
$shown = $false
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $tt = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($tt)
    $texts = $xml.GetElementsByTagName('text')
    [void]$texts.Item(0).AppendChild($xml.CreateTextNode($title))
    [void]$texts.Item(1).AppendChild($xml.CreateTextNode($body))
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
