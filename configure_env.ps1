# configure_env.ps1 -- GUI dialog to fill the M365 Copilot agent URLs into .env (no hand-editing).
# Pops one window with a field per agent; paste each URL, click Save, and .env is updated in place
# (existing secrets / other keys are preserved). Pre-fills any value already in .env so it is editable;
# for the first-party Researcher / Analyst agents it pre-fills a known-good DEFAULT so they usually
# work with no typing at all.
#
#   .\configure_env.ps1                              # all four fields
#   .\configure_env.ps1 -Only MCP_RESEARCHER_AGENT_URL   # one field (used by the dialog fallback)
#   .\configure_env.ps1 -Reason "Researcher did not load"  # show a banner explaining why it popped
#
# How to get a URL: open M365 Copilot (https://m365.cloud.microsoft/chat), pick the agent in the left
# sidebar, start a chat, and copy the URL from the address bar.
param(
    [string]$EnvPath = "",
    [string]$Only    = "",   # when set, show ONLY this one MCP_*_AGENT_URL field
    [string]$Reason  = ""    # optional banner explaining why this dialog popped
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $EnvPath) { $EnvPath = Join-Path $root ".env" }

# Known-good defaults for the FIRST-PARTY agents (Researcher / Analyst). These are the same
# Microsoft agents for every M365 Copilot user, so they pre-fill and usually work as-is. The
# main (T_) agent is tenant-specific, so it has NO default -- the user must paste it.
$defaults = @{
    "MCP_RESEARCHER_AGENT_URL" = "https://m365.cloud.microsoft/chat/agent/P_552e6eda-fc18-7fb9-0ef6-1bf2de3393e4.dr_work"
    "MCP_ANALYST_AGENT_URL"    = "https://m365.cloud.microsoft/chat/agent/P_8cfc4e6f-267e-db15-c6e7-3fc47a54f61e.diceberry"
}

# --- read current .env values --------------------------------------------------------------
function Get-EnvVal([string[]]$lines, [string]$key) {
    $m = $lines | Where-Object { $_ -match "^\s*$([regex]::Escape($key))\s*=" } | Select-Object -First 1
    if ($m) { return ($m -replace "^\s*$([regex]::Escape($key))\s*=\s*", "") }
    return ""
}
$lines = @()
if (Test-Path $EnvPath) { $lines = @(Get-Content $EnvPath) }

$fields = @(
    @{ Key = "MCP_IMPL_AGENT_URL";       Label = "メイン エージェント (必須)";        Hint = "チャット＆フリートが操作する主エージェント。M365 Copilot で開いた時の URL バーの URL。" },
    @{ Key = "MCP_FLEET_AGENT_URL";      Label = "フリート用 (任意)";                Hint = "並列実行が使うエージェント。空ならメインと同じものを使います。" },
    @{ Key = "MCP_RESEARCHER_AGENT_URL"; Label = "リサーチ用 (既定値あり)";           Hint = "/research が使う調査エージェント (Researcher)。既定値で大抵動きます。違う場合だけ貼り替え。" },
    @{ Key = "MCP_ANALYST_AGENT_URL";    Label = "アナリスト用 (既定値あり)";         Hint = "/analyze が使う分析エージェント (Analyst)。既定値で大抵動きます。違う場合だけ貼り替え。" }
)
# -Only narrows the form to a single field (the dialog-fallback case).
if ($Only) { $fields = @($fields | Where-Object { $_.Key -eq $Only }) }
if (-not $fields -or $fields.Count -eq 0) {
    $fields = @(@{ Key = $Only; Label = $Only; Hint = "M365 Copilot で対象エージェントを開き、アドレスバーの URL を貼り付け。" })
}

