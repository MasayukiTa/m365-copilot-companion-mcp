# Screen / region / window capture for the task router's `screenshot` job.
#   -Out <path>            where to save the PNG (required)
#   -Window <substring>    capture only the window whose title CONTAINS this (case-insensitive)
#   -Region "l,t,w,h"      capture a pixel region of the virtual screen
#   (none of the above)    capture the full virtual screen (all monitors)
#
# Window mode brings the match to the foreground, then copies its on-screen rectangle -- this is
# the reliable path for DWM/WPF windows (PrintWindow can return black for hardware-composited
# content). It steals focus for the instant of the grab, which is fine for a screenshot task.
param(
  [Parameter(Mandatory=$true)][string]$Out,
  [string]$Window = "",
  [string]$Proc = "",
  [string]$Region = ""
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinCap {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc e, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

# Per-Monitor-V2 so GetWindowRect and CopyFromScreen share ONE (physical-pixel) coordinate space.
# Without this PowerShell is DPI-virtualized: on a scaled/high-DPI monitor GetWindowRect returns
# logical coords while CopyFromScreen captures physical pixels -> the grabbed rect is offset and
# pulls in neighbouring windows. Must run before any window/graphics call. Best-effort (Win10 1703+).
try { [WinCap]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null } catch {}

function Save-Bitmap($bmp, $path) {
  $dir = Split-Path -Parent $path
  if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
}

function Capture-Rect($left, $top, $width, $height, $path) {
  if ($width -le 0 -or $height -le 0) { throw "bad rect ${width}x${height}" }
  $bmp = New-Object System.Drawing.Bitmap $width, $height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($left, $top, 0, 0, (New-Object System.Drawing.Size($width, $height)))
  Save-Bitmap $bmp $path
  $g.Dispose(); $bmp.Dispose()
}

function Capture-Hwnd($hwnd, $label, $path) {
  [WinCap]::ShowWindow($hwnd, 9) | Out-Null      # SW_RESTORE (un-minimize)
  [WinCap]::SetForegroundWindow($hwnd) | Out-Null
  Start-Sleep -Milliseconds 350
  # Prefer the DWM EXTENDED FRAME BOUNDS (the actually-visible window) over GetWindowRect, which
  # includes the invisible resize border (~7px/side) -> that border captured the desktop behind it.
  $r = New-Object WinCap+RECT
  $sz = [System.Runtime.InteropServices.Marshal]::SizeOf([type]([WinCap+RECT]))
  $hr = [WinCap]::DwmGetWindowAttribute($hwnd, 9, [ref]$r, $sz)   # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
  if ($hr -ne 0 -or ($r.Right - $r.Left) -le 0) { [WinCap]::GetWindowRect($hwnd, [ref]$r) | Out-Null }
  Capture-Rect $r.Left $r.Top ($r.Right - $r.Left) ($r.Bottom - $r.Top) $path
  Write-Output ("{0} {1}x{2} -> {3}" -f $label, ($r.Right-$r.Left), ($r.Bottom-$r.Top), $path)
}

if ($Proc -ne "") {
  # capture by PROCESS name (robust when the window has no title, e.g. FleetCockpit)
  $p = Get-Process -Name $Proc -ErrorAction SilentlyContinue |
       Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if (-not $p) { Write-Error "no running process '$Proc' with a visible window"; exit 2 }
  Capture-Hwnd $p.MainWindowHandle "PROC" $Out
}
elseif ($Window -ne "") {
  # find the first visible top-level window whose title contains $Window
  $script:found = [IntPtr]::Zero
  $cb = [WinCap+EnumProc]{
    param($h, $l)
    if ([WinCap]::IsWindowVisible($h)) {
      $sb = New-Object System.Text.StringBuilder 512
      [WinCap]::GetWindowText($h, $sb, $sb.Capacity) | Out-Null
      $t = $sb.ToString()
      if ($t -and $t.ToLower().Contains($Window.ToLower())) { $script:found = $h; return $false }
    }
    return $true
  }
  [WinCap]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  if ($script:found -eq [IntPtr]::Zero) { Write-Error "no visible window title contains '$Window'"; exit 2 }
  Capture-Hwnd $script:found "WINDOW" $Out
}
elseif ($Region -ne "") {
  $p = $Region.Split(",")
  Capture-Rect ([int]$p[0]) ([int]$p[1]) ([int]$p[2]) ([int]$p[3]) $Out
  Write-Output ("REGION -> {0}" -f $Out)
}
else {
  $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
  Capture-Rect $vs.Left $vs.Top $vs.Width $vs.Height $Out
  Write-Output ("FULL {0}x{1} -> {2}" -f $vs.Width, $vs.Height, $Out)
}
