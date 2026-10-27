// SelfImproveDashboard.cs -- native Windows (WPF) read-only view for the SELF-IMPROVEMENT
// controller (the "surpass" feature, M365_HARDENING_AND_UX Tier 2).
//
// relay/selfimprove/dashboard.py folds the self-improvement ledgers (genome Archive, burned
// registry, SWE grade stream, per-A/B selfimprove_report_*.json) into ONE dashboard_state dict
// and, via write_json(), drops it as .fleet/selfimprove_dashboard.json -- the self-improvement
// analogue of .fleet/status.json. THIS window tails that JSON (~1s) and renders the scorecard,
// the A/B history, the burned ledger, the pass@1 trend, and the archive summary. It is strictly
// READ-ONLY: there is no control here that writes any file (unlike FleetCockpit's release/steer).
//
//   [ python -m relay.selfimprove.dashboard (write_json) ] --(selfimprove_dashboard.json)--> [ this ]
//
// Theme/cards/glyphs match FleetCockpit.cs exactly: the sibling app slate palette, single orange
// accent #ea580c, Material Symbols rendered as vector geometry from ui/assets/material_glyphs.json
// (NO emoji), soft-band card borders + faint tint (never a harsh fill), pill badges. It follows
// the same shared settings.txt theme/lang so toggling the chat retints this too.
//
// Build: this file is compiled INTO FleetCockpit.exe alongside FleetCockpit.cs (same csc.exe,
// legacy .NET Framework 4.x / C# 5). Add it to the file list in ui\build_cockpit.bat, e.g.:
//     ... "%~dp0FleetCockpit.cs" "%~dp0SelfImproveDashboard.cs"
// It declares NO entry point (no Main) and lives in the global namespace, exactly like
// CockpitWindow, so it drops straight into the existing single-exe build.
//
// ============================================================================================
// INTEGRATION HOOK (the ONE line a maintainer adds to FleetCockpit.cs -- do NOT edit it here):
//
//   In CockpitWindow.BuildChrome(), in the `ctrls` header cluster (next to where _mainBtn is
//   created and added around line 411-414), insert:
//
//       var _siBtn = IconButton("account_tree", 18); _siBtn.ToolTip = _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement dashboard"; _siBtn.Click += delegate { new SelfImproveDashboardWindow().Show(); }; ctrls.Children.Add(_siBtn);
//
//   That single statement opens this window. SelfImproveDashboardWindow is in the global
//   namespace (same as CockpitWindow), so no using/namespace change is needed. It resolves the
//   JSON path itself (../.fleet/selfimprove_dashboard.json relative to the exe), so no argument
//   is required.
// ============================================================================================
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using System.Web.Script.Serialization;


partial class SelfImproveDashboardWindow : Window
{
    public const string WindowTitle = "Self-Improvement";

    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }

    // theme-dependent brushes (Theme.cs is the single source of truth)
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    Brush Accent;
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;             // 0 = Japanese, 1 = English
    long _settingsMtime = 0;

    // FIX 1: regeneration throttle
    DateTime _lastRegen = DateTime.MinValue;

    // The same two-location rule as FleetCockpit / CopilotChat / ApprovalPrompt and
    // tools/settings_path.py: read the repo's .config/settings.txt when it exists (every
    // context -- inside or outside the MSIX package -- resolves the repo identically), fall
    // back to the retired %APPDATA% copy while a machine is mid-migration, and ALWAYS WRITE
    // the new location. This window used to hard-code the %APPDATA% path alone, so the theme/
    // language it saved landed in a package-private copy no coordinator reads -- the same
    // panel-writes-one-file, run-reads-another split the shared resolver exists to close.
    static string SettingsFileNew
    { get { return Path.Combine(RepoRootForSettings(), ".config", "settings.txt"); } }

    static string SettingsFileOld
    {
        get
        {
            string app = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrEmpty(app))
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".copilot-bridge", "settings.txt");
            return Path.Combine(app, "copilot-bridge", "settings.txt");
        }
    }

    static string SettingsFile
    {
        get
        {
            try { if (File.Exists(SettingsFileNew)) return SettingsFileNew; } catch (Exception) { }
            try { if (File.Exists(SettingsFileOld)) return SettingsFileOld; } catch (Exception) { }
            return SettingsFileNew;
        }
    }

    static string SettingsFileForWrite
    {
        get
        {
            try { Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileNew)); }
            catch (Exception) { }
            return SettingsFileNew;
        }
    }

    static string RepoRootForSettings()
    {
        try
        {
            string exe = System.Reflection.Assembly.GetExecutingAssembly().Location;
            DirectoryInfo d = new DirectoryInfo(Path.GetDirectoryName(exe));
            while (d != null && !Directory.Exists(Path.Combine(d.FullName, ".fleet")))
                d = d.Parent;
            if (d != null) return d.FullName;
        }
        catch (Exception) { }
        return Directory.GetCurrentDirectory();
    }

    readonly string _jsonPath;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    ContentControl _iconHost;
    TextBlock _header, _sub, _freshnessLine;
    Button _themeBtn, _langBtn, _refreshBtn;
    StackPanel _body;
    ScrollViewer _sv;
    Border _headBar;

    public SelfImproveDashboardWindow() : this(null) { }

    public SelfImproveDashboardWindow(string path)
    {
        _jsonPath = ResolvePath(path);
        LoadGlyphs();
        LoadSettings();
        ApplyThemeBrushes();
        // Named once and matched by name: FleetCockpit's --authority guard raises THIS window
        // rather than opening a second one, and it identifies it by this exact title.
        Title = WindowTitle;
        Width = 920; Height = 740;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(1000);
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        // FIX 1a: regenerate the feed on window open so data is never stale
        RegenerateFeed();
        OnTick(null, null);
    }

    static string ResolvePath(string path)
    {
        if (!string.IsNullOrEmpty(path)) return path;
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "selfimprove_dashboard.json"));
    }

    // FIX 1a: fire-and-forget feed regeneration via `python -m relay.selfimprove.dashboard --write`
    // Resolves venv python from the repo root (exeDir is <repo>/ui/).
    // Guard: ignores the call if a regen was started within the last 1500ms.
    void RegenerateFeed()
    {
        try
        {
            if ((DateTime.Now - _lastRegen).TotalMilliseconds < 1500) return;
            _lastRegen = DateTime.Now;

            string exeDir   = AppDomain.CurrentDomain.BaseDirectory;
            string repoRoot = Path.GetFullPath(Path.Combine(exeDir, ".."));
            string venvPy   = Path.Combine(repoRoot, ".venv", "Scripts", "python.exe");
            string pyExe    = File.Exists(venvPy) ? venvPy : "python";

            var psi = new ProcessStartInfo();
            psi.FileName               = pyExe;
            psi.Arguments              = "-m relay.selfimprove.dashboard --write";
            psi.WorkingDirectory       = repoRoot;
            psi.UseShellExecute        = false;
            psi.CreateNoWindow         = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError  = true;

            var proc = new Process();
            proc.StartInfo = psi;
            proc.Start();
            // drain output async so the process doesn't block on a full buffer
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            // do NOT WaitForExit -- fire-and-forget; the 1s tail timer picks up the new file
        }
        catch (Exception) { }
    }






}
