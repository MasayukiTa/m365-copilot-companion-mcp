import json
import shutil
import subprocess
from typing import Optional

POWERSHELL_TIMEOUT = 20


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