# --- build the form ------------------------------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "Copilot エージェント URL の設定 (.env)"
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$y = 12
# Optional reason banner (shown when the relay pops this because an agent did not load).
if ($Reason) {
    $banner = New-Object System.Windows.Forms.Label
    $banner.Text = $Reason
    $banner.SetBounds(16, $y, 680, 50)
    $banner.ForeColor = [System.Drawing.Color]::FromArgb(180, 60, 0)
    $banner.Font = New-Object System.Drawing.Font($banner.Font, [System.Drawing.FontStyle]::Bold)
    $form.Controls.Add($banner)
    $y += 56
}

$intro = New-Object System.Windows.Forms.Label
$intro.Text = "各エージェントの URL を貼り付けて [保存] を押すと .env に反映されます。" + [Environment]::NewLine +
              "URL の取り方: M365 Copilot (https://m365.cloud.microsoft/chat) で対象エージェントを開き、アドレスバーの URL をコピー。"
$intro.SetBounds(16, $y, 680, 40)
$form.Controls.Add($intro)
$y += 48

$boxes = @{}
foreach ($f in $fields) {
    $lbl = New-Object System.Windows.Forms.Label
    $cur = Get-EnvVal $lines $f.Key
    # When .env has no value, pre-fill the known-good default (Researcher / Analyst) so the
    # field is usable with no typing. The main (T_) agent has no default -> stays blank.
    if (-not $cur -and $defaults.ContainsKey($f.Key)) { $cur = $defaults[$f.Key] }
    $lbl.Text = $f.Label + "   [" + $f.Key + "]"
    $lbl.SetBounds(16, $y, 680, 18); $lbl.Font = New-Object System.Drawing.Font($lbl.Font, [System.Drawing.FontStyle]::Bold)
    $form.Controls.Add($lbl)
    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = $f.Hint
    $hint.SetBounds(16, ($y + 20), 680, 18); $hint.ForeColor = [System.Drawing.Color]::DimGray
    $form.Controls.Add($hint)
    $tb = New-Object System.Windows.Forms.TextBox
    $tb.SetBounds(16, ($y + 40), 672, 24); $tb.Text = $cur
    $form.Controls.Add($tb)
    $boxes[$f.Key] = $tb
    $y += 78
}

$save = New-Object System.Windows.Forms.Button
$save.Text = "保存して閉じる"; $save.SetBounds(470, ($y + 6), 130, 30); $save.DialogResult = "OK"
$form.Controls.Add($save); $form.AcceptButton = $save
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = "キャンセル"; $cancel.SetBounds(608, ($y + 6), 90, 30); $cancel.DialogResult = "Cancel"
$form.Controls.Add($cancel); $form.CancelButton = $cancel

# Size the window to the content (so -Only shows a compact one-field dialog).
$form.ClientSize = New-Object System.Drawing.Size(712, ($y + 50))

$result = $form.ShowDialog()
if ($result -ne "OK") { Write-Host "cancelled - .env not changed"; exit 0 }

# --- write the values back, preserving everything else -------------------------------------
foreach ($f in $fields) {
    $val = $boxes[$f.Key].Text.Trim()
    if (-not $val) { continue }
    $line = "$($f.Key)=$val"
    if ($lines | Where-Object { $_ -match "^\s*$([regex]::Escape($f.Key))\s*=" }) {
        $lines = $lines | ForEach-Object { if ($_ -match "^\s*$([regex]::Escape($f.Key))\s*=") { $line } else { $_ } }
    } else {
        $lines += $line
    }
}
# Write UTF-8 WITHOUT a BOM. PowerShell 5.1's `Set-Content -Encoding UTF8` PREPENDS a BOM,
# which corrupts the first line for plain parsers: bootstrap.py then read .env's first key
# as "﻿MCP_API_KEY" and reported MCP_API_KEY missing, and python-dotenv left it unset
# (main.py crashed with KeyError). UTF8Encoding($false) = no BOM. CRLF line endings.
$text = ($lines -join "`r`n") + "`r`n"
[System.IO.File]::WriteAllText($EnvPath, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Saved agent URLs to $EnvPath"
[System.Windows.Forms.MessageBox]::Show(".env に保存しました。", "完了", "OK", "Information") | Out-Null
