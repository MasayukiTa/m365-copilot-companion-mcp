# ui_font.ps1 -- shared helper: a WinForms Font that can actually draw Japanese.
#
# WHY: a WinForms control that never sets an explicit .Font falls back to the WinForms default
# (Microsoft Sans Serif), which has no Japanese glyphs. Windows font linking then substitutes a
# fallback glyph source for the missing code points, and on a non-Japanese system locale (e.g.
# English Windows Sandbox) that fallback is a CHINESE font (SimSun / Microsoft YaHei) -- so
# Han-unified CJK code points get drawn with Chinese forms instead of Japanese ones, and
# Japanese text on screen looks like mis-shapen ("[?]") Chinese. Setting an explicit
# Japanese-capable font (Yu Gothic UI / Meiryo UI / MS UI Gothic, in that preference order) on
# every Form before adding controls means Windows never has to guess.
#
# PRECONDITION: `Add-Type -AssemblyName System.Drawing` must already have run in this process.
# Both call sites (scripts/start_all.ps1, scripts/configure_env.ps1) already do this before
# dot-sourcing this file, but this file also loads it itself defensively so it is safe to
# dot-source standalone (e.g. from a test probe).
#
# USAGE (dot-source from a sibling scripts/*.ps1 file):
#   . (Join-Path $PSScriptRoot "win/ui_font.ps1")
#   $form.Font = Get-JapaneseUiFont
#   $bold = Get-JapaneseUiFont -SizePoints 13 -Style Bold

try { Add-Type -AssemblyName System.Drawing | Out-Null } catch { }

function Get-JapaneseUiFont {
    param(
        [single]$SizePoints = 9,
        [System.Drawing.FontStyle]$Style = [System.Drawing.FontStyle]::Regular
    )
    # PREFERENCE ORDER: Yu Gothic UI (Win10/11 default Japanese UI font) -> Meiryo UI (older
    # Windows) -> MS UI Gothic (oldest fallback still installed almost everywhere). The first one
    # actually installed on this machine wins.
    $preferred = @("Yu Gothic UI", "Meiryo UI", "MS UI Gothic")
    try {
        $installed = (New-Object System.Drawing.Text.InstalledFontCollection).Families |
            ForEach-Object { $_.Name }
        foreach ($name in $preferred) {
            if ($installed -contains $name) {
                return New-Object System.Drawing.Font($name, $SizePoints, $Style)
            }
        }
    } catch {
        # Font enumeration blocked (locked-down environment) -- fall through to the safe default
        # below instead of throwing, so a dialog still opens rather than crashing on this probe.
    }
    # NONE OF THE THREE INSTALLED (or enumeration failed): fall back to the system default font,
    # rebuilt at the requested size/style so callers get a consistent Font object either way.
    # This never throws.
    try {
        $d = [System.Drawing.SystemFonts]::DefaultFont
        return New-Object System.Drawing.Font($d.FontFamily, $SizePoints, $Style)
    } catch {
        return [System.Drawing.SystemFonts]::DefaultFont
    }
}
