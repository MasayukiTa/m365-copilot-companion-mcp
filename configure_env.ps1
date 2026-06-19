# configure_env.ps1 -- GUI dialog to fill the M365 Copilot agent URLs into .env (no hand-editing).
# Pops one window with a field per agent; paste each URL, click Save, and .env is updated in place
# (existing secrets / other keys are preserved). Pre-fills any value already in .env so it is editable.
#
#   .\configure_env.ps1
#
# How to get a URL: open M365 Copilot (https://m365.cloud.microsoft/chat), pick the agent in the left
# sidebar, start a chat, and copy the URL from the address bar.
param([string]$EnvPath = "")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $EnvPath) { $EnvPath = Join-Path $root ".env" }

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
    @{ Key = "MCP_RESEARCHER_AGENT_URL"; Label = "リサーチ用 (任意)";                Hint = "/research が使う調査エージェント (Deep Research 等) の URL。" },
    @{ Key = "MCP_ANALYST_AGENT_URL";    Label = "アナリスト用 (任意)";              Hint = "/analyze が使う分析エージェントの URL。" }
)

# --- build the form ------------------------------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "Copilot エージェント URL の設定 (.env)"
$form.Size = New-Object System.Drawing.Size(720, 470)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$intro = New-Object System.Windows.Forms.Label
$intro.Text = "各エージェントの URL を貼り付けて [保存] を押すと .env に反映されます。" + [Environment]::NewLine +
              "URL の取り方: M365 Copilot (https://m365.cloud.microsoft/chat) で対象エージェントを開き、アドレスバーの URL をコピー。"
$intro.SetBounds(16, 12, 680, 40)
$form.Controls.Add($intro)

$boxes = @{}
$y = 60
foreach ($f in $fields) {
    $lbl = New-Object System.Windows.Forms.Label
    $cur = Get-EnvVal $lines $f.Key
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
Set-Content -Path $EnvPath -Value $lines -Encoding UTF8
Write-Host "Saved agent URLs to $EnvPath"
[System.Windows.Forms.MessageBox]::Show(".env に保存しました。", "完了", "OK", "Information") | Out-Null
