import json
import shutil
import subprocess
from typing import Optional

POWERSHELL_TIMEOUT = 15


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
        icon_xml = ""
        if icon_path:
            safe_icon = json.dumps(str(icon_path), ensure_ascii=False)
            icon_xml = (
                f"<image placement='appLogoOverride' hint-crop='circle' src={safe_icon}/>"
            )

        # XML payload follows the WinRT ToastTemplate spec.
        ps_script = f"""
$ErrorActionPreference = 'Stop'
$title = {safe_title}
$body  = {safe_body}
$appId = {safe_app}
$xml = @"
<toast><visual><binding template='ToastGeneric'>
<text>$title</text>
<text>$body</text>
{icon_xml}
</binding></visual></toast>
"@
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
        r = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=POWERSHELL_TIMEOUT,
        )
        if r.returncode != 0:
            return f"[notify_desktop error: PowerShell exit {r.returncode}\n{r.stderr.strip()}]"
        return f"Toast sent: {title}"
    except subprocess.TimeoutExpired:
        return f"[notify_desktop timeout after {POWERSHELL_TIMEOUT}s]"
    except Exception as e:
        return f"[notify_desktop error: {type(e).__name__}: {e}]"
