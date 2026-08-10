// FleetCockpit.cs -- native Windows (WPF) LIVE cockpit for parallel execution.
//
// relay/fleet_runner.py drives N autonomous Copilot conversations at once and writes a
// live snapshot to .fleet/status.json after every round-robin sweep. This window tails
// that JSON and renders one live card per goal. You can also release a running one from
// here (writes .fleet/commands.json, which the fleet consumes -> stops + frees its tab).
//
//   [ fleet_runner.py ] <--(commands.json)-- [ this ]
//                       --(status.json)----->
//
// Icons are Google Material Symbols, rendered as vector geometry from
// ui/assets/material_glyphs.json (NO emoji). Palette = the sibling app slate; status shows
// as a soft band border + faint tint (not a harsh fill) plus a saturated pill. Theme and
// language follow the shared settings.txt live, so toggling the chat retints this too.
// Build: ui\build_cockpit.bat  (Windows csc.exe, legacy C# 5).
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using System.Windows.Data;
using System.Globalization;
using System.Web.Script.Serialization;
using System.Collections.ObjectModel;
using System.Runtime.CompilerServices;

class CockpitProgram
{
    // Per-Monitor V2 DPI: WPF would otherwise run System-DPI-aware (render once at the PRIMARY
    // monitor's DPI, then Windows bitmap-stretches that onto a differently-scaled display like a
    // 2560x1440 external monitor -> visibly grainy text/edges). PROCESS awareness is declared in
    // app.manifest (embedded via /win32manifest; the OS loader reads it at process creation, so it
    // cannot lose the startup race a programmatic SetProcessDpiAwarenessContext call would). The
    // switch below is the WPF half: it turns on WPF's per-monitor RELAYOUT so the tree re-renders
    // vector-crisp at each monitor's native DPI. Both are needed; must precede any WPF type use.
    [STAThread]
    static void Main(string[] args)
    {
        try { AppContext.SetSwitch("Switch.System.Windows.DoNotScaleForDpiChanges", false); } catch { }
        if (args.Length >= 2 && args[0].Equals("--approval-gate", StringComparison.OrdinalIgnoreCase))
        {
            bool createdNew = false;
            bool ownsMutex = false;
            using (var mutex = new Mutex(true, "Local\\M365CompanionApprovalPrompt", out createdNew))
            {
                try
                {
                    ownsMutex = createdNew;
                    if (!ownsMutex)
                    {
                        try { ownsMutex = mutex.WaitOne(0, false); }
                        catch (AbandonedMutexException) { ownsMutex = true; }
                    }
                    // The existing prompt polls the gate directory and will surface this request.
                    if (!ownsMutex) return;
                    new Application().Run(new ApprovalPromptWindow(args[1]));
                }
                finally { if (ownsMutex) try { mutex.ReleaseMutex(); } catch { } }
            }
            return;
        }
        string path = args.Length > 0 ? args[0] : null;
        new Application().Run(new CockpitWindow(path));
    }
}

// Small native action surface launched alongside an approval toast. It is intentionally part of
// FleetCockpit.exe: no installer, protocol registration, browser, admin rights, or extra runtime is
// required. One process drains all pending gates so a burst of workers never creates a window storm.
class ApprovalPromptWindow : Window
{
    readonly JavaScriptSerializer _js = new JavaScriptSerializer();
    string _gateDir;
    string _currentPath;
    Dictionary<string, object> _current;
    TextBlock _kind, _question, _context, _count, _policyHelp;
    Button _approve, _deny;
    ComboBox _policy;
    Expander _detailExpander;
    DispatcherTimer _timer;
    bool _changingPolicy;
    bool _dark = true;
    int _lang = 0;
    long _settingsStamp = -1;
    Brush Bg, Surface, SurfaceSubtle, Line, Fg, Muted, Accent, AccentFg, Danger, Warning;

    static double NowUnix()
    { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    static string SettingsFile
    {
        get
        {
            string app = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            return Path.Combine(app, "copilot-bridge", "settings.txt");
        }
    }

    // True until the FIRST LoadNext() completes. The prompt is normally launched by
    // clicking a toast, so an empty gate list on that first pass means "the thing you
    // just clicked is already handled" and deserves a word. Later passes come from the
    // 2s poll, where closing quietly is correct -- the user watched it happen.
    bool _openedFromToast = true;

    public ApprovalPromptWindow(string initialGatePath)
    {
        LoadUiPreferences();
        ApplyThemeTokens();
        Title = L("承認が必要です", "Approval required");
        // Size against the WORK AREA, not a fixed number. 620 exceeded the desktop on a
        // short screen (and the UI scale multiplies it further), leaving the footer --
        // the Approve/Deny buttons -- off-screen with no way to reach them. WPF's
        // SystemParameters are already in device-independent units, which is the same
        // space Width/Height use, so no DPI conversion is needed here.
        double workW = SystemParameters.WorkArea.Width;
        double workH = SystemParameters.WorkArea.Height;
        // Divide by the UI zoom before clamping: the window is measured in unscaled
        // units, so at 1.5x a 620-unit window occupies 930 physical units. Without this
        // the footer -- the Approve/Deny row -- lands past the bottom edge, and since it
        // is docked rather than inside the scroller there is no way to scroll to it.
        double zoom = ReadUiScale();
        Width = Math.Min(600, Math.Max(360, workW / zoom - 60));
        Height = Math.Min(620, Math.Max(360, workH / zoom - 60));
        MinWidth = Math.Min(500, Width);
        MinHeight = Math.Min(480, Height);
        MaxHeight = workH;   // never taller than the desktop, whatever the content asks for
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = Bg; ShowInTaskbar = true; FontFamily = new FontFamily(Theme.UiFont);
        try
        {
            string full = Path.GetFullPath(initialGatePath);
            _gateDir = Path.GetDirectoryName(full);
            if (!Path.GetFileName(full).StartsWith("gate_", StringComparison.OrdinalIgnoreCase) ||
                !Path.GetExtension(full).Equals(".json", StringComparison.OrdinalIgnoreCase) ||
                !Path.GetFileName(_gateDir).Equals(".companion_gates", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("invalid approval-gate path");
            _currentPath = full;
        }
        catch { _gateDir = null; _currentPath = null; }

        Build();
        Loaded += delegate
        {
            if (_gateDir == null) { Close(); return; }
            LoadNext();
            _timer = new DispatcherTimer(); _timer.Interval = TimeSpan.FromSeconds(2);
            _timer.Tick += delegate
            {
                if (UiPreferencesChanged())
                {
                    ApplyThemeTokens(); Background = Bg; Title = L("承認が必要です", "Approval required");
                    Build(); LoadNext();
                }
                RefreshPendingCount(); if (_current == null) LoadNext();
            };
            _timer.Start();
            Activate();
        };
        Closed += delegate { if (_timer != null) _timer.Stop(); };
        KeyDown += delegate (object sender, KeyEventArgs e)
        { if (e.Key == Key.Escape) { Close(); e.Handled = true; } };
    }

    string L(string ja, string en) { return _lang == 0 ? ja : en; }

    // The zoom the cockpit is running at, read straight from settings.txt. This window
    // is not itself scaled, but it is sized in the same unscaled units, so it must know
    // the zoom to avoid asking for a window taller than the screen can show. "auto"
    // (the default) resolves to the persisted target, falling back to 1.5 like the
    // cockpit does. Clamped to the same 0.8-2.0 range the cockpit enforces.
    double ReadUiScale()
    {
        double scale = 1.0, target = 1.5, parsed = 0.0;
        bool auto = false, haveScale = false;
        try
        {
            if (File.Exists(SettingsFile))
            {
                foreach (string raw in File.ReadAllLines(SettingsFile, new UTF8Encoding(false)))
                {
                    string line = raw.Trim();
                    if (line.StartsWith("ui_scale="))
                    {
                        string v = line.Substring(9).Trim();
                        if (v.Equals("auto", StringComparison.OrdinalIgnoreCase)) auto = true;
                        else if (double.TryParse(v, System.Globalization.NumberStyles.Float,
                                                 System.Globalization.CultureInfo.InvariantCulture, out parsed))
                        { scale = parsed; haveScale = true; }
                    }
                    else if (line.StartsWith("ui_scale_target="))
                    {
                        double.TryParse(line.Substring(16).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out target);
                    }
                }
            }
        }
        catch { }
        if (auto || !haveScale) scale = target > 0.1 ? target : 1.5;
        return Math.Max(0.8, Math.Min(2.0, scale));
    }

    void LoadUiPreferences()
    {
        try
        {
            if (!File.Exists(SettingsFile)) { _settingsStamp = 0; return; }
            _settingsStamp = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
            foreach (string raw in File.ReadAllLines(SettingsFile, new UTF8Encoding(false)))
            {
                string line = raw.Trim();
                if (line.StartsWith("dark=")) _dark = line.Substring(5).Trim() != "0";
                else if (line.StartsWith("lang=")) _lang = line.Substring(5).Trim() == "1" ? 1 : 0;
            }
        }
        catch { }
    }

    bool UiPreferencesChanged()
    {
        try
        {
            long stamp = File.Exists(SettingsFile) ? File.GetLastWriteTimeUtc(SettingsFile).Ticks : 0;
            if (stamp == _settingsStamp) return false;
            LoadUiPreferences(); return true;
        }
        catch { return false; }
    }

    void ApplyThemeTokens()
    {
        Bg = Theme.Br(Theme.Bg(_dark)); Surface = Theme.Br(Theme.Surface(_dark));
        SurfaceSubtle = Theme.Br(Theme.SurfaceSubtle(_dark)); Line = Theme.Br(Theme.Border(_dark));
        Fg = Theme.Br(Theme.Text(_dark)); Muted = Theme.Br(Theme.Muted(_dark));
        Accent = Theme.Br(Theme.Accent(_dark)); AccentFg = Theme.Br(Theme.AccentFg(_dark));
        Danger = Theme.Br(Theme.Danger(_dark)); Warning = Theme.Br(Theme.Warning(_dark));
    }

    ControlTemplate FlatButtonTemplate()
    {
        var border = new FrameworkElementFactory(typeof(Border));
        border.SetBinding(Border.BackgroundProperty, new Binding("Background") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.SetBinding(Border.BorderBrushProperty, new Binding("BorderBrush") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.SetBinding(Border.BorderThicknessProperty, new Binding("BorderThickness") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.SetValue(Border.CornerRadiusProperty, new CornerRadius(Theme.RadSmall));
        var presenter = new FrameworkElementFactory(typeof(ContentPresenter));
        presenter.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        presenter.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        presenter.SetBinding(ContentPresenter.MarginProperty, new Binding("Padding") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
        border.AppendChild(presenter);
        return new ControlTemplate(typeof(Button)) { VisualTree = border };
    }

    void Build()
    {
        var root = new DockPanel();
        var head = new Border { Background = Surface, BorderBrush = Line,
            BorderThickness = new Thickness(0, 0, 0, 1), Padding = new Thickness(Theme.PadApp) };
        DockPanel.SetDock(head, Dock.Top);
        var headRow = new DockPanel();
        _count = new TextBlock { Foreground = Muted, FontSize = Theme.FsMeta, VerticalAlignment = VerticalAlignment.Center };
        DockPanel.SetDock(_count, Dock.Right); headRow.Children.Add(_count);
        var titles = new StackPanel();
        titles.Children.Add(new TextBlock { Text = L("承認が必要です", "Approval required"), Foreground = Fg,
            FontSize = Theme.FsTitle, FontWeight = FontWeights.SemiBold });
        titles.Children.Add(new TextBlock { Text = L("対象と影響を確認して判断します。", "Review the exact scope and impact before deciding."),
            Foreground = Muted, FontSize = Theme.FsMeta, Margin = new Thickness(0, 4, 0, 0) });
        headRow.Children.Add(titles); head.Child = headRow; root.Children.Add(head);

        var footer = new Border { Background = Surface, BorderBrush = Line,
            BorderThickness = new Thickness(0, 1, 0, 0), Padding = new Thickness(Theme.PadApp) };
        DockPanel.SetDock(footer, Dock.Bottom);
        var actions = new DockPanel();
        var later = new Button { Content = L("あとで", "Later"), Padding = new Thickness(16, 7, 16, 7),
            Background = SurfaceSubtle, Foreground = Fg, BorderBrush = Line, BorderThickness = new Thickness(1), Cursor = Cursors.Hand };
        later.Template = FlatButtonTemplate();
        later.Click += delegate { Close(); }; DockPanel.SetDock(later, Dock.Right); actions.Children.Add(later);
        _deny = new Button { Content = L("拒否", "Deny"), Padding = new Thickness(18, 7, 18, 7),
            Margin = new Thickness(0, 0, 8, 0), Background = Brushes.Transparent,
            Foreground = Danger, BorderBrush = Danger, BorderThickness = new Thickness(1), Cursor = Cursors.Hand };
        _deny.Template = FlatButtonTemplate();
        _deny.Click += delegate { Answer("denied"); }; DockPanel.SetDock(_deny, Dock.Right); actions.Children.Add(_deny);
        _approve = new Button { Content = L("承認", "Approve"), Padding = new Thickness(22, 8, 22, 8),
            Margin = new Thickness(0, 0, 8, 0), Background = Accent, Foreground = AccentFg,
            BorderThickness = new Thickness(0), FontWeight = FontWeights.SemiBold, Cursor = Cursors.Hand };
        _approve.Template = FlatButtonTemplate();
        _approve.Click += delegate { Answer("approved"); }; DockPanel.SetDock(_approve, Dock.Right); actions.Children.Add(_approve);
        footer.Child = actions; root.Children.Add(footer);

        var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        var body = new StackPanel { Margin = new Thickness(Theme.PadApp) };
        // Uniform border on purpose. A thicker left edge reads as a sticky-note accent
        // stripe; the card already carries the warning colour, which is what marks it.
        var decisionCard = new Border { Background = Surface, BorderBrush = Warning,
            BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(14, 12, 14, 12) };
        var decisionCol = new StackPanel();
        _kind = new TextBlock { Foreground = Muted, FontSize = Theme.FsMeta, FontWeight = FontWeights.SemiBold };
        decisionCol.Children.Add(_kind);
        _question = new TextBlock { Foreground = Fg, FontSize = Theme.FsBody, FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) };
        decisionCol.Children.Add(_question);
        var detailBox = new Border { Background = SurfaceSubtle, BorderBrush = Line, BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(Theme.RadSmall), Padding = new Thickness(12), Margin = new Thickness(0, 8, 0, 0) };
        _context = new TextBlock { Foreground = Muted, FontFamily = new FontFamily(Theme.CodeFont),
            FontSize = Theme.FsLog, TextWrapping = TextWrapping.Wrap };
        detailBox.Child = _context;
        // Foldable, but OPEN by default: this is the evidence for the decision, and a
        // gate that hides what it is asking about invites approving it unseen. It runs
        // long, so it is placed after the policy control rather than before it (see
        // where this is added to `body`), which keeps both reachable without scrolling.
        _detailExpander = new Expander
        {
            Header = L("承認対象の詳細（対象・ハッシュ・指示プレビュー）",
                       "What you are approving (target, digest, instruction preview)"),
            IsExpanded = true,
            Foreground = Muted,
            FontSize = Theme.FsMeta,
            Margin = new Thickness(0, 12, 0, 0),
            Content = detailBox,
        };
        decisionCard.Child = decisionCol; body.Children.Add(decisionCard);

        var policyBox = new Border { Background = SurfaceSubtle, BorderBrush = Line, BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(Theme.RadCard), Padding = new Thickness(14, 12, 14, 12), Margin = new Thickness(0, Theme.SectionGap, 0, 0) };
        var policyCol = new StackPanel();
        policyCol.Children.Add(new TextBlock { Text = L("今後の操作承認", "Approval policy"), Foreground = Fg,
            FontSize = Theme.FsSection, FontWeight = FontWeights.SemiBold });
        _policy = new ComboBox { Margin = new Thickness(0, 8, 0, 0), MinWidth = 220,
            HorizontalAlignment = HorizontalAlignment.Left, Background = Surface, Foreground = Fg };
        AddPolicyItem(L("確認（推奨）", "Confirm (recommended)"), "default");
        AddPolicyItem(L("自動", "Auto"), "auto"); AddPolicyItem(L("バイパス", "Bypass"), "bypass");
        _policy.SelectionChanged += PolicyChanged; policyCol.Children.Add(_policy);
        _policyHelp = new TextBlock { Foreground = Muted, FontSize = Theme.FsMeta, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 7, 0, 0) }; policyCol.Children.Add(_policyHelp);
        policyBox.Child = policyCol; body.Children.Add(policyBox);
        // Evidence goes LAST and starts OPEN. Collapsing what is being approved would
        // invite approving it unseen, which defeats the gate; the policy control sits
        // above it instead, so it is visible without scrolling either way. Anyone who
        // has already read the preview can fold it away.
        body.Children.Add(_detailExpander);
        scroll.Content = body; root.Children.Add(scroll); Content = root;
        SelectPolicy(ReadPolicy());
    }

    void AddPolicyItem(string label, string value)
    { _policy.Items.Add(new ComboBoxItem { Content = label, Tag = value, Foreground = Fg, Background = Surface }); }

    string SelectedPolicy()
    { var item = _policy.SelectedItem as ComboBoxItem; return item == null ? "default" : (string)item.Tag; }

    void SelectPolicy(string mode)
    {
        _changingPolicy = true;
        foreach (object obj in _policy.Items)
        {
            var item = obj as ComboBoxItem;
            if (item != null && (string)item.Tag == mode) { _policy.SelectedItem = item; break; }
        }
        _changingPolicy = false; UpdatePolicyHelp();
    }

    void PolicyChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_changingPolicy) return;
        string oldMode = ReadPolicy(), mode = SelectedPolicy();
        if (mode == "bypass")
        {
            string warning = L(
                "バイパスではローカルジョブと自律契約の手動確認を省略します。\nSTOP条件、ファイル範囲、外部Skillの初回・変更承認は残ります。\n\n本当に有効にしますか？",
                "Bypass skips manual approval for local jobs and autonomy contracts.\nSTOP rules, path limits, and external-Skill approval remain.\n\nEnable it?");
            if (MessageBox.Show(this, warning, L("バイパスを有効化", "Enable bypass"), MessageBoxButton.YesNo,
                MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
            { SelectPolicy(oldMode); return; }
        }
        SavePolicy(mode); UpdatePolicyHelp();
    }

    void UpdatePolicyHelp()
    {
        if (_policyHelp == null) return;
        string mode = SelectedPolicy();
        if (mode == "auto") _policyHelp.Text = L("安全判定が通った操作は自動実行。要確認はここで承認、禁止判定は拒否します。",
            "Safe operations run automatically; risky ones ask here; prohibited ones are denied.");
        else if (mode == "bypass") _policyHelp.Text = L("手動確認を省略します。STOP条件・パス制限・外部Skillのハッシュ承認は解除しません。",
            "Skip manual confirmation. STOP rules, path limits, and external-Skill hash approval remain.");
        else _policyHelp.Text = L("初回の操作クラスを確認し、承認済みでも危険な内容は毎回確認します。",
            "Confirm first-seen operation classes; risky payloads still ask every time.");
    }

    public static string ReadPolicy()
    {
        string fallback = (Environment.GetEnvironmentVariable("TASK_JOB_APPROVAL_MODE") ?? "default").Trim().ToLowerInvariant();
        if (fallback != "auto" && fallback != "bypass") fallback = "default";
        try
        {
            if (!File.Exists(SettingsFile)) return fallback;
            foreach (string line in File.ReadAllLines(SettingsFile, new UTF8Encoding(false)))
                if (line.StartsWith("job_approval_mode="))
                {
                    string mode = line.Substring(18).Trim().ToLowerInvariant();
                    if (mode == "default" || mode == "auto" || mode == "bypass") return mode;
                }
        }
        catch { }
        return fallback;
    }

    public static void SavePolicy(string mode)
    {
        try
        {
            var lines = new List<string>(); bool found = false;
            if (File.Exists(SettingsFile)) foreach (string line in File.ReadAllLines(SettingsFile))
            {
                if (line.StartsWith("job_approval_mode=")) { lines.Add("job_approval_mode=" + mode); found = true; }
                else lines.Add(line);
            }
            if (!found) lines.Add("job_approval_mode=" + mode);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile));
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
        }
        catch { }
    }

    Dictionary<string, object> ReadGate(string path)
    {
        try
        {
            string full = Path.GetFullPath(path);
            string root = Path.GetFullPath(_gateDir).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase) || !File.Exists(full)) return null;
            string text;
            using (var fs = new FileStream(full, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8)) text = sr.ReadToEnd();
            var gate = _js.DeserializeObject(text) as Dictionary<string, object>;
            if (gate != null) gate["path"] = full;
            return gate;
        }
        catch { return null; }
    }

    static string S(Dictionary<string, object> d, string key)
    { object v; return d != null && d.TryGetValue(key, out v) && v != null ? v.ToString() : ""; }

    static double D(Dictionary<string, object> d, string key)
    { double v; return double.TryParse(S(d, key), NumberStyles.Float, CultureInfo.InvariantCulture, out v) ? v : 0; }

    static bool Answered(Dictionary<string, object> d)
    { object v; try { return d != null && d.TryGetValue("answered", out v) && Convert.ToBoolean(v); } catch { return false; } }

    void LoadNext()
    {
        Dictionary<string, object> gate = _currentPath == null ? null : ReadGate(_currentPath);
        if (gate == null || Answered(gate))
        {
            gate = null; _currentPath = null;
            try
            {
                var candidates = new List<Dictionary<string, object>>();
                foreach (string path in Directory.GetFiles(_gateDir, "gate_*.json"))
                { var item = ReadGate(path); if (item != null && !Answered(item)) candidates.Add(item); }
                candidates.Sort(delegate (Dictionary<string, object> a, Dictionary<string, object> b)
                { return D(b, "asked_at").CompareTo(D(a, "asked_at")); });
                if (candidates.Count > 0) gate = candidates[0];
            }
            catch { }
        }
        _current = gate;
        if (gate != null)
        {
            // A request was actually shown, so this window did its job. Any later empty
            // pass is the normal end of the queue -- typically the user having just
            // answered -- and must close without comment. Leaving the flag set made
            // approving pop "already handled" and kept the window up until dismissed.
            _openedFromToast = false;
        }
        if (gate == null)
        {
            // Nothing left to decide. Closing in silence is indistinguishable from the
            // window failing to open: the user clicks the toast, a window flashes (or
            // does not appear at all) and nothing happens. That is exactly what a
            // request already answered elsewhere looks like -- e.g. approved from the
            // console, or handled on a second click. Say so once, then close.
            if (_openedFromToast)
            {
                _openedFromToast = false;
                try
                {
                    MessageBox.Show(this,
                        L("この承認はすでに処理済みです。ほかに待機中の承認はありません。",
                          "This request has already been handled. No approvals are waiting."),
                        L("承認は不要です", "Nothing to approve"),
                        MessageBoxButton.OK, MessageBoxImage.Information);
                }
                catch { }
            }
            Close();
            return;
        }
        _currentPath = S(gate, "path");
        string hay = (S(gate, "token") + " " + S(gate, "context") + " " + S(gate, "question")).ToLowerInvariant();
        bool skill = hay.Contains("gate_skill_") || hay.Contains("skill approval");
        bool high = hay.Contains("contract gate: delete") || hay.Contains("outbound") ||
                    hay.Contains("shell_destructive") || hay.Contains("destructive shell");
        bool expired = D(gate, "expires_at") > 0 && D(gate, "expires_at") < NowUnix();
        _kind.Text = (skill ? L("外部Skill", "External Skill") : high ? L("影響の大きい操作", "High impact") : L("操作", "Operation")) +
            (expired ? L("  ·  期限切れ", "  ·  Expired") : "");
        _kind.Foreground = expired || high ? Danger : Warning;
        _question.Text = S(gate, "question"); _context.Text = S(gate, "context");
        _approve.IsEnabled = !expired; _deny.IsEnabled = true;
        if (skill) _policyHelp.Text = L("外部Skillはこの設定に関係なく、初回と内容変更時に必ずハッシュ承認します。",
            "External Skills always require hash approval on first use and after changes, regardless of this policy.");
        else UpdatePolicyHelp();
        RefreshPendingCount();
    }

    void RefreshPendingCount()
    {
        if (_count == null || _gateDir == null) return;
        int count = 0;
        try { foreach (string path in Directory.GetFiles(_gateDir, "gate_*.json"))
        { var gate = ReadGate(path); if (gate != null && !Answered(gate)) count++; } } catch { }
        _count.Text = _lang == 0 ? count + " 件待機" : count + " pending";
    }

    void Answer(string verdict)
    {
        if (_current == null || string.IsNullOrEmpty(_currentPath)) return;
        string hay = (S(_current, "context") + " " + S(_current, "question")).ToLowerInvariant();
        bool high = hay.Contains("contract gate: delete") || hay.Contains("outbound") ||
                    hay.Contains("shell_destructive") || hay.Contains("destructive shell");
        if (verdict == "approved" && high && MessageBox.Show(this,
            L("影響の大きい操作です。対象を確認したうえで本当に承認しますか？\n\n",
              "This is a high-impact operation. Approve after reviewing the exact scope?\n\n") + S(_current, "question"),
            L("最終確認", "Final confirmation"), MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes) return;
        try
        {
            var gate = ReadGate(_currentPath); if (gate == null) return;
            gate.Remove("path"); gate["answered"] = true; gate["answer"] = verdict; gate["answered_at"] = NowUnix();
            string tmp = _currentPath + ".tmp";
            File.WriteAllText(tmp, _js.Serialize(gate), new UTF8Encoding(false));
            try { File.Replace(tmp, _currentPath, null); }
            catch { File.Copy(tmp, _currentPath, true); try { File.Delete(tmp); } catch { } }
        }
        catch { return; }
        _current = null; _currentPath = null; LoadNext();
    }
}

class CockpitWindow : Window
{
    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    static double NowUnix() { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    // theme-dependent brushes
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    Brush Accent;   // primary-action color; theme-dependent (spec), set in ApplyThemeBrushes
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;          // 0 = Japanese, 1 = English
    int _maxtabs = 3;
    bool _autoscale = false;   // RAM-aware autoscale on/off (NEW)
    int _autoMax = 100;        // ceiling (上限) tabs may grow to under autoscale. High by design:
                               // autoscale (ram_target_cap) self-limits to what free RAM allows
                               // (~3 on a 16 GB box, far more on a big-RAM machine), so this is the
                               // upper RAIL, not a hardware bound. Real limiters: free RAM + M365
                               // Copilot per-user fair-use/rate limits.
    double _diskFloor = 6.0;   // admission disk floor (GB) -> settings.txt disk_floor_gb=; user-editable
                               // in the settings panel. Persisted via SaveKey AND pushed live to a
                               // running fleet via {"set_disk_floor_gb":N} (fleet_runner.py ~L561).
    double _ramFloor = 2048.0; // admission RAM floor (MB) -> settings.txt ram_floor_mb=; user-editable.
                               // The free RAM the autoscale keeps for the user (RAM analog of the disk
                               // floor). Persisted via SaveKey AND pushed live via {"set_ram_floor_mb":N}.
    string _effort = "auto";   // effort mode min|max|ultra|auto -> settings.txt effort= (NEW)
    string _approval = "run";  // approval mode run|plan|auto -> settings.txt approval=
    bool _paused = false;      // local fleet pause/resume toggle state (NEW)
    // FIX B: optimistic "stopping" state set the instant Stop is clicked (dims non-terminal cards +
    // flips the Stop button's tooltip/icon) so the click never feels dead for the ~700ms sweep.
    // Cleared once the sweep confirms the run is no longer live, or a different run has started.
    bool _stopping = false;
    string _stopStartedMarker = "";   // `started` marker captured at Stop-click time (detects a NEW run)
    bool _autoArchive = false;  // P2: when ON, move this run's completed cards to History as soon
                                // as the RUN reaches its finished state -> settings.txt autoarchive=
    Button _autoArchiveBtn;     // gear-popup toggle for _autoArchive
    string _archivedRunStarted = "";   // `started` of the run already auto-archived (fire once/run)
    // P2 history search: live case-insensitive substring filter over title+result, debounced.
    TextBox _histSearchBox;
    string _histQuery = "";
    DispatcherTimer _histSearchTimer;
    long _settingsMtime = 0;

    // ── UI SCALE (4K readability) ────────────────────────────────────────────────
    // A window-level zoom: a ScaleTransform is set as the LayoutTransform on the SINGLE
    // root element that hosts ALL chrome (header + pinned toolbar + health strip + list +
    // composer), so everything scales AND reflows together. Persisted as ui_scale= in the
    // SHARED settings.txt with identical semantics to the chat app, so both apps zoom in
    // lock-step (an external edit is picked up on the next mtime-triggered LoadSettings).
    double _uiScale = 1.0;            // current EFFECTIVE zoom (clamped 0.8–2.0) -> pushed to _rootScale
    bool _uiScaleLoaded = false;      // true once a ui_scale= line was read from settings.txt
    // AUTO mode: ui_scale= may hold the literal "auto" instead of a number. In auto mode the
    // effective LayoutTransform scale is derived per-monitor so the PHYSICAL size stays constant:
    //   effective = clamp(_scaleTarget / currentMonitorScale, 0.8, 2.0)
    // where currentMonitorScale = this window's DPI/96. Net physical = monitorScale × effective ≈
    // _scaleTarget on EVERY monitor (the divide-by-monitorScale exactly counteracts WPF's own DPI
    // relayout, so we never double-apply). Manual mode ignores _scaleTarget and uses a fixed number.
    bool _uiAuto = true;              // true = auto mode (default for new users / ui_scale=auto)
    double _scaleTarget = 1.5;        // desired constant physical scale (persisted as ui_scale_target)
    bool _scaleTargetLoaded = false;  // true once ui_scale_target was read (else seed from primary DPI)
    Button _uiAutoBtn;                // gear-popup "自動"/"Auto" toggle (rebuilt each open)
    Button _uiScaleMinus, _uiScalePlus;  // gear-popup steppers (disabled while auto is on)
    ScaleTransform _rootScale;        // the LayoutTransform applied on BuildChrome's root
    // Self-fading "125%" overlay shown briefly on each change (reuses no other affordance
    // because the composer note is not visible from every context, e.g. gear popup / shortcut).
    Border _scaleToast;
    TextBlock _scaleToastText;
    DispatcherTimer _scaleToastTimer;
    // gear-popup live value label + stepper refs (re-themed nowhere else; rebuilt each open)
    TextBlock _uiScaleVal;

    readonly string _statusPath, _commandsPath, _historyPath, _openPath;
    string _convsPath, _hiddenPath, _resumeDismissPath;
    System.Collections.Generic.HashSet<string> _archivedKeys = new System.Collections.Generic.HashSet<string>();
    // Persistent "cleared" set: keys of TERMINAL cards the user dismissed via Clear. Survives
    // the runner regenerating status.json every second, so cleared cards stay gone mid-run.
    // Key is run+worker (started#name) -- same scheme as the history archive -- so a NEW run
    // (different `started`) never inherits an old hide, and a reused worker name can't collide.
    System.Collections.Generic.HashSet<string> _hiddenKeys = new System.Collections.Generic.HashSet<string>();
    List<object> _history = new List<object>();
    int _openSeq = 0;
    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "copilot-bridge", "settings.txt");

    TextBlock _header, _sub;
    WrapPanel _subChips;   // Feature 2: discrete Pill() chips replacing _sub's single concatenated sentence
    TextBlock _workerChip;       // live "N workers" neutral chip in the header controls row
    Border _workerChipBorder;   // the Border wrapping _workerChip (for PaintChrome re-theming)
    Button _themeBtn, _langBtn, _mainBtn, _siBtn;
    Button _approvalCenterBtn;
    TextBlock _approvalCenterLabel, _approvalCenterBadgeText;
    Border _approvalCenterBadge;
    Window _approvalCenterWindow;
    StackPanel _approvalPendingHost, _approvalRecentHost;
    TextBlock _approvalCenterSummary;
    string _approvalCenterSig = "";
    List<Dictionary<string, object>> _gateCache = new List<Dictionary<string, object>>();
    double _gateCacheAt = 0;
    Button _overflowBtn;
    System.Windows.Controls.Primitives.Popup _overflowPopup;
    Border _headBar;
    ListBox _list;                 // virtualizing host for the card/history rows
    // PINNED FILTER BAR: the すべて/実行中/承認待ち/完了 segmented control used to be row 0 INSIDE
    // _list, so it scrolled with the content and — because the list is bottom-anchored to the
    // composer via a '*' spacer — its Y position drifted as tasks accumulated (user complaint:
    // "動きまくって結構うっとうしい"). It now lives in a fixed host docked ABOVE _list (never
    // inside the scroll), so it is stationary regardless of card count / scroll offset.
    Border _pinnedToolbarHost;
    string _pinnedToolbarSig = "";
    // Persistent row backing store. We bind this to _list.ItemsSource ONCE and only ever mutate
    // it in place (SetRows reconciles item-by-item). Reassigning ItemsSource with a fresh List --
    // the old behaviour -- made the VirtualizingStackPanel treat every update as a collection
    // Reset: it discarded realized containers and snapped the scroll to the top, which is exactly
    // the "expand jumps the view" / "scrolling fights the 700ms tick" complaint. In-place edits
    // raise targeted Add/Remove/Replace notifications instead, so the pixel scroll offset stays.
    readonly ObservableCollection<object> _rows = new ObservableCollection<object>();
    // The lists handed to BuildCardToolbar (all/shown) are stashed here so the
    // ItemTemplate converter can rebuild the toolbar row on demand for a recycled container.
    List<Dictionary<string, object>> _toolbarAll = new List<Dictionary<string, object>>();
    List<Dictionary<string, object>> _toolbarShown = new List<Dictionary<string, object>>();
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();

    // Cached meta string for the directive-band row Sig (avoids per-tick rebuild when nothing changed).
    string _directiveBandMeta = "";
    // The on-board (non-History) workers the directive band aggregates its goals/lane counts from.
    List<Dictionary<string, object>> _directiveBandWorkers = new List<Dictionary<string, object>>();

    // ── A2-2: Evidence Spine (left panel) ────────────────────────────────────────
    // A fixed-width ~220px left column that shows an [COMPUTED] execution timeline for
    // the run derived from transcript turn timestamps. Labeled honestly: "実行タイムライン" /
    // "Execution timeline". Only visible when a run exists (workers present).
    Border _spinePanel;              // the left column Border; col0 is zeroed when idle
    ColumnDefinition _spineCol;      // Grid col0 -- we zero its Width when idle
    string _spineSig = "";           // last rendered state; rebuild only when changed

    // ── A2-2: Composer mode ────────────────────────────────────────────────────────
    // When a run is active the bottom composer adapts: placeholder, hint, and button
    // shift from "add goals / Start" to "steer / Send". This bool tracks the last
    // rendered state so PaintComposerMode is called only when it changes.
    bool _composerRunActive = false;

    // Per-worker disclosure state (Claude-Code-style "> / v"). Collapsed is the default:
    // a collapsed card renders only a lightweight summary line and (Feature 1) a slim always-on
    // steer affordance for non-terminal workers -- the heavy tabs/logs/review detail still only
    // builds when EXPANDED. Keyed by worker name; survives re-renders.
    HashSet<string> _expanded = new HashSet<string>();

    // ── Feature 1: collapsed-card steer affordance -- keystroke-safety state ───────────────────
    // Cards are rebuilt wholesale (Card(w) called again) whenever RowSig for that worker changes,
    // which happens routinely for a LIVE worker (its `last`/`turn`/etc. stream every sweep) --
    // completely independent of whether the user is mid-type in its steer box. So a naive TextBox
    // would lose its text (rebuilt empty) and/or its focus (a brand new control instance) on the
    // very next ~700ms sweep. Three fields defend against that:
    //   _steerDraft     -- per-worker in-progress text, read back into the TextBox's Text on every
    //                      (re)build, so a torn-down-and-rebuilt box never loses what was typed.
    //   _steerFocusWorker -- which worker's steer box currently holds keyboard focus, so a rebuild
    //                      knows it needs to re-Focus() the replacement box afterward.
    //   _steerBoxRef    -- the CURRENTLY realized TextBox per worker, updated every time Card(w)
    //                      (re)builds one, so the post-render restore pass (see RenderCards) always
    //                      focuses the box that is actually still in the visual tree.
    Dictionary<string, string> _steerDraft = new Dictionary<string, string>();
    string _steerFocusWorker = null;
    Dictionary<string, TextBox> _steerBoxRef = new Dictionary<string, TextBox>();
    // last status snapshot rendered, kept so a chevron toggle can rebuild ONLY its one card
    // in place (no full 164-card re-render) -- that's what makes expand/collapse feel instant.
    Dictionary<string, object> _lastRoot;

    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    // 4-tab view filter: 0=All, 1=Active (non-terminal non-pending), 2=Needs input (awaiting), 3=Done.
    int _cardFilter = 0;

    // Opt-in, CAPPED auto-retry (default OFF). When on, a newly-stopped non-DONE goal is
    // re-queued at most _autoRetryMax times -- bounded so a deterministically-failing task
    // (e.g. the tool-denial ones) can NEVER loop forever. Counted by goal TEXT, so a re-queued
    // copy (which gets a new worker name) shares the original goal's budget. Manual retry is
    // unaffected -- this only governs the automatic re-queue.
    bool _autoRetry = false;
    int _autoRetryMax = 1;
    Dictionary<string, int> _autoRetryCount = new Dictionary<string, int>();

    // ── P0 HEALTH STRIP ─────────────────────────────────────────────────────────────
    // Six infra dots (Server/Tunnel/Edge/Sign-in/Agent/Tool) in the header, always visible,
    // polled every ~15s on a background thread and marshaled to the UI via Dispatcher.
    // Colors: "green"|"red"|"yellow"|"gray". The motivating incident: the companion Edge
    // session died -> login wall -> agent fell back to default Copilot; the cockpit only
    // showed failing tasks. These dots make the infra cause glanceable and one-click fixable.
    // The 6th dot (Tool) is a DIFFERENT axis from the first 5: dots 0-4 all probe the FLEET
    // Edge (:9222) / server / tunnel, none of which tell you whether the INTERACTIVE bridge
    // chat (Edge profile copilot-bridge-edge, CDP :9223) can actually call an MCP tool. Tool
    // reads .fleet\tool_probe.json, written by the bridge's own idle self-probe (a separate
    // component -- bridge/copilot_bridge.py + tools/tool_probe.py -- this file only reads it).
    enum HealthState { Gray = 0, Green = 1, Yellow = 2, Red = 3, Checking = 4 }
    class DotState { public HealthState State = HealthState.Gray; public string Detail = ""; public DateTime Checked = DateTime.MinValue; }
    // Index map: 0=server 1=tunnel 2=edge 3=signin 4=agent 5=tool(bridge probe).
    readonly DotState[] _health = { new DotState(), new DotState(), new DotState(), new DotState(), new DotState(), new DotState() };
    readonly object _healthLock = new object();
    const int HEALTH_DOT_COUNT = 6;
    Border[] _healthDot;           // the 6 colored dots (re-tinted by ApplyHealthToUi)
    TextBlock[] _healthSpin;       // rotating in-progress marks, shown instead of a stale color
    TextBlock[] _healthLbl;        // the 6 labels (re-textable on language toggle)
    Border[] _healthDotWrap;       // per-dot wrapper (tooltip host)
    Button _fixBtn;                // the 「直す」/Fix button (shown only when a dot is red/yellow)
    TextBlock _fixNote;            // inline progress/toast text in the strip
    Border _healthStrip;           // the whole fixed-width strip container
    Thread _healthThread;
    volatile bool _healthStop = false;
    volatile bool _fixRunning = false;   // guard: never run two fixes at once
    volatile int _fixTargetMask = 0;     // health axes currently being repaired; renders as Checking
    readonly AutoResetEvent _healthWake = new AutoResetEvent(false); // immediate post-action refresh
    volatile bool _bridgeReconnectRunning = false;   // guard: manual "Reconnect chat" button, never two at once
    string _agentMarkerId = "";    // T_.../P_... id extracted from the configured agent URL (.env)
    volatile bool _startAllLaunched = false;   // reentry guard for RunStartAll (per-cooldown, not per-app-run only)
    double _startAllLastUnix = 0.0;            // NowUnix() at last RunStartAll launch; 120s cooldown
    bool _startupHealCheckDone = false;        // set after the first PollHealthOnce's auto-heal decision runs once

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
        string dir = Path.GetDirectoryName(_statusPath);
        _commandsPath = Path.Combine(dir, "commands.json");
        _historyPath = Path.Combine(dir, "history.json");
        _openPath = Path.Combine(dir, "open.json");
        _convsPath = Path.Combine(dir, "conversations.json");
        _hiddenPath = Path.Combine(dir, "cockpit_hidden.json");
        _resumeDismissPath = Path.Combine(dir, "cockpit_resume_dismissed.json");
        LoadGlyphs();
        LoadHistory();
        LoadHidden();
        LoadSettings();
        ApplyThemeBrushes();
        Title = "Fleet Cockpit";   // also lets the taskbar / alt-tab / automation name this window
        // Clamp to the work area: a fixed 1080x760 overflows a shorter desktop (and the
        // UI scale multiplies it), pushing the footer off-screen. SystemParameters are
        // in device-independent units, the same space Width/Height use.
        Width = Math.Min(1080, Math.Max(720, SystemParameters.WorkArea.Width - 80));
        Height = Math.Min(760, Math.Max(520, SystemParameters.WorkArea.Height - 80));
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        // ── UI-scale shortcuts (window level) ────────────────────────────────────
        // Ctrl+Plus/Minus/0 and Ctrl+MouseWheel zoom the whole window. Registered at the
        // window so they fire from anywhere; the composer's own PreviewKeyDown only handles
        // Return/arrow/Tab/Esc, so these never collide with typing (a digit/OemPlus with Ctrl
        // held is not text input). We only act when Ctrl is down and swallow the event then.
        this.PreviewKeyDown += OnScaleKeyDown;
        this.PreviewMouseWheel += OnScaleMouseWheel;
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(700);
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        OnTick(null, null);
    }

    // Stop the background health poll on close (the thread is IsBackground so it would die with the
    // app anyway, but this makes shutdown deterministic and stops the ~15s sleep slices promptly).
    protected override void OnClosed(EventArgs e)
    {
        _healthStop = true;
        try { _healthWake.Set(); } catch (Exception) { }
        base.OnClosed(e);
    }

    static string ResolvePath(string path)
    {
        if (!string.IsNullOrEmpty(path)) return path;
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;          // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "status.json"));
    }

    // ── i18n ────────────────────────────────────────────────────────────────────
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);

    // Open (or foreground if already running) the main chat window -- the reverse of
    // CopilotChat.OpenCockpit, so you can return to the main screen FROM the fleet cockpit
    // (previously only main -> fleet existed).
    void OpenMain()
    {
        try
        {
            var existing = System.Diagnostics.Process.GetProcessesByName("CopilotChat");
            if (existing.Length > 0)
            {
                try { if (existing[0].MainWindowHandle != IntPtr.Zero) SetForegroundWindow(existing[0].MainWindowHandle); } catch { }
                return;
            }
            string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "CopilotChat.exe");
            if (File.Exists(exe)) System.Diagnostics.Process.Start(exe);
        }
        catch { }
    }

    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "title") return ja ? "並列" : "Parallel";
        if (k == "done") return ja ? "完了" : "done";
        if (k == "running") return ja ? "実行中" : "Running";
        if (k == "elapsed") return ja ? "経過" : "elapsed";
        if (k == "goals") return ja ? "ゴール" : "goals";
        if (k == "concurrent") return ja ? "同時" : "concurrent";
        if (k == "freeram") return ja ? "空きRAM" : "free RAM";
        if (k == "freed") return ja ? "完了で即解放" : "freed on finish";
        if (k == "turn") return ja ? "ターン" : "turn";
        if (k == "release") return ja ? "解放" : "release";
        if (k == "released") return ja ? "解放済" : "released";
        if (k == "maxtabs") return ja ? "最大タブ" : "Max tabs";
        if (k == "autoscale") return ja ? "RAM自動調整" : "Auto (RAM)";
        if (k == "auto_on") return ja ? "ON" : "ON";
        if (k == "auto_off") return ja ? "OFF" : "OFF";
        if (k == "def_tabs") return ja ? "開始(デフォルト)" : "Start";
        if (k == "max_tabs2") return ja ? "上限" : "Max";
        if (k == "idle") return ja ? "未実行 — python -m relay.fleet_runner で並列実行を開始するとここに表示されます。"
                                   : "Not running — start with python -m relay.fleet_runner to see goals here.";
        if (k == "stale") return ja ? "更新が止まっています（フリート停止？）" : "no updates (fleet stopped?)";
        if (k == "stale_wait") return ja ? "更新待ち" : "Waiting for update";
        if (k == "applies_next") return ja ? "次回起動から適用" : "applies next run";
        if (k == "start") return ja ? "並列実行を開始" : "Start parallel run";
        if (k == "goalhint") return ja ? "1行に1ゴール（複数可）・Ctrl+Enter で開始" : "One goal per line · Ctrl+Enter to start";
        if (k == "folder") return ja ? "自律コーディング (フォルダ)" : "Autonomous coding (folder)";
        // Feature B: view-filter toolbar (4-tab spec)
        if (k == "flt_all") return ja ? "すべて" : "All";
        if (k == "flt_active") return ja ? "実行中" : "Active";
        if (k == "flt_needs") return ja ? "承認待ち" : "Needs input";
        if (k == "flt_done") return ja ? "完了" : "Done";
        // legacy key kept for safety (no longer rendered)
        if (k == "flt_unfinished") return ja ? "未完了のみ" : "Unfinished only";
        // Feature C: retry
        if (k == "retry") return ja ? "再試行" : "Retry";
        if (k == "retry_all") return ja ? "停止を一括再試行" : "Retry all stopped";
        if (k == "retry_note") return ja ? "停止中のため、このゴール用にフリートを再起動しました" : "No run live — relaunched a fleet for this goal";
        if (k == "autoretry") return ja ? "自動再試行" : "Auto-retry";
        if (k == "cap") return ja ? "上限" : "cap";
        if (k == "to_history") return ja ? "履歴へ" : "To history";
        if (k == "all_to_history") return ja ? "すべて履歴へ" : "All to history";
        if (k == "eta") return ja ? "完了予測" : "ETA";
        if (k == "eta_in") return ja ? "あと" : "in";
        if (k == "eta_calc") return ja ? "計測中…" : "estimating…";
        if (k == "rate") return ja ? "件/時" : "/h";
        // Effort selector + fleet-wide pause/stop (NEW)
        if (k == "effort") return ja ? "推論" : "Reasoning";
        if (k == "approval") return ja ? "承認" : "Approval";
        if (k == "run_mode") return ja ? "実行方式" : "Run mode";
        if (k == "pause") return ja ? "一時停止" : "Pause";
        if (k == "resume") return ja ? "再開" : "Resume";
        if (k == "stopall") return ja ? "全停止" : "Stop all";
        if (k == "steer_dead") return ja ? "走行が停止中のため割り込めません（再開後にどうぞ）" : "No run live — can't steer (resume the fleet first)";
        // Feature 1: collapsed-card steer affordance (placeholder watermark + send ack toast).
        if (k == "steer_collapsed_placeholder") return ja ? "割り込み指示…" : "Steer…";
        if (k == "steer_collapsed_ack") return ja ? "↳ 送信" : "↳ Sent";
        // ── P0 Health strip (infra state) + Fix button + INFRA_STUCK / agent badge ──────
        if (k == "hs_server") return ja ? "サーバ" : "Server";
        if (k == "hs_tunnel") return ja ? "トンネル" : "Tunnel";
        if (k == "hs_edge") return ja ? "Edge" : "Edge";
        if (k == "hs_signin") return ja ? "サインイン" : "Sign-in";
        if (k == "hs_agent") return ja ? "エージェント" : "Agent";
        if (k == "hs_fix") return ja ? "直す" : "Fix";
        if (k == "hs_fixing_button") return ja ? "修復中…" : "Fixing…";
        if (k == "hs_fix_hint") return ja ? "検出された不具合を直す" : "Fix the detected problem";
        if (k == "hs_ok") return ja ? "正常" : "OK";
        if (k == "hs_down") return ja ? "応答なし" : "down";
        if (k == "hs_unknown") return ja ? "未設定/不明" : "unknown";
        if (k == "hs_checking") return ja ? "確認中…" : "checking…";
        if (k == "hs_lastcheck") return ja ? "最終確認: " : "last check: ";
        if (k == "hs_never") return ja ? "未確認" : "not checked yet";
        if (k == "hs_srv_detail_ok") return ja ? "ローカルサーバは応答しています (127.0.0.1:8000)" : "Local server responding (127.0.0.1:8000)";
        if (k == "hs_srv_detail_bad") return ja ? "ローカルサーバが応答しません。start_all.bat を実行してください。" : "Local server not responding. Run start_all.bat.";
        if (k == "hs_tun_detail_ok") return ja ? "トンネル経由でサーバに到達できます" : "Server reachable through the tunnel";
        if (k == "hs_tun_detail_bad") return ja ? "トンネルからサーバに到達できません" : "Server not reachable through the tunnel";
        if (k == "hs_tun_detail_none") return ja ? "MCP_TUNNEL_URL が .env に未設定です" : "MCP_TUNNEL_URL is not set in .env";
        if (k == "hs_edge_detail_ok") return ja ? "コンパニオン Edge が稼働中 (:9222)" : "Companion Edge running (:9222)";
        if (k == "hs_edge_detail_bad") return ja ? "コンパニオン Edge に接続できません (:9222)" : "Companion Edge not reachable (:9222)";
        if (k == "hs_signin_ok") return ja ? "M365 にサインイン済み（ログイン画面なし）" : "Signed in to M365 (no login wall)";
        if (k == "hs_signin_bad") return ja ? "サインインが必要です（ログイン画面を検出）" : "Sign-in required (login wall detected)";
        if (k == "hs_agent_ok") return ja ? "専用エージェントに接続中" : "Bound to the configured agent";
        if (k == "hs_agent_warn") return ja ? "既定Copilotに落ちている可能性（エージェント未検出）" : "Possible default-Copilot fallback (agent tab not found)";
        if (k == "hs_agent_bad") return ja ? "実行中ですがM365チャットタブを検出できません" : "Run is active but no M365 chat tab is available";
        if (k == "hs_agent_gray") return ja ? "Edge 停止中のため判定不可" : "Edge down — cannot tell";
        if (k == "hs_fixing") return ja ? "修復中… " : "Fixing… ";
        if (k == "hs_fix_signin") return ja ? "サインイン用に Edge を開いています…" : "Opening Edge for sign-in…";
        if (k == "hs_fix_signin_toast") return ja ? "開いたEdgeでサインインしてください。完了すると自動で緑になります。" : "Sign in on the Edge that opened; it turns green automatically when done.";
        if (k == "hs_fix_edge") return ja ? "Edge を再起動しています…" : "Relaunching Edge…";
        if (k == "hs_fix_agent") return ja ? "コネクタを再接続しています…" : "Reconnecting the connector…";
        if (k == "hs_fix_agent_ok") return ja ? "再接続に成功しました" : "Reconnect succeeded";
        if (k == "hs_fix_agent_fail") return ja ? "再接続に失敗（手動確認が必要）" : "Reconnect failed (needs manual check)";
        if (k == "hs_fix_server") return ja ? "start_all.bat を実行してください" : "Please run start_all.bat";
        if (k == "hs_fix_stack") return ja ? "サーバ/トンネルを起動しています(最大2分)" : "Starting server/tunnel (up to ~2 min)";
        if (k == "hs_fix_done") return ja ? "完了" : "done";
        if (k == "hs_fix_err") return ja ? "修復でエラー" : "fix error";
        if (k == "hs_fix_manual_needed") return ja ? "手動での対応が必要です" : "Manual step needed";
        if (k == "infra_wait") return ja ? "インフラ待ち" : "Infra wait";
        if (k == "infra_retry") return ja ? "再投入" : "Re-queue";
        if (k == "badge_default_copilot") return ja ? "既定Copilot" : "default Copilot";
        // ── P1/P2 UX: artifacts, history groups/search/auto-archive, resume ──────────
        if (k == "artifacts") return ja ? "成果物" : "Artifacts";
        if (k == "copy_result") return ja ? "結果をコピー" : "Copy result";
        if (k == "copy_result_done") return ja ? "結果をコピーしました" : "Result copied";
        if (k == "copy_result_fail") return ja ? "結果をコピーできませんでした" : "Could not copy result";
        if (k == "reveal_artifacts") return ja ? "成果物を表示" : "Show artifacts";
        if (k == "reveal_artifacts_done") return ja ? "成果物をExplorerで表示しました" : "Opened artifacts in Explorer";
        if (k == "reveal_artifacts_fail") return ja ? "成果物を表示できませんでした" : "Could not open artifacts";
        if (k == "rerun_same") return ja ? "同じ条件でもう一度実行" : "Run again with the same goal";
        if (k == "rerun_started") return ja ? "同じ条件で再実行を開始しました" : "Started the same goal again";
        if (k == "path_missing") return ja ? "見つかりません" : "Not found";
        if (k == "hist_today") return ja ? "今日" : "Today";
        if (k == "hist_yesterday") return ja ? "昨日" : "Yesterday";
        if (k == "hist_earlier") return ja ? "その他" : "Earlier";
        if (k == "hist_search") return ja ? "履歴を検索…" : "Search history…";
        if (k == "autoarchive") return ja ? "完了を自動で履歴へ" : "Auto-archive on finish";
        if (k == "set_archive_section") return ja ? "自動アーカイブ" : "Auto-archive";
        // Capacity-wait banner (admission gate) + force-start
        // Settings panel (gear popup) -- consolidates the scattered toolbar controls
        if (k == "settings") return ja ? "設定" : "Settings";
        if (k == "set_tabs_section") return ja ? "並列タブ" : "Parallel tabs";
        if (k == "set_retry_section") return ja ? "自動再試行" : "Auto-retry";
        if (k == "set_capacity_section") return ja ? "容量ガード" : "Capacity guard";
        if (k == "disk_floor") return ja ? "実行下限ディスク (GB)" : "Disk floor (GB)";
        if (k == "disk_floor_hint") return ja ? "空きディスクがこの値を下回るとタブ開放を待機します。" : "Pauses opening tabs when free disk drops below this.";
        if (k == "ram_floor") return ja ? "確保する空きRAM (MB)" : "RAM floor (MB)";
        if (k == "ui_scale_section") return ja ? "表示サイズ" : "UI scale";
        if (k == "ui_scale") return ja ? "表示サイズ" : "UI scale";
        if (k == "ui_scale_hint") return ja ? "Ctrl+ホイールや Ctrl +/− でも変更できます（Ctrl+0 で自動）。" : "Also change with Ctrl+wheel or Ctrl +/− (Ctrl+0 = auto).";
        if (k == "ui_auto") return ja ? "自動" : "Auto";
        if (k == "ui_auto_on") return ja ? "自動: オン" : "Auto: on";
        if (k == "ui_auto_off") return ja ? "自動: オフ" : "Auto: off";
        if (k == "force_start") return ja ? "今すぐ開始" : "Start now";
        if (k == "floor_restore") return ja ? "容量制限を戻す" : "Restore limit";
        if (k == "floor_off") return ja ? "容量制限を一時解除しています" : "Capacity limit paused";
        // FIX B: Stop button's optimistic "stopping" state (distinct from the steady-state tooltip).
        if (k == "stop_stopping") return ja ? "停止中…" : "Stopping…";
        // FIX C: explicit verb -- distinct from the archive ("履歴へ"/"to_history") and per-card
        // release ("解放"/"release") dismiss verbs, so the three no longer read as ambiguous synonyms.
        if (k == "clear_history") return ja ? "履歴を空にする" : "Clear history";
        // FIX D: same lightweight toast wording for both the autoscale-ON and autoscale-OFF apply paths.
        if (k == "maxtabs_toast_prefix") return ja ? "最大タブ →" : "Max tabs ->";
        // ── 6th health dot "Tool": bridge self-probe (.fleet/tool_probe.json), see the class-field
        // comment near HEALTH_DOT_COUNT for what this axis covers and why it's separate from Agent.
        if (k == "hs_tool") return ja ? "ツール" : "Tool";
        if (k == "hs_tool_detail_ok") return ja ? "ツール呼び出し確認 OK" : "tool call confirmed OK";
        if (k == "hs_tool_detail_consent") return ja ? "consent待ち（再接続で解消可）" : "consent pending (reconnect can clear this)";
        if (k == "hs_tool_detail_down") return ja ? "応答なし(再接続が必要)" : "no response (reconnect needed)";
        if (k == "hs_tool_detail_no_tool") return ja ? "チャットは応答あり／ツール未接続の可能性" : "chat is answering; tool may not be attached";
        if (k == "hs_tool_detail_stale") return ja ? "自己診断が実行されていません" : "self-probe hasn't run recently";
        if (k == "hs_tool_detail_none") return ja ? "自己診断は未設定(機能無効)" : "self-probe not configured (feature off)";
        if (k == "hs_tool_detail_checking") return ja ? "ツール接続を確認中…" : "checking tool connection…";
        // Bridge-targeted Fix tier (RunFix priority 5) + the always-available manual button.
        if (k == "hs_fix_bridge") return ja ? "チャットのブリッジを再接続しています…" : "Reconnecting the chat bridge…";
        if (k == "hs_fix_bridge_ok") return ja ? "ブリッジの再接続に成功しました" : "Bridge reconnect succeeded";
        if (k == "hs_fix_bridge_fail") return ja ? "ブリッジの再接続に失敗（手動確認が必要）" : "Bridge reconnect failed (needs manual check)";
        if (k == "set_chat_section") return ja ? "チャット" : "Chat";
        if (k == "reconnect_chat") return ja ? "チャット再接続" : "Reconnect chat";
        if (k == "reconnect_chat_hint") return ja ? "対話チャット(ブリッジ :9223)のMCP接続を再接続します" : "Reconnect the interactive chat bridge's MCP connection (:9223)";
        if (k == "reconnect_chat_toast_start") return ja ? "チャット再接続中…" : "Reconnecting chat…";
        if (k == "reconnect_chat_toast_ok") return ja ? "チャット再接続に成功しました" : "Chat reconnect succeeded";
        if (k == "reconnect_chat_toast_fail") return ja ? "チャット再接続に失敗しました（手動確認が必要）" : "Chat reconnect failed (needs manual check)";
        if (k == "reconnect_chat_toast_dead") return ja
            ? "エージェントが読み込まれませんでした。次を実行してください: powershell -File scripts\\start_bridge.ps1 -HardReset -Keepalive"
            : "Agent did not load. Run: powershell -File scripts\\start_bridge.ps1 -HardReset -Keepalive";
        return k;
    }
    // Inline bilingual helper for one-off strings that don't warrant a T() dictionary entry
    // (e.g. the 詳細設定/Advanced panel). Same signature/behaviour as ApprovalPromptWindow's L()
    // elsewhere in this file -- kept as a small per-class duplicate rather than a shared static
    // because both classes' _lang fields are independent instance state.
    string L(string ja, string en) { return _lang == 0 ? ja : en; }
    string StatusLabel(string s)
    {
        bool ja = _lang == 0;
        if (s == "waiting") return ja ? "実行中" : "Running";
        if (s == "done") return ja ? "完了" : "Done";
        if (s == "stuck") return ja ? "停滞" : "Stuck";
        if (s == "maxturns") return ja ? "上限" : "Max turns";
        if (s == "error") return ja ? "エラー" : "Error";
        if (s == "cancelled") return ja ? "停止" : "Stopped";
        if (s == "pending") return ja ? "待機列" : "Queued";
        if (s == "ready") return ja ? "準備" : "Ready";
        return s;
    }

    // ── Material Symbols glyphs (vector, no emoji) ───────────────────────────────
    void LoadGlyphs()
    {
        try
        {
            string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets", "material_glyphs.json");
            if (!File.Exists(p)) return;
            var o = (Dictionary<string, object>)_js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8));
            if (o.ContainsKey("unitsPerEm")) _upm = Convert.ToDouble(o["unitsPerEm"]);
            var g = (Dictionary<string, object>)o["glyphs"];
            foreach (KeyValuePair<string, object> kv in g) _glyphs[kv.Key] = kv.Value.ToString();
        }
        catch (Exception) { }
    }
    UIElement MakeIcon(string name, double size, Brush fill)
    {
        if (!_glyphs.ContainsKey(name))
        {
            var ph = new Border(); ph.Width = size; ph.Height = size; return ph;
        }
        var path = new System.Windows.Shapes.Path();
        // Geometry.Parse returns a FROZEN StreamGeometry -> Clone() to get a writable
        // copy before setting Transform (otherwise InvalidOperationException).
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);  // font y-up -> WPF y-down
        path.Data = geo; path.Fill = fill; path.Stretch = Stretch.None;
        path.Width = size; path.Height = size;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        path.VerticalAlignment = VerticalAlignment.Center;
        return path;
    }

    // ── settings (shared with the chat) ──────────────────────────────────────────
    void LoadSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            foreach (string ln in File.ReadAllLines(SettingsFile))
            {
                int v;
                if (ln.StartsWith("dark=")) _dark = ln.Substring(5).Trim() != "0";
                else if (ln.StartsWith("lang=") && int.TryParse(ln.Substring(5).Trim(), out v)) _lang = v;
                else if (ln.StartsWith("maxtabs=") && int.TryParse(ln.Substring(8).Trim(), out v)) _maxtabs = Math.Max(1, Math.Min(100, v));
                else if (ln.StartsWith("autoscale_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoMax = Math.Max(1, Math.Min(100, v));
                else if (ln.StartsWith("autoscale=")) _autoscale = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoretry_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoRetryMax = Math.Max(1, Math.Min(3, v));
                else if (ln.StartsWith("autoretry=")) _autoRetry = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoarchive=")) _autoArchive = ln.Substring(12).Trim() == "1";
                else if (ln.StartsWith("disk_floor_gb="))
                {
                    double df;
                    if (double.TryParse(ln.Substring(14).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out df))
                        _diskFloor = Math.Max(0.0, Math.Min(100.0, df));
                }
                else if (ln.StartsWith("ram_floor_mb="))
                {
                    double rf;
                    if (double.TryParse(ln.Substring(13).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out rf))
                        _ramFloor = Math.Max(0.0, Math.Min(65536.0, rf));
                }
                else if (ln.StartsWith("ui_scale="))
                {
                    string sv = ln.Substring(9).Trim();
                    if (sv.Equals("auto", StringComparison.OrdinalIgnoreCase))
                    { _uiAuto = true; _uiScaleLoaded = true; }
                    else
                    {
                        double us;
                        if (double.TryParse(sv, System.Globalization.NumberStyles.Float,
                                            System.Globalization.CultureInfo.InvariantCulture, out us))
                        { _uiAuto = false; _uiScale = Math.Max(0.8, Math.Min(2.0, us)); _uiScaleLoaded = true; }
                    }
                }
                else if (ln.StartsWith("ui_scale_target="))
                {
                    double ut;
                    if (double.TryParse(ln.Substring(16).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out ut))
                    { _scaleTarget = Math.Max(0.8, Math.Min(3.0, ut)); _scaleTargetLoaded = true; }
                }
                else if (ln.StartsWith("effort="))
                {
                    string ef = ln.Substring(7).Trim();
                    if (ef == "min" || ef == "max" || ef == "ultra" || ef == "auto") _effort = ef;
                }
                else if (ln.StartsWith("approval="))
                {
                    string ap = ln.Substring(9).Trim();
                    if (ap == "run" || ap == "plan" || ap == "auto") _approval = ap;
                }
            }
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }
    void SaveKey(string key, string val)
    {
        try
        {
            var lines = new List<string>(); bool found = false;
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    if (ln.StartsWith(key + "=")) { lines.Add(key + "=" + val); found = true; }
                    else lines.Add(ln);
                }
            if (!found) lines.Add(key + "=" + val);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile));
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
        }
        catch (Exception) { }
    }

    // ── theme ─────────────────────────────────────────────────────────────────────
    void ApplyThemeBrushes()
    {
        // Single source of truth: Theme.cs (calm warm-neutral palette, spec Design Tokens).
        Bg = Theme.Br(Theme.Bg(_dark));
        CardBg = Theme.Br(Theme.Surface(_dark));
        Border = Theme.Br(Theme.Border(_dark));
        Fg = Theme.Br(Theme.Text(_dark));
        Muted = Theme.Br(Theme.Muted(_dark));
        QuoteBg = Theme.Br(Theme.SurfaceSubtle(_dark));
        BtnBg = Theme.Br(Theme.SurfaceSubtle(_dark));
        Accent = Theme.Br(Theme.Accent(_dark));
    }

    Color BgColor() { return Theme.Col(Theme.Bg(_dark)); }
    Color CardColor() { return Theme.Col(Theme.Surface(_dark)); }
    static Color Mix(Color a, Color b, double t)
    {
        return Color.FromRgb((byte)(a.R * t + b.R * (1 - t)),
                             (byte)(a.G * t + b.G * (1 - t)),
                             (byte)(a.B * t + b.B * (1 - t)));
    }
    string ColorKey(string s)
    {
        if (s == "waiting") return "good";
        if (s == "done") return "done";
        if (s == "stuck" || s == "maxturns" || s == "error") return "bad";
        return "muted";
    }
    Color StatusColor(string ck)
    {
        // Repointed to the new token palette. ColorKey()'s legacy good/done/bad keys map
        // onto info/success/danger; Ph3 replaces this path with canonical status + rail.
        if (ck == "good") return Theme.Col(Theme.Info(_dark));
        if (ck == "done") return Theme.Col(Theme.Success(_dark));
        if (ck == "bad") return Theme.Col(Theme.Danger(_dark));
        return Theme.Col(Theme.Muted(_dark));
    }

    void BuildChrome()
    {
        var root = new DockPanel();
        _headBar = new Border();
        _headBar.Padding = new Thickness(26, 20, 18, 8);
        DockPanel.SetDock(_headBar, Dock.Top);

        // Header = a 2-column Grid, NOT a DockPanel. The old DockPanel let the (un-clipped)
        // title StackPanel render its long subtitle PAST its arrange rect, sliding under the
        // right-docked control band whose opaque themed backgrounds then painted over the text
        // -> the subtitle "disappeared" exactly where it reached the RAM controls. A Grid gives
        // the title column a HARD bounded width (star) next to the auto-width controls, so the
        // two never overlap; the subtitle is then trimmed with an ellipsis instead of hidden.
        // Header = a 2-col x 2-row Grid. Row 0: title (col 0) + RAM/lang/theme controls (col 1).
        // Row 1: the SUBTITLE, spanning BOTH columns (full width, also under the controls). The
        // subtitle carries the long live line (elapsed + ETA + goals + concurrency); keeping it in
        // col 0 only meant the RAM-controls column ate its right end and the ETA got clipped ("8...").
        // Full-width row + wrapping = nothing is ever hidden.
        var headRow = new Grid();
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // title + controls
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // subtitle (full width)

        // right controls: live status/actions, language, theme (row 0, col 1).
        // Vertically CENTERED so the control cluster shares one horizontal center line with the
        // left-edge health strip (redesign: single clean header band, dots flush-left / controls
        // flush-right, all centered on one line — no big in-content title above).
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Center;
        ctrls.HorizontalAlignment = HorizontalAlignment.Right;

        // Read-only live tab pressure. Configuration belongs in Settings; while idle this chip is
        // hidden so the header does not duplicate the Start/ceiling/autoscale controls.
        _workerChip = new TextBlock();
        _workerChip.FontSize = 12; _workerChip.VerticalAlignment = VerticalAlignment.Center;
        _workerChip.Margin = new Thickness(0, 0, 12, 0);
        _workerChip.Padding = new Thickness(10, 3, 10, 3);
        UpdateWorkerChip(0, 0, false);   // initial paint
        _workerChipBorder = new Border();
        _workerChipBorder.Child = _workerChip;
        _workerChipBorder.BorderThickness = new Thickness(1);
        _workerChipBorder.CornerRadius = new CornerRadius(Theme.RadSmall);
        _workerChipBorder.Padding = new Thickness(0);
        _workerChipBorder.VerticalAlignment = VerticalAlignment.Center;
        _workerChipBorder.Margin = new Thickness(0, 0, 12, 0);
        _workerChipBorder.Visibility = Visibility.Collapsed;
        PaintWorkerChipBorder(_workerChipBorder);
        ctrls.Children.Add(_workerChipBorder);

        ctrls.Children.Add(EffortControl());
        ctrls.Children.Add(ApprovalControl());
        ctrls.Children.Add(ApprovalCenterControl());
        ctrls.Children.Add(FleetControls());
        // gear -> settings popup consolidating the scattered start/上限/retry/disk-floor controls.
        ctrls.Children.Add(SettingsControl());
        // Language and theme toggles: 1-click direct icon buttons in the header (frequently used).
        _langBtn = IconButton("translate", 18, _lang == 0 ? "言語切替" : "Toggle language");
        _langBtn.ToolTip = _lang == 0 ? "English / 日本語 切替" : "Toggle language";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); RebuildChrome(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18, _dark ? "Switch to light mode" : "Switch to dark mode");
        _themeBtn.ToolTip = _dark ? "Switch to light mode" : "Switch to dark mode";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyTheme(); };
        ctrls.Children.Add(_themeBtn);
        // Rare items (open main chat, self-improve) stay in the overflow. Keep fields non-null (PaintChrome references them).
        _mainBtn = IconButton("chat", 18, _lang == 0 ? "メインチャットを開く" : "Open main chat");
        _mainBtn.Click += delegate { OpenMain(); };
        _siBtn = IconButton("account_tree", 18, _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement");
        _siBtn.Click += delegate { new SelfImproveDashboardWindow().Show(); };
        ctrls.Children.Add(OverflowControl());
        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headRow.Children.Add(ctrls);

        // Redesign: the big in-content title was REMOVED (it duplicated the OS window title
        // "Fleet Cockpit" and truncated to "Fl…", wasting the top-left). _header is kept as a
        // live, non-null TextBlock so the existing PaintChrome/OnTick references (which set its
        // .Text/.Foreground) never NRE — it is simply NOT added to the visual tree.
        _header = new TextBlock(); _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis; _header.TextWrapping = TextWrapping.NoWrap;
        _header.Visibility = Visibility.Collapsed;   // defensive: never rendered even if re-parented

        // P0 HEALTH STRIP now occupies col 0 (where the title was): five infra dots + inline Fix
        // pill, left-aligned and vertically centered so the dots サーバ→エージェント sit on the same
        // center line as the right-side control cluster. One clean horizontal band.
        var healthHost = BuildHealthStrip();
        Grid.SetColumn((UIElement)healthHost, 0); Grid.SetRow((UIElement)healthHost, 0);
        headRow.Children.Add((UIElement)healthHost);

        // subtitle -- its OWN row spanning BOTH columns, so the long elapsed+ETA line uses the full
        // width and is never clipped by the controls column. Wrap (not ellipsis) so it's never hidden.
        // Feature 2: _sub itself is no longer shown -- UpdateHeader still builds its sentence (kept
        // as a plain-text fallback / ToolTip source) but the VISIBLE row 1 content is now _subChips,
        // a WrapPanel of discrete Pill() chips occupying the identical grid cell.
        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 18, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        _sub.Visibility = Visibility.Collapsed;
        headRow.Children.Add(_sub);

        _subChips = new WrapPanel();
        _subChips.Margin = new Thickness(0, 4, 18, 0);
        Grid.SetColumn(_subChips, 0); Grid.SetColumnSpan(_subChips, 2); Grid.SetRow(_subChips, 1);
        headRow.Children.Add(_subChips);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);
        root.Children.Add(BuildMtBanner());
        // BuildGateBanner() is NOT added here -- it is docked inside the run column (col1) so it
        // cannot overhang the timeline spine. See the comment at that call site.
        root.Children.Add(BuildCapBanner());
        // The composer docks to the BOTTOM (spec: agent-workspace feel, not a form). It must be
        // added before _list so the list — the LastChildFill element — fills the space above it.
        root.Children.Add(BuildInputBar());

        // Virtualizing card list. A ListBox brings its own ScrollViewer + VirtualizingStackPanel,
        // so with 100-164 cards only the ~10-20 visible rows are realized -> scrolling and
        // window maximize/restore stay fast. The container chrome is templated away so it reads
        // as a plain scrolling list (no selection highlight / mouse-over / focus rect).
        _list = new ListBox();
        _list.BorderThickness = new Thickness(0);
        _list.Background = Brushes.Transparent;
        _list.Padding = new Thickness(18, 6, 18, 4);   // was bottom 24 — left a gap between the last row and the composer
        ScrollViewer.SetVerticalScrollBarVisibility(_list, ScrollBarVisibility.Auto);
        ScrollViewer.SetHorizontalScrollBarVisibility(_list, ScrollBarVisibility.Disabled);
        // Pixel scroll (not item scroll) so the list can SIZE TO CONTENT inside an Auto row and
        // therefore bottom-anchor: a short list hugs the composer (no dead gap), a long list is
        // capped to the viewport (MaxHeight) and scrolls. Item-virtualization is traded away here;
        // the fleet ledger row count is modest and rows are light.
        ScrollViewer.SetCanContentScroll(_list, false);
        VirtualizingPanel.SetIsVirtualizing(_list, true);
        VirtualizingPanel.SetVirtualizationMode(_list, VirtualizationMode.Recycling);
        VirtualizingPanel.SetScrollUnit(_list, ScrollUnit.Pixel);
        _list.Focusable = false;
        _list.IsTabStop = false;
        KeyboardNavigation.SetDirectionalNavigation(_list, KeyboardNavigationMode.None);
        _list.ItemContainerStyle = BuildItemContainerStyle();
        _list.ItemTemplate = BuildRowTemplate();
        _list.ItemsSource = _rows;     // bound ONCE; SetRows mutates _rows in place (no Reset)

        // A2-2: 2-column Grid: col0 = Evidence Spine (fixed ~220px, collapses to 0 when idle),
        // col1 = _list (star, unchanged). The spine does NOT scroll with the list.
        var lanesGrid = new Grid();
        _spineCol = new ColumnDefinition();
        _spineCol.Width = new GridLength(0);       // starts hidden (idle); RefreshSpine will open it
        lanesGrid.ColumnDefinitions.Add(_spineCol);
        lanesGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        _spinePanel = new Border();
        _spinePanel.BorderThickness = new Thickness(0, 0, 1, 0);
        _spinePanel.Padding = new Thickness(0);
        Grid.SetColumn(_spinePanel, 0);
        lanesGrid.Children.Add(_spinePanel);

        // Bottom-anchor wrapper (chat-style): a '*' spacer row absorbs the slack ABOVE the rows,
        // so when the content is shorter than the viewport the rows sit just above the composer
        // instead of leaving a dead void between the last row and the input. The list row is Auto
        // (sizes to content) but the list's MaxHeight is bound to the wrapper height, so a long
        // list is capped to the viewport and scrolls normally.
        var listWrap = new Grid();
        listWrap.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        listWrap.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var maxBind = new System.Windows.Data.Binding("ActualHeight");
        maxBind.Source = listWrap;
        _list.SetBinding(FrameworkElement.MaxHeightProperty, maxBind);
        Grid.SetRow(_list, 1);
        listWrap.Children.Add(_list);

        // Col1 is now a DockPanel: the pinned filter bar sits at the TOP (fixed), the bottom-anchored
        // listWrap fills the rest. Because the toolbar host is OUTSIDE the ListBox's ScrollViewer, it
        // never scrolls and never moves as cards/history grow — it is anchored to the top of the run
        // area directly under the header. The left padding matches the list's inner padding (18) so
        // the bar and the cards under it share the same left edge.
        var col1 = new DockPanel { LastChildFill = true };
        // The approval banner belongs to the RUN column, not to the whole window. Docked on the
        // root it spanned the full width and overhung the timeline spine once that column opened,
        // so the banner's box crossed the spine's frame instead of lining up with the cards it
        // refers to. Docking it here keeps its left edge on the same 18px gutter as the toolbar
        // and the cards below it.
        col1.Children.Add(BuildGateBanner());
        _pinnedToolbarHost = new Border();
        _pinnedToolbarHost.Padding = new Thickness(18, 6, 18, 0);
        _pinnedToolbarHost.Visibility = Visibility.Collapsed;   // shown once there is a run/history to filter
        DockPanel.SetDock(_pinnedToolbarHost, Dock.Top);
        col1.Children.Add(_pinnedToolbarHost);
        col1.Children.Add(listWrap);   // LastChildFill -> fills below the pinned bar
        Grid.SetColumn(col1, 1);
        lanesGrid.Children.Add(col1);

        root.Children.Add(lanesGrid);

        // UI SCALE: anchor the ScaleTransform as the LayoutTransform on THIS root DockPanel — the
        // single element that contains ALL chrome (header + pinned toolbar host + health strip +
        // card list + composer). LayoutTransform (not RenderTransform) so children re-measure and
        // reflow at the zoomed size instead of being bitmap-stretched. RebuildChrome() calls
        // BuildChrome() afresh, so a NEW root gets the transform re-applied here every rebuild.
        _rootScale = new ScaleTransform(_uiScale, _uiScale);
        root.LayoutTransform = _rootScale;
        // Overlay toast lives OUTSIDE the scaled root (added to a Grid wrapper) so its size is
        // constant regardless of zoom; it self-fades on each scale change.
        var appRoot = new Grid();
        appRoot.Children.Add(root);
        appRoot.Children.Add(BuildScaleToast());
        Content = appRoot;
        PaintChrome();
        StartHealthPoll();
    }

    // A small centered self-fading overlay showing the current zoom % (e.g. "125%"). Built once per
    // BuildChrome; ShowScaleToast() sets the text, makes it visible, and restarts the fade timer.
    UIElement BuildScaleToast()
    {
        _scaleToast = new Border();
        _scaleToast.HorizontalAlignment = HorizontalAlignment.Center;
        _scaleToast.VerticalAlignment = VerticalAlignment.Top;
        _scaleToast.Margin = new Thickness(0, 18, 0, 0);
        _scaleToast.Padding = new Thickness(14, 7, 14, 7);
        _scaleToast.CornerRadius = new CornerRadius(8);
        _scaleToast.Background = Theme.Br(Theme.Surface(_dark));
        _scaleToast.BorderBrush = Border;
        _scaleToast.BorderThickness = new Thickness(1);
        _scaleToast.IsHitTestVisible = false;
        _scaleToast.Visibility = Visibility.Collapsed;
        _scaleToast.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 14, ShadowDepth = 2, Opacity = 0.22, Color = C("#000000") };
        _scaleToastText = new TextBlock();
        _scaleToastText.Foreground = Fg; _scaleToastText.FontSize = 15;
        _scaleToastText.FontWeight = FontWeights.SemiBold;
        _scaleToast.Child = _scaleToastText;
        return _scaleToast;
    }

    void ShowScaleToast() { ShowScaleToast(null); }
    // overrideText != null shows that literal (e.g. "自動" when entering auto mode); otherwise the %.
    void ShowScaleToast(string overrideText)
    {
        if (_scaleToast == null || _scaleToastText == null) return;
        _scaleToastText.Text = overrideText != null
            ? overrideText
            : T("ui_scale") + "  " + ((int)Math.Round(_uiScale * 100)) + "%";
        _scaleToast.BeginAnimation(UIElement.OpacityProperty, null);
        _scaleToast.Opacity = 1.0;
        _scaleToast.Visibility = Visibility.Visible;
        if (_scaleToastTimer == null)
        {
            _scaleToastTimer = new DispatcherTimer();
            _scaleToastTimer.Interval = TimeSpan.FromMilliseconds(900);
            _scaleToastTimer.Tick += delegate
            {
                _scaleToastTimer.Stop();
                if (_scaleToast == null) return;
                var fade = new DoubleAnimation(1.0, 0.0, new Duration(TimeSpan.FromMilliseconds(450)));
                fade.Completed += delegate { if (_scaleToast != null) _scaleToast.Visibility = Visibility.Collapsed; };
                _scaleToast.BeginAnimation(UIElement.OpacityProperty, fade);
            };
        }
        _scaleToastTimer.Stop();
        _scaleToastTimer.Start();
    }

    // Current monitor scale = this window's device pixels per DIP (DPI/96). 1.0 at 100%, 1.5 at 150%.
    // Read from the live PresentationSource; falls back to 1.0 before the window is sourced.
    double CurrentMonitorScale()
    {
        try
        {
            var src = System.Windows.PresentationSource.FromVisual(this);
            if (src != null && src.CompositionTarget != null)
            {
                double m11 = src.CompositionTarget.TransformToDevice.M11;
                if (m11 > 0.01) return m11;
            }
        }
        catch (Exception) { }
        return 1.0;
    }

    // AUTO effective scale for a given monitor scale: target physical size / monitor's own DPI scale,
    // clamped to the LayoutTransform range. monitorScale (WPF DPI) × effective ≈ _scaleTarget, i.e. the
    // physical size is constant across monitors. Clamp can cap it on extreme monitors (see numeric proof).
    double EffectiveAutoScale(double monitorScale)
    {
        if (monitorScale < 0.01) monitorScale = 1.0;
        return Math.Max(0.8, Math.Min(2.0, _scaleTarget / monitorScale));
    }

    // Push the current _uiScale onto the live LayoutTransform + gear label. Pure apply (no persist).
    void PushScaleToTransform()
    {
        if (_rootScale != null) { _rootScale.ScaleX = _uiScale; _rootScale.ScaleY = _uiScale; }
        if (_uiScaleVal != null) _uiScaleVal.Text = AutoLabelText();
    }

    // Recompute + apply the AUTO effective scale for THIS window's current monitor. Silent by default
    // (DPI-change recompute must not toast). Does NOT persist ui_scale (auto marker already persisted).
    void ApplyAutoScale(bool toast)
    {
        _uiScale = EffectiveAutoScale(CurrentMonitorScale());
        PushScaleToTransform();
        if (toast) ShowScaleToast(T("ui_auto"));
    }

    // Gear value-label text: "自動 (130%)" in auto, plain "130%" in manual.
    string AutoLabelText()
    {
        int pct = (int)Math.Round(_uiScale * 100);
        return _uiAuto ? (T("ui_auto") + " (" + pct + "%)") : (pct + "%");
    }

    // MANUAL apply: clamp 0.8–2.0, leave/enter manual mode, push to transform, persist ui_scale=number.
    void ApplyUiScale(double v, bool persist) { ApplyUiScale(v, persist, true); }
    void ApplyUiScale(double v, bool persist, bool toast)
    {
        _uiAuto = false;
        _uiScale = Math.Max(0.8, Math.Min(2.0, Math.Round(v, 2)));
        PushScaleToTransform();
        RefreshUiScaleControls();
        if (persist) { SaveKey("ui_scale", _uiScale.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture)); _uiScaleLoaded = true; }
        if (toast) ShowScaleToast();
    }

    // Switch to AUTO: persist ui_scale=auto, recompute for the current monitor, and (optionally) toast.
    void EnableAutoScale(bool toast)
    {
        _uiAuto = true;
        SaveKey("ui_scale", "auto");
        _uiScaleLoaded = true;
        ApplyAutoScale(toast);
        RefreshUiScaleControls();
    }

    // Keep the gear-popup toggle/label/steppers in sync with the current mode (called on mode changes).
    void RefreshUiScaleControls()
    {
        if (_uiScaleVal != null) _uiScaleVal.Text = AutoLabelText();
        if (_uiAutoBtn != null) _uiAutoBtn.Content = _uiAuto ? T("ui_auto_on") : T("ui_auto_off");
        // In auto mode the manual steppers are meaningless (value is monitor-derived) -> disable them.
        if (_uiScaleMinus != null) _uiScaleMinus.IsEnabled = !_uiAuto;
        if (_uiScalePlus != null) _uiScalePlus.IsEnabled = !_uiAuto;
    }

    // Window-level Ctrl+Plus / Ctrl+Minus / Ctrl+0 zoom. Handles both the main-row and NumPad keys.
    // OemPlus/OemMinus are the US-layout '=' and '-' keys; Add/Subtract are the numeric-keypad ones.
    // +/- while in auto -> switch to MANUAL at the resulting number; Ctrl+0 -> back to AUTO.
    void OnScaleKeyDown(object sender, KeyEventArgs e)
    {
        if ((Keyboard.Modifiers & ModifierKeys.Control) == 0) return;
        if (e.Key == Key.OemPlus || e.Key == Key.Add)
        { ApplyUiScale(_uiScale + 0.1, true); e.Handled = true; }
        else if (e.Key == Key.OemMinus || e.Key == Key.Subtract)
        { ApplyUiScale(_uiScale - 0.1, true); e.Handled = true; }
        else if (e.Key == Key.D0 || e.Key == Key.NumPad0)
        { EnableAutoScale(true); e.Handled = true; }   // Ctrl+0 = "back to automatic"
    }

    // Ctrl+MouseWheel = ±0.1 per notch (-> MANUAL). Swallowed only when Ctrl is held so normal list
    // scroll (no modifier) is untouched.
    void OnScaleMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if ((Keyboard.Modifiers & ModifierKeys.Control) == 0) return;
        ApplyUiScale(_uiScale + (e.Delta > 0 ? 0.1 : -0.1), true);
        e.Handled = true;
    }

    // PMv2 fires this when the window is dragged onto a differently-scaled monitor. In AUTO mode we
    // recompute the effective scale from the NEW monitor's DPI (neu.DpiScaleX = DPI/96) so the physical
    // size stays constant -- SILENTLY (an automatic recompute must not flash the % toast). Manual mode
    // is untouched: WPF has already relaid out at the new DPI and the fixed number still holds. Never
    // throw from here.
    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        try { base.OnDpiChanged(oldDpi, newDpi); } catch (Exception) { }
        try
        {
            if (_uiAuto)
            {
                double ms = (newDpi.DpiScaleX > 0.01) ? newDpi.DpiScaleX : CurrentMonitorScale();
                _uiScale = EffectiveAutoScale(ms);
                PushScaleToTransform();   // silent: no toast on automatic DPI-change recompute
            }
        }
        catch (Exception) { }
    }

    // First run / startup apply. AUTO is the default for NEW users: seed _scaleTarget from the PRIMARY
    // monitor's scale (the "comfortable size" the user is used to) and compute the per-monitor effective
    // scale now. If a scale was already persisted (manual OR auto, from either app) honor it and just
    // reflect it on the live transform. Guarded by _uiScaleLoaded so a chosen scale is never overridden.
    // Windows API used to break out of an inherited-hidden startup state (see ForceVisibleOnce).
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool RedrawWindow(IntPtr hWnd, IntPtr lprc, IntPtr hrgn, uint flags);

    // The daily launcher chain is fully windowless: a desktop shortcut runs
    // scripts\start_all_hidden.vbs, which runs start_all.ps1 with -WindowStyle Hidden
    // (SW_HIDE), which in turn starts this app. A process started from an SW_HIDE parent
    // INHERITS that "hidden" show-state through STARTUPINFO.wShowWindow, so WPF creates
    // the HWND without a proper first paint: DWM never gets a composed surface and the
    // window renders as black / stale rectangles on screen -- even though the visual tree
    // itself is fine (PrintWindow of the same HWND returns the correct content).
    // Forcing SW_SHOW + a full redraw once, right after the HWND exists, discards the
    // inherited state and makes the window paint normally. Cheap, idempotent, and a no-op
    // when the app was launched normally (already visible).
    void ForceVisibleOnce()
    {
        try
        {
            IntPtr h = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            if (h == IntPtr.Zero) return;
            const int SW_SHOW = 5;
            const uint RDW_INVALIDATE = 0x0001, RDW_ERASE = 0x0004,
                       RDW_ALLCHILDREN = 0x0080, RDW_UPDATENOW = 0x0100;
            ShowWindow(h, SW_SHOW);
            RedrawWindow(h, IntPtr.Zero, IntPtr.Zero,
                         RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW);
        }
        catch { }
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        ForceVisibleOnce();
        try
        {
            double monitorScale = CurrentMonitorScale();
            // Seed the target from the primary monitor's scale on first run (default 1.5 fallback).
            if (!_scaleTargetLoaded)
            {
                _scaleTarget = Math.Max(0.8, Math.Min(3.0, monitorScale));
                if (_scaleTarget < 0.81) _scaleTarget = 1.5;   // 100% primary -> still target a comfy 1.5
                SaveKey("ui_scale_target", _scaleTarget.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture));
                _scaleTargetLoaded = true;
            }
            if (!_uiScaleLoaded)
            {
                // NEW user default = AUTO. Persist the marker and compute for this monitor (silent).
                EnableAutoScale(false);
            }
            else if (_uiAuto)
            {
                ApplyAutoScale(false);   // reflect auto for THIS monitor silently
            }
            else
            {
                double m11 = 1.0;
                var src = System.Windows.PresentationSource.FromVisual(this);
                if (src != null && src.CompositionTarget != null) m11 = src.CompositionTarget.TransformToDevice.M11;
                double pxWidth = SystemParameters.PrimaryScreenWidth * m11;   // DIPs -> physical pixels
                if (pxWidth >= 2560 && Math.Abs(m11 - 1.0) < 0.001)
                    ApplyUiScale(1.25, true, true);    // announce the one-time 4K bump
                else
                    ApplyUiScale(1.0, true, false);    // silent explicit default so it never re-triggers
            }
        }
        catch (Exception) { }
    }

    // ══════════════════════════════════════════════════════════════════════════════════
    //  P0 HEALTH STRIP  —  infra state, glanceable + one-click fixable
    // ══════════════════════════════════════════════════════════════════════════════════
    // Prefer the status file supplied to this process: it lets an isolated/preview cockpit build
    // monitor and repair the selected workspace instead of accidentally acting on the directory
    // that happens to contain the exe. Normal installs still resolve exactly as before because
    // status.json lives in <repo>\.fleet. Fall back to exe\.. for custom/non-.fleet paths.
    string RepoRoot()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(Path.GetFullPath(_statusPath));
            if (string.Equals(Path.GetFileName(stateDir), ".fleet", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(Path.Combine(stateDir, ".."));
        }
        catch (Exception) { }
        return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
    }

    // Build the health strip: 5 dots with labels + an INLINE Fix pill + an inline note, all on ONE
    // horizontal row. Redesign: left-aligned & vertically centered (it sits at the left edge of the
    // header where the title used to be). The Fix pill appears immediately to the RIGHT of the 5th
    // dot (エージェント) only when something is red/yellow; the note trails inline after it. No fixed
    // width and no second (stacked) row — the appearing/disappearing Fix affordance can only push the
    // note (both are the row's trailing items) and never shifts the right-side controls, which live in
    // a SEPARATE grid column (col 1).
    UIElement BuildHealthStrip()
    {
        _healthDot = new Border[HEALTH_DOT_COUNT];
        _healthSpin = new TextBlock[HEALTH_DOT_COUNT];
        _healthLbl = new TextBlock[HEALTH_DOT_COUNT];
        _healthDotWrap = new Border[HEALTH_DOT_COUNT];

        _healthStrip = new Border();
        _healthStrip.HorizontalAlignment = HorizontalAlignment.Left;   // flush to the header's left edge
        _healthStrip.VerticalAlignment = VerticalAlignment.Center;
        _healthStrip.Margin = new Thickness(0, 0, 12, 0);

        // Single horizontal row: [dot label] x6, then the inline Fix pill, then the inline note.
        var row = new StackPanel { Orientation = Orientation.Horizontal,
                                   HorizontalAlignment = HorizontalAlignment.Left,
                                   VerticalAlignment = VerticalAlignment.Center };
        string[] keys = { "hs_server", "hs_tunnel", "hs_edge", "hs_signin", "hs_agent", "hs_tool" };
        for (int i = 0; i < HEALTH_DOT_COUNT; i++)
        {
            var wrap = new Border();
            wrap.Margin = new Thickness(i == 0 ? 0 : 8, 0, 0, 0);
            wrap.Padding = new Thickness(0);
            wrap.VerticalAlignment = VerticalAlignment.Center;
            var dr = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var mark = new Grid { Width = 10, Height = 10, VerticalAlignment = VerticalAlignment.Center,
                                  Margin = new Thickness(0, 0, 4, 0) };
            var dot = new Border { Width = 8, Height = 8, CornerRadius = new CornerRadius(4),
                                   HorizontalAlignment = HorizontalAlignment.Center,
                                   VerticalAlignment = VerticalAlignment.Center };
            var spin = BuildSpinner(10);
            spin.Visibility = Visibility.Collapsed;
            mark.Children.Add(dot);
            mark.Children.Add(spin);
            var lbl = new TextBlock { FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Text = T(keys[i]) };
            dr.Children.Add(mark);
            dr.Children.Add(lbl);
            wrap.Child = dr;
            _healthDot[i] = dot;
            _healthSpin[i] = spin;
            _healthLbl[i] = lbl;
            _healthDotWrap[i] = wrap;
            row.Children.Add(wrap);
        }

        // INLINE Fix pill (hidden unless red/yellow): refined warning-outlined pill (transparent fill,
        // Warning outline + text) with a small Material Symbol + the label. Sits right after the 5th dot.
        _fixBtn = new Button();
        _fixBtn.Content = BuildFixPillContent(false);
        _fixBtn.FontSize = 11; _fixBtn.FontWeight = FontWeights.SemiBold;
        _fixBtn.Padding = new Thickness(8, 1, 9, 1);
        _fixBtn.Margin = new Thickness(10, 0, 0, 0);      // gap after エージェント dot
        _fixBtn.Cursor = Cursors.Hand;
        _fixBtn.BorderThickness = new Thickness(1);
        _fixBtn.Template = FlatButtonTemplate();          // rounded (CornerRadius 4) pill chrome
        _fixBtn.VerticalAlignment = VerticalAlignment.Center;
        _fixBtn.Visibility = Visibility.Collapsed;
        _fixBtn.ToolTip = T("hs_fix_hint");
        System.Windows.Automation.AutomationProperties.SetName(_fixBtn, T("hs_fix"));
        _fixBtn.Click += delegate { RunFix(); };
        row.Children.Add(_fixBtn);

        _fixNote = new TextBlock { FontSize = 11, VerticalAlignment = VerticalAlignment.Center,
                                   Margin = new Thickness(8, 0, 0, 0), Text = "",
                                   TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 250 };
        row.Children.Add(_fixNote);

        _healthStrip.Child = row;
        PaintHealthChrome();
        ApplyHealthToUi();     // paint current cached states (Gray until first poll completes)
        return _healthStrip;
    }

    // The inline Fix pill's content: a small Material Symbol (settings/cog — the closest repair glyph
    // in the subset) at 14px + the localized "Fix" label, tinted with the Warning token to match the
    // pill's outline. Rebuilt on theme/lang flips via RebuildChrome (whole chrome is reconstructed).
    TextBlock BuildSpinner(double size)
    {
        var spin = new TextBlock { Text = "⟳", FontFamily = new FontFamily("Segoe UI Symbol"),
                                   FontSize = size, FontWeight = FontWeights.SemiBold,
                                   HorizontalAlignment = HorizontalAlignment.Center,
                                   VerticalAlignment = VerticalAlignment.Center,
                                   TextAlignment = TextAlignment.Center,
                                   RenderTransformOrigin = new Point(0.5, 0.5),
                                   Foreground = Theme.Br(Theme.Warning(_dark)) };
        var rotate = new RotateTransform(0);
        spin.RenderTransform = rotate;
        var animation = new DoubleAnimation(0, 360, new Duration(TimeSpan.FromMilliseconds(850)));
        animation.RepeatBehavior = RepeatBehavior.Forever;
        rotate.BeginAnimation(RotateTransform.AngleProperty, animation);
        return spin;
    }

    UIElement BuildFixPillContent(bool busy)
    {
        var sp = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        UIElement ic = busy ? (UIElement)BuildSpinner(13) : MakeIcon("settings", 14, Theme.Br(Theme.Warning(_dark)));
        ((FrameworkElement)ic).Margin = new Thickness(0, 0, 4, 0);
        ((FrameworkElement)ic).VerticalAlignment = VerticalAlignment.Center;
        sp.Children.Add(ic);
        sp.Children.Add(new TextBlock { Text = T(busy ? "hs_fixing_button" : "hs_fix"), FontSize = 11, FontWeight = FontWeights.SemiBold,
                                        VerticalAlignment = VerticalAlignment.Center });
        return sp;
    }

    // Re-tint the strip chrome (labels, Fix button) for the current theme. Called from PaintChrome.
    void PaintHealthChrome()
    {
        if (_healthLbl != null)
            for (int i = 0; i < _healthLbl.Length; i++)
                if (_healthLbl[i] != null) _healthLbl[i].Foreground = Muted;
        if (_fixBtn != null)
        {
            // Warning-outline (needs-attention), NOT the reserved accent fill.
            _fixBtn.Background = Brushes.Transparent;
            _fixBtn.Foreground = Theme.Br(Theme.Warning(_dark));
            _fixBtn.BorderBrush = Theme.Br(Theme.Warning(_dark));
            _fixBtn.Content = BuildFixPillContent(_fixRunning);
        }
        if (_healthSpin != null)
            for (int i = 0; i < _healthSpin.Length; i++)
                if (_healthSpin[i] != null) _healthSpin[i].Foreground = Theme.Br(Theme.Warning(_dark));
        if (_fixNote != null) _fixNote.Foreground = Muted;
    }

    // Map a HealthState to its Theme dot color for the current mode.
    Brush HealthBrush(HealthState s)
    {
        if (s == HealthState.Green) return Theme.Br(Theme.Success(_dark));
        if (s == HealthState.Red) return Theme.Br(Theme.Danger(_dark));
        if (s == HealthState.Yellow) return Theme.Br(Theme.Warning(_dark));
        if (s == HealthState.Checking) return Theme.Br(Theme.Warning(_dark));
        return Theme.Br(Theme.Muted(_dark));   // gray / unknown
    }

    // Apply the cached _health snapshot onto the dots + tooltips + Fix button visibility.
    // MUST run on the UI thread (called from BuildHealthStrip and from the Dispatcher marshal).
    void ApplyHealthToUi()
    {
        if (_healthDot == null) return;
        bool anyBad = false;
        DotState[] snap = new DotState[HEALTH_DOT_COUNT];
        lock (_healthLock)
            for (int i = 0; i < HEALTH_DOT_COUNT; i++)
                snap[i] = new DotState { State = _health[i].State, Detail = _health[i].Detail, Checked = _health[i].Checked };
        for (int i = 0; i < HEALTH_DOT_COUNT; i++)
        {
            bool checking = snap[i].State == HealthState.Checking || (_fixRunning && (_fixTargetMask & (1 << i)) != 0);
            if (_healthDot[i] != null)
            {
                _healthDot[i].Background = HealthBrush(snap[i].State);
                _healthDot[i].Visibility = checking ? Visibility.Collapsed : Visibility.Visible;
            }
            if (_healthSpin != null && _healthSpin[i] != null)
                _healthSpin[i].Visibility = checking ? Visibility.Visible : Visibility.Collapsed;
            if (snap[i].State == HealthState.Red || snap[i].State == HealthState.Yellow) anyBad = true;
            if (_healthDotWrap[i] != null)
            {
                string when = snap[i].Checked == DateTime.MinValue
                    ? T("hs_never")
                    : snap[i].Checked.ToLocalTime().ToString("HH:mm:ss");
                string detail = string.IsNullOrEmpty(snap[i].Detail) ? T("hs_checking") : snap[i].Detail;
                _healthDotWrap[i].ToolTip = T(_healthKeys[i]) + ": " + detail + "\n" + T("hs_lastcheck") + when;
            }
        }
        if (_fixBtn != null)
        {
            // Never hide the button mid-fix (it is disabled while running so it can't re-enter).
            _fixBtn.Visibility = (anyBad || _fixRunning) ? Visibility.Visible : Visibility.Collapsed;
            _fixBtn.IsEnabled = !_fixRunning;
            _fixBtn.Content = BuildFixPillContent(_fixRunning);
        }
        // Clear the stale hint text once everything the strip knows about is healthy again (not
        // mid-fix): RunFix's note() writes _fixNote.Text once and nothing else used to clear it,
        // so "run start_all.bat"-style residue could persist forever after the stack recovered.
        // This runs on the UI thread already (ApplyHealthToUi's documented contract), so no
        // Dispatcher marshal is needed here (mirrors the rest of this method).
        if (!anyBad && !_fixRunning && _fixNote != null && _fixNote.Text.Length > 0)
            _fixNote.Text = "";
    }
    static readonly string[] _healthKeys = { "hs_server", "hs_tunnel", "hs_edge", "hs_signin", "hs_agent", "hs_tool" };

    // Start (once) the background poll thread. Re-entrant-safe: only spawns if not already alive.
    // BuildChrome (and RebuildChrome) call this; a language flip rebuilds chrome but the thread keeps
    // running, so we don't restart it — we just refresh the UI from the still-updating cache.
    void StartHealthPoll()
    {
        _agentMarkerId = ExtractAgentMarker();
        if (_healthThread != null && _healthThread.IsAlive) { ApplyHealthToUi(); return; }
        _healthStop = false;
        _healthThread = new Thread(new ThreadStart(HealthLoop));
        _healthThread.IsBackground = true;   // dies with the app; never blocks shutdown
        _healthThread.Start();
    }

    // Background poll loop. ~15s cadence; each probe uses a short (3-4s) timeout so a dead
    // endpoint can't stall the sweep. NEVER touches WPF objects directly — it writes the cache
    // and marshals ApplyHealthToUi onto the Dispatcher.
    void HealthLoop()
    {
        while (!_healthStop)
        {
            try { PollHealthOnce(); } catch (Exception) { }

            // Startup auto-heal: once per app run, right after the FIRST sweep completes, check
            // whether the stack needs bringing up and do it ourselves -- this is what makes
            // launching FleetCockpit.exe directly (not via the desktop icon) self-healing too.
            // Guarded by _startupHealCheckDone (runs once) and RunStartAll's own 120s cooldown.
            if (!_startupHealCheckDone)
            {
                _startupHealCheckDone = true;
                HealthState srv0, tun0;
                lock (_healthLock) { srv0 = _health[0].State; tun0 = _health[1].State; }
                System.Diagnostics.Debug.WriteLine("[FleetCockpit] HealthLoop: startup auto-heal check server=" + srv0 + " tunnel=" + tun0);
                if (srv0 == HealthState.Red || tun0 == HealthState.Red)
                {
                    try
                    {
                        if (!Dispatcher.HasShutdownStarted)
                            Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = T("hs_fix_stack"); }));
                    }
                    catch (Exception) { }
                    RunStartAll();
                }
            }

            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate { try { ApplyHealthToUi(); } catch (Exception) { } }));
            }
            catch (Exception) { }
            // Normally poll every 15s, but wake immediately after a repair action changes state.
            // This removes the old dead interval where the user clicked Fix, the command had
            // already completed, yet the strip kept showing the pre-fix red snapshot.
            if (!_healthStop) _healthWake.WaitOne(15000);
        }
    }

    // One full infra sweep. Writes results into _health under _healthLock.
    void PollHealthOnce()
    {
        DateTime now = DateTime.UtcNow;

        // 0) Server: GET http://127.0.0.1:8000/health == 200
        bool srvOk = HttpOk("http://127.0.0.1:8000/health", 3500);
        SetDot(0, srvOk ? HealthState.Green : HealthState.Red,
               T(srvOk ? "hs_srv_detail_ok" : "hs_srv_detail_bad"), now);

        // 1) Tunnel: read MCP_TUNNEL_URL from ..\.env; GET <url>/health == 200. Gray if none.
        string tunnel = EnvValue("MCP_TUNNEL_URL");
        if (string.IsNullOrEmpty(tunnel))
            SetDot(1, HealthState.Gray, T("hs_tun_detail_none"), now);
        else
        {
            // MCP_TUNNEL_URL points at the /mcp path (e.g. https://host.devtunnels.ms/mcp);
            // /health is a SIBLING route at the tunnel origin, not nested under /mcp -- so
            // naively appending "/health" produced .../mcp/health, a 404 that always red'd
            // this dot even when the tunnel was serving correctly. Use the origin instead.
            string origin = tunnel;
            try { Uri u = new Uri(tunnel); origin = u.GetLeftPart(UriPartial.Authority); } catch (Exception) { }
            string turl = origin + "/health";
            // 6s, not the local 4s budget: this is a remote round-trip (devtunnels region)
            // that on a corporate machine also traverses the system proxy -- 4s false-reds it.
            bool tunOk = HttpOk(turl, 6000);
            SetDot(1, tunOk ? HealthState.Green : HealthState.Red,
                   T(tunOk ? "hs_tun_detail_ok" : "hs_tun_detail_bad"), now);
        }

        // 2) Edge: GET http://127.0.0.1:9222/json/version succeeds
        string edgeVersion = HttpGetBody("http://127.0.0.1:9222/json/version", 3500);
        bool edgeOk = edgeVersion != null;
        SetDot(2, edgeOk ? HealthState.Green : HealthState.Red,
               T(edgeOk ? "hs_edge_detail_ok" : "hs_edge_detail_bad"), now);

        // 5) Tool: independent of the fleet Edge (:9222) probed above -- this reads the BRIDGE's
        //    own idle self-probe result (.fleet/tool_probe.json). Runs unconditionally (not gated
        //    on edgeOk) because it reflects a completely separate Edge profile/CDP port (:9223).
        PollToolProbeOnce(now);

        // 3+4) Sign-in + Agent both derive from the tab list (:9222/json). If Edge is down,
        //      sign-in is unknown (gray) and agent is gray.
        if (!edgeOk)
        {
            SetDot(3, HealthState.Gray, T("hs_edge_detail_bad"), now);
            SetDot(4, HealthState.Gray, T("hs_agent_gray"), now);
            return;
        }
        string tabsJson = HttpGetBody("http://127.0.0.1:9222/json", 3500);
        List<string> urls = ExtractTabUrls(tabsJson);

        // 3) Sign-in: a stale login tab must not override a live M365 chat tab. Edge can retain
        //    an old login.microsoftonline.com page after authentication while the custom-agent
        //    conversation is already working in another tab. Red only when a login wall exists
        //    AND there is no usable signed-in M365 chat tab.
        bool onLoginWall = false;
        bool hasUsableM365Chat = false;
        foreach (string u in urls) if (LooksLikeLoginWall(u)) { onLoginWall = true; break; }
        foreach (string u in urls)
            if (LooksLikeUsableM365Chat(u)) { hasUsableM365Chat = true; break; }
        bool needsSignin = onLoginWall && !hasUsableM365Chat;
        SetDot(3, needsSignin ? HealthState.Red : HealthState.Green,
               T(needsSignin ? "hs_signin_bad" : "hs_signin_ok"), now);

        // 4) Agent: the tab URL is NOT a reliable signal -- the M365 SPA keeps the loaded
        //    custom agent while the URL normalizes to '/chat/?redirfrom=CsrToSSR' (verified:
        //    a working agent that returned real tool results showed exactly that URL). So we
        //    judge from the GROUND TRUTH instead: the newest transcript's last assistant turn.
        //    The custom agent prefixes its replies with its display name; a default-Copilot
        //    fallback returns the canned non-answer. While a run is live, a usable M365 chat tab
        //    is enough to show GREEN immediately (including before the first assistant reply),
        //    YELLOW is the canned default-Copilot fallback, and RED means the run is active but
        //    no usable chat tab exists. GRAY is reserved for idle / not currently running.
        if (!RunIsLive())
            SetDot(4, HealthState.Gray, T("hs_agent_gray"), now);
        else if (!hasUsableM365Chat)
            SetDot(4, HealthState.Red, T("hs_agent_bad"), now);
        else
        {
            string lastAssistant = NewestAssistantText();
            if (lastAssistant == null)
                SetDot(4, HealthState.Green, T("hs_agent_ok"), now);          // run live, first reply pending
            else if (LooksLikeCannedNonAnswer(lastAssistant))
                SetDot(4, HealthState.Yellow, T("hs_agent_warn"), now);        // default-Copilot fallback
            else
                SetDot(4, HealthState.Green, T("hs_agent_ok"), now);           // agent answering
        }
    }

    static readonly string[] _cannedNonAnswer = {
        "それに応答できませんでした", "I couldn't respond to that", "I can't respond to that",
    };
    static bool LooksLikeCannedNonAnswer(string s)
    {
        if (string.IsNullOrEmpty(s)) return false;
        foreach (string m in _cannedNonAnswer) if (s.IndexOf(m, StringComparison.OrdinalIgnoreCase) >= 0) return true;
        return false;
    }

    // 5) Tool: read the bridge's self-probe result, written independently by another component
    // (bridge/copilot_bridge.py + tools/tool_probe.py) roughly every ~10 min. This file only READS
    // it -- never writes .fleet/tool_probe.json. Shape: {"ts":<epoch float>,"ok":<bool>,
    // "kind":"answer"|"consent_card"|"canned_fallback"|"timeout"|"agent_unreachable"|"error",
    // "detail":<str>}.
    //   GRAY  -- file has never existed (probe disabled / MCP_TOOL_PROBE_SEC=0 on this machine).
    //            Deliberately NOT red: a new/unconfigured feature must never read as an outage.
    //   RED   -- file missing after having looked (can't happen here since we check Exists first,
    //            kept as a safety fallback) OR stale (>20 min since ts -- the probe itself isn't
    //            running) OR the probe failed with nothing coming back at all.
    //   GREEN -- ok==true AND fresh (<20 min old).
    //   YELLOW-- consent_card/canned_fallback, or the probe failed while "alive" says a reply
    //            DID arrive. Red is reserved for silence: a failed probe on a chat that is
    //            answering normally used to paint this dot red, and a red dot is read as
    //            "everything is broken" by every user who sees it.
    void PollToolProbeOnce(DateTime now)
    {
        string path = Path.Combine(RepoRoot(), ".fleet", "tool_probe.json");
        if (!File.Exists(path)) { SetDot(5, HealthState.Gray, T("hs_tool_detail_none"), now); return; }
        try
        {
            var o = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            double ts = (o != null && o.ContainsKey("ts")) ? Convert.ToDouble(o["ts"]) : 0.0;
            bool ok = (o != null && o.ContainsKey("ok")) && Convert.ToBoolean(o["ok"]);
            string kind = (o != null && o.ContainsKey("kind")) ? Convert.ToString(o["kind"]) : "";
            double ageMin = ts > 0 ? (NowUnix() - ts) / 60.0 : double.MaxValue;
            string ageTxt = ts > 0 ? AgeMinutesText(ageMin) : T("hs_never");
            if (ageMin >= 20.0)
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_stale"), now);
            else if (kind == "checking" || kind == "starting")
                SetDot(5, HealthState.Checking, ageTxt + " " + T("hs_tool_detail_checking"), now);
            else if (ok)
                SetDot(5, HealthState.Green, ageTxt + " " + T("hs_tool_detail_ok"), now);
            else if (kind == "consent_card" || kind == "canned_fallback")
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_consent"), now);
            else if (o != null && o.ContainsKey("alive") && Convert.ToBoolean(o["alive"]))
                // The chat answered, it just did not complete the probe's round trip. Calling
                // that "no response, reconnect needed" in red told everyone the stack was down
                // while it was serving turns perfectly well -- the one reading a user acts on.
                SetDot(5, HealthState.Yellow, ageTxt + " " + T("hs_tool_detail_no_tool"), now);
            else   // timeout | agent_unreachable | genuinely silent | unrecognized kind
                SetDot(5, HealthState.Red, ageTxt + " " + T("hs_tool_detail_down"), now);
        }
        catch (Exception)
        {
            // Malformed/partial JSON (e.g. read mid-write by the bridge) -- treat as down, not gray,
            // since the file DOES exist (the feature is active, just unreadable right now).
            SetDot(5, HealthState.Red, T("hs_tool_detail_down"), now);
        }
    }

    // "N分前" / "N min ago" -- small formatter local to the Tool dot's tooltip; not routed through
    // T() because it embeds a number (T() keys are static lookups, no interpolation).
    string AgeMinutesText(double ageMin)
    {
        int m = (int)Math.Max(0, Math.Round(ageMin));
        return (_lang == 0) ? (m + "分前") : (m + " min ago");
    }

    // Read the newest .fleet\transcripts\*.jsonl and return the text of its last assistant turn,
    // or null if none. Fully guarded, cheap (reads one file, scans lines). Runs on the poll thread.
    string NewestAssistantText()
    {
        try
        {
            string dir = Path.GetFullPath(Path.Combine(
                AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet", "transcripts"));
            if (!Directory.Exists(dir)) return null;
            string newest = null; DateTime best = DateTime.MinValue;
            foreach (string f in Directory.GetFiles(dir, "*.jsonl"))
            {
                DateTime wt = File.GetLastWriteTimeUtc(f);
                if (wt > best) { best = wt; newest = f; }
            }
            if (newest == null) return null;
            string last = null;
            foreach (string line in File.ReadLines(newest))
            {
                if (line.IndexOf("\"role\"", StringComparison.Ordinal) < 0) continue;
                if (line.IndexOf("\"assistant\"", StringComparison.Ordinal) < 0) continue;
                last = line;   // keep the last assistant line
            }
            if (last == null) return null;
            // extract the "text" field value (simple, tolerant): find "text":" ... unescaped close
            int ti = last.IndexOf("\"text\"", StringComparison.Ordinal);
            if (ti < 0) return "";
            int c = last.IndexOf(':', ti); if (c < 0) return "";
            int q = last.IndexOf('"', c + 1); if (q < 0) return "";
            var sb = new StringBuilder();
            for (int i = q + 1; i < last.Length; i++)
            {
                char ch = last[i];
                if (ch == '\\' && i + 1 < last.Length) { i++; char n = last[i]; sb.Append(n == 'n' ? '\n' : n); continue; }
                if (ch == '"') break;
                sb.Append(ch);
            }
            return sb.ToString();
        }
        catch (Exception) { return null; }
    }

    void SetDot(int i, HealthState s, string detail, DateTime whenUtc)
    {
        lock (_healthLock) { _health[i].State = s; _health[i].Detail = detail; _health[i].Checked = whenUtc; }
    }

    // Extract the T_.../P_... agent id from the configured agent URL in .env. The URL may be
    // '.../chat/?titleId=T_xxx' (deep link) OR '.../chat/agent/T_xxx' — both carry the same id.
    // Matched later (case-insensitively) inside any tab url, including /chat/agent/<id> conversation
    // forms. Returns "" if no agent URL is configured.
    string ExtractAgentMarker()
    {
        string url = EnvValue("MCP_FLEET_AGENT_URL");
        if (string.IsNullOrEmpty(url)) url = EnvValue("MCP_IMPL_AGENT_URL");
        return AgentIdFromUrl(url);
    }
    static string AgentIdFromUrl(string url)
    {
        if (string.IsNullOrEmpty(url) || url == null) return "";
        // titleId=<id>
        int ti = url.IndexOf("titleId=", StringComparison.OrdinalIgnoreCase);
        if (ti >= 0)
        {
            string tail = url.Substring(ti + 8);
            return TrimId(tail);
        }
        // /agent/<id>
        int ai = url.IndexOf("/agent/", StringComparison.OrdinalIgnoreCase);
        if (ai >= 0)
        {
            string tail = url.Substring(ai + 7);
            return TrimId(tail);
        }
        return "";
    }
    // Cut an id token at the first url separator (&, ?, /, #, whitespace). Keeps '.', '-', '_'
    // (agent ids like 'P_552e6eda-...-....dr_work' and 'T_02140b8c-f551-...' contain those).
    static string TrimId(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder();
        foreach (char c in s)
        {
            if (c == '&' || c == '?' || c == '/' || c == '#' || char.IsWhiteSpace(c)) break;
            sb.Append(c);
        }
        return sb.ToString();
    }

    // login-wall regex from doctor.ps1 (just-fixed): login.microsoftonline / login.live.com /
    // /adfs/ / adfs. / /oauth2/authorize / /signin / login_hint= . Implemented as substring checks.
    static bool LooksLikeLoginWall(string url)
    {
        if (string.IsNullOrEmpty(url)) return false;
        string u = url.ToLowerInvariant();
        return u.Contains("login.microsoftonline") || u.Contains("login.live.com")
            || u.Contains("/adfs/") || u.Contains("adfs.")
            || u.Contains("/oauth2/authorize") || u.Contains("/signin")
            || u.Contains("login_hint=");
    }

    static bool LooksLikeUsableM365Chat(string url)
    {
        if (string.IsNullOrEmpty(url) || LooksLikeLoginWall(url)) return false;
        string u = url.ToLowerInvariant();
        return (u.StartsWith("https://m365.cloud.microsoft/")
                || u.StartsWith("https://www.microsoft365.com/"))
            && u.Contains("/chat");
    }

    // Pull every "url":"..." value out of the raw :9222/json tab-list body. Uses the shared
    // JavaScriptSerializer when the body parses as an array; falls back to a cheap scan otherwise.
    List<string> ExtractTabUrls(string json)
    {
        var urls = new List<string>();
        if (string.IsNullOrEmpty(json)) return urls;
        try
        {
            object parsed = _js.DeserializeObject(json);
            if (parsed is object[])
            {
                foreach (object o in (object[])parsed)
                {
                    var d = o as Dictionary<string, object>;
                    if (d != null && d.ContainsKey("url") && d["url"] != null) urls.Add(d["url"].ToString());
                }
                return urls;
            }
        }
        catch (Exception) { }
        // Fallback: substring scan for "url":"..."
        int idx = 0;
        while (true)
        {
            int k = json.IndexOf("\"url\"", idx, StringComparison.OrdinalIgnoreCase);
            if (k < 0) break;
            int c = json.IndexOf(':', k); if (c < 0) break;
            int q1 = json.IndexOf('"', c + 1); if (q1 < 0) break;
            int q2 = json.IndexOf('"', q1 + 1); if (q2 < 0) break;
            urls.Add(json.Substring(q1 + 1, q2 - q1 - 1));
            idx = q2 + 1;
        }
        return urls;
    }

    // ── .env reader (utf-8, tolerate BOM) ───────────────────────────────────────────
    // The .env lives at the REPO ROOT (one level up from ...\ui), same file doctor.ps1 reads.
    // Cached by mtime so a poll every 15s doesn't re-read from disk each time.
    Dictionary<string, string> _envCache;
    long _envMtime = -1;
    string EnvValue(string key)
    {
        try
        {
            string envPath = Path.Combine(RepoRoot(), ".env");
            if (!File.Exists(envPath)) { _envCache = null; return ""; }
            long m = File.GetLastWriteTimeUtc(envPath).Ticks;
            if (_envCache == null || m != _envMtime)
            {
                var map = new Dictionary<string, string>();
                // UTF8 with BOM tolerated (new UTF8Encoding detects+strips a leading BOM).
                foreach (string raw in File.ReadAllLines(envPath, new UTF8Encoding(false)))
                {
                    string ln = raw;
                    if (ln.Length > 0 && ln[0] == '﻿') ln = ln.Substring(1);   // stray BOM guard
                    ln = ln.Trim();
                    if (ln.Length == 0 || ln[0] == '#') continue;
                    int eq = ln.IndexOf('=');
                    if (eq <= 0) continue;
                    string k = ln.Substring(0, eq).Trim();
                    string v = ln.Substring(eq + 1).Trim();
                    if (k.Length > 0) map[k] = v;
                }
                _envCache = map;
                _envMtime = m;
            }
            string val;
            if (_envCache != null && _envCache.TryGetValue(key, out val)) return val;
        }
        catch (Exception) { }
        return "";
    }

    // Enable TLS 1.2 process-wide: the tunnel is HTTPS and .NET Framework's default
    // protocol set can omit TLS 1.2, so the devtunnels handshake fails (false-red tunnel
    // dot) while plain-HTTP localhost is fine. Called once from the HTTP helpers.
    static bool _tlsReady = false;
    static void EnsureTls()
    {
        if (_tlsReady) return;
        try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; } catch (Exception) { }
        _tlsReady = true;
    }

    // GET a URL; true iff it returns HTTP 200. Short timeout, fully guarded.
    // Loopback URLs bypass the system proxy: on corporate machines the PAC/proxy can
    // swallow 127.0.0.1 requests, turning a healthy local server into a false-red dot.
    static bool HttpOk(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = true;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            else if (req.Proxy != null) req.Proxy.Credentials = CredentialCache.DefaultCredentials;
            using (var resp = (HttpWebResponse)req.GetResponse())
                return resp.StatusCode == HttpStatusCode.OK;
        }
        catch (Exception) { return false; }
    }

    // GET a URL; return the body string, or null on any failure (unreachable / non-200 / timeout).
    static string HttpGetBody(string url, int timeoutMs)
    {
        try
        {
            EnsureTls();
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            if (url.Contains("127.0.0.1") || url.Contains("localhost")) req.Proxy = null;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode != HttpStatusCode.OK) return null;
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                    return sr.ReadToEnd();
            }
        }
        catch (Exception) { return null; }
    }

    // ── Fix button: run the remedy for the WORST current problem ─────────────────────
    // Priority (worst first): sign-in RED -> Edge RED -> agent RED/YELLOW -> server RED.
    // Each remedy is a short-lived, windowless, async shell; never blocks the UI; guarded.
    // Never two at once (button disabled while running).
    void RunFix()
    {
        if (_fixRunning) return;
        // Decide the worst problem from the current cache (UI thread).
        HealthState signin, edge, agent, server, tunnel, tool;
        lock (_healthLock)
        {
            server = _health[0].State; tunnel = _health[1].State; edge = _health[2].State;
            signin = _health[3].State; agent = _health[4].State; tool = _health[5].State;
        }
        if (signin == HealthState.Red) _fixTargetMask = 1 << 3;
        else if (edge == HealthState.Red) _fixTargetMask = 1 << 2;
        else if (agent == HealthState.Yellow || agent == HealthState.Red) _fixTargetMask = 1 << 4;
        else if (server == HealthState.Red || tunnel == HealthState.Red)
            _fixTargetMask = ((server == HealthState.Red) ? (1 << 0) : 0)
                           | ((tunnel == HealthState.Red) ? (1 << 1) : 0);
        else if (tool == HealthState.Red || tool == HealthState.Yellow) _fixTargetMask = 1 << 5;
        else _fixTargetMask = 0;
        _fixRunning = true;
        ApplyHealthToUi();   // disable + keep the button visible
        if (_fixNote != null) _fixNote.Text = T("hs_fixing");

        string repo = RepoRoot();
        Action<string> note = delegate (string s)
        {
            try { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = s; })); }
            catch (Exception) { }
        };
        Action done = delegate
        {
            _fixRunning = false;
            _fixTargetMask = 0;
            try { _healthWake.Set(); } catch (Exception) { }
            try { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(new Action(delegate { ApplyHealthToUi(); })); } catch (Exception) { }
        };

        // Priority 1: Sign-in RED -> relaunch companion Edge HEADED for the user to sign in.
        if (signin == HealthState.Red)
        {
            note(T("hs_fix_signin"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    RunPowershellScript(Path.Combine(repo, "scripts", "start_companion_edge.ps1"),
                                        "-Foreground -Port 9222");
                    note(T("hs_fix_signin_toast"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 2: Edge RED -> hard-reset relaunch (preserves remembered headless/headed mode).
        if (edge == HealthState.Red)
        {
            note(T("hs_fix_edge"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    RunPowershellScript(Path.Combine(repo, "scripts", "start_companion_edge.ps1"),
                                        "-HardReset -Port 9222");
                    note(T("hs_fix_done"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 3: Agent RED/YELLOW (missing chat / default-Copilot fallback) -> edge_reconnect.
        if (agent == HealthState.Yellow || agent == HealthState.Red)
        {
            note(T("hs_fix_agent"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    int code = RunReconnect(repo);
                    note(code == 0 ? T("hs_fix_agent_ok") : T("hs_fix_agent_fail"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 4: Server RED or Tunnel RED -> route through the situation-aware repair
        // dispatcher (scripts\repair.ps1 -Auto -ResultJson) instead of blindly launching the
        // stack bring-up. doctor.ps1's layered Dev Tunnel diagnosis (and repair.ps1's tiers
        // built on it) know the difference between "server/tunnel just need starting" (Tier A,
        // fixed by start_all -- repair.ps1 runs this itself under -Auto) and "the devtunnel CLI
        // login expired" (Tier C, human-only -- start_all can NEVER fix this, so the old blind
        // RunStartAll() here used to do nothing useful and leave the user stuck). Run off the
        // UI thread like the other async tiers above so a slow repair pass cannot freeze the UI.
        if (server == HealthState.Red || tunnel == HealthState.Red)
        {
            note(T("hs_fix_stack"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    string repairPs1 = Path.Combine(repo, "scripts", "repair.ps1");
                    RepairResult rr = File.Exists(repairPs1) ? ParseRepairResult(RunRepairDispatcher(repairPs1)) : null;
                    if (rr == null)
                    {
                        // repair.ps1 missing, failed to run, or its output could not be parsed --
                        // never regress: fall back to the previous blind stack bring-up.
                        RunStartAll();
                        note(T("hs_fix_stack"));
                    }
                    else if (rr.HumanSteps.Count > 0)
                    {
                        // The key win: tell the user the exact manual step (e.g. devtunnel login)
                        // instead of silently re-running start_all, which cannot fix a Tier C cause.
                        note(T("hs_fix_manual_needed") + ":  " + string.Join("   /   ", rr.HumanSteps.ToArray()));
                    }
                    else if (rr.FinalBad == 0)
                    {
                        note(T("hs_fix_done"));
                    }
                    else
                    {
                        note(T("hs_fix_stack"));
                    }
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); RunStartAll(); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Priority 5: Tool RED/YELLOW (bridge-side problem -- the interactive chat's own MCP
        // connector, NOT the fleet Edge above) -> reconnect the BRIDGE specifically, i.e. target
        // :9223 (copilot-bridge-edge) instead of the :9222 fleet Edge Priority 3 above already
        // handles. This is a SEPARATE remedy path (:9222 reconnect above is untouched/still runs
        // for its own condition) because a healthy fleet Edge tells you nothing about the bridge.
        if (tool == HealthState.Red || tool == HealthState.Yellow)
        {
            note(T("hs_fix_bridge"));
            var t = new Thread(new ThreadStart(delegate
            {
                try
                {
                    string stdoutText;
                    int code = RunReconnect(repo, "http://127.0.0.1:9223", out stdoutText);
                    if (code == 0) note(T("hs_fix_bridge_ok"));
                    else if (AgentDidNotLoad(stdoutText)) note(T("reconnect_chat_toast_dead"));
                    else note(T("hs_fix_bridge_fail"));
                }
                catch (Exception ex) { note(T("hs_fix_err") + ": " + ex.Message); }
                finally { done(); }
            })) { IsBackground = true };
            t.Start();
            return;
        }

        // Nothing actionable (all green/gray) -> clear the running flag.
        done();
    }

    // ── Manual "チャット再接続"/"Reconnect chat" button (settings panel) ──────────────────
    // ALWAYS available, independent of the Tool dot's state (which may be GRAY on a machine
    // where the bridge self-probe feature isn't active yet, or the user may just want to force
    // it). Fires the same bridge-targeted (:9223) reconnect as RunFix's Priority 5 tier, but on
    // demand. Exception-guarded throughout; every UI touch is Dispatcher-marshaled so nothing can
    // throw into the UI thread. Reuses ShowScaleToast -- the cockpit's existing lightweight toast
    // -- for the optimistic "reconnecting…" message and the outcome (mirrors the steer-ack toast
    // pattern at "steer_collapsed_ack").
    void RunBridgeReconnectManual()
    {
        if (_bridgeReconnectRunning) return;
        _bridgeReconnectRunning = true;
        if (_reconnectChatBtn != null) _reconnectChatBtn.IsEnabled = false;
        try { ShowScaleToast(T("reconnect_chat_toast_start")); } catch (Exception) { }

        string repo = RepoRoot();
        var t = new Thread(new ThreadStart(delegate
        {
            string outcome;
            try
            {
                string stdoutText;
                int code = RunReconnect(repo, "http://127.0.0.1:9223", out stdoutText);
                outcome = code == 0 ? T("reconnect_chat_toast_ok")
                        : AgentDidNotLoad(stdoutText) ? T("reconnect_chat_toast_dead")
                        : T("reconnect_chat_toast_fail");
            }
            catch (Exception) { outcome = T("reconnect_chat_toast_fail"); }
            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate
                    {
                        _bridgeReconnectRunning = false;
                        if (_reconnectChatBtn != null) _reconnectChatBtn.IsEnabled = true;
                        try { ShowScaleToast(outcome); } catch (Exception) { }
                    }));
                else
                    _bridgeReconnectRunning = false;
            }
            catch (Exception) { _bridgeReconnectRunning = false; }
        })) { IsBackground = true };
        t.Start();
    }

    // Launch a PowerShell script windowless + async; wait for exit inside the caller's worker thread.
    void RunPowershellScript(string scriptPath, string extraArgs)
    {
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = "powershell";
        psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + scriptPath + "\" " + extraArgs;
        psi.WorkingDirectory = RepoRoot();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            // Drain streams so the child can't block on a full pipe; bounded wait.
            try { p.StandardOutput.ReadToEnd(); } catch (Exception) { }
            try { p.StandardError.ReadToEnd(); } catch (Exception) { }
            try { p.WaitForExit(120000); } catch (Exception) { }
        }
    }

    // edge_reconnect.py prints this exact marker (main(), the "not res.get('agent_loaded')" branch)
    // when the deep-link fell back to default Copilot -- a heavier remedy (start_bridge.ps1
    // -HardReset) is needed, a plain reconnect can't fix it. Checked against captured stdout.
    static bool AgentDidNotLoad(string stdoutText)
    {
        return !string.IsNullOrEmpty(stdoutText)
            && stdoutText.IndexOf("AGENT DID NOT LOAD", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    // Run  <repo>\.venv\Scripts\python.exe -m relay.edge_reconnect  and capture the exit code.
    // Original (fleet Edge :9222, default inside edge_reconnect.py) call shape -- unchanged, still
    // used by RunFix's Priority 3 (Agent YELLOW) tier above.
    int RunReconnect(string repo)
    {
        string dump;
        return RunReconnect(repo, null, out dump);
    }

    // Same as above but targets a SPECIFIC CDP endpoint (--cdp-url) instead of edge_reconnect.py's
    // :9222 default, and returns the captured stdout so callers can look for edge_reconnect.py's
    // "AGENT DID NOT LOAD" marker (printed when the deep-link fell back to default Copilot --
    // the case where a lighter reconnect can't help and start_bridge.ps1 -HardReset is needed).
    // cdpUrl == null keeps edge_reconnect.py's own default (:9222); pass "http://127.0.0.1:9223"
    // to target the interactive BRIDGE profile instead.
    int RunReconnect(string repo, string cdpUrl, out string stdoutText)
    {
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.edge_reconnect" + (string.IsNullOrEmpty(cdpUrl) ? "" : " --cdp-url " + cdpUrl);
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        stdoutText = "";
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            try { stdoutText = p.StandardOutput.ReadToEnd(); } catch (Exception) { }
            try { p.StandardError.ReadToEnd(); } catch (Exception) { }
            try { if (!p.WaitForExit(600000)) return -1; } catch (Exception) { return -1; }
            try { return p.ExitCode; } catch (Exception) { return -1; }
        }
    }

    // Parsed shape of scripts\repair.ps1 -ResultJson's final JSON line:
    //   { autofixed:[{id,note}], confirmNeeded:[{id,note}], humanSteps:[{id,step}],
    //     finalOk:<int>, finalBad:<int> }
    // RunFix's Priority 4 tier only needs HumanSteps (to surface the manual step, e.g.
    // "devtunnel login") and FinalBad (to tell "fixed" from "still broken, no human step
    // known") -- autofixed/confirmNeeded are part of the JSON but not consumed here.
    class RepairResult
    {
        public List<string> HumanSteps = new List<string>();
        public int FinalOk;
        public int FinalBad;
    }

    // Runs  scripts\repair.ps1 -Auto -ResultJson  windowless and returns its captured stdout.
    // -Auto: Tier A repairs (e.g. start the stack) run automatically; Tier B (install/rebuild)
    // is skipped, not attempted unattended; Tier C (human-only, e.g. devtunnel login) is only
    // ever printed, never attempted. Same ProcessStartInfo shape as RunPowershellScript/
    // RunReconnect above; up to ~2 min for the stack bring-up, same budget RunStartAll assumes.
    string RunRepairDispatcher(string repairPs1)
    {
        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = "powershell";
        psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + repairPs1 + "\" -Auto -ResultJson";
        psi.WorkingDirectory = RepoRoot();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        string stdoutText = "";
        using (var p = System.Diagnostics.Process.Start(psi))
        {
            try { stdoutText = p.StandardOutput.ReadToEnd(); } catch (Exception) { }
            try { p.StandardError.ReadToEnd(); } catch (Exception) { }
            try { p.WaitForExit(180000); } catch (Exception) { }
        }
        return stdoutText;
    }

    // Parses repair.ps1 -ResultJson's LAST non-empty stdout line as the compact JSON summary
    // (every earlier line is repair.ps1's normal human-readable progress text). Returns null
    // (never throws) if nothing parseable was produced -- the caller then falls back to the
    // previous blind RunStartAll() behavior so a missing/broken repair.ps1 can never regress
    // the Fix button. Reuses the file's existing _js (JavaScriptSerializer) instance.
    RepairResult ParseRepairResult(string stdoutText)
    {
        try
        {
            if (string.IsNullOrEmpty(stdoutText)) return null;
            string[] lines = stdoutText.Replace("\r\n", "\n").Split('\n');
            string lastLine = null;
            for (int i = lines.Length - 1; i >= 0; i--)
            {
                if (!string.IsNullOrEmpty(lines[i].Trim())) { lastLine = lines[i].Trim(); break; }
            }
            if (lastLine == null) return null;
            var d = _js.DeserializeObject(lastLine) as Dictionary<string, object>;
            if (d == null) return null;

            var rr = new RepairResult();
            object hs;
            if (d.TryGetValue("humanSteps", out hs) && hs is object[])
            {
                foreach (var item in (object[])hs)
                {
                    var m = item as Dictionary<string, object>;
                    object step;
                    if (m != null && m.TryGetValue("step", out step) && step != null)
                        rr.HumanSteps.Add(Convert.ToString(step));
                }
            }
            object fo, fb;
            rr.FinalOk  = d.TryGetValue("finalOk", out fo)  ? Convert.ToInt32(fo)  : 0;
            rr.FinalBad = d.TryGetValue("finalBad", out fb) ? Convert.ToInt32(fb) : 0;
            return rr;
        }
        catch (Exception) { return null; }
    }

    // Fire the full stack bring-up EXACTLY the way the desktop icon does: wscript.exe running
    // start_all_hidden.vbs, which in turn drives scripts\start_all.ps1 (Invoke-Startup starts
    // supervisor.ps1 [MCP server + devtunnel], companion Edge, bridge, UIs). start_all.ps1 is
    // idempotent -- it skips components already running -- so calling this when the stack is
    // already healthy is a safe no-op. Fire-and-forget: we do not wait for it to finish (up to
    // ~2 min), we just launch it and let it self-log. Guarded by a 120s cooldown so repeated
    // Fix clicks or health-poll ticks cannot stack multiple launches.
    void RunStartAll()
    {
        double nowU = NowUnix();
        if (_startAllLaunched && (nowU - _startAllLastUnix) < 120.0) return;
        _startAllLaunched = true;
        _startAllLastUnix = nowU;
        try
        {
            string vbs = Path.Combine(RepoRoot(), "scripts", "start_all_hidden.vbs");
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = "wscript.exe";
            psi.Arguments = "\"" + vbs + "\"";
            psi.WorkingDirectory = RepoRoot();
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            System.Diagnostics.Process.Start(psi);
            System.Diagnostics.Debug.WriteLine("[FleetCockpit] RunStartAll: launched " + vbs);
        }
        catch (Exception ex)
        {
            try
            {
                if (!Dispatcher.HasShutdownStarted)
                    Dispatcher.BeginInvoke(new Action(delegate { if (_fixNote != null) _fixNote.Text = T("hs_fix_err") + ": " + ex.Message; }));
            }
            catch (Exception) { }
        }
    }

    // A2-2: Refresh the Evidence Spine panel on each tick. Keyed off a signature so the
    // heavy DOM rebuild (and col-width change) only happens when the run state changes.
    // Called from OnTick after RenderCards (so _toolbarAll is already up to date).
    void RefreshSpine(Dictionary<string, object> root, bool idle)
    {
        if (_spinePanel == null || _spineCol == null) return;

        // NOT-RUNNING: the left "実行タイムライン" follows an EXPANDED past/history task, so inspecting
        // a finished task shows ITS timeline on the left (works after a run ends too, not only when
        // fully idle). A RUNNING fleet always owns the spine (the live run is never hidden), so this
        // past-task focus is suppressed while running. (user choice: 走行中は現在優先)
        bool spineRunning = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        if (!spineRunning && root != null && _history != null && _expanded != null && _expanded.Count > 0)
        {
            Dictionary<string, object> focusEntry = null;
            for (int hi = _history.Count - 1; hi >= 0; hi--)
            {
                var he = _history[hi] as Dictionary<string, object>;
                if (he == null) continue;
                string hk = S(he, "key");
                if (!string.IsNullOrEmpty(hk) && _expanded.Contains(hk)) { focusEntry = he; break; }
            }
            if (focusEntry != null)
            {
                string fkey = S(focusEntry, "key");
                string fsig = "PAST|" + fkey + "|" + (_dark ? "D" : "L") + _lang;
                if (fsig == _spineSig) return;
                _spineSig = fsig;
                _spineCol.Width = new GridLength(220);
                _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
                _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
                var pastList = new List<Dictionary<string, object>>();
                pastList.Add(focusEntry);
                bool jaP = _lang == 0;
                var wrap = new StackPanel();
                wrap.Margin = new Thickness(10, 10, 8, 0);
                var pastTag = new TextBlock();
                pastTag.Text = jaP ? "過去のタスク" : "Past task";
                pastTag.Foreground = Theme.Br(Theme.Accent(_dark));
                pastTag.FontSize = 10; pastTag.FontWeight = FontWeights.SemiBold;
                pastTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(pastTag);
                var titleTag = new TextBlock();
                titleTag.Text = CardTitle(S(focusEntry, "conv_title"), S(focusEntry, "goal"));
                titleTag.Foreground = Theme.Br(Theme.Muted(_dark)); titleTag.FontSize = 10;
                titleTag.TextTrimming = TextTrimming.CharacterEllipsis;
                titleTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(titleTag);
                wrap.Children.Add(BuildSpineContent(root, "ended", true, pastList));
                _spinePanel.Child = wrap;
                return;
            }
        }

        // Ensure _toolbarAll reflects current root (it may be empty on first tick before RenderCards).
        // If _toolbarAll is empty but root has workers, use root's workers directly for the check.
        bool hasWorkers = false;
        if (!idle)
        {
            if (_toolbarAll != null && _toolbarAll.Count > 0)
                hasWorkers = true;
            else if (root != null)
            {
                object wo2x;
                if (root.TryGetValue("workers", out wo2x) && wo2x is object[])
                    hasWorkers = ((object[])wo2x).Length > 0;
            }
        }

        // Build a lightweight signature: started + overall phase + worker count + theme/lang.
        string overallPhase = "idle";
        bool runEnded = false;
        // Collect workers from _toolbarAll or directly from root as fallback.
        var spineWorkers = new List<Dictionary<string, object>>();
        if (_toolbarAll != null && _toolbarAll.Count > 0)
            spineWorkers = _toolbarAll;
        else if (root != null)
        {
            object wox2;
            if (root.TryGetValue("workers", out wox2) && wox2 is object[])
                foreach (object ow2 in (object[])wox2)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 != null) spineWorkers.Add(ww2);
                }
        }
        if (root != null && hasWorkers)
        {
            bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            runEnded = !running;
            bool anyAttn = false;
            bool allDone = true;
            foreach (Dictionary<string, object> tw in spineWorkers)
            {
                string st = S(tw, "status");
                if (!IsTerminalWorker(tw)) { allDone = false; }
                if (st == "stuck" || st == "maxturns" || st == "error") anyAttn = true;
                if (st == "verifying") overallPhase = "verifying";
                else if ((st == "researching" || st == "refuting" || st == "waiting" || st == "ready")
                         && overallPhase == "idle") overallPhase = "running";
            }
            if (anyAttn) overallPhase = "attn";
            if (runEnded || allDone) overallPhase = "ended";
        }
        string started = root != null ? S(root, "started") : "";
        // Re-key on the PRIMARY worker's own status + phase-event count so this panel repaints
        // when workers[0] itself progresses, even while sibling workers keep overallPhase pinned
        // to "running" (a repaint-gating bug, not a BuildSpineContent rendering bug -- that method
        // already reads workers[0].phase_events correctly, it just wasn't being re-invoked).
        string primaryStatus = "";
        int primaryPhaseCount = 0;
        if (spineWorkers != null && spineWorkers.Count > 0)
        {
            Dictionary<string, object> primaryW = spineWorkers[0];
            if (primaryW != null)
            {
                primaryStatus = S(primaryW, "status");
                object pe;
                if (primaryW.TryGetValue("phase_events", out pe) && pe is object[]) primaryPhaseCount = ((object[])pe).Length;
            }
        }
        string spineSig = (hasWorkers ? "1" : "0") + "|" + started + "|" + overallPhase
                          + "|" + (_toolbarAll != null ? _toolbarAll.Count : 0)
                          + "|" + (_dark ? "D" : "L") + _lang
                          + "|" + primaryStatus + "|" + primaryPhaseCount;
        if (spineSig == _spineSig) return;
        _spineSig = spineSig;

        if (!hasWorkers)
        {
            _spineCol.Width = new GridLength(0);
            _spinePanel.Child = null;
            return;
        }

        _spineCol.Width = new GridLength(220);
        _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
        _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
        _spinePanel.Child = BuildSpineContent(root, overallPhase, runEnded, spineWorkers);
    }

    // Build the spine panel content: section header + vertical [COMPUTED] execution timeline.
    // Derives events honestly from available data: run started ts, transcript first-turn ts,
    // current overall phase (polled), run ended state. No fabricated phase_events.
    UIElement BuildSpineContent(Dictionary<string, object> root, string overallPhase, bool runEnded,
                                List<Dictionary<string, object>> workers)
    {
        bool ja = _lang == 0;

        var outer = new StackPanel();
        outer.Margin = new Thickness(10, 12, 8, 12);

        // ── Section label ──────────────────────────────────────────────────────────
        var sectionLbl = new TextBlock();
        sectionLbl.Text = ja ? "実行タイムライン" : "Execution timeline";
        sectionLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        sectionLbl.FontSize = 10.5;
        sectionLbl.FontWeight = FontWeights.SemiBold;
        sectionLbl.Margin = new Thickness(0, 0, 0, 1);
        outer.Children.Add(sectionLbl);

        // ── Check for real phase_events from the primary worker ────────────────────
        // The primary worker is the first/earliest worker in the workers list.
        // If phase_events is present and non-empty, render from those (REAL mode).
        // Otherwise, fall through to the [COMPUTED] turn-timestamp fallback below.
        bool usingRealEvents = false;
        var realPhaseEvents = new List<Tuple<string, string, string>>();  // label, timeStr, colorHex
        if (workers != null && workers.Count > 0)
        {
            Dictionary<string, object> primaryWorker = workers[0];
            object peRaw;
            if (primaryWorker.TryGetValue("phase_events", out peRaw) && peRaw is object[])
            {
                object[] peArr = (object[])peRaw;
                if (peArr.Length > 0)
                {
                    usingRealEvents = true;
                    foreach (object peObj in peArr)
                    {
                        var pe = peObj as Dictionary<string, object>;
                        if (pe == null) continue;
                        // ts: epoch double
                        double peTs = 0;
                        object peTsRaw;
                        if (pe.TryGetValue("ts", out peTsRaw) && peTsRaw != null)
                        {
                            try { peTs = Convert.ToDouble(peTsRaw); } catch { }
                        }
                        // event: the status-key string
                        string peEvent = "";
                        object peEventRaw;
                        if (pe.TryGetValue("event", out peEventRaw) && peEventRaw != null)
                            peEvent = peEventRaw.ToString();
                        // label: English fallback from the stored label field
                        string peFallbackLabel = peEvent;
                        object peLabelRaw;
                        if (pe.TryGetValue("label", out peLabelRaw) && peLabelRaw != null)
                            peFallbackLabel = peLabelRaw.ToString();
                        // Localized label via Theme.StatusLabel; fall back to stored English label
                        string localLabel = Theme.StatusLabel(peEvent, _lang);
                        if (string.IsNullOrEmpty(localLabel) || localLabel == peEvent)
                        {
                            // Theme.StatusLabel returns the key itself when unrecognized; use stored fallback
                            string knownKey = Theme.StatusLabel(peEvent, _lang);
                            localLabel = (knownKey == peEvent && !string.IsNullOrEmpty(peFallbackLabel))
                                ? peFallbackLabel : knownKey;
                        }
                        string railKind = Theme.StatusRail(peEvent);
                        string colorHex = Theme.RailColor(railKind, _dark);
                        string timeStr = "";
                        if (peTs > 0)
                        {
                            try
                            {
                                timeStr = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                                    .AddSeconds(peTs).ToLocalTime().ToString("HH:mm");
                            }
                            catch { }
                        }
                        realPhaseEvents.Add(new Tuple<string, string, string>(localLabel, timeStr, colorHex));
                    }
                }
            }
        }

        // Sub-label changes based on mode (real vs computed)
        var subLbl = new TextBlock();
        subLbl.Text = usingRealEvents
            ? (ja ? "(フェーズ遷移)" : "(phase transitions)")
            : (ja ? "(会話ターンから)" : "(from turns)");
        subLbl.Foreground = Theme.Br(Theme.Faint(_dark));
        subLbl.FontSize = 9.5;
        subLbl.Margin = new Thickness(0, 0, 0, 8);
        outer.Children.Add(subLbl);

        // ── If real events mode: render from phase_events and skip [COMPUTED] path ─
        if (usingRealEvents)
        {
            for (int i = 0; i < realPhaseEvents.Count; i++)
            {
                string evLabel = realPhaseEvents[i].Item1;
                string evTime  = realPhaseEvents[i].Item2;
                string evColor = realPhaseEvents[i].Item3;
                bool isLast = (i == realPhaseEvents.Count - 1);
                var row = new Grid();
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var lineAndDot = new StackPanel();
                lineAndDot.HorizontalAlignment = HorizontalAlignment.Center;
                if (i > 0)
                {
                    var connector = new Border();
                    connector.Width = 1.5; connector.Height = 8;
                    connector.Background = Theme.Br(Theme.Border(_dark));
                    connector.HorizontalAlignment = HorizontalAlignment.Center;
                    lineAndDot.Children.Add(connector);
                }
                var dot = new System.Windows.Shapes.Ellipse();
                dot.Width = 8; dot.Height = 8;
                dot.Fill = Theme.Br(evColor);
                dot.HorizontalAlignment = HorizontalAlignment.Center;
                dot.Margin = new Thickness(0, i == 0 ? 4 : 0, 0, 0);
                lineAndDot.Children.Add(dot);
                if (!isLast)
                {
                    var tail = new Border();
                    tail.Width = 1.5; tail.Height = 8;
                    tail.Background = Theme.Br(Theme.Border(_dark));
                    tail.HorizontalAlignment = HorizontalAlignment.Center;
                    lineAndDot.Children.Add(tail);
                }
                Grid.SetColumn(lineAndDot, 0);
                row.Children.Add(lineAndDot);
                var labelBlock = new StackPanel();
                labelBlock.VerticalAlignment = VerticalAlignment.Top;
                labelBlock.Margin = new Thickness(4, i == 0 ? 2 : 0, 0, 4);
                var labelTb = new TextBlock();
                labelTb.Text = evLabel;
                labelTb.Foreground = Theme.Br(evColor);
                labelTb.FontSize = 11; labelTb.FontWeight = FontWeights.SemiBold;
                labelTb.TextTrimming = TextTrimming.CharacterEllipsis;
                labelBlock.Children.Add(labelTb);
                if (!string.IsNullOrEmpty(evTime))
                {
                    var timeTb = new TextBlock();
                    timeTb.Text = evTime;
                    timeTb.Foreground = Theme.Br(Theme.Muted(_dark));
                    timeTb.FontSize = 10;
                    labelBlock.Children.Add(timeTb);
                }
                Grid.SetColumn(labelBlock, 1);
                row.Children.Add(labelBlock);
                outer.Children.Add(row);
            }
            var realTag = new TextBlock();
            realTag.Text = "[REAL]";
            realTag.Foreground = Theme.Br(Theme.Faint(_dark));
            realTag.FontSize = 9;
            realTag.Margin = new Thickness(0, 8, 0, 0);
            outer.Children.Add(realTag);
            return outer;
        }

        // ── [COMPUTED] fallback: derive timestamps from transcript ────────────────
        // Use the first worker in the workers list that has a transcript path.
        string transcriptPath = "";
        if (workers != null)
        {
            foreach (Dictionary<string, object> tw in workers)
            {
                string tp = S(tw, "transcript");
                if (!string.IsNullOrEmpty(tp) && File.Exists(tp)) { transcriptPath = tp; break; }
            }
            if (string.IsNullOrEmpty(transcriptPath) && workers.Count > 0)
                transcriptPath = S(workers[0], "transcript");
        }

        // Read meta ts (= "queued") and first turn ts (= "started") from transcript.
        double metaTs = 0, firstTurnTs = 0;
        try
        {
            if (!string.IsNullOrEmpty(transcriptPath) && File.Exists(transcriptPath))
            {
                string[] tlines;
                using (var fsr = new FileStream(transcriptPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fsr, Encoding.UTF8))
                    tlines = sr.ReadToEnd().Replace("\r", "").Split('\n');
                foreach (var tln in tlines)
                {
                    if (string.IsNullOrEmpty(tln)) continue;
                    Dictionary<string, object> obj;
                    try { obj = _js.DeserializeObject(tln) as Dictionary<string, object>; } catch { continue; }
                    if (obj == null) continue;
                    if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                    {
                        if (obj.ContainsKey("ts") && obj["ts"] != null) metaTs = Convert.ToDouble(obj["ts"]);
                        continue;
                    }
                    if (obj.ContainsKey("role") && obj.ContainsKey("ts") && obj["ts"] != null && firstTurnTs == 0)
                        firstTurnTs = Convert.ToDouble(obj["ts"]);
                    if (firstTurnTs > 0) break;
                }
            }
        }
        catch { }

        // Fall back: if meta ts is absent, use root["started"] (epoch, top-level).
        if (metaTs <= 0 && root != null) metaTs = Dbl(root, "started");

        // ── Helper: format epoch as "HH:mm" ──────────────────────────────────────
        // C# 5: use a local method-delegate pattern
        Func<double, string> fmtHM = delegate(double ts)
        {
            if (ts <= 0) return "";
            try
            {
                return new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                    .AddSeconds(ts).ToLocalTime().ToString("HH:mm");
            }
            catch { return ""; }
        };

        // ── Build events list ──────────────────────────────────────────────────────
        // [COMPUTED] Honest markers only: queued/received, started (first turn), now/phase, ended.
        var events = new List<Tuple<string, string, string>>();
        // Each tuple: (label, time-string, railColor-hex)
        string graphite = Theme.Muted(_dark);
        string live = Theme.Info(_dark);
        string attn = Theme.Warning(_dark);
        string ended = Theme.Success(_dark);
        string danger = Theme.Danger(_dark);

        // Marker 1: Queued / directive received
        string qLabel = ja ? "投入" : "Queued";
        string qTime = fmtHM(metaTs);
        events.Add(new Tuple<string, string, string>(qLabel, qTime, graphite));

        // Marker 2: Started (first turn in transcript)
        if (firstTurnTs > 0)
        {
            string sLabel = ja ? "開始" : "Started";
            string sTime = fmtHM(firstTurnTs);
            events.Add(new Tuple<string, string, string>(sLabel, sTime, live));
        }

        // Marker 3: Current / overall phase (polled; [COMPUTED])
        if (!runEnded)
        {
            string phLabel;
            string phColor;
            if (overallPhase == "attn")
            {
                phLabel = ja ? "要対応" : "Needs attention";
                phColor = attn;
            }
            else if (overallPhase == "verifying")
            {
                phLabel = ja ? "検証中" : "Verifying";
                phColor = attn;
            }
            else if (overallPhase == "running")
            {
                phLabel = ja ? "実行中" : "Running";
                phColor = live;
            }
            else
            {
                phLabel = ja ? "実行中" : "Running";
                phColor = live;
            }
            string nowTime = fmtHM(NowUnix());
            events.Add(new Tuple<string, string, string>(phLabel, nowTime, phColor));
        }
        else
        {
            // Marker 3 (ended): use root["updated"] as the best proxy for end time.
            double endedTs = root != null ? Dbl(root, "updated") : 0;
            string eLabel = ja ? "終了" : "Ended";
            string eTime = fmtHM(endedTs);
            string eColor = (overallPhase == "attn") ? danger : ended;
            events.Add(new Tuple<string, string, string>(eLabel, eTime, eColor));
        }

        // ── Render the vertical timeline ───────────────────────────────────────────
        for (int i = 0; i < events.Count; i++)
        {
            string evLabel = events[i].Item1;
            string evTime = events[i].Item2;
            string evColor = events[i].Item3;
            bool isLast = (i == events.Count - 1);

            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });  // dot + line col
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) }); // label col

            // Vertical connector: a thin line above the dot (hidden for first item).
            var lineAndDot = new StackPanel();
            lineAndDot.HorizontalAlignment = HorizontalAlignment.Center;
            if (i > 0)
            {
                var connector = new Border();
                connector.Width = 1.5;
                connector.Height = 8;
                connector.Background = Theme.Br(Theme.Border(_dark));
                connector.HorizontalAlignment = HorizontalAlignment.Center;
                lineAndDot.Children.Add(connector);
            }

            // Dot
            var dot = new System.Windows.Shapes.Ellipse();
            dot.Width = 8;
            dot.Height = 8;
            dot.Fill = Theme.Br(evColor);
            dot.HorizontalAlignment = HorizontalAlignment.Center;
            dot.Margin = new Thickness(0, i == 0 ? 4 : 0, 0, 0);
            lineAndDot.Children.Add(dot);

            // Tail connector below dot (hidden for last item)
            if (!isLast)
            {
                var tail = new Border();
                tail.Width = 1.5;
                tail.Height = 8;
                tail.Background = Theme.Br(Theme.Border(_dark));
                tail.HorizontalAlignment = HorizontalAlignment.Center;
                lineAndDot.Children.Add(tail);
            }

            Grid.SetColumn(lineAndDot, 0);
            row.Children.Add(lineAndDot);

            // Label + time block
            var labelBlock = new StackPanel();
            labelBlock.VerticalAlignment = VerticalAlignment.Top;
            labelBlock.Margin = new Thickness(4, i == 0 ? 2 : 0, 0, 4);

            var labelTb = new TextBlock();
            labelTb.Text = evLabel;
            labelTb.Foreground = Theme.Br(evColor);
            labelTb.FontSize = 11;
            labelTb.FontWeight = FontWeights.SemiBold;
            labelTb.TextTrimming = TextTrimming.CharacterEllipsis;
            labelBlock.Children.Add(labelTb);

            if (!string.IsNullOrEmpty(evTime))
            {
                var timeTb = new TextBlock();
                timeTb.Text = evTime;
                timeTb.Foreground = Theme.Br(Theme.Muted(_dark));
                timeTb.FontSize = 10;
                labelBlock.Children.Add(timeTb);
            }

            Grid.SetColumn(labelBlock, 1);
            row.Children.Add(labelBlock);

            outer.Children.Add(row);
        }

        // ── [COMPUTED] footer tag ─────────────────────────────────────────────────
        var tag = new TextBlock();
        tag.Text = "[COMPUTED]";
        tag.Foreground = Theme.Br(Theme.Faint(_dark));
        tag.FontSize = 9;
        tag.Margin = new Thickness(0, 8, 0, 0);
        outer.Children.Add(tag);

        return outer;
    }

    // ItemContainerStyle: invisible chrome. The ControlTemplate is just a ContentPresenter, so
    // there is NO selection highlight, NO mouse-over, NO focus rectangle -- it behaves like a
    // plain scrolling row, not a selectable list item.
    Style BuildItemContainerStyle()
    {
        var st = new Style(typeof(ListBoxItem));
        st.Setters.Add(new Setter(Control.BackgroundProperty, Brushes.Transparent));
        st.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(0)));
        st.Setters.Add(new Setter(FrameworkElement.MarginProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty, HorizontalAlignment.Stretch));
        st.Setters.Add(new Setter(UIElement.FocusableProperty, false));
        var tmpl = new ControlTemplate(typeof(ListBoxItem));
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Stretch);
        tmpl.VisualTree = cp;
        st.Setters.Add(new Setter(Control.TemplateProperty, tmpl));
        return st;
    }

    // ItemTemplate: a ContentControl whose Content is the item itself, run through RowConverter,
    // which calls the EXISTING builders by Row.Kind and returns the realized UIElement. Because
    // containers recycle, Convert runs for visible items only.
    DataTemplate BuildRowTemplate()
    {
        var dt = new DataTemplate();
        var cc = new FrameworkElementFactory(typeof(ContentControl));
        var b = new System.Windows.Data.Binding();
        b.Converter = new RowConverter(this);
        cc.SetBinding(ContentControl.ContentProperty, b);
        cc.SetValue(UIElement.FocusableProperty, false);
        dt.VisualTree = cc;
        return dt;
    }

    TextBox _goalInput;
    Button _startBtn, _folderBtn;
    TextBlock _startNote;
    Border _composerBox;             // the rounded composer surface (themed in PaintChrome)
    TextBlock _composerHint;         // footer "one goal per line · / for commands"
    TextBlock _composerWatermark;    // placeholder shown when the textarea is empty

    // ④ task-injection: a bottom-docked composer (spec: agent-workspace, not a form). Type goals
    // (one per line) and launch a fleet. The big top textarea + right-side vertical button stack
    // are gone; the box now sits at the bottom with an inline footer (folder=secondary, start=primary).
    UIElement BuildInputBar()
    {
        _inBar = new Border();
        _inBar.Padding = new Thickness(Theme.PadApp, 0, Theme.PadApp, 8);   // was top 4 — remove the gap above the composer
        DockPanel.SetDock(_inBar, Dock.Bottom);

        _composerBox = new Border();
        _composerBox.CornerRadius = new CornerRadius(Theme.RadComposer);
        _composerBox.BorderThickness = new Thickness(0);   // frameless at rest; focus adds 1px accent
        _composerBox.Padding = new Thickness(12, 6, 12, 6); // was (12,8,12,8) — tighter to hug content
        // Floating Codex/Claude-Code style: soft shadow carries the separation, no hard frame.
        _composerBox.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 14, ShadowDepth = 2, Opacity = 0.16, Color = System.Windows.Media.Color.FromRgb(0, 0, 0) };
        // Match the main chat composer: clicking the composer surface (including lower padding /
        // footer hint area) should focus the textarea. Do not mark handled so buttons still click.
        _composerBox.MouseLeftButtonDown += delegate { if (_goalInput != null) _goalInput.Focus(); };

        var col = new StackPanel();

        // ── textarea + watermark overlay (a Grid so they stack in the same cell) ──
        var taGrid = new Grid();
        _goalInput = new TextBox();
        _goalInput.AcceptsReturn = true; _goalInput.TextWrapping = TextWrapping.Wrap;
        _goalInput.MinHeight = 30; _goalInput.MaxHeight = 180;   // compact: ~30 (was 38), max 180 internal scroll
        _goalInput.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _goalInput.FontSize = Theme.FsBody;
        _goalInput.BorderThickness = new Thickness(0);            // the composer carries the border
        _goalInput.Background = Brushes.Transparent;
        _goalInput.Padding = new Thickness(2, 1, 2, 1);
        _goalInput.VerticalContentAlignment = VerticalAlignment.Top;
        BuildGoalCmdPopup();
        _goalInput.TextChanged += delegate { UpdateGoalCmdPopup(); };
        _goalInput.GotKeyboardFocus += delegate { PaintComposerFocus(true); };
        _goalInput.LostKeyboardFocus += delegate { PaintComposerFocus(false); };
        _goalInput.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            // slash-command autocomplete (like the main chat): navigate / accept / dismiss
            if (_gcmdPopup != null && _gcmdPopup.IsOpen && _gcmdList.Items.Count > 0)
            {
                if (e.Key == Key.Down) { _gcmdList.SelectedIndex = Math.Min(_gcmdList.SelectedIndex + 1, _gcmdList.Items.Count - 1); _gcmdList.ScrollIntoView(_gcmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Up) { _gcmdList.SelectedIndex = Math.Max(_gcmdList.SelectedIndex - 1, 0); _gcmdList.ScrollIntoView(_gcmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Tab || e.Key == Key.Return) { AcceptGoalCommand(); e.Handled = true; return; }
                if (e.Key == Key.Escape) { _gcmdPopup.IsOpen = false; e.Handled = true; return; }
            }
            if (e.Key == Key.Return && (Keyboard.Modifiers & ModifierKeys.Control) != 0)
            {
                e.Handled = true;
                // A2-2: Ctrl+Enter steers when a run is active; starts fleet otherwise.
                if (_composerRunActive) TrySendSteer();
                else StartFleet();
            }
        };
        taGrid.Children.Add(_goalInput);
        // short placeholder (spec Fleet: "タスクを入力..."); the "one per line / slash" guidance
        // moves to the footer hint so the box itself stays quiet.
        _composerWatermark = new TextBlock
        {
            Text = _lang == 0 ? "タスクを入力..." : "Add tasks...",
            IsHitTestVisible = false, FontSize = Theme.FsBody,
            Margin = new Thickness(4, 1, 4, 0), VerticalAlignment = VerticalAlignment.Top,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        taGrid.Children.Add(_composerWatermark);
        _goalInput.TextChanged += delegate { _composerWatermark.Visibility = string.IsNullOrEmpty(_goalInput.Text) ? Visibility.Visible : Visibility.Collapsed; };
        col.Children.Add(taGrid);

        // ── footer: left hint, right button cluster (folder=secondary, start=primary) ──
        var footer = new DockPanel { LastChildFill = false };
        footer.Margin = new Thickness(0, 6, 0, 0);  // was 8 -- tighter footer gap

        var btns = new StackPanel { Orientation = Orientation.Horizontal };
        _folderBtn = new Button();
        _folderBtn.Cursor = Cursors.Hand; _folderBtn.BorderThickness = new Thickness(1);
        _folderBtn.Height = Theme.BtnH; _folderBtn.FontSize = 12;
        _folderBtn.Padding = new Thickness(12, 0, 12, 0);
        _folderBtn.Click += delegate { FolderToGoals(); };
        btns.Children.Add(_folderBtn);
        _startBtn = new Button();
        _startBtn.Cursor = Cursors.Hand; _startBtn.BorderThickness = new Thickness(0);
        _startBtn.Height = Theme.BtnH; _startBtn.MinWidth = 132; _startBtn.FontWeight = FontWeights.SemiBold;
        _startBtn.Margin = new Thickness(8, 0, 0, 0); _startBtn.Padding = new Thickness(16, 0, 16, 0);
        // A2-2: when a run is active, the button sends a steer instead of starting a fleet.
        _startBtn.Click += delegate
        {
            if (_composerRunActive) TrySendSteer();
            else StartFleet();
        };
        btns.Children.Add(_startBtn);
        DockPanel.SetDock(btns, Dock.Right);
        footer.Children.Add(btns);

        // "/" affordance button on the left of the footer hint
        var slashBtn = new Button();
        slashBtn.Content = "/"; slashBtn.FontSize = 13; slashBtn.FontWeight = FontWeights.SemiBold;
        slashBtn.Foreground = Muted;   // match the footer hint's muted tone (was defaulting to dark Fg)
        slashBtn.Cursor = Cursors.Hand;
        slashBtn.BorderThickness = new Thickness(0); slashBtn.Background = Brushes.Transparent;
        slashBtn.Padding = new Thickness(4, 0, 6, 0); slashBtn.VerticalAlignment = VerticalAlignment.Center;
        slashBtn.ToolTip = _lang == 0 ? "スラッシュコマンドを入力" : "Type a slash command";
        slashBtn.Template = FlatButtonTemplate();
        slashBtn.Click += delegate
        {
            if (_goalInput == null) return;
            // Insert "/" at current line start (or just append if line non-empty without "/")
            string txt = _goalInput.Text ?? "";
            int caret = _goalInput.CaretIndex;
            if (caret > txt.Length) caret = txt.Length;
            int ls = caret > 0 ? txt.LastIndexOf('\n', caret - 1) + 1 : 0;
            string line = txt.Substring(ls, caret - ls);
            if (line.Length == 0 || line[0] != '/')
            {
                _goalInput.Text = txt.Substring(0, ls) + "/" + txt.Substring(ls);
                _goalInput.CaretIndex = ls + 1;
            }
            _goalInput.Focus();
        };
        DockPanel.SetDock(slashBtn, Dock.Left);
        footer.Children.Add(slashBtn);

        _composerHint = new TextBlock
        {
            Text = _lang == 0 ? "1行に1ゴール（複数可） ·「/」でコマンド" : "One goal per line · \"/\" for commands",
            FontSize = Theme.FsMeta, VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        footer.Children.Add(_composerHint);   // no Dock -> fills the left
        col.Children.Add(footer);

        // transient start feedback ("Started (N goals)" / errors), below the footer
        _startNote = new TextBlock();
        _startNote.FontSize = Theme.FsMeta; _startNote.Margin = new Thickness(2, 6, 0, 0);
        _startNote.TextWrapping = TextWrapping.Wrap;
        col.Children.Add(_startNote);

        _composerBox.Child = col;
        _inBar.Child = _composerBox;
        return _inBar;
    }
    Border _inBar;

    // Focus ring: composer is frameless at rest (BorderThickness=0). On focus, add a 1px
    // accent outline so keyboard users get a clear indicator without re-introducing the boxy look.
    void PaintComposerFocus(bool focused)
    {
        if (_composerBox == null) return;
        if (focused)
        {
            _composerBox.BorderThickness = new Thickness(1);
            _composerBox.BorderBrush = Theme.Br(Theme.Accent(_dark));
        }
        else
        {
            _composerBox.BorderThickness = new Thickness(0);
            _composerBox.BorderBrush = System.Windows.Media.Brushes.Transparent;
        }
    }


    void StartFleet()
    {
        try
        {
            // refuse if a fleet is already running (both would write the same status.json)
            Dictionary<string, object> st = ReadStatus();
            if (st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"])))
            {
                _startNote.Text = _lang == 0 ? "実行中です。完了後に。" : "Already running.";
                return;
            }
            var goals = new List<string>();
            foreach (string ln in (_goalInput.Text ?? "").Replace("\r", "").Split('\n'))
            {
                string s = ln.Trim();
                if (s.Length > 0 && !s.StartsWith("#")) goals.Add(s);
            }
            if (goals.Count == 0)
            {
                _startNote.Text = _lang == 0 ? "ゴールを入力してください。" : "Enter goals (one per line).";
                return;
            }
            if (goals.Count == 1 && goals[0].Equals("/help", StringComparison.OrdinalIgnoreCase))
            {
                _goalInput.Text = "";
                ShowGoalHelp();
                _startNote.Text = _lang == 0 ? "コマンド一覧を表示しました。" : "Command help shown.";
                return;
            }
            // /effort <value> and /approval <value>: apply and clear (don't submit as a goal)
            if (goals.Count == 1)
            {
                string g0 = goals[0];
                bool handled = false;
                if (g0.StartsWith("/effort ", StringComparison.OrdinalIgnoreCase))
                {
                    string v = g0.Substring(8).Trim().ToLower();
                    if (v == "min" || v == "max" || v == "ultra" || v == "auto")
                    { _effort = v; SaveKey("effort", _effort); PaintEffort(); _goalInput.Text = ""; _startNote.Text = (_lang == 0 ? "推論モード→ " : "Effort set to ") + _effort; handled = true; }
                }
                else if (g0.StartsWith("/approval ", StringComparison.OrdinalIgnoreCase))
                {
                    string v = g0.Substring(10).Trim().ToLower();
                    if (v == "run" || v == "plan" || v == "auto")
                    { _approval = v; SaveKey("approval", _approval); PaintApproval(); _goalInput.Text = ""; _startNote.Text = (_lang == 0 ? "実行方式→ " : "Run mode set to ") + _approval; handled = true; }
                }
                if (handled) return;
            }

            bool planMode = _approval == "plan" || _approval == "auto";
            SpawnFleet(goals, "goals_input.txt", planMode);
            _goalInput.Text = "";
            _startNote.Text = (_lang == 0 ? "開始しました（" : "Started (") + goals.Count
                              + (_lang == 0 ? " 件）" : " goals)")
                              + (planMode
                                  ? (_lang == 0 ? "。承認待ちの計画を各カードに出します。" : ". Each card will wait at plan approval.")
                                  : "");
            _lastSig = "";   // force a re-render once status.json starts updating
        }
        catch (Exception ex)
        {
            _startNote.Text = (_lang == 0 ? "起動失敗: " : "Failed: ") + ex.Message;
        }
    }

    // A2-2: Send a steer from the bottom composer. Parses "W2: ..." prefix to target a specific
    // worker; otherwise broadcasts to the first running worker (or ALL via broadcast if no live
    // specific worker is found -- the relay picks the right one). Reuses RequestSteer() exactly
    // as the per-card SteerRow does: writes {"steer":[{worker,text},...]} into commands.json.
    void TrySendSteer()
    {
        string text = (_goalInput != null ? _goalInput.Text : "").Trim();
        if (string.IsNullOrEmpty(text)) return;
        if (!RunIsLive())
        {
            if (_startNote != null) _startNote.Text = T("steer_dead");
            return;
        }

        // Parse optional "Wx: " / "W0: " / "W10: " prefix for targeted worker routing.
        string targetWorker = "";
        string steerText = text;
        if (text.Length > 2 && (text[0] == 'W' || text[0] == 'w'))
        {
            int colonIdx = text.IndexOf(':');
            if (colonIdx >= 2 && colonIdx <= 4)
            {
                string maybeWorker = text.Substring(0, colonIdx).Trim();
                bool allDigits = true;
                for (int ci = 1; ci < maybeWorker.Length; ci++)
                    if (!char.IsDigit(maybeWorker[ci])) { allDigits = false; break; }
                if (allDigits && maybeWorker.Length >= 2)
                {
                    targetWorker = maybeWorker;      // e.g. "W2"
                    steerText = text.Substring(colonIdx + 1).Trim();
                }
            }
        }

        // If no explicit worker prefix, broadcast to the first non-terminal running worker.
        if (string.IsNullOrEmpty(targetWorker))
        {
            var workers = _toolbarAll ?? new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> tw in workers)
            {
                string st = S(tw, "status");
                if (!IsTerminalWorker(tw) && st != "pending")
                {
                    targetWorker = S(tw, "name");
                    break;
                }
            }
            // Still empty: fall back to empty string (relay broadcasts to all workers).
        }

        RequestSteer(targetWorker, steerText);
        if (_goalInput != null) _goalInput.Text = "";
        if (_startNote != null)
            _startNote.Text = _lang == 0 ? "次のターンに送信しました" : "Queued for the next turn";
    }

    // A2-2: Paint the composer into either "idle/add-goals" or "active-run/steer" mode.
    // Only repaints when the mode actually changes (keyed off _composerRunActive).
    void PaintComposerMode(bool runActive)
    {
        if (_composerRunActive == runActive) return;
        _composerRunActive = runActive;

        bool ja = _lang == 0;
        if (runActive)
        {
            // Active-run mode: steer / intervene surface
            if (_composerWatermark != null)
                _composerWatermark.Text = ja
                    ? "ステア・割り込み...（例: W2: 修正案を確認して）"
                    : "Steer or intervene... (e.g. W2: check the fix)";
            if (_composerHint != null)
                _composerHint.Text = ja
                    ? "アクティブな実行に送信 ·「/」でコマンド"
                    : "sent to the active run · '/' for commands";
            if (_startBtn != null)
            {
                _startBtn.Content = ja ? "送信" : "Send";
                // Use accent color for primary action; same as the idle Start button.
                _startBtn.Background = Accent;
                _startBtn.Foreground = White;
            }
            if (_folderBtn != null)
            {
                // Folder button less prominent while steering
                _folderBtn.Visibility = Visibility.Collapsed;
            }
        }
        else
        {
            // Idle mode: add goals / start
            if (_composerWatermark != null)
                _composerWatermark.Text = ja ? "タスクを入力..." : "Add tasks...";
            if (_composerHint != null)
                _composerHint.Text = ja ? "1行に1ゴール（複数可） ·「/」でコマンド" : "One goal per line · \"/\" for commands";
            if (_startBtn != null)
            {
                _startBtn.Content = T("start");
                _startBtn.Background = Accent;
                _startBtn.Foreground = White;
            }
            if (_folderBtn != null)
            {
                _folderBtn.Visibility = Visibility.Visible;
            }
        }
    }

    // Spawn a fresh `python -m relay.fleet_runner` for the given goal texts. Factored out of
    // StartFleet so RETRY can reuse it: when a run has FINISHED, the cockpit relaunches a fleet
    // (instead of writing an add_goal that nothing alive would ever consume). The agent URL is
    // NOT passed -- the runner resolves it from MCP_FLEET_AGENT_URL / .env, exactly as a manual
    // Start does -- and --state-dir is the same .fleet dir this cockpit tails, so the relaunched
    // run shows up live here. Goals are handed over via a UTF-8 file, written as JSONL (one JSON
    // object per line -- see GoalsToJsonl) so a goal string containing embedded newlines survives
    // as a SINGLE goal instead of being shredded one-line-per-fragment by fleet_runner's
    // line-based reader (incident: a multi-line review prompt split into 53 nonsense goals).
    // Returns true if the process started. `goalsFileName` lets callers use a distinct file so a
    // retry spawn never clobbers the manual Start input file (or vice-versa).
    // Fire-and-forget: shell out to relay.fleet_reaper.reap_stale_run() so a phantom run's
    // status.json/history.json/fleet_run_active.json get finalized to a clean cancelled state,
    // not just the cockpit's in-memory view (see ArchiveAllStale's call site above). Mirrors
    // SpawnFleet's ProcessStartInfo shape. Never blocks the UI thread, never throws.
    void ReapStaleFleetRun()
    {
        try
        {
            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-c \"from relay.fleet_reaper import reap_stale_run; reap_stale_run()\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            System.Diagnostics.Process.Start(psi);
        }
        catch (Exception) { }
    }

    bool SpawnFleet(List<string> goals, string goalsFileName, bool planMode = false)
    {
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);
        string goalsFile = Path.Combine(stateDir, goalsFileName);
        File.WriteAllText(goalsFile, GoalsToJsonl(goals), new UTF8Encoding(false));

        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --goals-file \"" + goalsFile + "\""
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort;
        if (planMode) psi.Arguments += " --plan";
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // Render a goals list as the JSONL text SpawnFleet writes to its goals-file: one JSON object
    // per physical line, `{"text":"<goal, JSON-escaped>"}`. Using JavaScriptSerializer (already
    // used elsewhere in this file, e.g. the continuation-goal encoding below) guarantees proper
    // escaping of embedded newlines/quotes/backslashes/control chars, so a multi-line goal string
    // round-trips as ONE goal instead of one bogus goal per source line (fleet_runner._read_goals
    // splits a plain-text goals-file on newlines).
    //
    // A goal that is ALREADY a serialized JSON object (callers that need extra keys beyond "text",
    // e.g. the "続ける"/Continue flow's {"text":...,"resume_conv":...}) is detected and passed
    // through UNCHANGED rather than being wrapped a second time -- double-wrapping would bury the
    // real text/resume_conv keys inside an opaque string and fleet_runner would lose resume_conv.
    string GoalsToJsonl(List<string> goals)
    {
        var sb = new StringBuilder();
        foreach (string g in (goals ?? new List<string>()))
        {
            string trimmed = (g ?? "").Trim();
            string line = null;
            if (trimmed.StartsWith("{"))
            {
                try
                {
                    var parsed = _js.DeserializeObject(trimmed) as Dictionary<string, object>;
                    if (parsed != null) line = trimmed; // already a valid JSON-object goal; pass through as-is
                }
                catch (Exception) { line = null; }
            }
            if (line == null)
            {
                var gd = new Dictionary<string, object>();
                gd["text"] = g ?? "";
                line = _js.Serialize(gd);
            }
            sb.Append(line);
            sb.Append("\n");
        }
        return sb.ToString();
    }

    // P2 RESUME: spawn a fresh fleet with --resume (re-queues the unfinished goals from the durable
    // ledger, per fleet_runner.py). Mirrors SpawnFleet's launch construction but passes --resume
    // INSTEAD of a goals file (the runner reads .fleet/last_run_goals.json / last_run_done.json).
    bool SpawnFleetResume()
    {
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);

        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --resume"
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort;
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // Reimplements fleet_runner._goal_key EXACTLY: sha1 of the goal's UTF-8 bytes AFTER .strip(),
    // hex, first 16 chars. Must match byte-for-byte so the C# unfinished-count joins onto the
    // Python-written done-map. (Python str.strip() removes leading/trailing Unicode whitespace;
    // .NET String.Trim() does the same, so Trim() is the correct analog.)
    static string GoalKey(string text)
    {
        string norm = (text ?? "").Trim();
        byte[] bytes = new UTF8Encoding(false).GetBytes(norm);
        using (var sha = System.Security.Cryptography.SHA1.Create())
        {
            byte[] h = sha.ComputeHash(bytes);
            var sb = new StringBuilder(h.Length * 2);
            foreach (byte b in h) sb.Append(b.ToString("x2"));
            return sb.ToString().Substring(0, 16);
        }
    }

    // Count of goals in the last run's durable ledger that did NOT finish successfully (i.e. whose
    // key is absent from last_run_done.json with a DONE outcome). 0 when there is no ledger. Mirrors
    // fleet_runner's resume semantics: a goal is "done" only when its done-map outcome is DONE.
    int UnfinishedResumeCount()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(_statusPath);
            string goalsPath = Path.Combine(stateDir, "last_run_goals.json");
            if (!File.Exists(goalsPath)) return 0;
            var ledger = _js.DeserializeObject(File.ReadAllText(goalsPath, Encoding.UTF8)) as Dictionary<string, object>;
            if (ledger == null) return 0;
            object goalsObj;
            if (!ledger.TryGetValue("goals", out goalsObj) || !(goalsObj is object[])) return 0;

            // done-map: {goal_key: outcome}; a goal counts finished only when outcome == "DONE".
            var doneKeys = new HashSet<string>();
            string donePath = Path.Combine(stateDir, "last_run_done.json");
            if (File.Exists(donePath))
            {
                var dm = _js.DeserializeObject(File.ReadAllText(donePath, Encoding.UTF8)) as Dictionary<string, object>;
                if (dm != null)
                    foreach (var kv in dm)
                        if (kv.Value != null && kv.Value.ToString() == "DONE") doneKeys.Add(kv.Key);
            }

            int n = 0;
            foreach (object o in (object[])goalsObj)
            {
                var e = o as Dictionary<string, object>;
                if (e == null) continue;
                string text = S(e, "text");
                if (string.IsNullOrEmpty(text)) continue;
                // prefer the ledger's own key (written by Python), fall back to recomputing it
                string key = S(e, "key");
                if (string.IsNullOrEmpty(key)) key = GoalKey(text);
                if (!doneKeys.Contains(key)) n++;
            }
            return n;
        }
        catch (Exception) { return 0; }
    }

    // Fingerprint the exact resume state, not just its count. Dismissing one stale banner must
    // survive restarts, but a genuinely new interrupted run (new ledger/done-map contents) must
    // become visible again. The sidecar is UI-only: it never edits the resume ledger itself.
    string ResumeStateSignature()
    {
        try
        {
            string stateDir = Path.GetDirectoryName(_statusPath);
            string goalsPath = Path.Combine(stateDir, "last_run_goals.json");
            if (!File.Exists(goalsPath)) return "";
            string donePath = Path.Combine(stateDir, "last_run_done.json");
            string payload = File.ReadAllText(goalsPath, Encoding.UTF8) + "\n\0\n"
                           + (File.Exists(donePath) ? File.ReadAllText(donePath, Encoding.UTF8) : "");
            byte[] bytes = new UTF8Encoding(false).GetBytes(payload);
            using (var sha = System.Security.Cryptography.SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(bytes);
                var sb = new StringBuilder();
                foreach (byte b in hash) sb.Append(b.ToString("x2"));
                return sb.ToString().Substring(0, 24);
            }
        }
        catch (Exception) { return ""; }
    }

    bool ResumeStateDismissed(string signature)
    {
        if (string.IsNullOrEmpty(signature)) return false;
        try
        {
            if (!File.Exists(_resumeDismissPath)) return false;
            var data = _js.DeserializeObject(File.ReadAllText(_resumeDismissPath, Encoding.UTF8))
                       as Dictionary<string, object>;
            return data != null && S(data, "signature") == signature;
        }
        catch (Exception) { return false; }
    }

    void DismissResumeState(string signature)
    {
        if (string.IsNullOrEmpty(signature)) return;
        try
        {
            var data = new Dictionary<string, object>();
            data["signature"] = signature; data["dismissed_at"] = NowUnix();
            string tmp = _resumeDismissPath + ".tmp";
            File.WriteAllText(tmp, _js.Serialize(data), new UTF8Encoding(false));
            try { File.Replace(tmp, _resumeDismissPath, null); }
            catch { File.Copy(tmp, _resumeDismissPath, true); try { File.Delete(tmp); } catch { } }
        }
        catch (Exception) { }
        _lastSig = "";
        OnTick(null, null);
    }

    // --- slash-command autocomplete for the goal box (parity with the main chat) ---
    Popup _gcmdPopup; ListBox _gcmdList;
    Popup _helpPopup;   // /help text shown HERE (a compact popup) instead of dumped into the goal box
    static readonly string[][] _goalCommandsJa = {
        new[]{"/help","コマンド一覧を表示"},
        new[]{"/code","<機能> を実装し、pytest テストも書いて通す"},
        new[]{"/fix","<ファイル> の <不具合> を直し、テストを通す"},
        new[]{"/test","<対象> の pytest テストを書く"},
        new[]{"/refactor","<対象> を読みやすくリファクタする(挙動は変えない)"},
        new[]{"/doc","<対象> の README/説明 を書く"},
        new[]{"/review","<対象> をレビューして問題点を箇条書きで挙げる"},
        new[]{"/research","<問い> を Claude で深掘り調査する"},
        new[]{"/effort","推論モードを設定: min|max|ultra|auto"},
        new[]{"/approval","実行方式を設定: run|plan|auto（互換コマンド）"},
    };
    static readonly string[][] _goalCommandsEn = {
        new[]{"/help","Show the command list"},
        new[]{"/code","implement <feature> and write + pass pytest tests"},
        new[]{"/fix","fix <bug> in <file> and make the tests pass"},
        new[]{"/test","write pytest tests for <target>"},
        new[]{"/refactor","refactor <target> for readability (no behavior change)"},
        new[]{"/doc","write the README / docs for <target>"},
        new[]{"/review","review <target> and list the issues as bullets"},
        new[]{"/research","deep-research <question> with Claude"},
        new[]{"/effort","set reasoning mode: min|max|ultra|auto"},
        new[]{"/approval","set approval mode: run|plan|auto"},
    };
    // Localized at access time so the slash palette (and the template it inserts) follows the UI language.
    string[][] _goalCommands { get { return _lang == 0 ? _goalCommandsJa : _goalCommandsEn; } }
    void BuildGoalCmdPopup()
    {
        _gcmdList = new ListBox { MaxHeight = 220, BorderThickness = new Thickness(0) };
        _gcmdList.Background = BtnBg;
        _gcmdList.PreviewMouseLeftButtonUp += delegate { AcceptGoalCommand(); };
        var border = new Border { Child = _gcmdList, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(4) };
        border.Background = BtnBg; border.BorderBrush = Accent;
        _gcmdPopup = new Popup { PlacementTarget = _goalInput, Placement = PlacementMode.Top, StaysOpen = false, Width = 520 };
        _gcmdPopup.Child = border;
    }
    ListBoxItem MakeGCmdItem(string name, string template)
    {
        var sp = new StackPanel { Orientation = Orientation.Horizontal };
        var n = new TextBlock { Text = name, FontWeight = FontWeights.SemiBold, MinWidth = 90, Foreground = Accent };
        var d = new TextBlock { Text = template, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted, TextTrimming = TextTrimming.CharacterEllipsis };
        sp.Children.Add(n); sp.Children.Add(d);
        return new ListBoxItem { Content = sp, Tag = template, Padding = new Thickness(6, 4, 6, 4), Foreground = Fg };
    }
    void CurrentGoalLine(out int lineStart, out string line)
    {
        string txt = _goalInput.Text ?? ""; int caret = _goalInput.CaretIndex;
        if (caret > txt.Length) caret = txt.Length;
        lineStart = caret > 0 ? txt.LastIndexOf('\n', caret - 1) + 1 : 0;
        line = txt.Substring(lineStart, caret - lineStart);
    }
    void UpdateGoalCmdPopup()
    {
        try
        {
            int ls; string line; CurrentGoalLine(out ls, out line);
            if (line.Length >= 1 && line[0] == '/' && line.IndexOf(' ') < 0)
            {
                string pre = line.ToLower();
                _gcmdList.Items.Clear();
                foreach (var c in _goalCommands)
                    if (c[0].StartsWith(pre)) _gcmdList.Items.Add(MakeGCmdItem(c[0], c[1]));
                if (_gcmdList.Items.Count > 0) { _gcmdList.SelectedIndex = 0; _gcmdPopup.IsOpen = true; }
                else _gcmdPopup.IsOpen = false;
            }
            else if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
        }
        catch (Exception) { }
    }
    void AcceptGoalCommand()
    {
        if (_gcmdList.SelectedItem == null && _gcmdList.Items.Count > 0) _gcmdList.SelectedIndex = 0;
        var item = _gcmdList.SelectedItem as ListBoxItem;
        if (item != null)
        {
            string template = item.Tag as string;
            var sp = item.Content as StackPanel;
            string cmdName = (sp != null && sp.Children.Count > 0 && sp.Children[0] is TextBlock)
                ? ((TextBlock)sp.Children[0]).Text : "";

            // /help: show in a compact popup, NOT dumped into the goal box (which would
            // bury the card list). Remove the typed "/help" line and surface the popup.
            if (cmdName == "/help")
            {
                int lsh; string lineh; CurrentGoalLine(out lsh, out lineh);
                string txth = _goalInput.Text ?? "";
                int lineEndH = txth.IndexOf('\n', lsh);
                if (lineEndH < 0) lineEndH = txth.Length;
                _goalInput.Text = txth.Substring(0, lsh) + txth.Substring(lineEndH);
                _goalInput.CaretIndex = lsh;
                if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                ShowGoalHelp();
                return;
            }

            // /effort and /approval: if the current line has an argument, apply it immediately
            // and clear the line. If no arg, insert the template (prompts for value) instead.
            if (cmdName == "/effort" || cmdName == "/approval")
            {
                int ls2; string line2; CurrentGoalLine(out ls2, out line2);
                // line2 looks like "/effort" or "/effort max"
                string[] parts = line2.Trim().Split(new char[]{' '}, 2, StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length >= 2)
                {
                    string argVal = parts[1].Trim().ToLower();
                    bool applied = false;
                    if (cmdName == "/effort")
                    {
                        if (argVal == "min" || argVal == "max" || argVal == "ultra" || argVal == "auto")
                        {
                            _effort = argVal;
                            SaveKey("effort", _effort);
                            PaintEffort();
                            applied = true;
                            if (_startNote != null) _startNote.Text = (_lang == 0 ? "推論モード→ " : "Effort set to ") + _effort;
                        }
                    }
                    else  // /approval
                    {
                        if (argVal == "run" || argVal == "plan" || argVal == "auto")
                        {
                            _approval = argVal;
                            SaveKey("approval", _approval);
                            PaintApproval();
                            applied = true;
                            if (_startNote != null) _startNote.Text = (_lang == 0 ? "実行方式→ " : "Run mode set to ") + _approval;
                        }
                    }
                    if (applied)
                    {
                        // Remove the slash-command line from the input
                        string txt2 = _goalInput.Text ?? "";
                        int caret2 = _goalInput.CaretIndex;
                        if (caret2 > txt2.Length) caret2 = txt2.Length;
                        // Find end of the line (up to next \n or end of string)
                        int lineEnd = txt2.IndexOf('\n', ls2);
                        if (lineEnd < 0) lineEnd = txt2.Length;
                        _goalInput.Text = txt2.Substring(0, ls2) + txt2.Substring(lineEnd);
                        _goalInput.CaretIndex = ls2;
                        if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                        _goalInput.Focus();
                        return;
                    }
                }
                // No valid arg: insert template so user can type the value
                // template is the description string — instead insert the command name + space
                int ls3; string line3; CurrentGoalLine(out ls3, out line3);
                string txt3 = _goalInput.Text ?? ""; int caret3 = _goalInput.CaretIndex;
                if (caret3 > txt3.Length) caret3 = txt3.Length;
                string insert = cmdName + " ";
                _goalInput.Text = txt3.Substring(0, ls3) + insert + txt3.Substring(caret3);
                _goalInput.CaretIndex = ls3 + insert.Length;
                if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
                _goalInput.Focus();
                return;
            }

            // Default: replace current line with the template text
            int ls; string line; CurrentGoalLine(out ls, out line);
            string txt = _goalInput.Text ?? ""; int caret = _goalInput.CaretIndex;
            if (caret > txt.Length) caret = txt.Length;
            _goalInput.Text = txt.Substring(0, ls) + template + txt.Substring(caret);
            _goalInput.CaretIndex = ls + template.Length;
        }
        if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
        _goalInput.Focus();
    }
    string GoalHelpText()
    {
        // Structured (short lines, each wraps cleanly) instead of one long paragraph whose tail
        // was clipped at the popup's right edge. Commands, reasoning and approval are split out so
        // no single line runs long enough to need the old fixed 520px width.
        if (_lang != 0)
            return "Commands\n"
                + "/code <feature> - implement + pytest tests\n"
                + "/fix <target> - fix a bug + verify\n"
                + "/test <target> - add pytest tests\n"
                + "/refactor <target> - tidy without changing behavior\n"
                + "/doc <target> - write README / docs\n"
                + "/review <target> - review and list issues\n"
                + "/research <question> - deep research\n"
                + "\nReasoning (top bar)\n"
                + "min - fastest, least reasoning\n"
                + "max - deepest reasoning\n"
                + "ultra - max + extra checks\n"
                + "auto - pick per task\n"
                + "\nRun mode (top bar)\n"
                + "run - run now\n"
                + "plan - wait for plan approval\n"
                + "auto - plain fleet waits for plan approval\n"
                + "(folder autonomy uses GO / ASK / STOP)";
        return "コマンド\n"
            + "/code <機能> - 実装と pytest テスト\n"
            + "/fix <対象> - 不具合修正と検証\n"
            + "/test <対象> - pytest テスト追加\n"
            + "/refactor <対象> - 挙動を変えず整理\n"
            + "/doc <対象> - README/説明を書く\n"
            + "/review <対象> - 問題点レビュー\n"
            + "/research <問い> - 深掘り調査\n"
            + "\n推論（上部設定）\n"
            + "min - 最速・推論最小\n"
            + "max - 最も深い推論\n"
            + "ultra - max + 追加チェック\n"
            + "auto - タスクに応じ自動\n"
            + "\n実行方式（上部設定）\n"
            + "run - 即実行\n"
            + "plan - 計画承認待ち\n"
            + "auto - 通常フリートは計画承認待ち\n"
            + "（自律コーディング(フォルダ)は GO / ASK / STOP）";
    }

    // /help must NOT be dumped into the goal box (it turns the input into a giant scroll
    // region that covers the card list). Show it in a compact, dismissable popup anchored
    // to the goal input instead. Reuses the same popup styling as the slash palette.
    void ShowGoalHelp()
    {
        try
        {
            if (_goalInput == null) return;
            // Close every other floating popup/dropdown first (header settings/overflow/effort/
            // approval + the slash palette) so help is the only one showing; keep help itself open.
            CloseHeaderPopups("help");

            var txt = new TextBlock();
            txt.Text = GoalHelpText();
            txt.TextWrapping = TextWrapping.Wrap;
            txt.Foreground = Fg;
            txt.FontSize = 12;
            txt.LineHeight = 18;

            var sv = new ScrollViewer();
            sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
            sv.MaxHeight = 260;
            sv.Content = txt;

            var border = new Border();
            border.Child = sv;
            border.BorderThickness = new Thickness(1);
            border.CornerRadius = new CornerRadius(8);
            border.Padding = new Thickness(12, 10, 12, 10);
            border.Background = BtnBg;
            border.BorderBrush = Accent;

            // No fixed Width (the old 520 clipped the long approval line at the right edge). Let the
            // border size to content within a min/max band; each line wraps (txt.TextWrapping=Wrap).
            border.MinWidth = 360;
            border.MaxWidth = 560;
            if (_helpPopup == null)
                _helpPopup = new Popup { PlacementTarget = _goalInput, Placement = PlacementMode.Top, StaysOpen = false };
            _helpPopup.Child = border;
            _helpPopup.IsOpen = true;
        }
        catch (Exception) { }
    }

    // ③ Claude-Code-style: pick a folder + a plain-language instruction -> code_task runs
    // it as ONE self-verifying task. It auto-detects how to verify (pytest if there is a
    // test suite, else compile; npm test for Node) and only accepts DONE once that
    // actually passes. No per-file fan-out, no goals to review -- you say it, it runs.
    void FolderToGoals()
    {
        try
        {
            // Modern Explorer-style picker (the old WinForms tree dialog is clunky): the
            // user browses to the target folder and picks ANY file in it; we use its
            // parent folder. Reliable + no COM.
            var ofd = new Microsoft.Win32.OpenFileDialog();
            ofd.Title = _lang == 0 ? "対象フォルダ内の任意のファイルを選択（その親フォルダが対象になります）"
                                   : "Pick ANY file inside the target folder (its parent folder is used)";
            ofd.Filter = _lang == 0 ? "すべてのファイル|*.*" : "All files|*.*";
            ofd.CheckFileExists = true;
            if (ofd.ShowDialog() != true) return;
            string folder = Path.GetDirectoryName(ofd.FileName);
            if (string.IsNullOrEmpty(folder)) return;
            string instr = PromptInstruction();
            if (string.IsNullOrEmpty(instr)) return;

            // Autonomy Contract pre-flight (folder path only — quick Start is unchanged).
            if (!ShowAutonomyContract(folder, instr)) return;

            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);

            // code_task: natural language in, auto-verified autonomous run out. Writes the
            // same status.json this cockpit already tails, so the task shows up live.
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            if (_approval == "auto")
                psi.Arguments = "-m relay.overnight_task -i \"" + instr.Replace("\"", "'")
                    + "\" -f \"" + folder + "\" --state-dir \"" + stateDir + "\""
                    + " --hours 8 --auto-mode --effort " + _effort;
            else
                psi.Arguments = "-m relay.code_task -i \"" + instr.Replace("\"", "'")
                    + "\" -f \"" + folder + "\" --state-dir \"" + stateDir + "\" --effort " + _effort
                    + (_approval == "plan" ? " --plan" : "");
            psi.WorkingDirectory = repo; psi.UseShellExecute = false; psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }

            var p = new System.Diagnostics.Process();
            p.StartInfo = psi;
            p.Start();
            _startNote.Text = _lang == 0
                ? (_approval == "auto" ? "自動承認ゲート付き長時間タスクを開始。ASK/STOPなら実行せずレポートします。"
                   : _approval == "plan" ? "コーディング(計画モード)を開始。計画提示後「承認待ち」になります。カードに承認/修正を steer で送ってください。"
                   : "コーディングタスクを開始（検証方法は自動検出。テスト/コンパイルが通るまで完了しません）。")
                : (_approval == "auto" ? "Started long task with auto approval gate. ASK/STOP writes a report without launching."
                   : _approval == "plan" ? "Coding (plan mode) started -> it will pause at 承認待ち; steer the card to approve/edit."
                   : "Coding task started (auto-verified; not done until tests/compile pass).");
        }
        catch (Exception ex)
        {
            _startNote.Text = (_lang == 0 ? "フォルダ処理失敗: " : "Folder error: ") + ex.Message;
        }
    }

    string PromptInstruction()
    {
        var w = new Window();
        w.Title = _lang == 0 ? "フォルダへの指示" : "Instruction";
        w.Width = 480; w.Height = 230; w.Background = Bg;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        var sp = new StackPanel(); sp.Margin = new Thickness(16);
        var lbl = new TextBlock();
        lbl.Text = _lang == 0 ? "各ファイルに適用する指示（編集はMCPツールで実行されます）:"
                              : "Instruction applied to each file (edits run via MCP tools):";
        lbl.Foreground = Fg; lbl.TextWrapping = TextWrapping.Wrap; lbl.Margin = new Thickness(0, 0, 0, 8);
        sp.Children.Add(lbl);
        var tb = new TextBox();
        tb.MinHeight = 64; tb.AcceptsReturn = true; tb.TextWrapping = TextWrapping.Wrap;
        tb.Background = BtnBg; tb.Foreground = Fg; tb.BorderBrush = Border;
        tb.BorderThickness = new Thickness(1); tb.Padding = new Thickness(8); tb.CaretBrush = Fg;
        sp.Children.Add(tb);
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right; btns.Margin = new Thickness(0, 12, 0, 0);
        string[] box = new string[1];
        var ok = new Button();
        ok.Content = _lang == 0 ? "生成" : "Generate"; ok.IsDefault = true;
        ok.Background = Accent; ok.Foreground = White; ok.BorderThickness = new Thickness(0);
        ok.Padding = new Thickness(14, 4, 14, 4); ok.Cursor = Cursors.Hand; ok.FontWeight = FontWeights.SemiBold;
        ok.Click += delegate { box[0] = tb.Text; w.DialogResult = true; };
        var cancel = new Button();
        cancel.Content = _lang == 0 ? "取消" : "Cancel"; cancel.IsCancel = true;
        cancel.Margin = new Thickness(8, 0, 0, 0); cancel.Padding = new Thickness(14, 4, 14, 4);
        cancel.Cursor = Cursors.Hand; cancel.Background = BtnBg; cancel.Foreground = Fg;
        cancel.BorderBrush = Border; cancel.BorderThickness = new Thickness(1);
        btns.Children.Add(ok); btns.Children.Add(cancel);
        sp.Children.Add(btns);
        w.Content = sp;
        bool? r = w.ShowDialog();
        return (r == true) ? box[0] : null;
    }

    // Small modal text-input dialog for the "続ける" (Continue) follow-up instruction on a
    // finished history row. Modeled on PromptInstruction(): same Window pattern, Owner=this,
    // theme brushes, OK+Cancel. Returns the typed string, or null on empty/cancel.
    string PromptFollowup()
    {
        var w = new Window();
        w.Title = _lang == 0 ? "続けて指示する" : "Continue this task";
        w.Width = 480; w.Height = 230; w.Background = Bg;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        var sp = new StackPanel(); sp.Margin = new Thickness(16);
        var lbl = new TextBlock();
        lbl.Text = _lang == 0 ? "前回の成果物を踏まえて追加で何をさせますか？"
                              : "Follow-up instruction (uses the prior task's saved outputs):";
        lbl.Foreground = Fg; lbl.TextWrapping = TextWrapping.Wrap; lbl.Margin = new Thickness(0, 0, 0, 8);
        sp.Children.Add(lbl);
        var tb = new TextBox();
        tb.MinHeight = 64; tb.AcceptsReturn = true; tb.TextWrapping = TextWrapping.Wrap;
        tb.Background = BtnBg; tb.Foreground = Fg; tb.BorderBrush = Border;
        tb.BorderThickness = new Thickness(1); tb.Padding = new Thickness(8); tb.CaretBrush = Fg;
        sp.Children.Add(tb);
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right; btns.Margin = new Thickness(0, 12, 0, 0);
        string[] box = new string[1];
        var ok = new Button();
        ok.Content = _lang == 0 ? "続ける" : "Continue"; ok.IsDefault = true;
        ok.Background = Accent; ok.Foreground = White; ok.BorderThickness = new Thickness(0);
        ok.Padding = new Thickness(14, 4, 14, 4); ok.Cursor = Cursors.Hand; ok.FontWeight = FontWeights.SemiBold;
        ok.Click += delegate { box[0] = tb.Text; w.DialogResult = true; };
        var cancel = new Button();
        cancel.Content = _lang == 0 ? "取消" : "Cancel"; cancel.IsCancel = true;
        cancel.Margin = new Thickness(8, 0, 0, 0); cancel.Padding = new Thickness(14, 4, 14, 4);
        cancel.Cursor = Cursors.Hand; cancel.Background = BtnBg; cancel.Foreground = Fg;
        cancel.BorderBrush = Border; cancel.BorderThickness = new Thickness(1);
        btns.Children.Add(ok); btns.Children.Add(cancel);
        sp.Children.Add(btns);
        w.Content = sp;
        bool? r = w.ShowDialog();
        if (r != true) return null;
        string txt = box[0];
        if (txt == null || txt.Trim().Length == 0) return null;
        return txt;
    }

    // Build the FRESH goal text for a "Continue" run. There is NO stable reopenable URL for this
    // agent, so we do NOT reopen the old conversation: instead we PREPEND the prior task's context
    // and tell the agent to re-read its on-disk outputs before doing the new instruction.
    string BuildContinueGoal(string priorGoal, string followup)
    {
        if (_lang == 0)
        {
            return "【前回タスクの続き】\n前回のゴール: " + priorGoal
                + "\n前回の成果物はディスク上に保存済み（このタスクで作成・更新したファイル/フォルダ）。まず関連するファイル/フォルダを list_directory / read_file で読み直して前回の文脈と成果物を把握してから、次の追加指示を実行して。完了したら最後の行に DONE。\n追加指示: "
                + followup;
        }
        return "[Continuation of a prior task]\nPrior goal: " + priorGoal
            + "\nThe prior task's outputs are saved on disk (files/folders this task created or updated). First re-read the relevant files/folders with list_directory / read_file to recover the prior context and outputs, then carry out the follow-up instruction below. When finished, put DONE on the last line.\nFollow-up: "
            + followup;
    }

    // ── Autonomy Contract pre-flight dialog (folder autonomous path only) ─────────
    // Shows a modal summary of what the agent will and will not do before the run
    // launches. Returns true if the user clicks Delegate, false if they Cancel.
    // REAL fields are shown plainly; FUTURE/unimplemented items are labeled with ⚠.
    bool ShowAutonomyContract(string folder, string instruction)
    {
        bool ja = _lang == 0;

        var w = new Window();
        w.Title = ja ? "自律委任 — 確認" : "Autonomy Contract";
        w.Width = 540;
        w.SizeToContent = SizeToContent.Height;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner;
        w.Owner = this;
        w.Background = Theme.Br(Theme.Surface(_dark));
        w.ResizeMode = ResizeMode.NoResize;

        var outer = new StackPanel();
        outer.Margin = new Thickness(0);

        // ── Title bar band ────────────────────────────────────────────────────────
        var titleBand = new Border();
        titleBand.Background = Theme.Br(Theme.Bg(_dark));
        titleBand.BorderBrush = Theme.Br(Theme.Border(_dark));
        titleBand.BorderThickness = new Thickness(0, 0, 0, 1);
        titleBand.Padding = new Thickness(24, 16, 24, 16);
        var titleTb = new TextBlock();
        titleTb.Text = ja ? "AUTONOMY CONTRACT / 自律委任" : "AUTONOMY CONTRACT / 自律委任";
        titleTb.FontSize = 11;
        titleTb.FontWeight = FontWeights.SemiBold;
        titleTb.Foreground = Theme.Br(Theme.Muted(_dark));
        titleBand.Child = titleTb;
        outer.Children.Add(titleBand);

        // ── Body ──────────────────────────────────────────────────────────────────
        var body = new StackPanel();
        body.Margin = new Thickness(24, 20, 24, 8);

        // Helper: section label (small-caps style via all-upper + Muted + small font)
        // C#5: no local methods, use a Func<> delegate
        Func<string, TextBlock> makeSectionLabel = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt.ToUpperInvariant();
            tb2.FontSize = 10;
            tb2.FontWeight = FontWeights.SemiBold;
            tb2.Foreground = Theme.Br(Theme.Muted(_dark));
            tb2.Margin = new Thickness(0, 0, 0, 3);
            return tb2;
        };

        // Helper: value text (body size, Text color)
        Func<string, TextBlock> makeValue = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt;
            tb2.FontSize = 13;
            tb2.Foreground = Theme.Br(Theme.Text(_dark));
            tb2.TextWrapping = TextWrapping.Wrap;
            return tb2;
        };

        // Helper: a [FUTURE] caveat row with ⚠ prefix (Warning color, smaller)
        Func<string, TextBlock> makeFutureNote = delegate(string txt)
        {
            var tb2 = new TextBlock();
            tb2.Text = txt;
            tb2.FontSize = 11;
            tb2.Foreground = Theme.Br(Theme.Warning(_dark));
            tb2.TextWrapping = TextWrapping.Wrap;
            tb2.Margin = new Thickness(0, 2, 0, 0);
            return tb2;
        };

        // Helper: thin rule between sections
        Func<Border> makeRule = delegate()
        {
            var sep = new Border();
            sep.Height = 1;
            sep.Background = Theme.Br(Theme.Border(_dark));
            sep.Margin = new Thickness(0, 14, 0, 14);
            return sep;
        };

        // ── 1. Directive [REAL] ───────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "指示 / Directive" : "Directive / 指示"));
        var directiveBorder = new Border();
        directiveBorder.BorderBrush = Theme.Br(Theme.Border(_dark));
        directiveBorder.BorderThickness = new Thickness(1);
        directiveBorder.CornerRadius = new CornerRadius(6);
        directiveBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        directiveBorder.Padding = new Thickness(10, 8, 10, 8);
        directiveBorder.Margin = new Thickness(0, 0, 0, 0);
        var directiveTb = new TextBlock();
        directiveTb.Text = instruction;
        directiveTb.FontSize = 13;
        directiveTb.Foreground = Theme.Br(Theme.Text(_dark));
        directiveTb.TextWrapping = TextWrapping.Wrap;
        directiveBorder.Child = directiveTb;
        body.Children.Add(directiveBorder);
        body.Children.Add(makeRule());

        // ── 2. Scope [参考 / not enforced] ────────────────────────────────────────
        // The folder Scope is INFORMATIONAL only. tools/contract_gate.py keeps it on the contract
        // for context, but check_op() inspects op_class alone -- it never tests whether a path is
        // inside this folder -- so the folder boundary is NOT enforced. Label it honestly so the
        // dialog doesn't imply a guard that doesn't exist (op-class gate below stays [REAL]).
        body.Children.Add(makeSectionLabel(ja ? "作業対象 / Target" : "Target / 作業対象"));
        body.Children.Add(makeValue(folder));
        var scopeTag = new TextBlock();
        scopeTag.Text = ja ? "[参考表示 — フォルダ境界は強制されません / not enforced]"
                           : "[reference only — folder boundary not enforced / 参考]";
        scopeTag.FontSize = 10;
        scopeTag.Foreground = Theme.Br(Theme.Faint(_dark));
        scopeTag.TextWrapping = TextWrapping.Wrap;
        scopeTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(scopeTag);
        body.Children.Add(makeRule());

        // ── 3. Allowed [REAL] ─────────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "許可 / Allowed" : "Allowed / 許可"));
        body.Children.Add(makeValue(
            ja ? "ファイルの編集・コマンド実行・テスト実行 / edit files, run commands, run tests"
               : "edit files · run commands · run tests"));
        var allowedSub = new TextBlock();
        allowedSub.Text = ja ? "(relayのデフォルト実行権限 — from relay permission policy)"
                             : "(relay default capability — from relay permission policy)";
        allowedSub.FontSize = 11;
        allowedSub.Foreground = Theme.Br(Theme.Muted(_dark));
        allowedSub.TextWrapping = TextWrapping.Wrap;
        allowedSub.Margin = new Thickness(0, 2, 0, 0);
        body.Children.Add(allowedSub);
        var allowedTag = new TextBlock();
        allowedTag.Text = "[REAL]";
        allowedTag.FontSize = 10;
        allowedTag.Foreground = Theme.Br(Theme.Faint(_dark));
        allowedTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(allowedTag);
        body.Children.Add(makeRule());

        // ── 4. Ask before [REAL — tool gate enforces these op-classes] ──────────
        body.Children.Add(makeSectionLabel(ja ? "確認してから / Ask before" : "Ask before / 確認してから"));
        var askNote = new TextBlock();
        askNote.Text = ja ? "(対応ツール経由の操作だけ、ツールゲートが実行前に一時停止して確認します — only ops routed through supported tools are checked)"
                          : "(tool gate pauses for approval only on ops routed through supported tools — not a blanket guard)";
        askNote.FontSize = 11;
        askNote.Foreground = Theme.Br(Theme.Muted(_dark));
        askNote.TextWrapping = TextWrapping.Wrap;
        askNote.Margin = new Thickness(0, 0, 0, 6);
        body.Children.Add(askNote);

        // Three checkboxes — all checked by default
        var cbDelete = new CheckBox();
        cbDelete.Content = ja ? "対応ツール経由のファイル削除 / Delete files (via supported tools)"
                              : "Delete files via supported tools / 対応ツール経由のファイル削除";
        cbDelete.IsChecked = true;
        cbDelete.Foreground = Theme.Br(Theme.Text(_dark));
        cbDelete.FontSize = 13;
        cbDelete.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbDelete);

        var cbOutbound = new CheckBox();
        cbOutbound.Content = ja ? "対応ツール経由のOutlook送信 / Outlook sends (via supported tools)"
                                : "Outlook sends via supported tools / 対応ツール経由のOutlook送信";
        cbOutbound.IsChecked = true;
        cbOutbound.Foreground = Theme.Br(Theme.Text(_dark));
        cbOutbound.FontSize = 13;
        cbOutbound.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbOutbound);

        var cbShell = new CheckBox();
        cbShell.Content = ja ? "対応ツール経由の破壊的shell / Destructive shell (via supported tools)"
                             : "Destructive shell via supported tools / 対応ツール経由の破壊的shell";
        cbShell.IsChecked = true;
        cbShell.Foreground = Theme.Br(Theme.Text(_dark));
        cbShell.FontSize = 13;
        cbShell.Margin = new Thickness(0, 2, 0, 2);
        body.Children.Add(cbShell);

        var askTag = new TextBlock();
        askTag.Text = "[REAL — enforced via tool gate, supported tools only]";
        askTag.FontSize = 10;
        askTag.Foreground = Theme.Br(Theme.Faint(_dark));
        askTag.Margin = new Thickness(0, 3, 0, 0);
        body.Children.Add(askTag);
        body.Children.Add(makeRule());

        // ── 5. Stop when: turn budget [REAL — relay enforces budget_turns] ────
        body.Children.Add(makeSectionLabel(ja ? "停止条件 / Stop when" : "Stop when / 停止条件"));
        var budgetNote = new TextBlock();
        budgetNote.Text = ja ? "(ターン上限はrelayが強制します。0 = 上限なし — relay enforces budget; 0 = no budget)"
                             : "(turn budget is enforced by the relay — 0 means no budget)";
        budgetNote.FontSize = 11;
        budgetNote.Foreground = Theme.Br(Theme.Muted(_dark));
        budgetNote.TextWrapping = TextWrapping.Wrap;
        budgetNote.Margin = new Thickness(0, 0, 0, 6);
        body.Children.Add(budgetNote);

        var budgetRow = new StackPanel();
        budgetRow.Orientation = Orientation.Horizontal;
        budgetRow.Margin = new Thickness(0, 0, 0, 2);
        var budgetLbl = new TextBlock();
        budgetLbl.Text = ja ? "ターン上限 / Turn budget: " : "ターン上限 / Turn budget: ";
        budgetLbl.FontSize = 13;
        budgetLbl.Foreground = Theme.Br(Theme.Text(_dark));
        budgetLbl.VerticalAlignment = VerticalAlignment.Center;
        budgetRow.Children.Add(budgetLbl);

        var budgetBox = new TextBox();
        budgetBox.Text = "200";
        budgetBox.Width = 70;
        budgetBox.FontSize = 13;
        budgetBox.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        budgetBox.Foreground = Theme.Br(Theme.Text(_dark));
        budgetBox.BorderBrush = Theme.Br(Theme.Border(_dark));
        budgetBox.BorderThickness = new Thickness(1);
        budgetBox.Padding = new Thickness(6, 2, 6, 2);
        budgetBox.VerticalAlignment = VerticalAlignment.Center;
        budgetBox.ToolTip = ja ? "0 = 上限なし / 0 = no budget" : "0 = no budget";
        budgetRow.Children.Add(budgetBox);

        var budgetZeroLbl = new TextBlock();
        budgetZeroLbl.Text = ja ? "  (0 = 上限なし)" : "  (0 = no budget)";
        budgetZeroLbl.FontSize = 11;
        budgetZeroLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        budgetZeroLbl.VerticalAlignment = VerticalAlignment.Center;
        budgetRow.Children.Add(budgetZeroLbl);
        body.Children.Add(budgetRow);

        var budgetTag = new TextBlock();
        budgetTag.Text = "[REAL — enforced by relay per-worker turn count]";
        budgetTag.FontSize = 10;
        budgetTag.Foreground = Theme.Br(Theme.Faint(_dark));
        budgetTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(budgetTag);
        body.Children.Add(makeRule());

        // ── 6. Acceptance [REAL/partial] ─────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "受入条件 / Acceptance" : "Acceptance / 受入条件"));
        body.Children.Add(makeValue(ja ? "指定なし / none specified" : "none specified (no checks[] provided)"));
        var acceptTag = new TextBlock();
        acceptTag.Text = "[REAL/partial — checks[] from goal, if any]";
        acceptTag.FontSize = 10;
        acceptTag.Foreground = Theme.Br(Theme.Faint(_dark));
        acceptTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(acceptTag);
        body.Children.Add(makeRule());

        // ── 7. Report [REAL] ─────────────────────────────────────────────────────
        body.Children.Add(makeSectionLabel(ja ? "報告 / Report" : "Report / 報告"));
        body.Children.Add(makeValue(ja ? "完了時に要約と最終更新 / summary + last update on completion"
                                      : "summary + last update on completion"));
        var reportSub = new TextBlock();
        reportSub.Text = ja ? "(outcome + transcript末尾からカードに表示)"
                            : "(outcome + last transcript excerpt shown on card)";
        reportSub.FontSize = 11;
        reportSub.Foreground = Theme.Br(Theme.Muted(_dark));
        reportSub.TextWrapping = TextWrapping.Wrap;
        reportSub.Margin = new Thickness(0, 2, 0, 0);
        body.Children.Add(reportSub);
        var reportTag = new TextBlock();
        reportTag.Text = "[REAL]";
        reportTag.FontSize = 10;
        reportTag.Foreground = Theme.Br(Theme.Faint(_dark));
        reportTag.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(reportTag);
        body.Children.Add(makeRule());

        // ── 8. Effort / Approval (read-only display, header dropdowns are authoritative) ──
        var effortRow = new StackPanel();
        effortRow.Orientation = Orientation.Horizontal;
        effortRow.Margin = new Thickness(0, 0, 0, 4);
        var effortLbl = new TextBlock();
        effortLbl.Text = (ja ? "推論 / Effort: " : "Effort: ");
        effortLbl.FontSize = 12;
        effortLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        effortLbl.VerticalAlignment = VerticalAlignment.Center;
        effortRow.Children.Add(effortLbl);
        var effortVal = new TextBlock();
        effortVal.Text = _effort;
        effortVal.FontSize = 12;
        effortVal.FontWeight = FontWeights.SemiBold;
        effortVal.Foreground = Theme.Br(Theme.Text(_dark));
        effortVal.VerticalAlignment = VerticalAlignment.Center;
        effortVal.Margin = new Thickness(4, 0, 24, 0);
        effortRow.Children.Add(effortVal);
        var approvalLbl = new TextBlock();
        approvalLbl.Text = (ja ? "実行方式 / Run mode: " : "Run mode: ");
        approvalLbl.FontSize = 12;
        approvalLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        approvalLbl.VerticalAlignment = VerticalAlignment.Center;
        effortRow.Children.Add(approvalLbl);
        var approvalVal = new TextBlock();
        approvalVal.Text = _approval;
        approvalVal.FontSize = 12;
        approvalVal.FontWeight = FontWeights.SemiBold;
        approvalVal.Foreground = Theme.Br(Theme.Text(_dark));
        approvalVal.VerticalAlignment = VerticalAlignment.Center;
        approvalVal.Margin = new Thickness(4, 0, 0, 0);
        effortRow.Children.Add(approvalVal);
        body.Children.Add(effortRow);
        var effortNote = new TextBlock();
        effortNote.Text = ja ? "(ヘッダーのドロップダウンが主設定 / header dropdowns are authoritative)"
                             : "(header dropdowns are authoritative — shown here for reference)";
        effortNote.FontSize = 10;
        effortNote.Foreground = Theme.Br(Theme.Faint(_dark));
        effortNote.Margin = new Thickness(0, 1, 0, 0);
        body.Children.Add(effortNote);

        outer.Children.Add(body);

        // ── Footer: Cancel + Delegate buttons ────────────────────────────────────
        var footerBand = new Border();
        footerBand.BorderBrush = Theme.Br(Theme.Border(_dark));
        footerBand.BorderThickness = new Thickness(0, 1, 0, 0);
        footerBand.Background = Theme.Br(Theme.Bg(_dark));
        footerBand.Padding = new Thickness(24, 14, 24, 14);

        var footerRow = new StackPanel();
        footerRow.Orientation = Orientation.Horizontal;
        footerRow.HorizontalAlignment = HorizontalAlignment.Right;

        bool[] delegated = new bool[1];
        delegated[0] = false;

        var cancelBtn = new Button();
        cancelBtn.Content = ja ? "キャンセル / Cancel" : "Cancel";
        cancelBtn.IsCancel = true;
        cancelBtn.Height = Theme.BtnH;
        cancelBtn.Padding = new Thickness(16, 0, 16, 0);
        cancelBtn.Background = Brushes.Transparent;
        cancelBtn.Foreground = Theme.Br(Theme.Text(_dark));
        cancelBtn.BorderBrush = Theme.Br(Theme.Border(_dark));
        cancelBtn.BorderThickness = new Thickness(1);
        cancelBtn.Cursor = Cursors.Hand;
        cancelBtn.Margin = new Thickness(0, 0, 10, 0);
        cancelBtn.Click += delegate { w.DialogResult = false; };
        footerRow.Children.Add(cancelBtn);

        var delegateBtn = new Button();
        delegateBtn.Content = ja ? "委任する →" : "Delegate →";
        delegateBtn.IsDefault = true;
        delegateBtn.Height = Theme.BtnH;
        delegateBtn.Padding = new Thickness(20, 0, 20, 0);
        delegateBtn.Background = Theme.Br(Theme.Accent(_dark));
        delegateBtn.Foreground = new SolidColorBrush(C("#FFFFFF"));
        delegateBtn.BorderThickness = new Thickness(0);
        delegateBtn.FontWeight = FontWeights.SemiBold;
        delegateBtn.Cursor = Cursors.Hand;
        delegateBtn.Click += delegate { delegated[0] = true; w.DialogResult = true; };
        footerRow.Children.Add(delegateBtn);

        footerBand.Child = footerRow;
        outer.Children.Add(footerBand);

        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.MaxHeight = 680;
        scroll.Content = outer;
        w.Content = scroll;

        bool? result = w.ShowDialog();
        bool didDelegate = result == true && delegated[0];
        if (didDelegate)
        {
            // Write active_contract.json with user-chosen op-class gates + turn budget.
            // ask_before = only the op-classes whose checkbox was checked (unchecked = not gated).
            // stop_when = ["budget"] when budgetTurns > 0, else [].
            // budget_turns = the integer value from the text box (0 = no budget).
            try
            {
                string contractDir = Path.GetDirectoryName(_statusPath);
                string contractPath = Path.Combine(contractDir, "active_contract.json");
                long epochSec = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                string escapedFolder = folder.Replace("\\", "\\\\").Replace("\"", "\\\"");

                // Build ask_before array from checkboxes
                var askBefore = new System.Collections.Generic.List<string>();
                if (cbDelete.IsChecked == true) askBefore.Add("\"delete\"");
                if (cbOutbound.IsChecked == true) askBefore.Add("\"outbound\"");
                if (cbShell.IsChecked == true) askBefore.Add("\"shell_destructive\"");
                string askBeforeJson = "[" + string.Join(",", askBefore.ToArray()) + "]";

                // Parse turn budget (default 0 on invalid input)
                int budgetTurns = 0;
                int parsedBudget;
                if (int.TryParse(budgetBox.Text, out parsedBudget) && parsedBudget >= 0)
                    budgetTurns = parsedBudget;
                string stopWhenJson = budgetTurns > 0 ? "[\"budget\"]" : "[]";

                string contractJson = "{\"active\":true,\"scope\":\"" + escapedFolder
                    + "\",\"ask_before\":" + askBeforeJson
                    + ",\"stop_when\":" + stopWhenJson
                    + ",\"budget_turns\":" + budgetTurns
                    + ",\"started\":" + epochSec + "}";
                File.WriteAllText(contractPath, contractJson, new UTF8Encoding(false));
            }
            catch (Exception) { }
        }
        return didDelegate;
    }

    System.Windows.Controls.Primitives.Popup _settingsPopup;
    Button _gearBtn;
    Button _maxMinus, _maxPlus;
    ComboBox _effortBox;
    ComboBox _approvalBox;
    TextBlock _effortLbl;
    TextBlock _approvalLbl;
    Button _pauseBtn, _stopBtn;
    System.Windows.Shapes.Path _pauseIcon, _stopIcon;   // drawn geometry (no font glyph needed)
    Button _autoToggle;
    Button _autoMinus, _autoPlus;
    TextBlock _autoLbl, _autoValue;

    // 詳細設定 / Advanced (settings panel) -- アクセス範囲 (folder_access.json) +
    // 接続クライアント (.unlock_state.json via tools.security's grant_ip/revoke_ip). Rebuilt
    // fresh every time the gear popup opens, same as the rest of BuildSettingsPanel.
    Button _accessFullBtn, _accessRestrictedBtn;
    StackPanel _folderRowsPanel;
    StackPanel _clientRowsPanel;

    // Autoscale toggle for the Settings panel. Autoscale, start tabs and ceiling are one concept,
    // so they live together instead of splitting one setting between the header and gear menu.
    UIElement AutoscaleControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 2, 0, 4);
        group.ToolTip = _lang == 0
            ? "空きRAMに応じて並列タブ数を開始値から上限まで自動調整します。"
            : "Automatically adjust parallel tabs from the start value up to the ceiling based on free RAM.";

        // a. autoscale ON/OFF toggle (simple themed button whose label flips)
        _autoToggle = new Button();
        _autoToggle.ToolTip = _lang == 0 ? "RAM空きに応じて並列タブ数を自動調整 (ON/OFF)" : "Auto-adjust parallel tabs to free RAM (ON/OFF)";
        _autoToggle.Cursor = Cursors.Hand; _autoToggle.BorderThickness = new Thickness(1);
        _autoToggle.Padding = new Thickness(10, 3, 10, 3); _autoToggle.FontSize = 12;
        _autoToggle.FontWeight = FontWeights.SemiBold;
        _autoToggle.Margin = new Thickness(0);
        _autoToggle.VerticalAlignment = VerticalAlignment.Center;
        _autoToggle.Click += delegate
        {
            _autoscale = !_autoscale;
            SaveKey("autoscale", _autoscale ? "1" : "0");
            PaintAutoToggle();
            UpdateAutoEnabled();
            // live-apply: reflect the new mode into a running fleet immediately.
            if (_autoscale) RequestSetAutoscaleIfLive();
            else RequestSetMaxtabsIfLive();
        };
        group.Children.Add(_autoToggle);

        // Paint it NOW, not only from the Click handler. The settings panel rebuilds this
        // control every time it opens, and PaintAutoToggle is what sets the button's label and
        // colours -- so until the user clicked it the toggle rendered as an empty pill with no
        // text, giving no clue whether RAM auto-adjust was on or off. Clicking to find out also
        // flipped the setting, which is the opposite of what someone checking the state wants.
        PaintAutoToggle();
        UpdateAutoEnabled();

        return group;
    }

    // Ceiling (上限) stepper -- mirrors MaxTabsStepper's −/+ pattern. Lives in the settings panel.
    // Reuses the _autoLbl/_autoMinus/_autoValue/_autoPlus fields so PaintChrome/UpdateAutoEnabled/
    // SetAutoMax keep driving it unchanged.
    UIElement CeilingStepper()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center;
        _autoLbl = new TextBlock(); _autoLbl.VerticalAlignment = VerticalAlignment.Center;
        _autoLbl.FontSize = 12; _autoLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_autoLbl);
        _autoMinus = MiniButton("−");
        _autoMinus.Click += delegate { SetAutoMax(_autoMax - 1); };
        wrap.Children.Add(_autoMinus);
        _autoValue = new TextBlock(); _autoValue.VerticalAlignment = VerticalAlignment.Center;
        _autoValue.FontSize = 13; _autoValue.FontWeight = FontWeights.SemiBold;
        _autoValue.Margin = new Thickness(8, 0, 8, 0); _autoValue.MinWidth = 14;
        _autoValue.TextAlignment = TextAlignment.Center;
        wrap.Children.Add(_autoValue);
        _autoPlus = MiniButton("+");
        _autoPlus.Click += delegate { SetAutoMax(_autoMax + 1); };
        wrap.Children.Add(_autoPlus);
        return wrap;
    }

    // ── settings panel (gear popup) ──────────────────────────────────────────────
    // A gear button that opens a Popup consolidating the formerly-scattered toolbar controls:
    // start tabs, ceiling (上限) tabs, auto-retry on/off, retry cap, and the NEW user-editable
    // disk floor (GB). Built fresh on every open so theme/lang are always current. Uses the same
    // flat control templates (MiniButton/FlatButtonTemplate) the rest of the cockpit uses.
    UIElement SettingsControl()
    {
        _gearBtn = IconButton("settings", 18, _lang == 0 ? "設定" : "Settings");
        _gearBtn.ToolTip = _lang == 0 ? "設定（タブ数・再試行・容量床）" : "Settings (tabs / retry / disk floor)";
        _settingsPopup = new System.Windows.Controls.Primitives.Popup();
        _settingsPopup.PlacementTarget = _gearBtn;
        _settingsPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
        _settingsPopup.StaysOpen = false;          // click-away closes it
        _settingsPopup.AllowsTransparency = true;
        // Right-align: offset so the panel's right edge lines up with the gear button's right edge
        // (grows leftward, avoids right-window-edge clipping). MinWidth=280, button~30px -> -(280-30).
        _settingsPopup.HorizontalOffset = -250;
        _gearBtn.Click += delegate
        {
            if (_settingsPopup.IsOpen) { _settingsPopup.IsOpen = false; return; }
            CloseHeaderPopups("settings");
            _settingsPopup.Child = BuildSettingsPanel();   // rebuild so theme/lang/values are fresh
            _settingsPopup.IsOpen = true;
        };
        return _gearBtn;
    }

    // Overflow "⋯" button: opens a compact popup listing the four low-frequency header actions.
    // Built like _settingsPopup: StaysOpen=false, rebuilt fresh on each open.
    UIElement OverflowControl()
    {
        _overflowBtn = new Button();
        _overflowBtn.Width = 36; _overflowBtn.Height = 30; _overflowBtn.Cursor = Cursors.Hand;
        _overflowBtn.BorderThickness = new Thickness(1); _overflowBtn.Margin = new Thickness(4, 0, 0, 0);
        _overflowBtn.ToolTip = _lang == 0 ? "その他のメニュー" : "More options";
        System.Windows.Automation.AutomationProperties.SetName(_overflowBtn, _lang == 0 ? "その他のメニュー" : "More options");
        // Draw three dots as text (no glyph needed; plain text at this size is crisp)
        _overflowBtn.Content = new TextBlock { Text = "⋯", FontSize = 14,
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        _overflowPopup = new System.Windows.Controls.Primitives.Popup();
        _overflowPopup.PlacementTarget = _overflowBtn;
        _overflowPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
        _overflowPopup.StaysOpen = false;
        _overflowPopup.AllowsTransparency = true;
        // Right-align: offset so the panel's right edge lines up with the overflow button's right edge
        // (grows leftward, avoids right-window-edge clipping). MinWidth=200, button~36px -> -(200-36).
        _overflowPopup.HorizontalOffset = -164;
        _overflowBtn.Click += delegate
        {
            if (_overflowPopup.IsOpen) { _overflowPopup.IsOpen = false; return; }
            CloseHeaderPopups("overflow");
            _overflowPopup.Child = BuildOverflowPanel();
            _overflowPopup.IsOpen = true;
        };
        return _overflowBtn;
    }

    UIElement BuildOverflowPanel()
    {
        // Only rare items live here: main chat and self-improve.
        // Lang/theme are now 1-click icon buttons directly in the header.
        var card = new Border();
        card.Background = Theme.Br(Theme.Surface(_dark));
        card.BorderBrush = Theme.Br(Theme.Border(_dark));
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(8);
        card.Padding = new Thickness(4, 4, 4, 4);
        card.Margin = new Thickness(0, 4, 8, 4);
        card.MinWidth = 200;
        // Light, tasteful shadow (no heavy DropShadow; a thin border carries the panel)
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 8, ShadowDepth = 1, Opacity = 0.18, Color = C("#000000") };

        var col = new StackPanel();

        // Item 1: Open main chat -- icon row (settings-list style), not a flush text button.
        col.Children.Add(OverflowItem("chat",
            _lang == 0 ? "メインチャットを開く" : "Open main chat",
            delegate { _overflowPopup.IsOpen = false; OpenMain(); }));

        // Item 2: Self-improvement dashboard
        col.Children.Add(OverflowItem("account_tree",
            _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement",
            delegate { _overflowPopup.IsOpen = false; new SelfImproveDashboardWindow().Show(); }));

        card.Child = col;
        return card;
    }

    // A menu row in the overflow popup: [icon]  label, full-width hover highlight -- so the two
    // entries read as distinct, tappable rows (settings-list style) instead of flush text that
    // needs a divider to tell apart.
    Button OverflowItem(string glyph, string label, Action action)
    {
        var b = new Button();
        b.Cursor = Cursors.Hand;
        b.Background = Brushes.Transparent;
        b.BorderThickness = new Thickness(0);
        b.Padding = new Thickness(10, 9, 16, 9);
        b.HorizontalContentAlignment = HorizontalAlignment.Left;
        b.Template = FlatButtonTemplate(true);   // left-align so the two rows' icons line up

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        var icHost = new ContentControl();
        icHost.Content = MakeIcon(glyph, 16, Theme.Br(Theme.Muted(_dark)));
        icHost.Width = 22; icHost.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(icHost);
        var tb = new TextBlock();
        tb.Text = label; tb.Foreground = Fg; tb.FontSize = 13;
        tb.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(tb);
        b.Content = row;

        // FlatButtonTemplate has no IsMouseOver trigger, so drive the row highlight manually.
        Brush hov = Theme.Br(Theme.SurfaceSubtle(_dark));
        b.MouseEnter += delegate { b.Background = hov; };
        b.MouseLeave += delegate { b.Background = Brushes.Transparent; };

        Action a = action;
        b.Click += delegate { a(); };
        return b;
    }

    // One labeled −/+ stepper row for the settings panel. label on the left, [− value +] on the right.
    UIElement SettingsStepperRow(string label, TextBlock valueBlock, Button minus, Button plus)
    {
        var row = new DockPanel(); row.Margin = new Thickness(0, 5, 0, 5); row.LastChildFill = false;
        var lbl = new TextBlock(); lbl.Text = label; lbl.Foreground = Fg; lbl.FontSize = 12.5;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(lbl, Dock.Left); row.Children.Add(lbl);
        var stp = new StackPanel(); stp.Orientation = Orientation.Horizontal;
        stp.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(stp, Dock.Right);
        stp.Children.Add(minus);
        valueBlock.Foreground = Fg; valueBlock.FontSize = 13; valueBlock.FontWeight = FontWeights.SemiBold;
        valueBlock.Margin = new Thickness(10, 0, 10, 0); valueBlock.MinWidth = 28;
        valueBlock.TextAlignment = TextAlignment.Center; valueBlock.VerticalAlignment = VerticalAlignment.Center;
        stp.Children.Add(valueBlock);
        stp.Children.Add(plus);
        row.Children.Add(stp);
        return row;
    }

    TextBlock _diskFloorVal;
    TextBlock SectionHeader(string text)
    {
        var t = new TextBlock(); t.Text = text; t.Foreground = Muted; t.FontSize = 11;
        t.FontWeight = FontWeights.SemiBold; t.Margin = new Thickness(0, 10, 0, 2);
        return t;
    }

    System.Windows.Controls.Primitives.Popup _advancedPopup;

    // The row that opens 詳細設定 to the side. Styled as a menu item -- label left, chevron
    // right -- so it reads as "there is more behind this", not as another setting.
    UIElement AdvancedSubmenuRow()
    {
        var btn = new Button();
        btn.Cursor = Cursors.Hand;
        btn.HorizontalContentAlignment = HorizontalAlignment.Stretch;
        btn.Padding = new Thickness(8, 6, 8, 6);
        btn.Margin = new Thickness(0, 2, 0, 2);
        btn.Background = BtnBg; btn.Foreground = Fg; btn.BorderBrush = Border;
        btn.BorderThickness = new Thickness(1);
        btn.Template = FlatButtonTemplate();

        var row = new DockPanel(); row.LastChildFill = true;
        var chev = new TextBlock();
        chev.Text = "›";                       // ›
        chev.Foreground = Muted; chev.FontSize = 14;
        chev.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(chev, Dock.Right); row.Children.Add(chev);
        var lbl = new TextBlock();
        lbl.Text = L("アクセス範囲・接続クライアント", "Access scope & connected clients");
        lbl.Foreground = Fg; lbl.FontSize = 12.5;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(lbl);
        btn.Content = row;

        // Automation name: the row's own label describes its CONTENTS, so without this the
        // submenu is unfindable by name in tooling and by a screen reader looking for 詳細設定.
        System.Windows.Automation.AutomationProperties.SetName(btn, L("詳細設定", "Advanced"));

        btn.Click += delegate { ToggleAdvancedPopup(btn); };
        return btn;
    }

    void ToggleAdvancedPopup(UIElement anchor)
    {
        if (_advancedPopup != null && _advancedPopup.IsOpen)
        {
            _advancedPopup.IsOpen = false;
            return;
        }
        _advancedPopup = new System.Windows.Controls.Primitives.Popup();
        _advancedPopup.PlacementTarget = anchor;
        // Left, not Right: the settings panel is right-aligned to the gear and grows leftward,
        // so its right edge is already at the window edge -- a submenu placed to the right
        // opens off-screen or on top of its own parent.
        _advancedPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Left;
        _advancedPopup.VerticalOffset = -8;
        _advancedPopup.AllowsTransparency = true;
        _advancedPopup.StaysOpen = false;

        var card = new System.Windows.Controls.Border();
        card.Background = CardBg; card.BorderBrush = Border; card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(10); card.Padding = new Thickness(16, 12, 16, 14);
        card.Margin = new Thickness(8, 6, 8, 6); card.MinWidth = 340; card.MaxWidth = 420;
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 16, ShadowDepth = 2, Opacity = 0.28, Color = C("#000000") };

        var col = new StackPanel(); col.Orientation = Orientation.Vertical;
        var title = new TextBlock(); title.Text = L("詳細設定", "Advanced"); title.Foreground = Fg;
        title.FontSize = 14; title.FontWeight = FontWeights.SemiBold;
        title.Margin = new Thickness(0, 0, 0, 6);
        col.Children.Add(title);
        // The lists inside can be long (one row per allowed folder, one per connected client),
        // and a popup does not scroll on its own -- without this it grows past the screen and
        // the buttons at the bottom become unreachable.
        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        scroll.MaxHeight = 520;
        scroll.Content = BuildAdvancedSettingsSection();
        col.Children.Add(scroll);
        card.Child = col;
        _advancedPopup.Child = card;

        // The parent settings popup closes on any click outside its own visual tree, and this
        // child is a separate HWND -- so opening it would immediately dismiss the parent and
        // take the child with it. Hold the parent open for as long as the child is up.
        if (_settingsPopup != null)
        {
            _settingsPopup.StaysOpen = true;
            _advancedPopup.Closed += delegate
            {
                if (_settingsPopup != null) _settingsPopup.StaysOpen = false;
            };
        }
        _advancedPopup.IsOpen = true;
    }

    UIElement BuildSettingsPanel()
    {
        var card = new Border();
        card.Background = CardBg; card.BorderBrush = Border; card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(10); card.Padding = new Thickness(16, 12, 16, 14);
        card.Margin = new Thickness(0, 6, 8, 6); card.MinWidth = 280;
        // soft shadow so the floating panel reads as elevated
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 16, ShadowDepth = 2, Opacity = 0.28, Color = C("#000000") };

        var col = new StackPanel(); col.Orientation = Orientation.Vertical;

        var title = new TextBlock(); title.Text = T("settings"); title.Foreground = Fg;
        title.FontSize = 14; title.FontWeight = FontWeights.SemiBold; title.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(title);

        // ── Parallel tabs: start + ceiling (上限) steppers ──
        col.Children.Add(SectionHeader(T("set_tabs_section")));
        col.Children.Add(AutoscaleControls());
        var startMinus = MiniButton("−"); startMinus.Click += delegate { SetMaxTabs(_maxtabs - 1); };
        var startPlus = MiniButton("+"); startPlus.Click += delegate { SetMaxTabs(_maxtabs + 1); };
        _maxMinus = startMinus; _maxPlus = startPlus;   // keep refs so PaintChrome re-themes them (mirrors ceiling)
        _maxValue = new TextBlock(); _maxValue.Text = _maxtabs.ToString();
        col.Children.Add(SettingsStepperRow(T("def_tabs"), _maxValue, startMinus, startPlus));
        // ceiling stepper -- reuses _autoLbl/_autoMinus/_autoValue/_autoPlus via CeilingStepper fields
        var ceilMinus = MiniButton("−"); ceilMinus.Click += delegate { SetAutoMax(_autoMax - 1); };
        var ceilPlus = MiniButton("+"); ceilPlus.Click += delegate { SetAutoMax(_autoMax + 1); };
        _autoValue = new TextBlock(); _autoValue.Text = _autoMax.ToString();
        _autoMinus = ceilMinus; _autoPlus = ceilPlus;   // keep refs so UpdateAutoEnabled can grey them
        col.Children.Add(SettingsStepperRow(T("max_tabs2"), _autoValue, ceilMinus, ceilPlus));

        // ── Auto-retry: on/off toggle + cap ──
        col.Children.Add(SectionHeader(T("set_retry_section")));
        _autoRetryBtn = new Button();
        _autoRetryBtn.BorderThickness = new Thickness(1); _autoRetryBtn.Cursor = Cursors.Hand;
        _autoRetryBtn.Padding = new Thickness(10, 4, 10, 4); _autoRetryBtn.FontSize = 12;
        _autoRetryBtn.FontWeight = FontWeights.SemiBold; _autoRetryBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _autoRetryBtn.Margin = new Thickness(0, 2, 0, 4);
        _autoRetryBtn.Template = FlatButtonTemplate();
        _autoRetryBtn.ToolTip = _lang == 0
            ? "停止したゴールを自動で再投入（上限まで・既定OFF）。無限ループは起きません。"
            : "Auto re-queue stopped goals (up to the cap; default OFF). Never loops forever.";
        _autoRetryBtn.Click += delegate { _autoRetry = !_autoRetry; SaveKey("autoretry", _autoRetry ? "1" : "0"); PaintAutoRetryBtn(); };
        PaintAutoRetryBtn();
        col.Children.Add(_autoRetryBtn);
        var capMinus = MiniButton("−"); capMinus.Click += delegate { SetAutoRetryMax(_autoRetryMax - 1); };
        var capPlus = MiniButton("+"); capPlus.Click += delegate { SetAutoRetryMax(_autoRetryMax + 1); };
        _autoRetryCapVal = new TextBlock(); _autoRetryCapVal.Text = _autoRetryMax.ToString();
        col.Children.Add(SettingsStepperRow(T("cap"), _autoRetryCapVal, capMinus, capPlus));

        // ── P2 (c): Auto-archive on finish (default OFF) ──
        col.Children.Add(SectionHeader(T("set_archive_section")));
        _autoArchiveBtn = new Button();
        _autoArchiveBtn.BorderThickness = new Thickness(1); _autoArchiveBtn.Cursor = Cursors.Hand;
        _autoArchiveBtn.Padding = new Thickness(10, 4, 10, 4); _autoArchiveBtn.FontSize = 12;
        _autoArchiveBtn.FontWeight = FontWeights.SemiBold; _autoArchiveBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _autoArchiveBtn.Margin = new Thickness(0, 2, 0, 4);
        _autoArchiveBtn.Template = FlatButtonTemplate();
        _autoArchiveBtn.ToolTip = _lang == 0
            ? "ラン終了時に完了カードを自動で履歴へ移動（既定OFF）。"
            : "Move completed cards to History automatically when the run finishes (default OFF).";
        _autoArchiveBtn.Click += delegate { _autoArchive = !_autoArchive; SaveKey("autoarchive", _autoArchive ? "1" : "0"); PaintAutoArchiveBtn(); };
        PaintAutoArchiveBtn();
        col.Children.Add(_autoArchiveBtn);

        // ── Capacity guard: NEW user-editable disk floor (GB) ──
        col.Children.Add(SectionHeader(T("set_capacity_section")));
        var dfMinus = MiniButton("−"); dfMinus.Click += delegate { SetDiskFloor(_diskFloor - 1.0); };
        var dfPlus = MiniButton("+"); dfPlus.Click += delegate { SetDiskFloor(_diskFloor + 1.0); };
        _diskFloorVal = new TextBlock(); _diskFloorVal.Text = FmtFloor(_diskFloor);
        col.Children.Add(SettingsStepperRow(T("disk_floor"), _diskFloorVal, dfMinus, dfPlus));
        // RAM floor (MB) -- the free-RAM reserve the autoscale keeps for the user (256 MB steps)
        var rfMinus = MiniButton("−"); rfMinus.Click += delegate { SetRamFloor(_ramFloor - 256.0); };
        var rfPlus = MiniButton("+"); rfPlus.Click += delegate { SetRamFloor(_ramFloor + 256.0); };
        _ramFloorVal = new TextBlock(); _ramFloorVal.Text = ((int)_ramFloor).ToString();
        col.Children.Add(SettingsStepperRow(T("ram_floor"), _ramFloorVal, rfMinus, rfPlus));
        var hint = new TextBlock(); hint.Text = T("disk_floor_hint"); hint.Foreground = Muted;
        hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap; hint.Margin = new Thickness(0, 0, 0, 2);
        col.Children.Add(hint);

        // ── Chat: always-available manual bridge reconnect. Unlike the Fix button (only shown
        // when a dot is red/yellow), this fires on demand regardless of the Tool dot's state --
        // it may be GRAY (self-probe feature not yet active on this machine) or the user may just
        // want to force a reconnect without waiting for the next probe cycle. Warning-outline pill,
        // matching the visual language the inline Fix pill already uses for repair actions.
        col.Children.Add(SectionHeader(T("set_chat_section")));
        _reconnectChatBtn = new Button();
        _reconnectChatBtn.Content = T("reconnect_chat");
        _reconnectChatBtn.FontSize = 12; _reconnectChatBtn.FontWeight = FontWeights.SemiBold;
        _reconnectChatBtn.Cursor = Cursors.Hand; _reconnectChatBtn.BorderThickness = new Thickness(1);
        _reconnectChatBtn.Padding = new Thickness(10, 4, 10, 4);
        _reconnectChatBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _reconnectChatBtn.Margin = new Thickness(0, 2, 0, 4);
        _reconnectChatBtn.Template = FlatButtonTemplate();
        _reconnectChatBtn.Background = Brushes.Transparent;
        _reconnectChatBtn.Foreground = Theme.Br(Theme.Warning(_dark));
        _reconnectChatBtn.BorderBrush = Theme.Br(Theme.Warning(_dark));
        _reconnectChatBtn.ToolTip = T("reconnect_chat_hint");
        System.Windows.Automation.AutomationProperties.SetName(_reconnectChatBtn, T("reconnect_chat"));
        _reconnectChatBtn.Click += delegate { RunBridgeReconnectManual(); };
        col.Children.Add(_reconnectChatBtn);

        // ── 表示サイズ / UI scale: [自動 ▸] then [−] [自動 (130%)] [+] (step 0.1, live-apply + persist) ──
        // Auto toggle keeps a CONSTANT physical size across monitors (target/monitorScale). When Auto is
        // on the value shows "自動 (NNN%)" (the current effective %) and the −/+ steppers are disabled;
        // clicking −/+ (or toggling Auto off) drops to manual at the current number.
        col.Children.Add(SectionHeader(T("ui_scale_section")));
        _uiAutoBtn = MiniButton(_uiAuto ? T("ui_auto_on") : T("ui_auto_off"));
        _uiAutoBtn.Width = double.NaN; _uiAutoBtn.MinWidth = 78; _uiAutoBtn.Padding = new Thickness(8, 0, 8, 0);
        _uiAutoBtn.Click += delegate
        {
            if (_uiAuto) ApplyUiScale(_uiScale, true);   // Auto -> manual at the current effective number
            else EnableAutoScale(true);                  // manual -> Auto (recompute for this monitor)
        };
        var autoRow = new DockPanel(); autoRow.Margin = new Thickness(0, 5, 0, 0); autoRow.LastChildFill = false;
        DockPanel.SetDock(_uiAutoBtn, Dock.Right); autoRow.Children.Add(_uiAutoBtn);
        col.Children.Add(autoRow);
        _uiScaleMinus = MiniButton("−"); _uiScaleMinus.Click += delegate { ApplyUiScale(_uiScale - 0.1, true); };
        _uiScalePlus = MiniButton("+"); _uiScalePlus.Click += delegate { ApplyUiScale(_uiScale + 0.1, true); };
        _uiScaleVal = new TextBlock(); _uiScaleVal.Text = AutoLabelText();
        _uiScaleVal.MinWidth = 84;   // wider: holds "自動 (130%)"
        col.Children.Add(SettingsStepperRow(T("ui_scale"), _uiScaleVal, _uiScaleMinus, _uiScalePlus));
        var usHint = new TextBlock(); usHint.Text = T("ui_scale_hint"); usHint.Foreground = Muted;
        usHint.FontSize = 10.5; usHint.TextWrapping = TextWrapping.Wrap; usHint.Margin = new Thickness(0, 0, 0, 2);
        col.Children.Add(usHint);
        RefreshUiScaleControls();   // set label/toggle/stepper-enabled to match the current mode

        // ── 詳細設定 / Advanced ──
        // A submenu ROW, not an inline section. Inlined, it added roughly a screen of folder
        // list + client list to a panel that is already long, so the everyday settings above
        // it got pushed off and the operator could not find it at all. It opens to the side
        // like a context-menu submenu, which is where people expect "more, over here".
        col.Children.Add(SectionHeader(L("詳細設定", "Advanced")));
        col.Children.Add(AdvancedSubmenuRow());

        card.Child = col;
        UpdateAutoEnabled();   // grey the ceiling stepper if autoscale is off
        return card;
    }

    // ══════════════════════════════════════════════════════════════════════════════════
    // 詳細設定 / Advanced
    // ══════════════════════════════════════════════════════════════════════════════════
    //
    // アクセス範囲 (access scope): the folder allow-list tools/folder_policy.py reads from
    // .fleet/folder_access.json ({"enabled":bool, "global":[...], "scopes":{...}}). This panel
    // only ever touches "enabled" and "global" -- "scopes" (per fleet-lane overrides) is read
    // back and re-written byte-for-byte untouched on every save, never edited here.
    //
    // フルアクセス (full access, enabled:false) is the operator's explicit choice and MUST stay
    // the default and MUST stay reachable with one click and no confirmation dialog -- do not
    // "improve" this into a warning prompt or flip the default to restricted.
    //
    // 接続クライアント (connected clients): tools/security.py's grant_ip/revoke_ip are LOCAL
    // admin functions (never MCP tools -- see that file's header comment) that this panel shells
    // out to via `python -m tools.security ...`, exactly like the file's own _cli() docstring
    // says the cockpit does. The cockpit never opens or writes .unlock_state.json itself, so the
    // atomic tmp-file+os.replace write tools/security.py already does is the only writer.
    UIElement BuildAdvancedSettingsSection()
    {
        var col = new StackPanel();
        col.Children.Add(BuildAccessScopeControls());
        col.Children.Add(BuildConnectedClientsControls());
        return col;
    }

    // ── アクセス範囲 ──────────────────────────────────────────────────────────────────
    string FolderAccessPath() { return Path.Combine(RepoRoot(), ".fleet", "folder_access.json"); }

    Dictionary<string, object> ReadFolderAccessRaw()
    {
        try
        {
            string p = FolderAccessPath();
            if (File.Exists(p))
            {
                var d = _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8)) as Dictionary<string, object>;
                if (d != null) return d;
            }
        }
        catch (Exception) { }
        return new Dictionary<string, object>();
    }

    // DEFAULT-OPEN, mirroring folder_policy.py's own _normalise(): a missing/unreadable/
    // malformed policy file means unrestricted (フルアクセス), never restricted.
    bool AccessEnabled()
    {
        var d = ReadFolderAccessRaw();
        object v;
        if (d.TryGetValue("enabled", out v) && v != null)
        {
            try { return Convert.ToBoolean(v); } catch (Exception) { }
        }
        return false;
    }

    List<string> AccessGlobalFolders()
    {
        var d = ReadFolderAccessRaw();
        var outp = new List<string>();
        object v;
        if (d.TryGetValue("global", out v) && v is object[])
            foreach (object o in (object[])v) if (o != null) outp.Add(o.ToString());
        return outp;
    }

    // Whole-dict atomic write (tmp file in the same directory + File.Replace), same shape as
    // AnswerGate's gate-file write above -- .fleet/ is not written concurrently by the live
    // server for this particular file, but a torn write here would still corrupt the JSON a
    // reader (this same UI, or a future python reader) parses next, so the same discipline
    // applies. Never edits keys the caller didn't set (SetAccessScope/AddAccessFolder/
    // RemoveAccessFolder each read-modify-write a single key so "scopes" always survives).
    void WriteFolderAccessRaw(Dictionary<string, object> d)
    {
        try
        {
            string path = FolderAccessPath();
            string dir = Path.GetDirectoryName(path);
            Directory.CreateDirectory(dir);
            string json = _js.Serialize(d);
            string tmp = Path.Combine(dir, Path.GetFileName(path) + ".tmp");
            File.WriteAllText(tmp, json, new UTF8Encoding(false));
            if (File.Exists(path))
            {
                try { File.Replace(tmp, path, null); }
                catch (Exception)
                {
                    File.Copy(tmp, path, true);
                    try { File.Delete(tmp); } catch (Exception) { }
                }
            }
            else
            {
                File.Move(tmp, path);
            }
        }
        catch (Exception) { }
    }

    // Toggle ONLY "enabled". "global" (the saved folder list) and "scopes" are read back and
    // written out exactly as found -- choosing フルアクセス must never lose the folder list a
    // user built up under 指定フォルダのみ, so that re-enabling restriction restores it intact.
    void SetAccessScope(bool restricted)
    {
        var d = ReadFolderAccessRaw();
        d["enabled"] = restricted;
        if (!d.ContainsKey("global")) d["global"] = new object[0];
        if (!d.ContainsKey("scopes")) d["scopes"] = new Dictionary<string, object>();
        WriteFolderAccessRaw(d);
        PaintAccessScopeButtons(restricted);
    }

    void PaintAccessScopeButtons(bool restricted)
    {
        if (_accessFullBtn != null)
        {
            bool sel = !restricted;
            _accessFullBtn.Background = sel ? Theme.Br(Theme.Accent(_dark)) : BtnBg;
            _accessFullBtn.Foreground = sel ? White : Muted;
            _accessFullBtn.BorderBrush = sel ? Theme.Br(Theme.Accent(_dark)) : Border;
        }
        if (_accessRestrictedBtn != null)
        {
            bool sel = restricted;
            _accessRestrictedBtn.Background = sel ? Theme.Br(Theme.Accent(_dark)) : BtnBg;
            _accessRestrictedBtn.Foreground = sel ? White : Muted;
            _accessRestrictedBtn.BorderBrush = sel ? Theme.Br(Theme.Accent(_dark)) : Border;
        }
    }

    void RefreshFolderRows(List<string> folders)
    {
        if (_folderRowsPanel == null) return;
        _folderRowsPanel.Children.Clear();
        if (folders.Count == 0)
        {
            var none = new TextBlock();
            none.Text = L("(フォルダ未設定)", "(no folders yet)");
            none.Foreground = Muted; none.FontSize = 11; none.Margin = new Thickness(0, 2, 0, 2);
            _folderRowsPanel.Children.Add(none);
            return;
        }
        foreach (string f in folders)
        {
            var row = new DockPanel(); row.Margin = new Thickness(0, 2, 0, 2); row.LastChildFill = false;
            var rm = MiniButton("×");
            rm.Width = 22; rm.Height = 22; rm.FontSize = 12;
            rm.ToolTip = L("削除", "Remove");
            System.Windows.Automation.AutomationProperties.SetName(rm, L("削除", "Remove") + ": " + f);
            string captured = f;
            rm.Click += delegate { RemoveAccessFolder(captured); };
            DockPanel.SetDock(rm, Dock.Right); row.Children.Add(rm);
            var tb = new TextBlock(); tb.Text = f; tb.Foreground = Fg; tb.FontSize = 11.5;
            tb.VerticalAlignment = VerticalAlignment.Center; tb.TextTrimming = TextTrimming.CharacterEllipsis;
            tb.ToolTip = f; tb.Margin = new Thickness(0, 0, 6, 0);
            DockPanel.SetDock(tb, Dock.Left); row.Children.Add(tb);
            _folderRowsPanel.Children.Add(row);
        }
    }

    // Same "pick any file, use its parent folder" picker FolderToGoals() already uses --
    // reliable, no COM FolderBrowserDialog needed.
    void AddAccessFolder()
    {
        try
        {
            var ofd = new Microsoft.Win32.OpenFileDialog();
            ofd.Title = L("追加するフォルダ内の任意のファイルを選択（その親フォルダが追加されます）",
                          "Pick ANY file inside the folder to add (its parent folder is added)");
            ofd.Filter = L("すべてのファイル|*.*", "All files|*.*");
            ofd.CheckFileExists = true;
            if (ofd.ShowDialog() != true) return;
            string folder = Path.GetDirectoryName(ofd.FileName);
            if (string.IsNullOrEmpty(folder)) return;

            var d = ReadFolderAccessRaw();
            var list = new List<string>();
            object v;
            if (d.TryGetValue("global", out v) && v is object[])
                foreach (object o in (object[])v) if (o != null) list.Add(o.ToString());
            bool exists = false;
            foreach (string existing in list)
                if (string.Equals(existing, folder, StringComparison.OrdinalIgnoreCase)) { exists = true; break; }
            if (!exists) list.Add(folder);
            d["global"] = list.ToArray();
            if (!d.ContainsKey("enabled")) d["enabled"] = false;
            if (!d.ContainsKey("scopes")) d["scopes"] = new Dictionary<string, object>();
            WriteFolderAccessRaw(d);
            RefreshFolderRows(list);
        }
        catch (Exception) { }
    }

    void RemoveAccessFolder(string folder)
    {
        try
        {
            var d = ReadFolderAccessRaw();
            var list = new List<string>();
            object v;
            if (d.TryGetValue("global", out v) && v is object[])
                foreach (object o in (object[])v) if (o != null) list.Add(o.ToString());
            list.RemoveAll(delegate (string x) { return string.Equals(x, folder, StringComparison.OrdinalIgnoreCase); });
            d["global"] = list.ToArray();
            WriteFolderAccessRaw(d);
            RefreshFolderRows(list);
        }
        catch (Exception) { }
    }

    UIElement BuildAccessScopeControls()
    {
        var wrap = new StackPanel();
        var subHead = new TextBlock(); subHead.Text = L("アクセス範囲", "Access scope");
        subHead.Foreground = Fg; subHead.FontSize = 12.5; subHead.FontWeight = FontWeights.SemiBold;
        subHead.Margin = new Thickness(0, 4, 0, 4);
        wrap.Children.Add(subHead);

        bool restricted = AccessEnabled();
        var folders = AccessGlobalFolders();

        var choiceRow = new StackPanel(); choiceRow.Orientation = Orientation.Horizontal;
        choiceRow.Margin = new Thickness(0, 0, 0, 4);

        _accessFullBtn = new Button();
        _accessFullBtn.Content = L("フルアクセス", "Full access");
        _accessFullBtn.FontSize = 12; _accessFullBtn.FontWeight = FontWeights.SemiBold;
        _accessFullBtn.Cursor = Cursors.Hand; _accessFullBtn.BorderThickness = new Thickness(1);
        _accessFullBtn.Padding = new Thickness(10, 4, 10, 4); _accessFullBtn.Template = FlatButtonTemplate();
        _accessFullBtn.ToolTip = L("すべてのフォルダを操作できます（既定）。", "Can operate on any folder (default).");
        _accessFullBtn.Click += delegate { SetAccessScope(false); };
        choiceRow.Children.Add(_accessFullBtn);

        _accessRestrictedBtn = new Button();
        _accessRestrictedBtn.Content = L("指定フォルダのみ", "Selected folders only");
        _accessRestrictedBtn.FontSize = 12; _accessRestrictedBtn.FontWeight = FontWeights.SemiBold;
        _accessRestrictedBtn.Cursor = Cursors.Hand; _accessRestrictedBtn.BorderThickness = new Thickness(1);
        _accessRestrictedBtn.Padding = new Thickness(10, 4, 10, 4); _accessRestrictedBtn.Template = FlatButtonTemplate();
        _accessRestrictedBtn.Margin = new Thickness(6, 0, 0, 0);
        _accessRestrictedBtn.ToolTip = L("下のリストのフォルダだけに操作を限定します。", "Limits operations to the folders listed below.");
        _accessRestrictedBtn.Click += delegate { SetAccessScope(true); };
        choiceRow.Children.Add(_accessRestrictedBtn);

        wrap.Children.Add(choiceRow);
        PaintAccessScopeButtons(restricted);

        var hint = new TextBlock();
        hint.Text = L("フルアクセス: すべてのフォルダを操作可能（既定）。指定フォルダのみ: 下のリストのフォルダに限定します。",
                      "Full access: can operate on any folder (default). Selected folders only: limited to the list below.");
        hint.Foreground = Muted; hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap;
        hint.Margin = new Thickness(0, 4, 0, 6);
        wrap.Children.Add(hint);

        _folderRowsPanel = new StackPanel();
        RefreshFolderRows(folders);
        var listScroll = new ScrollViewer();
        listScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        listScroll.MaxHeight = 110;
        listScroll.Content = _folderRowsPanel;
        wrap.Children.Add(listScroll);

        var addBtn = new Button();
        addBtn.Content = L("＋ フォルダを追加", "+ Add folder");
        addBtn.FontSize = 12; addBtn.Cursor = Cursors.Hand; addBtn.BorderThickness = new Thickness(1);
        addBtn.Padding = new Thickness(10, 4, 10, 4); addBtn.HorizontalAlignment = HorizontalAlignment.Left;
        addBtn.Margin = new Thickness(0, 4, 0, 10); addBtn.Template = FlatButtonTemplate();
        addBtn.Background = BtnBg; addBtn.Foreground = Fg; addBtn.BorderBrush = Border;
        addBtn.Click += delegate { AddAccessFolder(); };
        wrap.Children.Add(addBtn);

        return wrap;
    }

    // ── 接続クライアント ──────────────────────────────────────────────────────────────
    // Never opens/writes .unlock_state.json directly -- shells out to
    // `python -m tools.security ...` / `python -m tools.lock_state show`, the exact CLI
    // surface tools/security.py's _cli() and tools/lock_state.py's _cli() document as being
    // for this panel. Those modules own the atomic write and the require_unlocked() gate;
    // this panel only ever reads their JSON output and asks them to grant/revoke.
    string RunPyModule(string module, string args, int timeoutMs)
    {
        try
        {
            string repo = RepoRoot();
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m " + module + (string.IsNullOrEmpty(args) ? "" : " " + args);
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            string stdoutText = "";
            using (var p = System.Diagnostics.Process.Start(psi))
            {
                try { stdoutText = p.StandardOutput.ReadToEnd(); } catch (Exception) { }
                try { p.StandardError.ReadToEnd(); } catch (Exception) { }
                try { p.WaitForExit(timeoutMs); } catch (Exception) { }
            }
            return stdoutText;
        }
        catch (Exception) { return ""; }
    }

    List<Dictionary<string, object>> ListUnlockGrants()
    {
        var outp = new List<Dictionary<string, object>>();
        string stdout = RunPyModule("tools.security", "list", 15000);
        try
        {
            var arr = _js.DeserializeObject(stdout) as object[];
            if (arr != null)
                foreach (object o in arr)
                {
                    var d = o as Dictionary<string, object>;
                    if (d != null) outp.Add(d);
                }
        }
        catch (Exception) { }
        return outp;
    }

    // .fleet/lock_state.json's most recent refusal, via tools/lock_state.py's own CLI -- the
    // primary path this panel exists for: a refusal happens, the operator opens 詳細設定, and
    // the refused client is already sitting at the top with an Allow button, never a box to
    // type an address into.
    Dictionary<string, object> ReadRefusedClient()
    {
        string stdout = RunPyModule("tools.lock_state", "show", 15000);
        try
        {
            var d = _js.DeserializeObject(stdout) as Dictionary<string, object>;
            if (d != null) return d;
        }
        catch (Exception) { }
        return new Dictionary<string, object>();
    }

    void GrantClientIp(string ip)
    {
        RunPyModule("tools.security", "grant \"" + ip.Replace("\"", "") + "\"", 15000);
    }

    void RevokeClientIp(string ip)
    {
        RunPyModule("tools.security", "revoke \"" + ip.Replace("\"", "") + "\"", 15000);
    }

    void RefreshClientRows()
    {
        if (_clientRowsPanel == null) return;
        _clientRowsPanel.Children.Clear();

        var grants = ListUnlockGrants();
        var refused = ReadRefusedClient();
        string refusedIp = "";
        object rv;
        if (refused.TryGetValue("client_ip", out rv) && rv != null) refusedIp = rv.ToString();

        // Surface the refused IP up top UNLESS it is already granted and not expired -- an
        // already-resolved refusal shows up in the ordinary list below, not as "pending" too.
        if (!string.IsNullOrEmpty(refusedIp))
        {
            bool covered = false;
            foreach (var g in grants)
            {
                object ipv, ev;
                if (g.TryGetValue("ip", out ipv) && ipv != null && ipv.ToString() == refusedIp)
                {
                    bool expired = false;
                    if (g.TryGetValue("expired", out ev) && ev != null)
                        try { expired = Convert.ToBoolean(ev); } catch (Exception) { }
                    covered = !expired;
                    break;
                }
            }
            if (!covered) _clientRowsPanel.Children.Add(BuildPendingClientRow(refusedIp));
        }

        if (grants.Count == 0)
        {
            var none = new TextBlock();
            none.Text = L("(接続履歴なし)", "(no clients yet)");
            none.Foreground = Muted; none.FontSize = 11; none.Margin = new Thickness(0, 2, 0, 2);
            _clientRowsPanel.Children.Add(none);
            return;
        }
        foreach (var g in grants) _clientRowsPanel.Children.Add(BuildClientRow(g));
    }

    UIElement BuildPendingClientRow(string ip)
    {
        var card = new Border();
        card.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        card.BorderBrush = Theme.Br(Theme.Danger(_dark));
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(6);
        card.Padding = new Thickness(8, 6, 8, 6);
        card.Margin = new Thickness(0, 0, 0, 6);

        var col = new StackPanel();
        var badge = new TextBlock();
        badge.Text = L("直近で拒否された接続", "Most recently refused");
        badge.Foreground = Theme.Br(Theme.Danger(_dark)); badge.FontSize = 10.5; badge.FontWeight = FontWeights.SemiBold;
        col.Children.Add(badge);

        var row = new DockPanel(); row.Margin = new Thickness(0, 4, 0, 0); row.LastChildFill = false;
        var allow = new Button();
        allow.Content = L("許可", "Allow");
        allow.FontSize = 11.5; allow.FontWeight = FontWeights.SemiBold; allow.Cursor = Cursors.Hand;
        allow.BorderThickness = new Thickness(0); allow.Padding = new Thickness(10, 3, 10, 3);
        allow.Template = FlatButtonTemplate(); allow.Background = Theme.Br(Theme.Accent(_dark)); allow.Foreground = White;
        string capturedIp = ip;
        allow.Click += delegate { GrantClientIp(capturedIp); RefreshClientRows(); };
        DockPanel.SetDock(allow, Dock.Right); row.Children.Add(allow);

        var ipTb = new TextBlock(); ipTb.Text = ip; ipTb.Foreground = Fg; ipTb.FontSize = 12;
        ipTb.VerticalAlignment = VerticalAlignment.Center; ipTb.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(ipTb, Dock.Left); row.Children.Add(ipTb);
        col.Children.Add(row);

        card.Child = col;
        return card;
    }

    UIElement BuildClientRow(Dictionary<string, object> g)
    {
        string ip = "";
        object v;
        if (g.TryGetValue("ip", out v) && v != null) ip = v.ToString();
        bool expired = false;
        if (g.TryGetValue("expired", out v) && v != null)
            try { expired = Convert.ToBoolean(v); } catch (Exception) { }
        double remainingSec = 0;
        if (g.TryGetValue("remaining_seconds", out v) && v != null)
            try { remainingSec = Convert.ToDouble(v); } catch (Exception) { }

        var row = new DockPanel(); row.Margin = new Thickness(0, 3, 0, 3); row.LastChildFill = false;

        var revoke = new Button();
        revoke.Content = L("取り消し", "Revoke");
        revoke.FontSize = 11; revoke.Cursor = Cursors.Hand; revoke.BorderThickness = new Thickness(1);
        revoke.Padding = new Thickness(8, 3, 8, 3); revoke.Template = FlatButtonTemplate();
        revoke.Background = Brushes.Transparent; revoke.Foreground = Theme.Br(Theme.Danger(_dark));
        revoke.BorderBrush = Theme.Br(Theme.Danger(_dark));
        string capturedIp1 = ip;
        revoke.Click += delegate { RevokeClientIp(capturedIp1); RefreshClientRows(); };
        DockPanel.SetDock(revoke, Dock.Right); row.Children.Add(revoke);

        var allow = new Button();
        allow.Content = L("許可", "Allow");
        allow.FontSize = 11; allow.Cursor = Cursors.Hand; allow.BorderThickness = new Thickness(1);
        allow.Padding = new Thickness(8, 3, 8, 3); allow.Template = FlatButtonTemplate();
        allow.Background = BtnBg; allow.Foreground = Fg; allow.BorderBrush = Border;
        allow.Margin = new Thickness(0, 0, 6, 0);
        allow.ToolTip = L("延長（同じTTLで再付与）", "Extend (re-grant with the same TTL rule)");
        string capturedIp2 = ip;
        allow.Click += delegate { GrantClientIp(capturedIp2); RefreshClientRows(); };
        DockPanel.SetDock(allow, Dock.Right); row.Children.Add(allow);

        var statusTb = new TextBlock();
        statusTb.FontSize = 11; statusTb.VerticalAlignment = VerticalAlignment.Center;
        statusTb.Margin = new Thickness(0, 0, 8, 0);
        if (expired)
        {
            statusTb.Text = L("期限切れ", "Expired");
            statusTb.Foreground = Theme.Br(Theme.Danger(_dark));
        }
        else
        {
            double days = remainingSec / 86400.0;
            statusTb.Text = days.ToString("0.#", CultureInfo.InvariantCulture) + L("日", "d");
            statusTb.Foreground = Muted;
        }
        DockPanel.SetDock(statusTb, Dock.Right); row.Children.Add(statusTb);

        var ipTb = new TextBlock(); ipTb.Text = ip; ipTb.Foreground = Fg; ipTb.FontSize = 12;
        ipTb.VerticalAlignment = VerticalAlignment.Center; ipTb.TextTrimming = TextTrimming.CharacterEllipsis;
        ipTb.ToolTip = ip;
        DockPanel.SetDock(ipTb, Dock.Left); row.Children.Add(ipTb);

        return row;
    }

    UIElement BuildConnectedClientsControls()
    {
        var wrap = new StackPanel();
        var subHead = new TextBlock(); subHead.Text = L("接続クライアント", "Connected clients");
        subHead.Foreground = Fg; subHead.FontSize = 12.5; subHead.FontWeight = FontWeights.SemiBold;
        subHead.Margin = new Thickness(0, 10, 0, 4);
        wrap.Children.Add(subHead);

        var hint = new TextBlock();
        hint.Text = L("拒否された接続先はここで許可できます。", "Refused connections can be allowed here.");
        hint.Foreground = Muted; hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap;
        hint.Margin = new Thickness(0, 0, 0, 4);
        wrap.Children.Add(hint);

        _clientRowsPanel = new StackPanel();
        RefreshClientRows();
        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.MaxHeight = 170;
        scroll.Content = _clientRowsPanel;
        wrap.Children.Add(scroll);

        return wrap;
    }

    static string FmtFloor(double v)
    {
        return v.ToString("0.#", System.Globalization.CultureInfo.InvariantCulture);
    }

    // Disk floor (GB) setter: clamp, persist via SaveKey, refresh the panel value, AND push the new
    // floor LIVE to a running fleet via {"set_disk_floor_gb":N} through the SAME merge-with-existing
    // ReadCommands->WriteCommands path Pause/ForceStart use, so the runner (fleet_runner.py ~L561)
    // picks it up on its next ~1s poll. Mirrors how 強制開始 writes the floor live.
    void SetDiskFloor(double v)
    {
        _diskFloor = Math.Max(0.0, Math.Min(100.0, Math.Round(v, 1)));
        SaveKey("disk_floor_gb", FmtFloor(_diskFloor));
        if (_diskFloorVal != null) _diskFloorVal.Text = FmtFloor(_diskFloor);
        if (RunIsLive())
        {
            var cmd = ReadCommands();
            cmd["set_disk_floor_gb"] = _diskFloor;
            WriteCommands(cmd);
        }
    }

    // RAM floor (MB) setter -- exact mirror of SetDiskFloor: clamp, persist, refresh panel value,
    // and push LIVE to a running fleet via {"set_ram_floor_mb":N} (fleet_runner consumes it next
    // sweep -> ram_box[0] -> autoscale keeps that much RAM free). Stepped by 256 MB.
    TextBlock _ramFloorVal;
    void SetRamFloor(double v)
    {
        _ramFloor = Math.Max(0.0, Math.Min(65536.0, Math.Round(v)));
        SaveKey("ram_floor_mb", ((int)_ramFloor).ToString(System.Globalization.CultureInfo.InvariantCulture));
        if (_ramFloorVal != null) _ramFloorVal.Text = ((int)_ramFloor).ToString();
        if (RunIsLive())
        {
            var cmd = ReadCommands();
            cmd["set_ram_floor_mb"] = _ramFloor;
            WriteCommands(cmd);
        }
    }

    // Effort selector: ComboBox dropdown for min/max/ultra/auto.
    // Persists effort= to settings.txt; the fleet runner reads it at launch.
    static readonly string[] _effortModes = { "min", "max", "ultra", "auto" };
    UIElement EffortControl()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);

        _effortLbl = new TextBlock(); _effortLbl.VerticalAlignment = VerticalAlignment.Center;
        _effortLbl.FontSize = 12; _effortLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_effortLbl);

        _effortBox = new ComboBox();
        _effortBox.ToolTip = _lang == 0 ? "推論の深さ（各ワーカーの調査/反論の強度）" : "Reasoning effort (research/refute depth per worker)";
        _effortBox.Cursor = Cursors.Hand; _effortBox.FontSize = 12;
        _effortBox.FontWeight = FontWeights.SemiBold; _effortBox.MinWidth = 78;
        _effortBox.Padding = new Thickness(8, 2, 4, 2);
        _effortBox.VerticalAlignment = VerticalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(_effortBox, _lang == 0 ? "推論" : "Reasoning");
        FillComboWithHelp(_effortBox, _effortModes, EffortHelp(), _effort);  // per-option hover help
        _effortBox.DropDownOpened += delegate { CloseHeaderPopups("effort"); };
        _effortBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_effortBox);
            if (string.IsNullOrEmpty(sel) || sel == _effort) return;
            _effort = sel;
            SaveKey("effort", _effort);
        };
        wrap.Children.Add(_effortBox);

        PaintEffort();
        return wrap;
    }
    void PaintEffort()
    {
        if (_effortLbl != null) { _effortLbl.Text = T("effort"); _effortLbl.Foreground = Muted; }
        if (_effortBox == null) return;
        // keep the dropdown in sync with _effort (e.g. settings.txt changed externally) without
        // re-firing the persist handler -- assigning the same value is a no-op for SelectionChanged.
        if (!Equals(ComboVal(_effortBox), _effort)) ComboSelectVal(_effortBox, _effort);
        _effortBox.Background = BtnBg; _effortBox.Foreground = Fg; _effortBox.BorderBrush = Border;
        System.Windows.Automation.AutomationProperties.SetName(_effortBox, _lang == 0 ? "推論" : "Reasoning");
        StyleFlatCombo(_effortBox);   // re-template each paint so a theme toggle retints resting + popup
    }

    // WPY ComboBox bug fix: the stock Aero ControlTemplate ignores Background/Foreground and
    // paints a SYSTEM-themed toggle (a light/dark fill that does NOT track our slate theme -- in
    // LIGHT mode it shows as a dark box). We replace the whole template with a flat one whose
    // toggle border, the editable area, and the dropdown Popup/items are bound to OUR theme
    // brushes, so resting + open states match the cockpit in both dark and light. Built entirely
    // in code-behind (no XAML) via FrameworkElementFactory, and re-applied on every PaintEffort/
    // PaintApproval so toggling the theme retints it. Reusable for both dropdowns.
    void StyleFlatCombo(ComboBox cb)
    {
        if (cb == null) return;
        var tmpl = new ControlTemplate(typeof(ComboBox));

        // root grid: [content area *][toggle arrow auto], wrapped by a themed border
        var border = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        border.SetValue(System.Windows.Controls.Border.BackgroundProperty, BtnBg);
        border.SetValue(System.Windows.Controls.Border.BorderBrushProperty, Border);
        border.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new Thickness(1));
        border.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(4));

        var grid = new FrameworkElementFactory(typeof(Grid));
        var c0 = new FrameworkElementFactory(typeof(ColumnDefinition));
        c0.SetValue(ColumnDefinition.WidthProperty, new GridLength(1, GridUnitType.Star));
        var c1 = new FrameworkElementFactory(typeof(ColumnDefinition));
        c1.SetValue(ColumnDefinition.WidthProperty, GridLength.Auto);
        grid.AppendChild(c0); grid.AppendChild(c1);

        // an invisible ToggleButton spanning both columns IS the click target that opens the popup.
        // Its template is just a transparent border so it contributes no system chrome of its own.
        var toggle = new FrameworkElementFactory(typeof(ToggleButton));
        toggle.SetValue(Grid.ColumnSpanProperty, 2);
        toggle.SetValue(ToggleButton.BackgroundProperty, Brushes.Transparent);
        toggle.SetValue(ToggleButton.BorderThicknessProperty, new Thickness(0));
        toggle.SetValue(ToggleButton.FocusableProperty, false);
        toggle.SetValue(ToggleButton.IsTabStopProperty, false);
        var togTmpl = new ControlTemplate(typeof(ToggleButton));
        var togBd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border));
        togBd.SetValue(System.Windows.Controls.Border.BackgroundProperty, Brushes.Transparent);
        togTmpl.VisualTree = togBd;
        toggle.SetValue(ToggleButton.TemplateProperty, togTmpl);
        var togBind = new System.Windows.Data.Binding("IsDropDownOpen");
        togBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        togBind.Mode = BindingMode.TwoWay;
        toggle.SetBinding(ToggleButton.IsCheckedProperty, togBind);

        // the selected-value text (column 0)
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(Grid.ColumnProperty, 0);
        var selBind = new System.Windows.Data.Binding("SelectionBoxItem");
        selBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        cp.SetBinding(ContentPresenter.ContentProperty, selBind);
        cp.SetValue(ContentPresenter.MarginProperty, new Thickness(8, 2, 2, 2));
        cp.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        cp.SetValue(System.Windows.Documents.TextElement.ForegroundProperty, Fg);
        cp.SetValue(UIElement.IsHitTestVisibleProperty, false);

        // a small themed arrow glyph (column 1)
        var arrow = new FrameworkElementFactory(typeof(System.Windows.Shapes.Path));
        arrow.SetValue(Grid.ColumnProperty, 1);
        arrow.SetValue(System.Windows.Shapes.Path.DataProperty, Geometry.Parse("M 0 0 L 8 0 L 4 5 Z"));
        arrow.SetValue(System.Windows.Shapes.Path.FillProperty, Muted);
        arrow.SetValue(System.Windows.Shapes.Path.MarginProperty, new Thickness(4, 0, 8, 0));
        arrow.SetValue(System.Windows.Shapes.Path.VerticalAlignmentProperty, VerticalAlignment.Center);
        arrow.SetValue(UIElement.IsHitTestVisibleProperty, false);

        // the dropdown popup: a themed border around the item host, so the OPEN list also matches.
        var popup = new FrameworkElementFactory(typeof(Popup), "PART_Popup");
        popup.SetValue(Popup.PlacementProperty, PlacementMode.Bottom);
        popup.SetValue(Popup.AllowsTransparencyProperty, true);
        popup.SetValue(Popup.FocusableProperty, false);
        var popBind = new System.Windows.Data.Binding("IsDropDownOpen");
        popBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        popup.SetBinding(Popup.IsOpenProperty, popBind);
        var popBd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border));
        popBd.SetValue(System.Windows.Controls.Border.BackgroundProperty, CardBg);
        popBd.SetValue(System.Windows.Controls.Border.BorderBrushProperty, Border);
        popBd.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new Thickness(1));
        popBd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(4));
        popBd.SetValue(System.Windows.Controls.Border.MarginProperty, new Thickness(0, 2, 0, 0));
        popBd.SetValue(FrameworkElement.MinWidthProperty, 78.0);
        var sv = new FrameworkElementFactory(typeof(ScrollViewer));
        sv.SetValue(ScrollViewer.MaxHeightProperty, 220.0);
        var ip = new FrameworkElementFactory(typeof(ItemsPresenter));
        sv.AppendChild(ip); popBd.AppendChild(sv); popup.AppendChild(popBd);

        grid.AppendChild(toggle); grid.AppendChild(cp); grid.AppendChild(arrow); grid.AppendChild(popup);
        border.AppendChild(grid);
        tmpl.VisualTree = border;
        cb.Template = tmpl;

        // per-item style: themed background + Fg text + Accent-mix hover/selected, so the OPEN
        // list never falls back to the system (dark) item chrome.
        cb.ItemContainerStyle = FlatComboItemStyle();
        cb.ApplyTemplate();
    }

    Style FlatComboItemStyle()
    {
        var st = new Style(typeof(ComboBoxItem));
        st.Setters.Add(new Setter(Control.BackgroundProperty, BtnBg));
        st.Setters.Add(new Setter(Control.ForegroundProperty, Fg));
        st.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(8, 4, 8, 4)));
        st.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        var it = new ControlTemplate(typeof(ComboBoxItem));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.PaddingProperty, new TemplateBindingExtension(Control.PaddingProperty));
        var icp = new FrameworkElementFactory(typeof(ContentPresenter));
        bd.AppendChild(icp);
        it.VisualTree = bd;
        // hover + selected -> an accent-tinted fill (matches the cockpit's hover idiom), still readable.
        var hover = new Trigger { Property = ComboBoxItem.IsHighlightedProperty, Value = true };
        hover.Setters.Add(new Setter(Control.BackgroundProperty,
            new SolidColorBrush(Mix(C("#ea580c"), CardColor(), 0.22)), "Bd"));
        hover.Setters.Add(new Setter(Control.ForegroundProperty, Fg));
        it.Triggers.Add(hover);
        st.Setters.Add(new Setter(Control.TemplateProperty, it));
        return st;
    }

    // Run-mode selector: run/plan/auto controls how a task starts.  It is intentionally labelled
    // separately from the Approval Center; the old "Approval" label made users expect this combo
    // to contain the actual pending decisions.
    static readonly string[] _approvalModes = { "run", "plan", "auto" };
    UIElement ApprovalControl()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);

        _approvalLbl = new TextBlock(); _approvalLbl.VerticalAlignment = VerticalAlignment.Center;
        _approvalLbl.FontSize = 12; _approvalLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_approvalLbl);

        _approvalBox = new ComboBox();
        _approvalBox.ToolTip = _lang == 0
            ? "実行方式: run=すぐ実行、plan=計画承認待ち、auto=通常フリートは計画承認待ち/フォルダ自律はGO-ASK-STOP判定"
            : "Run mode: run=run now, plan=wait for approval, auto=plain fleet waits for plan approval; folder autonomy uses GO/ASK/STOP";
        _approvalBox.Cursor = Cursors.Hand; _approvalBox.FontSize = 12;
        _approvalBox.FontWeight = FontWeights.SemiBold; _approvalBox.MinWidth = 74;
        _approvalBox.Padding = new Thickness(8, 2, 4, 2);
        _approvalBox.VerticalAlignment = VerticalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(_approvalBox, _lang == 0 ? "実行方式" : "Run mode");
        FillComboWithHelp(_approvalBox, _approvalModes, ApprovalHelp(), _approval);  // per-option hover help
        _approvalBox.DropDownOpened += delegate { CloseHeaderPopups("approval"); };
        _approvalBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_approvalBox);
            if (string.IsNullOrEmpty(sel) || sel == _approval) return;
            _approval = sel;
            SaveKey("approval", _approval);
        };
        wrap.Children.Add(_approvalBox);

        PaintApproval();
        return wrap;
    }
    void PaintApproval()
    {
        if (_approvalLbl != null) { _approvalLbl.Text = T("run_mode"); _approvalLbl.Foreground = Muted; }
        if (_approvalBox == null) return;
        if (!Equals(ComboVal(_approvalBox), _approval)) ComboSelectVal(_approvalBox, _approval);
        _approvalBox.Background = BtnBg; _approvalBox.Foreground = Fg; _approvalBox.BorderBrush = Border;
        System.Windows.Automation.AutomationProperties.SetName(_approvalBox, _lang == 0 ? "実行方式" : "Run mode");
        StyleFlatCombo(_approvalBox);   // same flat-template fix so the open list matches the theme
    }

    // ── per-option hover help for the 推論 / 承認 dropdowns ──────────────────────────────────
    // Other cockpit buttons carry a ToolTip; the effort/approval dropdown OPTIONS did not. We now
    // add each option as a ComboBoxItem with its own ToolTip, so hovering an item in the open list
    // explains what that mode does. ComboVal/ComboSelectVal keep the string-based selection logic.
    Dictionary<string, string> EffortHelp()
    {
        return _lang == 0
            ? new Dictionary<string, string> {
                { "min", "最小: 速い・浅い。簡単なタスク向け（調査/反論を抑制）" },
                { "max", "最大: 深い調査と自己反論。難タスク向け（遅いが高品質）" },
                { "ultra", "ウルトラ: 最深。research＋self-test＋反論をフル動員（最重・最高品質）" },
                { "auto", "自動: タスク難度に応じて min〜ultra を自動選択（推奨）" } }
            : new Dictionary<string, string> {
                { "min", "Min: fast, shallow. Easy tasks (research/refute suppressed)." },
                { "max", "Max: deep research + self-refute. Hard tasks (slower, higher quality)." },
                { "ultra", "Ultra: deepest. Full research + self-test + refutation (heaviest, best)." },
                { "auto", "Auto: pick min..ultra by task difficulty (recommended)." } };
    }
    Dictionary<string, string> ApprovalHelp()
    {
        return _lang == 0
            ? new Dictionary<string, string> {
                { "run", "run: 承認を挟まず即実行。" },
                { "plan", "plan: 計画を提示して「承認待ち」で停止。カードに承認/修正を steer。" },
                { "auto", "auto: 通常フリートは計画承認待ち／フォルダ自律は GO-ASK-STOP ゲートで自走。" } }
            : new Dictionary<string, string> {
                { "run", "run: execute immediately, no approval step." },
                { "plan", "plan: present a plan and pause at approval; steer the card to approve/edit." },
                { "auto", "auto: plain fleet waits for plan approval; folder autonomy self-runs via GO-ASK-STOP." } };
    }

    // Persistent decision entry point.  Unlike the run-mode combo, this button represents actual
    // human decisions and remains useful while the fleet is idle.
    UIElement ApprovalCenterControl()
    {
        _approvalCenterBtn = new Button();
        _approvalCenterBtn.Cursor = Cursors.Hand;
        _approvalCenterBtn.BorderThickness = new Thickness(1);
        _approvalCenterBtn.Padding = new Thickness(10, 3, 8, 3);
        _approvalCenterBtn.Margin = new Thickness(0, 0, 12, 0);
        _approvalCenterBtn.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBtn.Template = FlatButtonTemplate();

        var row = new StackPanel(); row.Orientation = Orientation.Horizontal;
        row.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterLabel = new TextBlock();
        _approvalCenterLabel.FontSize = 12; _approvalCenterLabel.FontWeight = FontWeights.SemiBold;
        _approvalCenterLabel.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(_approvalCenterLabel);

        _approvalCenterBadge = new Border();
        _approvalCenterBadge.CornerRadius = new CornerRadius(8);
        _approvalCenterBadge.MinWidth = 18; _approvalCenterBadge.Height = 18;
        _approvalCenterBadge.Margin = new Thickness(7, 0, 0, 0);
        _approvalCenterBadge.Padding = new Thickness(4, 0, 4, 0);
        _approvalCenterBadgeText = new TextBlock();
        _approvalCenterBadgeText.FontSize = 10; _approvalCenterBadgeText.FontWeight = FontWeights.Bold;
        _approvalCenterBadgeText.HorizontalAlignment = HorizontalAlignment.Center;
        _approvalCenterBadgeText.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBadge.Child = _approvalCenterBadgeText;
        row.Children.Add(_approvalCenterBadge);
        _approvalCenterBtn.Content = row;
        _approvalCenterBtn.Click += delegate { CloseHeaderPopups(null); ShowApprovalCenter(); };
        PaintApprovalCenterButton(0);
        return _approvalCenterBtn;
    }

    void PaintApprovalCenterButton(int pending)
    {
        if (_approvalCenterBtn == null) return;
        bool hasPending = pending > 0;
        _approvalCenterBtn.Background = hasPending ? Theme.Br(Theme.SurfaceSubtle(_dark)) : BtnBg;
        _approvalCenterBtn.BorderBrush = hasPending ? Theme.Br(Theme.Warning(_dark)) : Border;
        _approvalCenterBtn.Foreground = Fg;
        if (_approvalCenterLabel != null)
        {
            _approvalCenterLabel.Text = _lang == 0 ? "承認" : "Approvals";
            _approvalCenterLabel.Foreground = hasPending ? Theme.Br(Theme.Warning(_dark)) : Fg;
        }
        if (_approvalCenterBadge != null)
            _approvalCenterBadge.Background = hasPending ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.SurfaceSubtle(_dark));
        if (_approvalCenterBadgeText != null)
        {
            _approvalCenterBadgeText.Text = pending.ToString();
            _approvalCenterBadgeText.Foreground = hasPending ? White : Muted;
        }
        string accessible = _lang == 0
            ? ("承認センター、未処理 " + pending + " 件")
            : ("Approval Center, " + pending + " pending");
        _approvalCenterBtn.ToolTip = accessible;
        System.Windows.Automation.AutomationProperties.SetName(_approvalCenterBtn, accessible);
    }
    void FillComboWithHelp(ComboBox cb, string[] opts, Dictionary<string, string> help, string current)
    {
        cb.Items.Clear();
        foreach (string m in opts)
        {
            var it = new ComboBoxItem(); it.Content = m;
            string h; if (help != null && help.TryGetValue(m, out h)) it.ToolTip = h;
            cb.Items.Add(it);
            if (m == current) cb.SelectedItem = it;
        }
    }
    static string ComboVal(ComboBox cb)
    {
        var it = cb.SelectedItem as ComboBoxItem;
        return it != null ? (it.Content as string) : (cb.SelectedItem as string);
    }
    static void ComboSelectVal(ComboBox cb, string val)
    {
        foreach (var o in cb.Items)
        {
            var it = o as ComboBoxItem;
            if (it != null && (it.Content as string) == val) { cb.SelectedItem = it; return; }
        }
    }

    // Fleet-wide controls: Pause/Resume toggle + Stop-all. Both write into commands.json
    // via WriteCommands, merging with ReadCommands first so a queued close/steer/add isn't
    // clobbered. fleet_runner._drain_commands consumes {"pause":bool}/{"stop":true} each sweep.
    UIElement FleetControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);

        _pauseBtn = new Button();
        _pauseBtn.ToolTip = _lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)";
        System.Windows.Automation.AutomationProperties.SetName(_pauseBtn, _lang == 0 ? "一時停止" : "Pause");
        _pauseBtn.Cursor = Cursors.Hand; _pauseBtn.BorderThickness = new Thickness(1);
        _pauseBtn.Width = 32; _pauseBtn.Height = 32; _pauseBtn.Padding = new Thickness(0);
        _pauseBtn.Margin = new Thickness(0, 0, 8, 0);
        _pauseBtn.VerticalAlignment = VerticalAlignment.Center;
        // FlatButtonTemplate makes Background=BtnBg actually render (the default Aero template
        // paints its own light gradient, making a light-colored icon invisible in dark mode).
        _pauseBtn.Template = FlatButtonTemplate();
        _pauseIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform };
        {
            var vb = new Viewbox();
            vb.Width = 16; vb.Height = 16;
            vb.Stretch = Stretch.Uniform;
            vb.HorizontalAlignment = HorizontalAlignment.Center;
            vb.VerticalAlignment = VerticalAlignment.Center;
            vb.Child = _pauseIcon;
            _pauseBtn.Content = vb;
        }
        _pauseBtn.Click += delegate
        {
            // Pause only means something to a LIVE fleet. With no live consumer the command would
            // sit unread in commands.json while the label lied "Resume" -- so do nothing then (the
            // button is also disabled per-tick by RefreshPauseEnabled, this is belt-and-suspenders).
            if (!RunIsLive()) { if (_paused) { _paused = false; PaintPause(); } return; }
            _paused = !_paused;
            var cmd = ReadCommands();
            cmd["pause"] = _paused;
            WriteCommands(cmd);
            PaintPause();
        };
        group.Children.Add(_pauseBtn);

        _stopBtn = new Button();
        _stopBtn.ToolTip = _lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run";
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn, _lang == 0 ? "停止" : "Stop");
        _stopBtn.Cursor = Cursors.Hand; _stopBtn.BorderThickness = new Thickness(1);
        _stopBtn.Width = 32; _stopBtn.Height = 32; _stopBtn.Padding = new Thickness(0);
        _stopBtn.VerticalAlignment = VerticalAlignment.Center;
        // Same fix: FlatButtonTemplate so Background is honoured and the icon fill contrasts correctly.
        _stopBtn.Template = FlatButtonTemplate();
        _stopIcon = new System.Windows.Shapes.Path { Stretch = Stretch.Uniform,
            Data = Geometry.Parse("M3,3 H13 V13 H3 Z") };   // a stop square
        {
            var vb2 = new Viewbox();
            vb2.Width = 16; vb2.Height = 16;
            vb2.Stretch = Stretch.Uniform;
            vb2.HorizontalAlignment = HorizontalAlignment.Center;
            vb2.VerticalAlignment = VerticalAlignment.Center;
            vb2.Child = _stopIcon;
            _stopBtn.Content = vb2;
        }
        _stopBtn.Click += delegate
        {
            var cmd = ReadCommands();
            cmd["stop"] = true;
            WriteCommands(cmd);
            // FIX B: immediate optimistic feedback -- don't wait for the ~700ms sweep to show
            // anything changed. Flip the button into its "stopping" state and dim every
            // non-terminal card NOW; RefreshStoppingState (called each OnTick) clears this the
            // moment the sweep confirms the run actually stopped (or a different run starts).
            _stopping = true;
            _stopStartedMarker = _lastRoot != null ? S(_lastRoot, "started") : "";
            PaintStop();
            ForceRender();
            // No live fleet to consume the stop (status went stale, e.g. a run was killed and its
            // last status froze with workers still shown as "running") -> clear every card NOW so the
            // button is never a no-op. Mirrors the per-card release stale path (search ArchiveAndHide).
            if (_lastRoot != null
                && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
            {
                ArchiveAllStale();
                // ArchiveAllStale only edits the LOCAL _history/_hiddenKeys lists (history.json /
                // cockpit_hidden.json) -- it never touches status.json or fleet_run_active.json, so
                // a fresh cockpit launch (or any other reader) would still see the phantom
                // running=true. Finalize the actual sidecars too. See relay/fleet_reaper.py.
                ReapStaleFleetRun();
            }
        };
        group.Children.Add(_stopBtn);

        PaintPause();
        PaintStop();
        return group;
    }
    // Icon buttons (spec): pause shows two bars; while paused it shows a play triangle (resume).
    // Quiet neutral chrome — no orange fill. The intent is carried by the icon + tooltip, not color.
    void PaintPause()
    {
        if (_pauseBtn == null) return;
        if (_pauseIcon != null)
        {
            _pauseIcon.Data = Geometry.Parse(_paused ? "M4,2 L13,8 L4,14 Z"          // play (resume)
                                                      : "M3,2 H6 V14 H3 Z M10,2 H13 V14 H10 Z"); // pause bars
            _pauseIcon.Fill = _paused ? Theme.Br(Theme.Accent(_dark)) : Fg;
        }
        _pauseBtn.ToolTip = _paused
            ? (_lang == 0 ? "再開（凍結を解除して続行）" : "Resume (unfreeze and continue)")
            : (_lang == 0 ? "新規ターン/タブを止めて凍結（再開で続行・状態は保持）" : "Freeze: no new turns/tabs (resume continues; state kept)");
        _pauseBtn.Background = BtnBg; _pauseBtn.Foreground = Fg; _pauseBtn.BorderBrush = Border;
        if (_stopIcon != null && !_stopping) _stopIcon.Fill = Fg;
    }
    // FIX B: paints the Stop button's optimistic "stopping" state (accent-colored icon + tooltip/
    // automation-name swap), mirroring PaintPause's icon-swap pattern for Pause/Resume. Called from
    // the same spots PaintPause is (theme/lang refresh + the click handler) so it never goes stale.
    void PaintStop()
    {
        if (_stopBtn == null) return;
        if (_stopIcon != null) _stopIcon.Fill = _stopping ? Theme.Br(Theme.Accent(_dark)) : Fg;
        _stopBtn.ToolTip = _stopping
            ? T("stop_stopping")
            : (_lang == 0 ? "全ワーカーを停止して走行を終了" : "Cancel every worker and end the run");
        System.Windows.Automation.AutomationProperties.SetName(_stopBtn,
            _stopping ? T("stop_stopping") : (_lang == 0 ? "停止" : "Stop"));
    }
    // Per-tick: Pause is enabled only when a run is LIVE (something to pause). When no run is live we
    // also drop a stale "paused" state so the label can never sit on "Resume" over a dead/absent run.
    void RefreshPauseEnabled(Dictionary<string, object> root)
    {
        bool live = Liveness(root) == 1;
        if (_pauseBtn != null)
        {
            _pauseBtn.IsEnabled = live;
            _pauseBtn.Opacity = live ? 1.0 : 0.5;
            if (!live && _paused) { _paused = false; PaintPause(); }
        }
        // Codex P2 ④: Stop is a strong action; when no run is live it should read as inert, not
        // "still need to stop something?". Disable + dim it (re-enabled the moment a run goes live).
        if (_stopBtn != null)
        {
            _stopBtn.IsEnabled = live;
            _stopBtn.Opacity = live ? 1.0 : 0.5;
            _stopBtn.Cursor = live ? Cursors.Hand : Cursors.Arrow;
        }
    }

    // FIX B: resolve the optimistic "stopping" state set by the Stop click handler. Cleared (and a
    // re-render forced via the Sig() dependency below) once the sweep confirms the run is no longer
    // LIVE -- i.e. actually stopped -- OR a different run has since started (a new `started` marker),
    // so the dimmed cards/button never stay stuck once the real signal arrives.
    void RefreshStoppingState(Dictionary<string, object> root)
    {
        if (!_stopping) return;
        bool live = Liveness(root) == 1;
        string startedNow = root != null ? S(root, "started") : "";
        bool newRun = !string.IsNullOrEmpty(_stopStartedMarker) && !string.IsNullOrEmpty(startedNow)
                      && startedNow != _stopStartedMarker;
        if (!live || newRun)
        {
            _stopping = false;
            PaintStop();
        }
    }

    // MaxTabsStepper() removed: the 開始(デフォルト) stepper now lives only in the settings panel
    // (BuildSettingsPanel builds it inline and assigns _maxMinus/_maxPlus/_maxValue).
    TextBlock _maxValue;

    // Toggle label/colour: ON => accent border + accent soft bg (clearly colored),
    // OFF => muted neutral. Task 2: ON state must be visually distinct with color.
    void PaintAutoToggle()
    {
        if (_autoToggle == null) return;
        _autoToggle.Content = T("autoscale") + ": " + (_autoscale ? "ON" : "OFF");
        if (_autoscale)
        {
            _autoToggle.Foreground = Theme.Br(Theme.Accent(_dark));
            _autoToggle.BorderBrush = Theme.Br(Theme.Accent(_dark));
            _autoToggle.Background = Theme.Br(Theme.AccentSoft(_dark));
        }
        else
        {
            _autoToggle.Foreground = Muted;
            _autoToggle.BorderBrush = Border;
            _autoToggle.Background = Brushes.Transparent;
        }
    }

    // Ceiling stepper only matters under autoscale: grey/disable it when autoscale is off.
    void UpdateAutoEnabled()
    {
        bool on = _autoscale;
        if (_autoMinus != null) _autoMinus.IsEnabled = on;
        if (_autoPlus != null) _autoPlus.IsEnabled = on;
        double op = on ? 1.0 : 0.45;
        if (_autoMinus != null) _autoMinus.Opacity = op;
        if (_autoPlus != null) _autoPlus.Opacity = op;
        if (_autoValue != null) _autoValue.Opacity = op;
        if (_autoLbl != null) _autoLbl.Opacity = op;
    }

    void SetAutoMax(int v)
    {
        _autoMax = Math.Max(1, Math.Min(100, v));
        SaveKey("autoscale_max", _autoMax.ToString());
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        // live-apply only while autoscale is on (ceiling is meaningless otherwise).
        if (_autoscale) RequestSetAutoscaleIfLive();
    }

    // status.json freshness thresholds, mirrored from fleet_runner so the cockpit agrees with the
    // fleet's OWN watchdog on what "the driver is gone" means. A LIVE fleet legitimately freezes
    // status.json while a worker is blocked in a bounded acceptance eval -- up to EVAL_STALL_CEILING_S
    // when a worker is "verifying", otherwise up to the plain stall window. Treating that as dead would
    // be the WORSE bug (double-launch on Retry, dropped Steer), so we only call a run dead past these.
    const double DEAD_STALL_SECS = 150.0;        // == fleet_runner --stall-s default
    const double EVAL_STALL_CEILING_S = 1500.0;  // == relay_fleet.EVAL_STALL_CEILING_S

    // 0 = idle / no run, 1 = LIVE (fresh, or legitimately frozen inside a bounded eval),
    // 2 = DEAD (status claims running but has frozen past the stall window with no eval in flight,
    //     i.e. the fleet_runner process is gone). Reads status.json ONCE. Pure given the file.
    int Liveness(Dictionary<string, object> st)
    {
        if (st == null) return 0;
        bool running = !st.ContainsKey("running") || Convert.ToBoolean(st["running"]);
        bool idle = st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]);
        if (!running || idle) return 0;
        double age = NowUnix() - Dbl(st, "updated");
        if (age <= DEAD_STALL_SECS) return 1;             // fresh enough -> live
        // frozen past the stall window: still LIVE iff a worker is in a bounded eval (mirrors
        // fleet_runner._watchdog_should_reset), bounded by the eval ceiling as the failsafe.
        bool evalInFlight = false;
        if (st.ContainsKey("workers") && st["workers"] is object[])
            foreach (object o in (object[])st["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (NowUnix() < Dbl(w, "eval_busy_until")) { evalInFlight = true; break; }
                if (S(w, "status") == "verifying") { evalInFlight = true; break; }
            }
        if (evalInFlight) return age > EVAL_STALL_CEILING_S ? 2 : 1;
        return 2;                                          // frozen, nothing in eval -> driver gone
    }

    // true iff a run is currently LIVE: running, not idle, AND its status is fresh enough that the
    // fleet_runner is provably still consuming commands.json (so steer/retry/pause actually land).
    bool RunIsLive()
    {
        try { return Liveness(ReadStatus()) == 1; }
        catch (Exception) { return false; }
    }
    // true iff a run claims to be running but its driver has died (frozen status). Used to switch
    // controls to their honest local fallback instead of writing commands nobody will ever read.
    bool RunIsDead()
    {
        try { return Liveness(ReadStatus()) == 2; }
        catch (Exception) { return false; }
    }
    void RequestSetAutoscaleIfLive()
    {
        if (RunIsLive()) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
    }
    void RequestSetMaxtabsIfLive()
    {
        if (RunIsLive()) RequestSetMaxtabs(_maxtabs);
    }
    void SetMaxTabs(int v)
    {
        _maxtabs = Math.Max(1, Math.Min(100, v));
        SaveKey("maxtabs", _maxtabs.ToString());
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        // FIX D: the same stepper gesture must behave the same whether autoscale is ON or OFF.
        // RequestSetMaxtabs writes into commands.json via the SAME live-apply mechanism
        // RequestSetAutoscale (and the old "Apply now" banner button) already used -- there is no
        // technical reason the non-autoscale path needs a separate negotiation banner, so both
        // paths now apply immediately and show the identical lightweight auto-dismissing toast
        // (ShowScaleToast -- the same self-fading overlay already used for UI-zoom feedback).
        bool running = RunIsLive();
        if (running)
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
            ShowScaleToast(T("maxtabs_toast_prefix") + " " + _maxtabs);
        }
        else if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
    }

    Border _mtBanner;
    TextBlock _mtBannerLbl;

    // B: changing max-tabs during a live run -> apply now or next run.
    UIElement BuildMtBanner()
    {
        _mtBanner = new Border();
        _mtBanner.Visibility = Visibility.Collapsed;
        _mtBanner.CornerRadius = new CornerRadius(10);
        _mtBanner.BorderThickness = new Thickness(1);
        _mtBanner.Padding = new Thickness(14, 9, 12, 9);
        _mtBanner.Margin = new Thickness(26, 0, 18, 6);
        DockPanel.SetDock(_mtBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _mtApplyNow = new Button();
        _mtApplyNow.Cursor = Cursors.Hand; _mtApplyNow.BorderThickness = new Thickness(0);
        _mtApplyNow.Padding = new Thickness(12, 4, 12, 4); _mtApplyNow.FontWeight = FontWeights.SemiBold;
        _mtApplyNow.Click += delegate
        {
            if (_autoscale) RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            else RequestSetMaxtabs(_maxtabs);
            _mtBanner.Visibility = Visibility.Collapsed;
        };
        btns.Children.Add(_mtApplyNow);
        _mtLater = new Button();
        _mtLater.Cursor = Cursors.Hand; _mtLater.BorderThickness = new Thickness(1);
        _mtLater.Padding = new Thickness(12, 4, 12, 4); _mtLater.Margin = new Thickness(8, 0, 0, 0);
        _mtLater.Click += delegate { _mtBanner.Visibility = Visibility.Collapsed; };
        btns.Children.Add(_mtLater);
        dp.Children.Add(btns);
        _mtBannerLbl = new TextBlock();
        _mtBannerLbl.VerticalAlignment = VerticalAlignment.Center; _mtBannerLbl.FontSize = 13;
        dp.Children.Add(_mtBannerLbl);
        _mtBanner.Child = dp;
        return _mtBanner;
    }
    Button _mtApplyNow, _mtLater;

    // ── TASK 1+2: capacity-wait banner + 強制開始 (force-start) ──────────────────────
    // The runner's admission gate holds new workers in status=="pending"/pill=="待機列" whenever
    // C: free drops below the reserved disk floor (or free RAM is too tight) -- but nothing told the
    // user WHY a task just sits queued. This banner surfaces that condition reactively each OnTick
    // and offers 強制開始, which disables the disk gate live via {"set_disk_floor_gb":0.0}
    // (consumed by fleet_runner._drain_commands, fleet_runner.py ~L561-563). It's reversible: we
    // capture the floor we zeroed and offer 床を戻す to write it back.
    Border _capBanner;
    TextBlock _capBannerLbl;
    Button _capForceBtn, _capRestoreBtn;
    bool _diskFloorForced = false;      // true once the user hit 強制開始 (gate disabled live)
    double _diskFloorPrev = 0.0;        // the floor we zeroed, so 床を戻す can restore it

    // ── TASK 2 (Bucket C): pending-gate approval banner ──────────────────────────────────
    // When status.json has a non-empty `pending_gates` array, a worker is blocked waiting for
    // human approval. This banner surfaces the gate prominently so it cannot be missed.
    Border _gateBanner;
    ScrollViewer _gateScroll;           // bounds the banner so it cannot starve the rest of the window
    StackPanel _gateCardsPanel;         // holds one card per pending gate (or first gate + count)
    string _gateSig = "";               // last rendered gate set (by token); rebuild only on change

    // RAM admission headroom (mirrors relay_fleet.auto_concurrency headroom_mb=2048): when free
    // physical RAM is around/under this the runner can't open another tab. We additionally surface
    // RAM in the banner when avail_mb is conspicuously low, even if disk is fine.
    const int RAM_FLOOR_MB = 2048;

    UIElement BuildCapBanner()
    {
        _capBanner = new Border();
        _capBanner.Visibility = Visibility.Collapsed;
        _capBanner.CornerRadius = new CornerRadius(10);
        _capBanner.BorderThickness = new Thickness(1);
        _capBanner.Padding = new Thickness(14, 9, 12, 9);
        _capBanner.Margin = new Thickness(26, 0, 18, 6);
        DockPanel.SetDock(_capBanner, Dock.Top);
        var dp = new DockPanel();
        var btns = new StackPanel(); btns.Orientation = Orientation.Horizontal;
        btns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(btns, Dock.Right);
        _capForceBtn = new Button();
        _capForceBtn.Cursor = Cursors.Hand; _capForceBtn.BorderThickness = new Thickness(0);
        _capForceBtn.Padding = new Thickness(12, 4, 12, 4); _capForceBtn.FontWeight = FontWeights.SemiBold;
        _capForceBtn.Template = FlatButtonTemplate();
        _capForceBtn.Click += delegate { ForceStart(); };
        btns.Children.Add(_capForceBtn);
        _capRestoreBtn = new Button();
        _capRestoreBtn.Cursor = Cursors.Hand; _capRestoreBtn.BorderThickness = new Thickness(1);
        _capRestoreBtn.Padding = new Thickness(12, 4, 12, 4); _capRestoreBtn.Margin = new Thickness(8, 0, 0, 0);
        _capRestoreBtn.Template = FlatButtonTemplate();
        _capRestoreBtn.Visibility = Visibility.Collapsed;
        _capRestoreBtn.Click += delegate { RestoreFloor(); };
        btns.Children.Add(_capRestoreBtn);
        dp.Children.Add(btns);
        _capBannerLbl = new TextBlock();
        _capBannerLbl.VerticalAlignment = VerticalAlignment.Center; _capBannerLbl.FontSize = 13;
        _capBannerLbl.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(_capBannerLbl);
        _capBanner.Child = dp;
        return _capBanner;
    }

    // Reactively show/hide the capacity-wait banner each tick. Condition: a LIVE run with at least
    // one worker held at "pending"/"待機列" AND (disk below floor OR RAM conspicuously low). Once
    // the gate clears (or the run ends) the banner hides itself -- it never sticks.
    void UpdateCapBanner(Dictionary<string, object> root)
    {
        if (_capBanner == null) return;

        // The 床OFF (force-running) indicator is independent of the queue: while the user has the
        // gate disabled, keep showing it (with 床を戻す) so the override is never silent.
        if (_diskFloorForced)
        {
            _capBanner.Visibility = Visibility.Visible;
            _capBannerLbl.Text = T("floor_off");
            _capForceBtn.Visibility = Visibility.Collapsed;
            _capRestoreBtn.Visibility = Visibility.Visible;
            _capRestoreBtn.Content = T("floor_restore");
            return;
        }

        bool running = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        bool pendingHeld = false;
        if (running && root.ContainsKey("workers") && root["workers"] is object[])
            foreach (object o in (object[])root["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w != null && S(w, "status") == "pending") { pendingHeld = true; break; }
            }

        double freeDisk = Dbl(root ?? new Dictionary<string, object>(), "free_disk_gb");
        double floor = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        int availMb = I(root ?? new Dictionary<string, object>(), "avail_mb");
        bool diskGated = floor > 0 && freeDisk > 0 && freeDisk < floor;
        // gate on the live status' ram_floor_mb if present, else the cockpit's configurable _ramFloor
        // (no longer the hardcoded RAM_FLOOR_MB const).
        double ramFloorNow = Dbl(root ?? new Dictionary<string, object>(), "ram_floor_mb");
        if (ramFloorNow <= 0) ramFloorNow = _ramFloor;
        bool ramGated = availMb > 0 && ramFloorNow > 0 && availMb < ramFloorNow;

        if (running && pendingHeld && (diskGated || ramGated))
        {
            _capForceBtn.Visibility = Visibility.Visible;
            _capForceBtn.Content = T("force_start");
            _capRestoreBtn.Visibility = Visibility.Collapsed;
            string msg;
            if (diskGated)
            {
                int need = (int)Math.Ceiling(floor - freeDisk);
                msg = _lang == 0
                    ? "ディスクの空きが少ないため、新しいタブを開かずに待機しています（空き " + freeDisk + "GB / 確保 " + floor + "GB）。"
                      + need + "GB 空けると自動で再開します。"
                    : "Disk space is low, so new tabs are paused (free " + freeDisk + "GB / floor " + floor + "GB). "
                      + "Freeing " + need + "GB resumes automatically.";
                if (ramGated)
                    msg += _lang == 0 ? "（空きRAMも " + availMb + "MB と少なめ）"
                                      : " (free RAM is also low at " + availMb + "MB)";
            }
            else
            {
                msg = _lang == 0
                    ? "RAMが少ないため、新しいタブを開かずに待機しています（空き " + availMb + "MB）。"
                    : "RAM is low, so new tabs are paused (free " + availMb + "MB).";
            }
            _capBannerLbl.Text = msg;
            _capBanner.Visibility = Visibility.Visible;
        }
        else
        {
            _capBanner.Visibility = Visibility.Collapsed;
        }
    }

    // 強制開始: disable the disk gate live. Capture the current floor first so 床を戻す can restore
    // it. Writes {"set_disk_floor_gb":0.0} via the SAME merge-with-existing WriteCommands path Pause
    // uses, so a queued close/steer/set_maxtabs isn't clobbered. Consumed by fleet_runner.py ~L561.
    void ForceStart()
    {
        if (!RunIsLive()) return;     // nothing alive to consume the command
        var root = ReadStatus();
        _diskFloorPrev = Dbl(root ?? new Dictionary<string, object>(), "disk_floor_gb");
        var cmd = ReadCommands();
        cmd["set_disk_floor_gb"] = 0.0;
        WriteCommands(cmd);
        _diskFloorForced = true;
        UpdateCapBanner(root);
    }

    // 床を戻す: write the previously-captured floor back, re-arming the disk gate.
    void RestoreFloor()
    {
        var cmd = ReadCommands();
        cmd["set_disk_floor_gb"] = _diskFloorPrev;
        WriteCommands(cmd);
        _diskFloorForced = false;
        UpdateCapBanner(ReadStatus());
    }

    // ── APPROVAL CENTER: durable gates, available even while the fleet is idle ───────────
    string GateDirectory()
    {
        try
        {
            string raw = Environment.GetEnvironmentVariable("MCP_ALLOWED_BASE") ?? "";
            if (string.IsNullOrWhiteSpace(raw)) raw = EnvValue("MCP_ALLOWED_BASE");
            raw = (raw ?? "").Trim();
            string basePath;
            if (raw.Length == 0 || raw == "*")
                basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            else
            {
                string[] roots = raw.Split(Path.PathSeparator);
                basePath = roots.Length > 0 ? roots[0].Trim().Trim('"') : "";
                if (basePath == "~") basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                else if (basePath.StartsWith("~" + Path.DirectorySeparatorChar) || basePath.StartsWith("~/"))
                    basePath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), basePath.Substring(2));
                if (basePath.Length == 2 && basePath[1] == ':') basePath += Path.DirectorySeparatorChar;
                if (basePath.Length == 0) basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            }
            return Path.Combine(Path.GetFullPath(basePath), ".companion_gates");
        }
        catch (Exception)
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".companion_gates");
        }
    }

    List<Dictionary<string, object>> ReadAllGates(Dictionary<string, object> statusRoot)
    {
        // A 1.5-second cache keeps the 700ms UI tick from repeatedly parsing a large audit history,
        // while still surfacing new approvals promptly. AnswerGate invalidates it immediately.
        double now = NowUnix();
        if (now - _gateCacheAt < 1.5) return new List<Dictionary<string, object>>(_gateCache);
        var gates = new List<Dictionary<string, object>>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            string dir = GateDirectory();
            if (Directory.Exists(dir))
            {
                foreach (string p in Directory.GetFiles(dir, "gate_*.json"))
                {
                    try
                    {
                        string text;
                        using (var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                        using (var sr = new StreamReader(fs, Encoding.UTF8)) text = sr.ReadToEnd();
                        var g = _js.DeserializeObject(text) as Dictionary<string, object>;
                        if (g == null) continue;
                        string token = S(g, "token");
                        if (token.Length == 0) token = Path.GetFileNameWithoutExtension(p);
                        g["token"] = token; g["path"] = p;
                        if (seen.Add(token)) gates.Add(g);
                    }
                    catch (Exception) { }
                }
            }
        }
        catch (Exception) { }

        gates.Sort(delegate (Dictionary<string, object> a, Dictionary<string, object> b)
        { return Dbl(b, "asked_at").CompareTo(Dbl(a, "asked_at")); });
        _gateCache = gates; _gateCacheAt = now;
        return new List<Dictionary<string, object>>(_gateCache);
    }

    static bool GateAnswered(Dictionary<string, object> gate)
    {
        try { return gate.ContainsKey("answered") && Convert.ToBoolean(gate["answered"]); }
        catch (Exception) { return false; }
    }

    static bool GateExpired(Dictionary<string, object> gate)
    {
        double expiry = Dbl(gate, "expires_at");
        return expiry > 0 && expiry < NowUnix();
    }

    string GateKind(Dictionary<string, object> gate)
    {
        string token = S(gate, "token").ToLowerInvariant();
        string hay = (S(gate, "context") + " " + S(gate, "question")).ToLowerInvariant();
        if (token.StartsWith("gate_skill_") || hay.Contains("skill approval")) return _lang == 0 ? "Skill" : "Skill";
        if (hay.Contains("contract gate: delete") || hay.Contains(" delete")) return _lang == 0 ? "削除" : "Delete";
        if (hay.Contains("outbound") || hay.Contains("send_immediately")) return _lang == 0 ? "外部送信" : "External send";
        if (hay.Contains("shell_destructive") || hay.Contains("destructive shell")) return _lang == 0 ? "破壊的shell" : "Destructive shell";
        if (hay.Contains("task_router job class")) return _lang == 0 ? "ジョブ" : "Job";
        return _lang == 0 ? "確認" : "Decision";
    }

    bool GateNeedsSecondConfirmation(Dictionary<string, object> gate)
    {
        string kind = GateKind(gate);
        return kind == "削除" || kind == "Delete" || kind == "外部送信" ||
               kind == "External send" || kind == "破壊的shell" || kind == "Destructive shell";
    }

    string GateAge(Dictionary<string, object> gate)
    {
        double age = Math.Max(0, NowUnix() - Dbl(gate, "asked_at"));
        if (age < 60) return _lang == 0 ? "たった今" : "just now";
        if (age < 3600) return ((int)(age / 60)).ToString() + (_lang == 0 ? "分前" : " min ago");
        if (age < 86400) return ((int)(age / 3600)).ToString() + (_lang == 0 ? "時間前" : " hr ago");
        return ((int)(age / 86400)).ToString() + (_lang == 0 ? "日前" : " days ago");
    }

    List<Dictionary<string, object>> PendingGates(Dictionary<string, object> root)
    {
        var pending = new List<Dictionary<string, object>>();
        foreach (var gate in ReadAllGates(root)) if (!GateAnswered(gate)) pending.Add(gate);
        return pending;
    }

    // First two lines of a gate context, ellipsised. Keeps the banner card a fixed, predictable
    // height so the Approve/Deny row below it stays on screen no matter how long the context is.
    static string GateContextPreview(string context)
    {
        if (string.IsNullOrEmpty(context)) return "";
        string[] lines = context.Replace("\r\n", "\n").Split('\n');
        var kept = new List<string>();
        foreach (string line in lines)
        {
            if (line.Trim().Length == 0) continue;
            kept.Add(line.Length > 110 ? line.Substring(0, 110) + "..." : line);
            if (kept.Count == 2) break;
        }
        string text = string.Join("\n", kept.ToArray());
        int shown = kept.Count, total = 0;
        foreach (string line in lines) if (line.Trim().Length > 0) total++;
        if (total > shown) text += "\n... (+" + (total - shown) + " 行)";
        return text;
    }

    // Builds the outer gate banner container (always Collapsed until a gate appears).
    UIElement BuildGateBanner()
    {
        _gateBanner = new Border();
        _gateBanner.Visibility = Visibility.Collapsed;
        _gateBanner.CornerRadius = new CornerRadius(10);
        // Uniform, not a thicker left edge: a one-sided stripe reads as sticky-note
        // decoration. The banner's border colour already carries the urgency.
        _gateBanner.BorderThickness = new Thickness(1);
        _gateBanner.Padding = new Thickness(14, 10, 12, 10);
        // 18px left gutter: the same one _pinnedToolbarHost and the card list use, so inside the
        // run column the banner shares its left edge with the cards it is about.
        _gateBanner.Margin = new Thickness(18, 6, 18, 6);
        DockPanel.SetDock(_gateBanner, Dock.Top);
        _gateCardsPanel = new StackPanel();
        // The banner is DOCKED, so it sits outside the card list's ScrollViewer -- the only
        // scroller in the window. Left unbounded it takes whatever height it wants: with three
        // pending Skill gates at 1.5x zoom the health strip and counters were measured at zero
        // height, the list's viewport collapsed to nothing, and controls landed at y=1199 on a
        // 1080-tall screen. On a shorter display the Approve/Deny rows themselves cross the
        // bottom edge, and with the list scroller gone there is nothing left to scroll -- the
        // request becomes impossible to answer. Its own scroller bounds it instead.
        _gateScroll = new ScrollViewer();
        _gateScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _gateScroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _gateScroll.Content = _gateCardsPanel;
        _gateBanner.Child = _gateScroll;
        return _gateBanner;
    }

    // Called each OnTick. Rebuilds the gate banner cards ONLY when the pending token set changes
    // (sig check), so the buttons don't flicker every 700ms while a gate is active.
    void UpdateGateBanner(Dictionary<string, object> root)
    {
        if (_gateBanner == null || _gateCardsPanel == null) return;

        // Read the durable gate directory directly.  This is deliberately independent of
        // status.json so Skill/import approvals remain visible before and after a fleet run.
        List<Dictionary<string, object>> gates = PendingGates(root);
        PaintApprovalCenterButton(gates.Count);

        // Re-cap on every tick: the window can be resized and the zoom changed at any time.
        // ActualHeight is in physical units while the banner lives inside the scaled root, so
        // divide by the zoom to get the units this MaxHeight is actually measured in.
        if (_gateScroll != null)
        {
            double zoom = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
            double usable = (ActualHeight > 0 ? ActualHeight : 760) / zoom;
            _gateScroll.MaxHeight = Math.Max(150, usable * 0.45);
        }
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
            RefreshApprovalCenter();

        // Build a signature from the current token set (order-insensitive for stability).
        var sb2 = new StringBuilder();
        foreach (var g in gates) sb2.Append(S(g, "token")).Append(';');
        string newSig = sb2.ToString();
        if (newSig == _gateSig) return;   // nothing changed; skip rebuild to avoid flicker
        _gateSig = newSig;

        _gateCardsPanel.Children.Clear();

        if (gates.Count == 0)
        {
            _gateBanner.Visibility = Visibility.Collapsed;
            return;
        }

        // Heading row: "承認が必要です / Approval needed"  (amber/warning, not red/error)
        bool ja3 = _lang == 0;
        var headTb = new TextBlock();
        headTb.Text = ja3
            ? "承認が必要です / Approval needed"
            : "Approval needed / 承認が必要です";
        headTb.FontSize = 13;
        headTb.FontWeight = FontWeights.SemiBold;
        headTb.Foreground = Theme.Br(Theme.Warning(_dark));
        headTb.Margin = new Thickness(0, 0, 0, 8);
        _gateCardsPanel.Children.Add(headTb);

        // Show first gate (or all). If more than 1, show a count note below.
        int showCount = gates.Count > 3 ? 3 : gates.Count;
        for (int gi = 0; gi < showCount; gi++)
        {
            var g2 = gates[gi];
            string question = S(g2, "question");
            string context2 = S(g2, "context");
            string gatePath = S(g2, "path");

            // Question card.
            var gCard = new Border();
            gCard.BorderBrush = Theme.Br(Theme.Warning(_dark));
            gCard.BorderThickness = new Thickness(1);
            gCard.CornerRadius = new CornerRadius(6);
            gCard.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            gCard.Padding = new Thickness(10, 8, 10, 8);
            gCard.Margin = new Thickness(0, 0, 0, gi < showCount - 1 ? 8 : 0);

            var gInner = new StackPanel();

            var qTb = new TextBlock();
            qTb.Text = question;
            qTb.FontSize = 13;
            qTb.Foreground = Theme.Br(Theme.Text(_dark));
            qTb.TextWrapping = TextWrapping.Wrap;
            gInner.Children.Add(qTb);

            if (!string.IsNullOrEmpty(context2))
            {
                var ctxTb = new TextBlock();
                // Preview only. A Skill gate's context is a multi-line file/hash manifest; printed
                // in full it pushed the Approve/Deny row below the fold. The complete text is one
                // click away in the Approval Center, which scrolls.
                ctxTb.Text = GateContextPreview(context2);
                ctxTb.ToolTip = context2;
                ctxTb.FontSize = 11;
                ctxTb.Foreground = Theme.Br(Theme.Muted(_dark));
                ctxTb.TextWrapping = TextWrapping.Wrap;
                ctxTb.Margin = new Thickness(0, 3, 0, 0);
                gInner.Children.Add(ctxTb);
            }

            // Approve / Deny buttons.
            var btnRow = new StackPanel();
            btnRow.Orientation = Orientation.Horizontal;
            btnRow.Margin = new Thickness(0, 8, 0, 0);

            var approveBtn = new Button();
            approveBtn.Cursor = Cursors.Hand;
            approveBtn.BorderThickness = new Thickness(0);
            approveBtn.Padding = new Thickness(14, 4, 14, 4);
            approveBtn.FontWeight = FontWeights.SemiBold;
            approveBtn.FontSize = 12;
            approveBtn.Background = Theme.Br(Theme.Accent(_dark));
            approveBtn.Foreground = White;
            bool reviewFirst = GateNeedsSecondConfirmation(g2) || GateKind(g2) == "Skill";
            approveBtn.Content = reviewFirst
                ? (ja3 ? "詳細を確認" : "Review details")
                : (ja3 ? "承認 / Approve" : "Approve / 承認");
            approveBtn.IsEnabled = !GateExpired(g2);
            if (!approveBtn.IsEnabled)
                approveBtn.ToolTip = ja3 ? "期限切れです。元の操作から承認を再要求してください。" : "Expired. Request approval again from the originating action.";
            string gPath2 = gatePath;
            Dictionary<string, object> capturedGate = g2;
            approveBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                // High-impact operations require their exact scope to be reviewed in the
                // Approval Center before the second confirmation is offered.
                if (GateNeedsSecondConfirmation(capturedGate) || GateKind(capturedGate) == "Skill") ShowApprovalCenter();
                else AnswerGate(gPath2, "approved");
            };
            btnRow.Children.Add(approveBtn);

            var denyBtn = new Button();
            denyBtn.Cursor = Cursors.Hand;
            denyBtn.BorderThickness = new Thickness(1);
            denyBtn.Padding = new Thickness(14, 4, 14, 4);
            denyBtn.FontSize = 12;
            denyBtn.Margin = new Thickness(8, 0, 0, 0);
            denyBtn.Background = Brushes.Transparent;
            denyBtn.Foreground = Theme.Br(Theme.Danger(_dark));
            denyBtn.BorderBrush = Theme.Br(Theme.Danger(_dark));
            denyBtn.Content = ja3 ? "拒否 / Deny" : "Deny / 拒否";
            string gPath3 = gatePath;
            denyBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                AnswerGate(gPath3, "denied");
            };
            btnRow.Children.Add(denyBtn);

            gInner.Children.Add(btnRow);
            gCard.Child = gInner;
            _gateCardsPanel.Children.Add(gCard);
        }

        if (gates.Count > showCount)
        {
            var moreBtn = new Button();
            moreBtn.Content = (ja3
                ? ("+ さらに " + (gates.Count - showCount) + " 件の承認待ち")
                : ("+ " + (gates.Count - showCount) + " more gate(s) pending"));
            moreBtn.FontSize = 11; moreBtn.Cursor = Cursors.Hand;
            moreBtn.Foreground = Theme.Br(Theme.Warning(_dark));
            moreBtn.Background = Brushes.Transparent; moreBtn.BorderThickness = new Thickness(0);
            moreBtn.HorizontalAlignment = HorizontalAlignment.Left;
            moreBtn.Padding = new Thickness(0); moreBtn.Margin = new Thickness(0, 6, 0, 0);
            moreBtn.Click += delegate { ShowApprovalCenter(); };
            _gateCardsPanel.Children.Add(moreBtn);
        }

        _gateBanner.Visibility = Visibility.Visible;
    }

    void ShowApprovalCenter()
    {
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
        {
            _approvalCenterWindow.Activate();
            return;
        }
        var w = new Window();
        _approvalCenterWindow = w;
        w.Title = _lang == 0 ? "承認センター" : "Approval Center";
        // Clamp to the work area. A fixed 760x720 already filled a 1080-tall screen once
        // the UI scale (1.5 by default) was applied, pushing the Approve/Deny row off the
        // bottom -- and because the footer is docked rather than inside the scroller,
        // there was nothing to scroll to reach it: the request became undecidable.
        double waW = SystemParameters.WorkArea.Width;
        double waH = SystemParameters.WorkArea.Height;
        double scale = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
        w.Width = Math.Min(760, Math.Max(480, waW / scale - 60));
        w.Height = Math.Min(720, Math.Max(380, waH / scale - 60));
        w.MinWidth = Math.Min(560, w.Width);
        w.MinHeight = Math.Min(460, w.Height);
        w.MaxHeight = waH;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        w.Background = Bg;
        w.KeyDown += delegate (object sender, KeyEventArgs e)
        { if (e.Key == Key.Escape) { w.Close(); e.Handled = true; } };
        w.Closed += delegate
        {
            _approvalCenterWindow = null; _approvalPendingHost = null; _approvalRecentHost = null;
            _approvalCenterSummary = null; _approvalCenterSig = "";
        };

        var root = new DockPanel();
        var head = new Border();
        head.Background = CardBg; head.BorderBrush = Border;
        head.BorderThickness = new Thickness(0, 0, 0, 1);
        head.Padding = new Thickness(24, 18, 24, 16);
        DockPanel.SetDock(head, Dock.Top);
        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titleCol = new StackPanel();
        var title = new TextBlock(); title.Text = w.Title; title.Foreground = Fg;
        title.FontSize = 20; title.FontWeight = FontWeights.SemiBold;
        titleCol.Children.Add(title);
        var note = new TextBlock();
        note.Text = _lang == 0
            ? "実際の操作許可をここで判断します。run / plan / auto の実行方式とは別です。"
            : "Decide actual operation permissions here. This is separate from run / plan / auto mode.";
        note.Foreground = Muted; note.FontSize = 12; note.Margin = new Thickness(0, 4, 0, 0);
        note.TextWrapping = TextWrapping.Wrap; titleCol.Children.Add(note);
        headGrid.Children.Add(titleCol);
        _approvalCenterSummary = new TextBlock();
        _approvalCenterSummary.Foreground = Theme.Br(Theme.Warning(_dark));
        _approvalCenterSummary.FontWeight = FontWeights.SemiBold;
        _approvalCenterSummary.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterSummary.Margin = new Thickness(18, 0, 0, 0);
        Grid.SetColumn(_approvalCenterSummary, 1); headGrid.Children.Add(_approvalCenterSummary);
        head.Child = headGrid; root.Children.Add(head);

        var scroll = new ScrollViewer(); scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        var body = new StackPanel(); body.Margin = new Thickness(24, 20, 24, 28);
        body.Children.Add(BuildJobApprovalPolicyPanel());
        var pendingTitle = new TextBlock();
        pendingTitle.Text = _lang == 0 ? "判断が必要" : "Needs your decision";
        pendingTitle.Foreground = Fg; pendingTitle.FontSize = 14; pendingTitle.FontWeight = FontWeights.SemiBold;
        pendingTitle.Margin = new Thickness(0, 0, 0, 10); body.Children.Add(pendingTitle);
        _approvalPendingHost = new StackPanel(); body.Children.Add(_approvalPendingHost);

        var recentTitle = new TextBlock();
        recentTitle.Text = _lang == 0 ? "最近の判断" : "Recent decisions";
        recentTitle.Foreground = Fg; recentTitle.FontSize = 14; recentTitle.FontWeight = FontWeights.SemiBold;
        recentTitle.Margin = new Thickness(0, 24, 0, 10); body.Children.Add(recentTitle);
        _approvalRecentHost = new StackPanel(); body.Children.Add(_approvalRecentHost);
        scroll.Content = body; root.Children.Add(scroll);
        w.Content = root;
        _approvalCenterSig = "";
        RefreshApprovalCenter();
        w.ShowDialog();
    }

    UIElement BuildJobApprovalPolicyPanel()
    {
        var box = new Border(); box.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        box.BorderBrush = Border; box.BorderThickness = new Thickness(1);
        box.CornerRadius = new CornerRadius(8); box.Padding = new Thickness(14, 12, 14, 12);
        box.Margin = new Thickness(0, 0, 0, 20);
        var col = new StackPanel();
        var title = new TextBlock(); title.Text = _lang == 0 ? "操作承認ポリシー" : "Operation approval policy";
        title.Foreground = Fg; title.FontSize = 13; title.FontWeight = FontWeights.SemiBold;
        col.Children.Add(title);
        var note = new TextBlock(); note.Foreground = Muted; note.FontSize = 11;
        note.TextWrapping = TextWrapping.Wrap; note.Margin = new Thickness(0, 4, 0, 0);
        note.Text = _lang == 0
            ? "ローカルジョブと自律契約に即時反映。STOP・パス制限・外部Skillの初回/変更承認は常に残ります。"
            : "Applies live to local jobs and autonomy contracts. STOP, path limits, and external-Skill trust remain enforced.";
        col.Children.Add(note);

        var selector = new ComboBox(); selector.MinWidth = 230; selector.HorizontalAlignment = HorizontalAlignment.Left;
        selector.Margin = new Thickness(0, 10, 0, 0); selector.Background = CardBg; selector.Foreground = Fg;
        var labels = _lang == 0
            ? new string[] { "確認（推奨）", "自動", "バイパス" }
            : new string[] { "Confirm (recommended)", "Auto", "Bypass" };
        var values = new string[] { "default", "auto", "bypass" };
        string current = ApprovalPromptWindow.ReadPolicy();
        for (int i = 0; i < values.Length; i++)
        {
            var item = new ComboBoxItem(); item.Content = labels[i]; item.Tag = values[i];
            selector.Items.Add(item); if (values[i] == current) selector.SelectedItem = item;
        }
        StyleFlatCombo(selector); col.Children.Add(selector);
        var help = new TextBlock(); help.Foreground = Muted; help.FontSize = 11;
        help.TextWrapping = TextWrapping.Wrap; help.Margin = new Thickness(0, 7, 0, 0);
        col.Children.Add(help);
        Action updateHelp = delegate
        {
            var selected = selector.SelectedItem as ComboBoxItem;
            string mode = selected == null ? "default" : selected.Tag as string;
            if (mode == "auto") help.Text = _lang == 0
                ? "安全な操作は自動実行、要確認は承認待ち、禁止判定は拒否。"
                : "Safe operations run automatically; risky ones ask; prohibited ones are denied.";
            else if (mode == "bypass") help.Text = _lang == 0
                ? "手動確認を省略。常時有効な安全境界は解除しません。"
                : "Skip manual confirmations. Always-on safety boundaries remain.";
            else help.Text = _lang == 0
                ? "初回クラスを確認し、承認後も危険な内容は毎回確認。"
                : "Confirm first-seen classes; risky payloads still ask every time.";
        };
        updateHelp();
        bool reverting = false;
        selector.SelectionChanged += delegate
        {
            if (reverting) return;
            var item = selector.SelectedItem as ComboBoxItem; if (item == null) return;
            string next = item.Tag as string; string previous = ApprovalPromptWindow.ReadPolicy();
            if (next == "bypass")
            {
                string warning = _lang == 0
                    ? "バイパスではローカルジョブと自律契約の手動確認を省略します。\nSTOP条件、ファイル範囲、外部Skill承認は残ります。\n\n有効にしますか？"
                    : "Bypass skips manual approval for local jobs and autonomy contracts.\nSTOP rules, path limits, and external-Skill approval remain.\n\nEnable it?";
                if (MessageBox.Show(_approvalCenterWindow, warning, _lang == 0 ? "バイパスを有効化" : "Enable bypass",
                    MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
                {
                    reverting = true;
                    foreach (object obj in selector.Items)
                    { var old = obj as ComboBoxItem; if (old != null && (old.Tag as string) == previous) selector.SelectedItem = old; }
                    reverting = false; updateHelp(); return;
                }
            }
            ApprovalPromptWindow.SavePolicy(next); updateHelp();
        };
        box.Child = col; return box;
    }

    void RefreshApprovalCenter()
    {
        if (_approvalCenterWindow == null || _approvalPendingHost == null || _approvalRecentHost == null) return;
        var all = ReadAllGates(ReadStatus());
        var sig = new StringBuilder();
        foreach (var gate in all)
        {
            // Expiry belongs in the signature. Without it a card drawn while the request
            // was still valid kept its Approve button ENABLED after the deadline passed:
            // the click then wrote "approved" that the trust store refused, so the user
            // saw an approval that never took effect. Redraw when the deadline crosses.
            sig.Append(S(gate, "token")).Append(':')
               .Append(GateAnswered(gate) ? S(gate, "answer") : "open").Append(':')
               .Append(GateExpired(gate) ? "exp" : "live").Append(';');
        }
        string currentSig = sig.ToString() + (_dark ? "D" : "L") + _lang;
        if (currentSig == _approvalCenterSig) return;
        _approvalCenterSig = currentSig;

        _approvalPendingHost.Children.Clear(); _approvalRecentHost.Children.Clear();
        var pending = new List<Dictionary<string, object>>();
        var recent = new List<Dictionary<string, object>>();
        foreach (var gate in all)
        {
            if (GateAnswered(gate)) recent.Add(gate); else pending.Add(gate);
        }
        if (_approvalCenterSummary != null)
            _approvalCenterSummary.Text = _lang == 0 ? (pending.Count + " 件 未処理") : (pending.Count + " pending");
        PaintApprovalCenterButton(pending.Count);

        if (pending.Count == 0)
        {
            var empty = new Border(); empty.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            empty.BorderBrush = Border; empty.BorderThickness = new Thickness(1);
            empty.CornerRadius = new CornerRadius(8); empty.Padding = new Thickness(16, 18, 16, 18);
            var text = new TextBlock();
            text.Text = _lang == 0 ? "未処理の承認はありません。" : "No approvals are waiting.";
            text.Foreground = Muted; text.FontSize = 13; empty.Child = text;
            _approvalPendingHost.Children.Add(empty);
        }
        else foreach (var gate in pending) _approvalPendingHost.Children.Add(BuildGateDecisionCard(gate, true));

        int limit = Math.Min(20, recent.Count);
        if (limit == 0)
        {
            var none = new TextBlock(); none.Text = _lang == 0 ? "履歴はまだありません。" : "No decision history yet.";
            none.Foreground = Muted; none.FontSize = 12; _approvalRecentHost.Children.Add(none);
        }
        else for (int i = 0; i < limit; i++) _approvalRecentHost.Children.Add(BuildGateDecisionCard(recent[i], false));
    }

    UIElement BuildGateDecisionCard(Dictionary<string, object> gate, bool actionable)
    {
        bool expired = GateExpired(gate);
        var card = new Border();
        card.Background = CardBg;
        card.BorderBrush = actionable ? Theme.Br(expired ? Theme.Danger(_dark) : Theme.Warning(_dark)) : Border;
        // Actionable cards are marked by border COLOUR, not by a fatter left edge --
        // a one-sided stripe reads as sticky-note decoration.
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(8); card.Padding = new Thickness(14, 12, 14, 12);
        card.Margin = new Thickness(0, 0, 0, 10);
        var col = new StackPanel();

        var meta = new DockPanel(); meta.LastChildFill = true;
        var chip = new Border(); chip.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        chip.BorderBrush = actionable ? Theme.Br(Theme.Warning(_dark)) : Border;
        chip.BorderThickness = new Thickness(1); chip.CornerRadius = new CornerRadius(8);
        chip.Padding = new Thickness(7, 2, 7, 2); chip.Margin = new Thickness(0, 0, 8, 0);
        var chipText = new TextBlock(); chipText.Text = GateKind(gate); chipText.FontSize = 10;
        chipText.FontWeight = FontWeights.SemiBold; chipText.Foreground = actionable ? Theme.Br(Theme.Warning(_dark)) : Muted;
        chip.Child = chipText; DockPanel.SetDock(chip, Dock.Left); meta.Children.Add(chip);
        var age = new TextBlock();
        age.Text = (expired ? (_lang == 0 ? "期限切れ · " : "Expired · ") : "") + GateAge(gate);
        age.Foreground = expired ? Theme.Br(Theme.Danger(_dark)) : Muted; age.FontSize = 11;
        age.VerticalAlignment = VerticalAlignment.Center; meta.Children.Add(age);
        col.Children.Add(meta);

        var question = new TextBlock(); question.Text = S(gate, "question");
        question.Foreground = Fg; question.FontSize = 13; question.FontWeight = FontWeights.SemiBold;
        question.TextWrapping = TextWrapping.Wrap; question.Margin = new Thickness(0, 8, 0, 0);
        col.Children.Add(question);

        string context = S(gate, "context");
        if (context.Length > 0)
        {
            var details = new Expander(); details.Header = _lang == 0 ? "対象と詳細を確認" : "Review scope and details";
            details.Foreground = Muted; details.FontSize = 11; details.Margin = new Thickness(0, 7, 0, 0);
            details.IsExpanded = actionable && (GateNeedsSecondConfirmation(gate) || GateKind(gate) == "Skill");
            var detailText = new TextBlock(); detailText.Text = context; detailText.Foreground = Muted;
            detailText.FontFamily = new FontFamily("Cascadia Mono, Consolas"); detailText.FontSize = 11;
            detailText.TextWrapping = TextWrapping.Wrap; detailText.Margin = new Thickness(10, 6, 0, 0);
            details.Content = detailText; col.Children.Add(details);
        }

        if (actionable)
        {
            var row = new StackPanel(); row.Orientation = Orientation.Horizontal; row.Margin = new Thickness(0, 12, 0, 0);
            var approve = new Button(); approve.Content = _lang == 0 ? "承認" : "Approve";
            approve.Cursor = Cursors.Hand; approve.Padding = new Thickness(18, 6, 18, 6);
            approve.BorderThickness = new Thickness(0); approve.Background = Accent; approve.Foreground = White;
            approve.FontWeight = FontWeights.SemiBold; approve.IsEnabled = !expired;
            System.Windows.Automation.AutomationProperties.SetName(approve, _lang == 0 ? "この操作を承認" : "Approve this operation");
            Dictionary<string, object> captured = gate;
            approve.Click += delegate
            {
                if (GateNeedsSecondConfirmation(captured))
                {
                    string msg = (_lang == 0 ? "影響の大きい操作です。対象を確認したうえで本当に承認しますか？\n\n" :
                        "This is a high-impact operation. Approve after reviewing the exact scope?\n\n") + S(captured, "question");
                    if (MessageBox.Show(_approvalCenterWindow, msg, _lang == 0 ? "最終確認" : "Final confirmation",
                        MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes) return;
                }
                AnswerGate(S(captured, "path"), "approved"); RefreshApprovalCenter();
            };
            row.Children.Add(approve);

            var deny = new Button(); deny.Content = _lang == 0 ? "拒否" : "Deny";
            deny.Cursor = Cursors.Hand; deny.Padding = new Thickness(18, 6, 18, 6);
            deny.Margin = new Thickness(8, 0, 0, 0); deny.Background = Brushes.Transparent;
            deny.Foreground = Theme.Br(Theme.Danger(_dark)); deny.BorderBrush = Theme.Br(Theme.Danger(_dark));
            deny.BorderThickness = new Thickness(1);
            System.Windows.Automation.AutomationProperties.SetName(deny, _lang == 0 ? "この操作を拒否" : "Deny this operation");
            deny.Click += delegate { AnswerGate(S(captured, "path"), "denied"); RefreshApprovalCenter(); };
            row.Children.Add(deny); col.Children.Add(row);

            if (expired)
            {
                var expiredNote = new TextBlock();
                expiredNote.Text = _lang == 0 ? "期限切れのため承認できません。拒否するか、元の操作から再要求してください。" :
                    "This request expired. Deny it or request approval again from the originating action.";
                expiredNote.Foreground = Theme.Br(Theme.Danger(_dark)); expiredNote.FontSize = 11;
                expiredNote.Margin = new Thickness(0, 7, 0, 0); expiredNote.TextWrapping = TextWrapping.Wrap;
                col.Children.Add(expiredNote);
            }
        }
        else
        {
            var verdict = new TextBlock();
            string answer = S(gate, "answer");
            bool approved = answer.Equals("approved", StringComparison.OrdinalIgnoreCase);
            verdict.Text = approved ? (_lang == 0 ? "承認済み" : "Approved") : (_lang == 0 ? "拒否済み" : "Denied");
            verdict.Foreground = Theme.Br(approved ? Theme.Success(_dark) : Theme.Danger(_dark));
            verdict.FontSize = 11; verdict.FontWeight = FontWeights.SemiBold; verdict.Margin = new Thickness(0, 8, 0, 0);
            col.Children.Add(verdict);
        }
        card.Child = col; return card;
    }

    // Atomic gate answer: read the file at `path`, set answered=true + answer=<verdict>, write back.
    // Uses temp-file + rename for atomicity (consistent with the relay's reader expectation).
    void AnswerGate(string path, string verdict)
    {
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            // Normalize: forward slashes may be in the path from status.json.
            string localPath = Path.GetFullPath(path.Replace('/', Path.DirectorySeparatorChar));
            string gateRoot = Path.GetFullPath(GateDirectory()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!localPath.StartsWith(gateRoot, StringComparison.OrdinalIgnoreCase)) return;
            if (!File.Exists(localPath)) return;

            string text;
            using (var fs2 = new FileStream(localPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr2 = new System.IO.StreamReader(fs2, Encoding.UTF8))
                text = sr2.ReadToEnd();

            var gd = _js.DeserializeObject(text) as Dictionary<string, object>;
            if (gd == null) gd = new Dictionary<string, object>();
            gd["answered"] = true;
            gd["answer"] = verdict;
            gd["answered_at"] = NowUnix();

            string updated = _js.Serialize(gd);
            // Atomic: write to a temp file in the same directory, then rename.
            string dir2 = Path.GetDirectoryName(localPath);
            string tmp = Path.Combine(dir2, Path.GetFileName(localPath) + ".tmp");
            File.WriteAllText(tmp, updated, new UTF8Encoding(false));
            try { File.Replace(tmp, localPath, null); }
            catch (Exception)
            {
                // Older/non-NTFS environments may not implement Replace; keep the same-directory
                // fallback for compatibility, then remove the temporary file.
                File.Copy(tmp, localPath, true);
                try { File.Delete(tmp); } catch (Exception) { }
            }

            // Force a sig reset so the banner refreshes on the next tick (the relay
            // will drop this gate from pending_gates once it sees answered=true).
            _gateSig = "";
            _approvalCenterSig = "";
            _gateCacheAt = 0;
        }
        catch (Exception) { }
    }

    Button MiniButton(string txt)
    {
        var b = new Button(); b.Content = txt; b.Width = 26; b.Height = 26;
        b.FontSize = 15; b.Cursor = Cursors.Hand; b.BorderThickness = new Thickness(1);
        b.Padding = new Thickness(0); b.VerticalContentAlignment = VerticalAlignment.Center;
        // theme fill/text/border at creation. The flat template TemplateBinds to these; the
        // settings-panel steppers are built in BuildSettingsPanel and never re-themed by PaintChrome,
        // so without this they fell back to the Button system default = the "wrong colour" reported.
        b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border;
        b.Template = FlatButtonTemplate();   // honour Background in BOTH themes (see FlatButtonTemplate)
        return b;
    }

    // The stock Aero Button template paints a SYSTEM gradient over our Background and a light/dark
    // system hover -> in LIGHT mode the −/+ steppers read as a dark/grey box that ignores BtnBg.
    // This flat template binds the fill/stroke straight to the control's Background/BorderBrush, so
    // the steppers track our theme exactly. Built in code-behind (no XAML), reused for all MiniButtons.
    ControlTemplate FlatButtonTemplate() { return FlatButtonTemplate(false); }
    // leftAlign=true => content hugs the left edge (list/menu rows, so icons line up across rows);
    // default false keeps the centered content the icon/pause/stop/stepper buttons rely on.
    ControlTemplate FlatButtonTemplate(bool leftAlign)
    {
        var t = new ControlTemplate(typeof(Button));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.BorderBrushProperty, new TemplateBindingExtension(Control.BorderBrushProperty));
        bd.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new TemplateBindingExtension(Control.BorderThicknessProperty));
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(4));
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(ContentPresenter.HorizontalAlignmentProperty, leftAlign ? HorizontalAlignment.Left : HorizontalAlignment.Center);
        cp.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        bd.AppendChild(cp);
        t.VisualTree = bd;
        return t;
    }
    Button IconButton(string glyph, double size)
    {
        return IconButton(glyph, size, "");
    }
    Button IconButton(string glyph, double size, string autoName)
    {
        var b = new Button(); b.Width = 36; b.Height = 30; b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1); b.Margin = new Thickness(4, 0, 0, 0);
        b.Content = MakeIcon(glyph, size, Fg); b.Tag = glyph;
        if (!string.IsNullOrEmpty(autoName))
            System.Windows.Automation.AutomationProperties.SetName(b, autoName);
        return b;
    }

    // Close all header popups/dropdowns except the one named by `except`.
    // except: "settings" | "overflow" | "effort" | "approval" | null (close all)
    void CloseHeaderPopups(string except)
    {
        if (except != "settings" && _settingsPopup != null) _settingsPopup.IsOpen = false;
        if (except != "overflow" && _overflowPopup != null) _overflowPopup.IsOpen = false;
        if (except != "effort" && _effortBox != null) _effortBox.IsDropDownOpen = false;
        if (except != "approval" && _approvalBox != null) _approvalBox.IsDropDownOpen = false;
        // The /help popup is a separate HWND that used to linger when a header popup opened. Fold it
        // into the same one-at-a-time close path (plus the slash-command palette) so opening
        // settings/overflow/effort/approval also dismisses help, and vice versa.
        if (except != "help" && _helpPopup != null) _helpPopup.IsOpen = false;
        if (except != "slash" && _gcmdPopup != null) _gcmdPopup.IsOpen = false;
    }

    // Read-only runtime status. Hide it while idle/finished; Settings owns configuration.
    void UpdateWorkerChip(int openTabs, int liveCap, bool isLive)
    {
        if (_workerChip == null) return;
        if (_workerChipBorder != null)
            _workerChipBorder.Visibility = isLive ? Visibility.Visible : Visibility.Collapsed;
        if (!isLive) return;
        int open = Math.Max(0, openTabs);
        int cap = Math.Max(1, liveCap > 0 ? liveCap : _maxtabs);
        string label = _lang == 0 ? ("タブ " + open + "/" + cap) : ("Tabs " + open + "/" + cap);
        _workerChip.Text = label;
        _workerChip.Foreground = Theme.Br(Theme.Muted(_dark));
    }

    void PaintWorkerChipBorder(Border b)
    {
        if (b == null) return;
        b.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        b.BorderBrush = Theme.Br(Theme.Border(_dark));
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        _header.Foreground = Fg;
        _sub.Foreground = Muted;
        if (_list != null) _list.Background = Bg;
        // satellite icon removed per UX feedback -- nothing to paint at top-left of the title row.
        // restyle the header buttons for the theme
        foreach (Button b in new Button[] { _mainBtn, _siBtn, _themeBtn, _langBtn, _gearBtn, _maxMinus, _maxPlus, _autoMinus, _autoPlus })
            if (b != null) { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        if (_overflowBtn != null) { _overflowBtn.Background = BtnBg; _overflowBtn.Foreground = Fg; _overflowBtn.BorderBrush = Border; }
        if (_gearBtn != null) _gearBtn.Content = MakeIcon("settings", 18, Fg);
        if (_themeBtn != null) _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        if (_langBtn != null) _langBtn.Content = MakeIcon("translate", 18, Fg);
        if (_maxValue != null) _maxValue.Foreground = Fg;
        if (_autoLbl != null) _autoLbl.Foreground = Muted;
        if (_autoValue != null) _autoValue.Foreground = Fg;
        if (_workerChip != null) UpdateWorkerChip(0, 0, false);
        PaintWorkerChipBorder(_workerChipBorder);
        PaintHealthChrome();
        ApplyHealthToUi();     // re-tint the dots for the new theme
        _agentMarkerId = ExtractAgentMarker();
        PaintAutoToggle();
        UpdateAutoEnabled();
        PaintEffort();
        PaintApproval();
        PaintApprovalCenterButton(PendingGates(ReadStatus()).Count);
        PaintPause();
        PaintStop();
        if (_stopBtn != null) { _stopBtn.Background = BtnBg; _stopBtn.Foreground = Fg; _stopBtn.BorderBrush = Border; }
        if (_inBar != null) _inBar.Background = Bg;
        // Floating composer: SurfaceSubtle bg (quieter than BtnBg); faint border for low-contrast frame.
        if (_composerBox != null)
        {
            _composerBox.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _composerBox.BorderBrush = Theme.Br(Theme.Border(_dark));
            _composerBox.Opacity = 1.0;
        }
        if (_goalInput != null)
        {
            _goalInput.Background = Brushes.Transparent; _goalInput.Foreground = Fg;
            _goalInput.BorderBrush = Brushes.Transparent; _goalInput.CaretBrush = Fg;
        }
        if (_composerWatermark != null) _composerWatermark.Foreground = Muted;
        if (_composerHint != null) _composerHint.Foreground = Muted;
        if (_startBtn != null) { _startBtn.Background = Accent; _startBtn.Foreground = White; }
        if (_folderBtn != null) { _folderBtn.Background = Brushes.Transparent; _folderBtn.Foreground = Fg; _folderBtn.BorderBrush = Theme.Br(Theme.BorderStrong(_dark)); }
        if (_startNote != null) _startNote.Foreground = Muted;
        // Banners: a quiet surface card with a 3px warning LEFT rail (spec), not an orange fill.
        // The action is a secondary warning button (outline), not a primary orange block.
        var warn = Theme.Br(Theme.Warning(_dark));
        if (_mtBanner != null)
        {
            _mtBanner.Background = CardBg;
            _mtBanner.BorderThickness = new Thickness(1);
            _mtBanner.BorderBrush = warn;
            if (_mtBannerLbl != null) _mtBannerLbl.Foreground = Fg;
        }
        if (_mtApplyNow != null) { _mtApplyNow.Background = Brushes.Transparent; _mtApplyNow.Foreground = warn; _mtApplyNow.BorderBrush = warn; _mtApplyNow.BorderThickness = new Thickness(1); }
        if (_mtLater != null) { _mtLater.Background = Brushes.Transparent; _mtLater.Foreground = Muted; _mtLater.BorderBrush = Border; }
        if (_gateBanner != null)
        {
            _gateBanner.Background = CardBg;
            // Uniform, not a thicker left edge: a one-sided stripe reads as sticky-note
        // decoration. The banner's border colour already carries the urgency.
        _gateBanner.BorderThickness = new Thickness(1);
            _gateBanner.BorderBrush = warn;
            // Force a full rebuild on theme change so buttons repaint correctly.
            _gateSig = "";
        }
        if (_capBanner != null)
        {
            _capBanner.Background = CardBg;
            _capBanner.BorderThickness = new Thickness(1);
            _capBanner.BorderBrush = warn;
            if (_capBannerLbl != null) _capBannerLbl.Foreground = Fg;
        }
        if (_capForceBtn != null) { _capForceBtn.Background = Brushes.Transparent; _capForceBtn.Foreground = warn; _capForceBtn.BorderBrush = warn; }
        if (_capRestoreBtn != null) { _capRestoreBtn.Background = Brushes.Transparent; _capRestoreBtn.Foreground = Fg; _capRestoreBtn.BorderBrush = Border; }
        Relabel();
    }

    void Relabel()
    {
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        if (_autoLbl != null) _autoLbl.Text = T("max_tabs2");
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        PaintAutoToggle();
        PaintEffort();
        PaintApproval();
        PaintApprovalCenterButton(PendingGates(ReadStatus()).Count);
        PaintPause();
        PaintStop();
        // _stopBtn / _pauseBtn now render drawn icons (PaintPause/PaintStop), not text labels.
        if (_startBtn != null) _startBtn.Content = T("start");
        if (_folderBtn != null) _folderBtn.Content = T("folder");
        if (_goalInput != null) _goalInput.ToolTip = T("goalhint");
        // _startNote is now transient feedback only; the persistent hint lives in the composer footer.
        if (_mtApplyNow != null) _mtApplyNow.Content = _lang == 0 ? "今すぐ反映" : "Apply now";
        if (_mtLater != null) _mtLater.Content = _lang == 0 ? "次回起動から" : "Next run";
    }

    // Language toggle must re-evaluate EVERY localized string, not just the handful Relabel() touches.
    // Strings set once at construction (the goal-box hint watermark, tooltips, the settings-panel
    // labels, filter/retry buttons, ...) otherwise keep their build-time language -> the user sees a
    // half-translated UI after toggling. Rebuilding the whole chrome re-runs every T()/_lang branch.
    // _rows is bound once and survives, so cards persist; we preserve the typed goal text + scroll.
    void RebuildChrome()
    {
        string goalText = _goalInput != null ? _goalInput.Text : null;
        bool prevRunActive = _composerRunActive;
        BuildChrome();
        if (goalText != null && _goalInput != null) _goalInput.Text = goalText;
        // A2-2: reset spine+composer state after a full chrome rebuild so PaintComposerMode
        // re-applies the current mode (BuildChrome created fresh controls with idle defaults).
        _spineSig = "";
        _composerRunActive = !prevRunActive; // force the guard in PaintComposerMode to fire
        PaintChrome();
        ForceRender();
    }

    void ApplyTheme()
    {
        ApplyThemeBrushes();
        PaintChrome();
        ForceRender();
    }
    void ForceRender()
    {
        _lastSig = "";
        OnTick(null, null);
    }

    // ── poll loop ─────────────────────────────────────────────────────────────────
    void OnTick(object sender, EventArgs e)
    {
        // follow external theme/lang/maxtabs edits (e.g. the chat toggled the theme)
        try
        {
            if (File.Exists(SettingsFile))
            {
                long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
                if (m != _settingsMtime)
                {
                    bool d0 = _dark; int l0 = _lang; double s0 = _uiScale;
                    bool a0 = _uiAuto; double t0 = _scaleTarget;
                    LoadSettings();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { RebuildChrome(); }
                    // External ui_scale edit (e.g. the chat app zoomed / switched to auto): apply it live
                    // so both apps stay in lock-step. In AUTO the effective scale is per-monitor, so we
                    // recompute for THIS window's monitor (not the other app's) — silently. In MANUAL we
                    // push the shared number and flash the %. RebuildChrome above (if lang changed) already
                    // re-anchored the transform on the new root.
                    if (_uiAuto)
                    {
                        // Recompute if we just entered auto, or the target changed, so the transform tracks.
                        if (!a0 || Math.Abs(t0 - _scaleTarget) > 0.001 || Math.Abs(s0 - EffectiveAutoScale(CurrentMonitorScale())) > 0.001)
                            ApplyAutoScale(false);
                        RefreshUiScaleControls();
                    }
                    else if (a0 || Math.Abs(s0 - _uiScale) > 0.001)
                    {
                        // Other app set a manual number (or switched auto->manual): reflect + toast.
                        _uiScale = Math.Max(0.8, Math.Min(2.0, _uiScale));
                        PushScaleToTransform(); RefreshUiScaleControls(); ShowScaleToast();
                    }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> root = ReadStatus();
        RefreshPauseEnabled(root);          // Pause is only meaningful for a live run; grey it out otherwise
        RefreshStoppingState(root);         // FIX B: resolve the optimistic "stopping" state once the sweep confirms it
        UpdateGateBanner(root);             // Bucket C TASK 2: show pending approval gates (blocks worker until answered)
        UpdateCapBanner(root);              // TASK 1: surface the admission-gate wait reactively each tick
        bool idle = root == null || I(root, "total") == 0
                    || (root.ContainsKey("idle") && Convert.ToBoolean(root["idle"]));
        if (idle)
        {
            _header.Text = "Fleet";
            _sub.Text = "";                // the empty-state block in the body carries the message now
            if (_subChips != null) _subChips.Children.Clear();   // Feature 2: no chips while idle either
            UpdateWorkerChip(0, 0, false); // configuration lives in Settings while idle
            // A2-2: collapse spine and idle composer when no run
            RefreshSpine(root, true);
            PaintComposerMode(false);
            // Idle: only history (or the empty state) is shown — no live board to filter, so the
            // pinned filter bar is hidden here just as the toolbar row was never emitted when idle.
            if (_pinnedToolbarHost != null)
            {
                _pinnedToolbarHost.Visibility = Visibility.Collapsed;
                _pinnedToolbarHost.Child = null;
                _pinnedToolbarSig = "";
            }
            // P2 RESUME affordance: only meaningful when NO run is live (this idle branch). Compute the
            // unfinished count from the last run's durable ledger; hide when 0.
            int resumeN = UnfinishedResumeCount();
            string resumeSig = resumeN > 0 ? ResumeStateSignature() : "";
            bool resumeDismissed = resumeN > 0 && ResumeStateDismissed(resumeSig);
            string isig = "IDLE" + _history.Count + (_dark ? "D" : "L") + _lang + "|q" + (_histQuery ?? "")
                          + "|r" + resumeN + "|rs" + resumeSig + "|rd" + (resumeDismissed ? "1" : "0");
            if (_lastSig != isig)
            {
                _lastRoot = null;
                var rows = new List<object>();
                if (resumeN > 0 && !resumeDismissed)
                {
                    var rd = new Dictionary<string, object>();
                    rd["n"] = resumeN; rd["signature"] = resumeSig;
                    rows.Add(MkRow(8, null, rd));   // resume affordance (idle + N>0 only)
                }
                AppendHistoryRows(rows, null);   // idle: no live run on board -> show all history
                if (rows.Count == 0) rows.Add(MkRow(5, null, null));   // empty state when nothing to show
                SetRows(rows);
                _lastSig = isig;
            }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        bool runningNow = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        // Snapshot exposes actual browser tabs and the current live concurrency cap. This is
        // operational status, not another place to edit those settings.
        {
            int openTabs = I(root, "open_tabs");
            int liveCap = I(root, "max_concurrent");
            UpdateWorkerChip(openTabs, liveCap, runningNow);
        }
        // only archive while the run is LIVE -- otherwise the finished run's final
        // snapshot would re-add cleared tasks every tick (Clear would never stick).
        if (runningNow) ArchiveTerminal(root);
        // opt-in auto-retry runs every tick (before the sig short-circuit) so it catches a
        // stopped goal even when nothing else changed. Bounded by _autoRetryMax per goal text.
        if (runningNow && _autoRetry) AutoRetryScan(root);
        // P2 (c) AUTO-ARCHIVE: when the RUN has finished (not running AND every worker terminal),
        // move its completed cards to History -- reusing the exact "すべて履歴へ" code path
        // (ArchiveAllTerminal). Fires ONCE per run, keyed on `started`, and only when enabled.
        if (_autoArchive && !runningNow) MaybeAutoArchive(root);
        // A2-2: spine refresh runs every tick (keyed off its own sig, cheap when unchanged).
        // _toolbarAll is populated by the last RenderCards/BuildRows; for the first tick it may
        // be empty, but RefreshSpine re-reads workers from root directly so that's fine.
        // PaintComposerMode: show "steer" surface while run is live.
        RefreshSpine(root, false);
        PaintComposerMode(runningNow);
        string sig = Sig(root);
        if (sig == _lastSig) return;
        _lastSig = sig;
        RenderCards(root);
    }

    // P2 (c): if the run has finished and every on-board worker is terminal, archive them ALL to
    // History via the same path the "すべて履歴へ" button uses. Guarded to fire once per run by
    // the run's `started` identity (a new run resets it) so a finished snapshot re-read each tick
    // doesn't re-archive (and Clear can still stick).
    void MaybeAutoArchive(Dictionary<string, object> root)
    {
        if (root == null) return;
        string started = S(root, "started");
        if (string.IsNullOrEmpty(started)) return;
        if (_archivedRunStarted == started) return;            // already handled this run

        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        var wArr = (object[])wo;
        if (wArr.Length == 0) return;
        foreach (object o in wArr)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (!IsTerminalWorker(w)) return;                  // not fully finished yet -> wait
        }
        // _toolbarShown is populated by the last RenderCards; on the finished tick it holds this
        // run's terminal workers. If it hasn't been built yet (empty), defer to a later tick rather
        // than marking the run handled with nothing archived.
        if (_toolbarShown == null || _toolbarShown.Count == 0) return;
        _archivedRunStarted = started;                         // mark BEFORE archiving (idempotent)
        ArchiveAllTerminal();                                   // reuse the exact bulk-archive path
    }

    // Opt-in, CAPPED auto-retry. For each STOPPED non-DONE goal, re-queue it at most
    // _autoRetryMax times (counted by goal text). The cap is the safety rail: a goal that
    // keeps failing stops being re-queued once it hits the budget -- never an infinite loop.
    // Only acts while a run is live (add_goal is consumed by the running fleet).
    void AutoRetryScan(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        foreach (object o in (object[])wo)
        {
            var w = o as Dictionary<string, object>;
            if (w == null) continue;
            if (!IsTerminalWorker(w)) continue;
            if (S(w, "outcome") == "DONE") continue;
            string goal = S(w, "goal");
            if (string.IsNullOrEmpty(goal)) continue;
            int n = 0;
            if (_autoRetryCount.ContainsKey(goal)) n = _autoRetryCount[goal];
            if (n >= _autoRetryMax) continue;          // budget spent -> never loop
            _autoRetryCount[goal] = n + 1;             // count BEFORE re-queue (idempotent per tick)
            RetryGoal(w);
        }
    }

    Dictionary<string, object> ReadStatus()
    {
        try
        {
            if (!File.Exists(_statusPath)) return null;
            string text;
            using (var fs = new FileStream(_statusPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
                text = sr.ReadToEnd();
            if (string.IsNullOrEmpty(text)) return null;
            return (Dictionary<string, object>)_js.DeserializeObject(text);
        }
        catch (Exception) { return null; }
    }

    static string S(Dictionary<string, object> d, string k)
    { if (d.ContainsKey(k) && d[k] != null) return d[k].ToString(); return ""; }

    // Cheap deterministic FNV-1a 32-bit hash over a string. Used for RowSig so two strings
    // of the same length but different content produce different signatures, forcing a re-render.
    static int StableShortHash(string s)
    {
        if (s == null || s.Length == 0) return 0;
        unchecked
        {
            int hash = (int)2166136261u;
            for (int i = 0; i < s.Length; i++)
                hash = (hash ^ (int)s[i]) * 16777619;
            return hash;
        }
    }

    static int I(Dictionary<string, object> d, string k)
    { try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { } return 0; }
    static double Dbl(Dictionary<string, object> d, string k)
    { try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToDouble(d[k]); } catch (Exception) { } return 0; }

    string Sig(Dictionary<string, object> root)
    {
        var sb = new StringBuilder();
        sb.Append(_dark ? "D" : "L").Append(_lang).Append('|');
        sb.Append(_stopping ? "st1" : "st0").Append('|');   // FIX B: force a re-render when Stop toggles the dim
        sb.Append("h").Append(_history.Count).Append('|');
        sb.Append(S(root, "done_count")).Append('/').Append(S(root, "total")).Append('|');
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var w = (Dictionary<string, object>)o;
                string nm = S(w, "name");
                sb.Append(nm).Append(S(w, "status")).Append(S(w, "turn"));
                // only an EXPANDED card shows live progress text, so only its `last` hash
                // changes need to force a re-render. Collapsed cards stay put while their
                // worker streams -- that's what keeps a 164-task fleet from thrashing.
                if (_expanded.Contains(nm)) sb.Append('#').Append(StableShortHash(S(w, "last")));
                sb.Append(';');
            }
        return sb.ToString();
    }

    void UpdateHeader(Dictionary<string, object> root)
    {
        int total = I(root, "total");
        int done = I(root, "done_count");
        bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        double started = Dbl(root, "started");
        double updated = Dbl(root, "updated");
        double elapsed = running ? (started > 0 ? NowUnix() - started : 0)
                                 : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                                    : (updated > 0 && started > 0 ? updated - started : 0));

        // Spec: header is always "Fleet" (product surface name, both languages).
        _header.Text = "Fleet";

        // Compute running/queued/done counts from the workers array.
        int cntRunning = 0, cntQueued = 0, cntDoneW = 0;
        object wo2;
        if (root.TryGetValue("workers", out wo2) && wo2 is object[])
        {
            foreach (object ow in (object[])wo2)
            {
                var ww = ow as Dictionary<string, object>;
                if (ww == null) continue;
                string wst = S(ww, "status");
                if (IsTerminalWorker(ww)) cntDoneW++;
                else if (wst == "pending") cntQueued++;
                else cntRunning++;
            }
        }

        // Sub: "N running · M queued · K done" triple + elapsed
        bool ja2 = _lang == 0;

        // TASK 5: run-ended state — when running==false AND every worker is terminal, show ended summary.
        bool allTerminal = false;
        int cntAttn = 0;
        object wo3;
        if (root.TryGetValue("workers", out wo3) && wo3 is object[])
        {
            var wArr = (object[])wo3;
            if (wArr.Length > 0)
            {
                allTerminal = true;
                foreach (object ow2 in wArr)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 == null) continue;
                    if (!IsTerminalWorker(ww2)) { allTerminal = false; }
                    string wst2 = S(ww2, "status");
                    if (wst2 == "stuck" || wst2 == "maxturns" || wst2 == "error") cntAttn++;
                }
            }
        }

        string triple;
        if (!running && allTerminal)
        {
            // Run-ended header: "{done} done · {attn} needs attention · run ended"
            if (ja2)
            {
                triple = cntDoneW + " 完了 · " + cntAttn + " 要対応 · 終了";
            }
            else
            {
                triple = cntDoneW + " done · " + cntAttn + " needs attention · run ended";
            }
        }
        else
        {
            triple = ja2
                ? (cntRunning + " 実行中 · " + cntQueued + " 待機 · " + cntDoneW + " 完了")
                : (cntRunning + " running · " + cntQueued + " queued · " + cntDoneW + " done");
        }

        // Live ETA: project remaining work from the throughput observed SO FAR (done/elapsed).
        string eta = "";
        if (running && total > 0 && done < total)
        {
            if (done > 0 && elapsed > 1.0)
            {
                double etaS = (total - done) * (elapsed / done);
                double perH = done / elapsed * 3600.0;
                eta = "    " + T("eta") + " " + DateTime.Now.AddSeconds(etaS).ToString("HH:mm")
                      + " (" + T("eta_in") + " " + Fmt(etaS) + " · "
                      + perH.ToString("0.#") + " " + T("rate") + ")";
            }
            else
            {
                eta = "    " + T("eta") + " " + T("eta_calc");
            }
        }

        // Two-stage stale indicator: soft (45 s) shows a calm wait hint; hard (90 s) shows a stop warning.
        const double STALE_SOFT = 45.0;
        const double STALE_HARD = 90.0;
        if (running && updated > 0)
        {
            double staleAge = NowUnix() - updated;
            if (staleAge > STALE_HARD)
                triple = triple + " — " + T("stale");
            else if (staleAge > STALE_SOFT)
                triple = triple + " — " + T("stale_wait");
        }

        // TASK 4: evidence freshness — "· evidence {N}s/m ago" from top-level `updated` field [COMPUTED].
        // Omit entirely if updated is missing (zero) — never show "unknown".
        string freshness = "";
        if (updated > 0)
        {
            double ageS = NowUnix() - updated;
            if (ageS >= 0)
            {
                string ageFmt;
                if (ageS < 60.0)
                    ageFmt = ((int)ageS).ToString() + (ja2 ? "秒" : "s");
                else
                    ageFmt = ((int)(ageS / 60.0)).ToString() + (ja2 ? "分" : "m");
                freshness = " · " + (ja2 ? "最終更新 " : "updated ") + ageFmt + (ja2 ? "前" : " ago");
            }
        }

        _sub.Text = triple + freshness + "    " + T("elapsed") + " " + Fmt(elapsed) + eta;
        _sub.ToolTip = _sub.Text;   // _sub itself is Collapsed (see BuildChrome) -- kept alive purely
                                    // as the plain-text fallback for any future consumer of its Text/
                                    // ToolTip; nothing else in this file reads them today (audited).

        // Feature 2: the single concatenated sentence above is what actually got read as a
        // fallback; what's actually DISPLAYED is a WrapPanel of discrete Pill() chips (same helper
        // HistoryRow etc. already use), one per fact, rebuilt from the SAME source variables (not
        // parsed back out of the sentence) so wording/pluralization can't drift between the two.
        if (_subChips != null)
        {
            _subChips.Children.Clear();
            if (!running && allTerminal)
            {
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
                _subChips.Children.Add(ChipMargin(Pill(cntAttn + " " + (ja2 ? "要対応" : "needs attention"),
                    cntAttn > 0 ? "warning" : "neutral")));
                _subChips.Children.Add(ChipMargin(Pill(ja2 ? "終了" : "run ended", "neutral")));
            }
            else
            {
                _subChips.Children.Add(ChipMargin(Pill(cntRunning + " " + (ja2 ? "実行中" : "running"), "info")));
                _subChips.Children.Add(ChipMargin(Pill(cntQueued + " " + (ja2 ? "待機" : "queued"), "neutral")));
                _subChips.Children.Add(ChipMargin(Pill(cntDoneW + " " + (ja2 ? "完了" : "done"), "success")));
            }
            if (running && updated > 0)
            {
                double staleAge2 = NowUnix() - updated;
                if (staleAge2 > STALE_HARD)
                    _subChips.Children.Add(ChipMargin(Pill(T("stale"), "danger")));
                else if (staleAge2 > STALE_SOFT)
                    _subChips.Children.Add(ChipMargin(Pill(T("stale_wait"), "warning")));
            }
            if (!string.IsNullOrEmpty(freshness))
                _subChips.Children.Add(ChipMargin(Pill(freshness.TrimStart(' ', '·'), "neutral")));
            _subChips.Children.Add(ChipMargin(Pill(T("elapsed") + " " + Fmt(elapsed), "neutral")));
            if (!string.IsNullOrEmpty(eta))
                _subChips.Children.Add(ChipMargin(Pill(eta.Trim(), "neutral")));
            _subChips.ToolTip = _sub.Text;   // same fallback sentence, shown on hover over the chip row
        }
    }

    // Roll seconds up through y/d/h/m/s so a long run reads "1h39m27s" / "1d1h1m1s" instead of
    // an unbounded "99m27s". Show every unit from the highest non-zero down to seconds (so
    // ordering is unambiguous). Month is intentionally omitted: its token would clash with
    // minutes ('m') and its length is variable; a 365-day year is a good-enough cap for run time.
    static string Fmt(double sec)
    {
        if (sec < 0) sec = 0;
        long s = (long)sec;
        long y = s / 31536000; s -= y * 31536000;
        long d = s / 86400;    s -= d * 86400;
        long h = s / 3600;     s -= h * 3600;
        long m = s / 60;       s -= m * 60;
        var sb = new StringBuilder();
        if (y > 0)                  sb.Append(y).Append('y');
        if (sb.Length > 0 || d > 0) sb.Append(d).Append('d');
        if (sb.Length > 0 || h > 0) sb.Append(h).Append('h');
        if (sb.Length > 0 || m > 0) sb.Append(m).Append('m');
        sb.Append(s).Append('s');
        return sb.ToString();
    }

    void RenderCards(Dictionary<string, object> root)
    {
        _lastRoot = root;               // cache for single-card toggles
        // Preserve scroll position across the rebuild. Without this, every worker update
        // (status/turn change) reset the list and snapped the view back to the TOP -- which is
        // exactly why scrolling "didn't work" while tasks were live: the user scrolled down, a
        // tick fired, and they were yanked up again. Capture now, restore after the new content
        // is laid out (read the ListBox's internal ScrollViewer, which #ListScroller finds).
        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;

        SetRows(BuildRows(root));
        RefreshPinnedToolbar();     // rebuild the pinned filter bar (only when its signature changed)

        if (off > 0.0)
        {
            double target = off;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 != null) s2.ScrollToVerticalOffset(target);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }

        // Feature 1 keystroke-safety: mirrors the scroll-restore pass just above, same Loaded
        // priority (i.e. runs after this sweep's layout has realized whatever containers SetRows
        // just replaced). If the user was mid-type in some worker's steer box, its card may have
        // just been rebuilt (RowSig changed for reasons unrelated to steering -- a LIVE worker's
        // `last`/`turn` streams every sweep) into a BRAND NEW TextBox instance. The new instance
        // already has the right text (CollapsedSteerRow/SteerRow seed it from _steerDraft on every
        // build) -- what it does NOT have yet is focus, so re-apply it here and seat the caret at
        // the end. Runs unconditionally (not gated on scroll offset) since a steer rebuild can
        // happen with zero scroll.
        string focusWorker = _steerFocusWorker;
        if (!string.IsNullOrEmpty(focusWorker))
        {
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != focusWorker) return;   // focus moved on before this ran
                TextBox tb2;
                if (_steerBoxRef.TryGetValue(focusWorker, out tb2) && tb2 != null)
                {
                    try { tb2.Focus(); tb2.CaretIndex = tb2.Text != null ? tb2.Text.Length : 0; }
                    catch (Exception) { }
                }
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }
    }

    // Rebuild the PINNED filter bar into its fixed host above the list. Called after BuildRows (so
    // _toolbarAll/_toolbarShown are current). Uses the same signature the toolbar row used (case 0
    // in RowSig) so the heavy Border is only re-created when a count/filter/theme actually changes —
    // otherwise we leave the existing element in place (no flicker, no needless layout). The bar is
    // hidden in the empty state (no workers AND no history), matching the old behavior where the
    // toolbar row was simply not emitted.
    void RefreshPinnedToolbar()
    {
        if (_pinnedToolbarHost == null) return;
        bool empty = (_toolbarAll == null || _toolbarAll.Count == 0) && (_history == null || _history.Count == 0);
        if (empty)
        {
            _pinnedToolbarHost.Visibility = Visibility.Collapsed;
            _pinnedToolbarHost.Child = null;
            _pinnedToolbarSig = "";
            return;
        }
        // Signature identical to RowSig(case 0) so we rebuild on exactly the same triggers.
        int[] tc = ToolbarCounts(_toolbarAll);
        string g = (_dark ? "D" : "L") + _lang.ToString();
        string sig = "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                     + "|all" + tc[0] + ":act" + tc[1] + ":need" + tc[2] + ":done" + tc[3]
                     + ":max" + tc[5] + ":bad" + tc[6] + ":hid" + tc[7]
                     + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
        _pinnedToolbarHost.Visibility = Visibility.Visible;
        if (sig == _pinnedToolbarSig && _pinnedToolbarHost.Child != null) return;   // unchanged -> keep as-is
        _pinnedToolbarSig = sig;
        _pinnedToolbarHost.Child = BuildCardToolbar(_toolbarAll, _toolbarShown);
    }

    // Build the lightweight row-model list for the virtualizing ListBox: a toolbar row, one card
    // row per filtered/sorted worker, then (if history non-empty) a history-header row + one
    // history row per entry. The heavy UIElements are NOT built here -- the ItemTemplate's
    // converter builds them lazily for visible rows only.
    List<object> BuildRows(Dictionary<string, object> root)
    {
        // gather workers in natural order
        var workers = new List<Dictionary<string, object>>();
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
                workers.Add((Dictionary<string, object>)o);

        // Feature B: apply the view filter. Outcome is null/"" while running, so only terminal
        // workers ever have outcome=="DONE"; filter 1 hides DONE (keeps failures + running),
        // filter 2 shows only DONE.
        // Persistent hide: skip TERMINAL cards the user cleared during a live run (their key is
        // in _hiddenKeys). Non-terminal workers are never hidden, and a fresh run gets new keys
        // (started changes), so this only suppresses exactly the cards the user dismissed.
        string startedRoot = S(root, "started");
        var shown = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> w in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRoot, w)))
                continue;
            string oc = S(w, "outcome");
            string st = S(w, "status");
            // Tab 1 = Active: non-terminal AND not pending (actively working statuses)
            if (_cardFilter == 1 && (IsTerminalWorker(w) || st == "pending")) continue;
            // Tab 2 = Needs input: awaiting only
            if (_cardFilter == 2 && st != "awaiting") continue;
            // Tab 3 = Done: outcome == DONE
            if (_cardFilter == 3 && oc != "DONE") continue;
            shown.Add(w);
        }
        // All tabs use display-rank order (active->pending->terminal). Tab 0 partitions below.
        shown = StableByDisplayRank(shown);

        // stash for the converter (it rebuilds the toolbar row from these when a container recycles)
        _toolbarAll = workers;
        _toolbarShown = shown;

        var rows = new List<object>();
        // Empty state (spec): no run yet and no history -> a calm centered suggestion block instead
        // of a blank workspace (the big top textarea is already gone -- it's the bottom composer now).
        if (workers.Count == 0 && _history.Count == 0)
        {
            rows.Add(MkRow(5, null, null));
            return rows;
        }
        // NOTE: the toolbar (すべて/実行中/承認待ち/完了 filter) is NO LONGER a scrolling row. It is
        // pinned in _pinnedToolbarHost above the list (see RefreshPinnedToolbar, called from
        // RenderCards). Row kind 0 is retained in the converter for safety but never emitted here.

        // TASK 3: Directive band — show only while there is at least one ON-BOARD worker (i.e. not
        // every card has been sent to History). Once the whole run is cleared to History the band
        // disappears (user request 2026-06-28). On-board = workers minus _hiddenKeys (History);
        // NOT filtered by the active tab, so the band is independent of which tab is selected.
        var onBoard = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> ow in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(ow) && _hiddenKeys.Contains(WorkerKey(startedRoot, ow)))
                continue;   // in History -> not on the board
            onBoard.Add(ow);
        }
        // Compute _directiveBandMeta here so the Sig captures the live elapsed/lane counts.
        if (onBoard.Count > 0)
        {
            // Compute meta: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
            double dbStarted = Dbl(root, "started");
            double dbNow = NowUnix();
            bool dbRunning = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            double dbElapsed = dbRunning
                ? (dbStarted > 0 ? dbNow - dbStarted : 0)
                : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                   : (Dbl(root, "updated") > 0 && dbStarted > 0 ? Dbl(root, "updated") - dbStarted : 0));
            int dbActive = 0;
            foreach (Dictionary<string, object> dw in onBoard)
                if (!IsTerminalWorker(dw) && S(dw, "status") != "pending") dbActive++;
            bool dbJa = _lang == 0;
            var dbMeta = new StringBuilder();
            if (dbStarted > 0)
            {
                string startHM = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                    .AddSeconds(dbStarted).ToLocalTime().ToString("HH:mm");
                dbMeta.Append(dbJa ? "開始 " : "started ").Append(startHM);
                dbMeta.Append(" · ").Append(Fmt(dbElapsed));
            }
            if (dbElapsed > 0 || dbStarted == 0)
            {
                if (dbMeta.Length > 0) dbMeta.Append(" · ");
                if (!dbRunning && dbActive == 0)
                {
                    // Run is done: show completed count instead of active/total lane status
                    int dbDone = 0;
                    foreach (Dictionary<string, object> dw2 in onBoard)
                        if (S(dw2, "outcome") == "DONE") dbDone++;
                    dbMeta.Append(dbDone);
                    dbMeta.Append(dbJa ? "件完了" : " done");
                }
                else
                {
                    dbMeta.Append(dbActive).Append("/").Append(onBoard.Count);
                    dbMeta.Append(dbJa ? " lane 稼働中" : " lanes active");
                }
            }
            _directiveBandMeta = dbMeta.ToString();

            // DirectiveBand aggregates goals from the on-board set (so lanes moved to History also
            // drop out of the "Goals (N)" count), not the full _toolbarAll.
            _directiveBandWorkers = onBoard;
            rows.Add(MkRow(6, onBoard[0], null));      // directive band: first on-board worker's goal [REAL]
        }
        // Tab 0 (All): partition active/queued vs terminal with a divider.
        // Tabs 1-3 are explicit single-criterion views -- no re-partition needed.
        if (_cardFilter == 0)
        {
            var active = new List<Dictionary<string, object>>();
            var done = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> w in shown)
                (IsTerminalWorker(w) ? done : active).Add(w);
            foreach (Dictionary<string, object> w in active)
                rows.Add(MkRow(1, w, null));
            if (done.Count > 0)
            {
                rows.Add(MkRow(4, null, null));       // "完了 (this run)" divider
                foreach (Dictionary<string, object> w in done)
                    rows.Add(MkRow(1, w, null));
            }
        }
        else
        {
            foreach (Dictionary<string, object> w in shown)
                rows.Add(MkRow(1, w, null));          // one card per worker
        }
        // ③ Dedup: a terminal worker is archived into history the moment it finishes, but its card
        // also stays ON-BOARD (in `完了`) until the user moves it to History. During that window the
        // same started#name shows in BOTH places. Compute the on-board key set and skip history rows
        // that match it -- so a finished task appears once (on board) and only drops into History
        // once it actually leaves the board (すべて履歴へ / →履歴).
        var onBoardKeys = new System.Collections.Generic.HashSet<string>();
        foreach (Dictionary<string, object> bw in onBoard)
            onBoardKeys.Add(WorkerKey(startedRoot, bw));
        AppendHistoryRows(rows, onBoardKeys);
        return rows;
    }

    // Append a history-header row + one history row per entry (newest first) to the row model.
    void AppendHistoryRows(List<object> rows, System.Collections.Generic.HashSet<string> onBoardKeys)
    {
        if (_history.Count == 0) return;
        // ③ Build the visible (deduped) entry list first: skip any history entry whose key
        // (started#name) is still ON-BOARD, so the same finished task is not shown twice.
        var visible = new List<Dictionary<string, object>>();
        for (int i = _history.Count - 1; i >= 0; i--)
        {
            var e = _history[i] as Dictionary<string, object>;
            if (e == null) continue;
            string ek = S(e, "key");
            if (onBoardKeys != null && !string.IsNullOrEmpty(ek) && onBoardKeys.Contains(ek))
                continue;   // still on the board -> don't duplicate it in History
            visible.Add(e);
        }
        if (visible.Count == 0) return;               // everything is still on-board -> no History section yet
        rows.Add(MkRow(2, null, null));               // history header (carries the search box)

        // P2 (b) SEARCH: case-insensitive substring filter over title + result/goal text.
        string q = (_histQuery ?? "").Trim();
        List<Dictionary<string, object>> filtered = visible;
        if (q.Length > 0)
        {
            string ql = q.ToLowerInvariant();
            filtered = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> e in visible)
            {
                string hay = (CardTitle(S(e, "conv_title"), S(e, "goal")) + " "
                              + S(e, "goal") + " " + S(e, "display_result") + " "
                              + S(e, "last") + " " + S(e, "outcome")).ToLowerInvariant();
                if (hay.IndexOf(ql, StringComparison.Ordinal) >= 0) filtered.Add(e);
            }
        }
        if (filtered.Count == 0) return;              // header still shown so the search box stays reachable

        // P2 (a) DATE GROUPS: emit a subheader (今日 / 昨日 / YYYY-MM-DD / その他) whenever the day
        // bucket changes. `visible` is already newest-first, so groups come out most-recent-first.
        string lastGroup = null;
        foreach (Dictionary<string, object> e in filtered)
        {
            string grp = HistoryGroupLabel(e);
            if (grp != lastGroup)
            {
                var gd = new Dictionary<string, object>();
                gd["label"] = grp;
                rows.Add(MkRow(7, null, gd));         // date-group subheader
                lastGroup = grp;
            }
            rows.Add(MkRow(3, null, e));             // one history row per visible entry
        }
    }

    // The date-group bucket label for a history entry, derived from its `ts` (archived-at unix
    // seconds). Entries archived before `ts` existed (old history) have no timestamp -> その他 /
    // Earlier. Today / Yesterday are relative to local midnight; older days show YYYY-MM-DD.
    string HistoryGroupLabel(Dictionary<string, object> e)
    {
        double ts = Dbl(e, "ts");
        if (ts <= 0) return T("hist_earlier");
        DateTime day;
        try { day = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime().Date; }
        catch (Exception) { return T("hist_earlier"); }
        DateTime today = DateTime.Now.Date;
        if (day == today) return T("hist_today");
        if (day == today.AddDays(-1)) return T("hist_yesterday");
        return day.ToString("yyyy-MM-dd");
    }

    // The date-group subheader UIElement (Kind==7). Small muted caption, matching the History chrome.
    UIElement HistoryGroupHeader(string label)
    {
        return new TextBlock {
            Text = label, Foreground = Muted, FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(12, 10, 8, 2) };
    }

    // Build one row model and freeze its render signature (so SetRows can diff old-vs-new rows
    // with a plain string compare). Replaces the old `new Row(...)` call sites.
    Row MkRow(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        var r = new Row(kind, w, hist);
        r.Sig = RowSig(kind, w, hist);
        return r;
    }

    // Per-row render signature: captures every bit of state the row's builder reads, evaluated at
    // build time. Equal Sig => identical UIElement => SetRows can leave that container untouched.
    string RowSig(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        string g = (_dark ? "D" : "L") + _lang.ToString();   // theme/lang re-chrome every row
        switch (kind)
        {
            case 0:  // toolbar: reflects per-category counts (same helper the toolbar renders from,
                     // so a status-only reclassification like pending->done flips the signature)
                int[] tc0 = ToolbarCounts(_toolbarAll);
                return "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                       + "|all" + tc0[0] + ":act" + tc0[1] + ":need" + tc0[2] + ":done" + tc0[3]
                       + ":max" + tc0[5] + ":bad" + tc0[6] + ":hid" + tc0[7]
                       + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
            case 2: return "HH|" + g;                          // history header (static chrome; search box preserved across renders)
            case 7:                                            // date-group subheader: keyed on its label
                return "HG|" + g + "|" + (hist != null ? S(hist, "label") : "");
            case 8:                                            // resume affordance: keyed on N
                return "RA|" + g + "|" + (hist != null ? I(hist, "n") : 0)
                       + "|" + (hist != null ? S(hist, "signature") : "");
            case 4: return "DV|" + g;                          // "完了 (this run)" divider
            case 5: return "ES|" + g;                          // empty state (static chrome)
            case 6:                                            // directive band: keyed on first-worker goal + started
                return "DB|" + g + "|" + (w != null ? S(w, "goal") + "|" + S(w, "name") : "")
                       + "|tc" + (_toolbarAll.Count) + "|" + _directiveBandMeta;
            case 3:                                            // history row: stable per entry, but expand state changes the render
            {
                string hk3 = hist != null ? S(hist, "key") : "";
                bool hOpen = !string.IsNullOrEmpty(hk3) && _expanded.Contains(hk3);
                return "h|" + g + "|" + (hist != null ? RuntimeHelpers.GetHashCode(hist) : 0)
                       + "|" + (hOpen ? "E" : "C");
            }
            default:                                           // kind 1: worker card
                string nm = S(w, "name");
                var sb = new StringBuilder("c|");
                sb.Append(g).Append('|').Append(nm)
                  .Append(S(w, "status")).Append(S(w, "turn")).Append(S(w, "outcome"))
                  .Append(S(w, "closed"));   // closed flips the left rail to neutral -> must re-render
                // The collapsed card now shows a result line + meta (turn/reviews/verified), so its
                // signature must track `last`/reviews/verified too -- otherwise the at-a-glance line
                // would freeze while the worker streams. Hash (not length) catches same-length content changes.
                string lastVal = S(w, "last");
                string drVal = S(w, "display_result");
                sb.Append(_expanded.Contains(nm) ? "#E" : "#C")
                  .Append(StableShortHash(lastVal)).Append(':').Append(StableShortHash(drVal))
                  .Append(':').Append(S(w, "verify_attempts")).Append(S(w, "verified"));
                // P0: conv_url drives the agent badge (agent-bound vs 既定Copilot); reason drives the
                // INFRA_STUCK row text — track both so those cards re-render when they change.
                sb.Append(':').Append(StableShortHash(S(w, "conv_url")))
                  .Append(':').Append(StableShortHash(S(w, "reason")))
                  // P1: the card headline prefers conv_title (Copilot auto-title) -> re-render when it arrives
                  .Append(':').Append(StableShortHash(S(w, "conv_title")));
                // TASK 3 (Bucket C): track next_step + self_confidence so the collapsed row re-renders.
                sb.Append('|').Append(S(w, "next_step").Length).Append(':').Append(S(w, "self_confidence"));
                // FIX B: _stopping dims non-terminal cards -- track it so a Stop click (or its
                // resolution) re-templates this card instead of reusing the old realized element.
                sb.Append('|').Append(_stopping ? "1" : "0");
                // Feature 1: fold whether THIS card's collapsed steer box currently has a non-empty
                // draft and/or keyboard focus into the signature. This does not, by itself, protect
                // the draft/focus (that's _steerDraft + _steerBoxRef + the RenderCards restore pass,
                // which work regardless of Sig) -- it exists so that starting/ending a steer draft
                // or focus on this specific card is itself a tracked, deterministic state transition
                // rather than invisible to the diff, matching how every other read field here is
                // captured. Booleans only (not the draft text itself) -- empty-to-non-empty and
                // unfocused-to-focused are each a single transition, so this does NOT force a
                // rebuild on every keystroke the way hashing the live draft text would.
                string draftSt;
                bool hasDraft = _steerDraft.TryGetValue(nm, out draftSt) && !string.IsNullOrEmpty(draftSt);
                bool hasFocus = _steerFocusWorker == nm;
                sb.Append('|').Append(hasDraft ? "d1" : "d0").Append(hasFocus ? "f1" : "f0");
                return sb.ToString();
        }
    }

    // Reconcile the persistent _rows collection toward `rows` IN PLACE. Item-by-item Replace plus
    // tail Add/Remove -- never Clear() and never a fresh ItemsSource -- so the VirtualizingStackPanel
    // never sees a collection Reset and the pixel scroll offset is preserved. Only rows whose Sig
    // changed are swapped, so unchanged cards keep their realized containers (no flicker, and a
    // chevron toggle re-templates exactly the one card that changed).
    void SetRows(List<object> rows)
    {
        if (_list == null) return;
        int n = rows.Count, m = _rows.Count;
        int common = n < m ? n : m;
        for (int i = 0; i < common; i++)
        {
            var nw = rows[i] as Row;
            var od = _rows[i] as Row;
            if (od == null || nw == null || od.Sig != nw.Sig)
                _rows[i] = rows[i];        // targeted Replace -> only this container re-templates
        }
        if (n > m) { for (int i = m; i < n; i++) _rows.Add(rows[i]); }
        else if (m > n) { for (int i = m - 1; i >= n; i--) _rows.RemoveAt(i); }
    }

    // Walk the visual tree from _list to find its internal ScrollViewer (present once the
    // template is applied). Used to capture/restore scroll offset across a re-render. Returns
    // null before the first layout pass.
    ScrollViewer ListScroller()
    {
        if (_list == null) return null;
        return FindScrollViewer(_list);
    }
    static ScrollViewer FindScrollViewer(DependencyObject root)
    {
        if (root == null) return null;
        int n = VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            DependencyObject ch = VisualTreeHelper.GetChild(root, i);
            ScrollViewer sv = ch as ScrollViewer;
            if (sv != null) return sv;
            ScrollViewer deep = FindScrollViewer(ch);
            if (deep != null) return deep;
        }
        return null;
    }

    // Tiny per-row model. Kind: 0=toolbar, 1=card, 2=history-header, 3=history-row, 4=completed-divider.
    class Row
    {
        public int Kind;
        public Dictionary<string, object> Worker;
        public Dictionary<string, object> Hist;
        // Render signature, computed at build time (MkRow). Two rows with equal Sig produce an
        // identical UIElement, so SetRows skips re-templating them -- and a later compare of an
        // old row vs a freshly-built one is just a string compare (the state is frozen in here).
        public string Sig;
        public Row(int kind, Dictionary<string, object> worker, Dictionary<string, object> hist)
        { Kind = kind; Worker = worker; Hist = hist; }
    }

    // Converts a Row model into its realized UIElement by dispatching on Row.Kind to the EXISTING
    // builders. Holds a back-reference to the window (C# 5: no lambdas-as-closures over fields in
    // a converter, so pass `this`). Called for visible rows only because containers recycle.
    class RowConverter : IValueConverter
    {
        readonly CockpitWindow _w;
        public RowConverter(CockpitWindow w) { _w = w; }
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            var r = value as Row;
            if (r == null) return null;
            if (r.Kind == 0) return _w.BuildCardToolbar(_w._toolbarAll, _w._toolbarShown);
            if (r.Kind == 1) return _w.Card(r.Worker);
            if (r.Kind == 2) return _w.HistoryHeader();
            if (r.Kind == 3) return _w.HistoryRow(r.Hist);
            if (r.Kind == 4) return _w.CompletedDivider();
            if (r.Kind == 5) return _w.EmptyState();
            if (r.Kind == 6) return _w.DirectiveBand(r.Worker);
            if (r.Kind == 7) return _w.HistoryGroupHeader(r.Hist != null ? S(r.Hist, "label") : "");
            if (r.Kind == 8) return _w.ResumeAffordance(
                r.Hist != null ? I(r.Hist, "n") : 0,
                r.Hist != null ? S(r.Hist, "signature") : "");
            return null;
        }
        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        { return Binding.DoNothing; }
    }

    // Stable sort by severity rank (C# 5: List.Sort is NOT stable, so do a manual stable sort
    // by bucketing in rank order while preserving original index order within each rank).
    static List<Dictionary<string, object>> StableBySeverity(List<Dictionary<string, object>> src)
    {
        var outp = new List<Dictionary<string, object>>();
        for (int rank = 0; rank <= 3; rank++)
            foreach (Dictionary<string, object> w in src)
                if (SeverityRank(w) == rank) outp.Add(w);
        return outp;
    }

    // Default-view order: what is HAPPENING now floats to the top, finished work sinks to the
    // bottom. Active (running/verifying/sending/...) -> pending (queued) -> terminal (done/freed/
    // failed). Stable within each bucket so a worker keeps its place (and its W-name identity)
    // and cards do not jump around as unrelated workers tick. status.json lists workers in launch
    // order, so without this the earliest-launched (now-completed) workers stay pinned at the top.
    static List<Dictionary<string, object>> StableByDisplayRank(List<Dictionary<string, object>> src)
    {
        var outp = new List<Dictionary<string, object>>();
        for (int rank = 0; rank <= 2; rank++)
            foreach (Dictionary<string, object> w in src)
                if (DisplayRank(w) == rank) outp.Add(w);
        return outp;
    }

    // 0 = active (currently working), 1 = pending (queued, not yet started), 2 = terminal (done/
    // freed/failed). Drives the default card order so the live worker is first and history sinks.
    static int DisplayRank(Dictionary<string, object> w)
    {
        if (IsTerminalWorker(w) || S(w, "status") == "freed") return 2;
        if (S(w, "status") == "pending") return 1;
        return 0;
    }

    // Feature B/C toolbar: filter selector + outcome summary + bulk-retry. Rebuilt each
    // RenderCards as the first row of _cards (no worker Tag, so ToggleExpand skips it).
    // Empty state (spec): a centered narrow block with quiet suggestion chips, shown when there is
    // no run and no history. Suggestions just pre-fill the bottom composer -- they don't launch.
    UIElement EmptyState()
    {
        var outer = new Border { Margin = new Thickness(0, 80, 0, 0) };
        var block = new StackPanel { MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Center };

        block.Children.Add(new TextBlock {
            Text = _lang == 0 ? "タスクはまだありません" : "No fleet tasks",
            Foreground = Fg, FontSize = 15, FontWeight = FontWeights.SemiBold,
            HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) });
        block.Children.Add(new TextBlock {
            Text = _lang == 0 ? "複数のタスクを並行で走らせ、ここで進捗を確認します。"
                              : "Run several tasks in parallel, then monitor progress here.",
            Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap,
            TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 0, 0, 18) });

        string[] suggestions = _lang == 0
            ? new string[] { "失敗テストを修正", "UIの問題をレビュー", "READMEを更新", "フォルダのタスクを実行" }
            : new string[] { "Fix failing tests", "Review UI issues", "Update README", "Run folder task" };
        var wrap = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Center };
        foreach (string s in suggestions)
        {
            string text = s;
            var chip = new Button {
                Content = text, Cursor = Cursors.Hand, FontSize = 12,
                Background = Brushes.Transparent, Foreground = Fg,
                BorderBrush = Border, BorderThickness = new Thickness(1),
                Padding = new Thickness(12, 5, 12, 5), Margin = new Thickness(4, 4, 4, 4)
            };
            chip.Click += delegate
            {
                if (_goalInput != null)
                {
                    _goalInput.Text = text;
                    _goalInput.CaretIndex = text.Length;
                    _goalInput.Focus();
                }
            };
            wrap.Children.Add(chip);
        }
        block.Children.Add(wrap);
        outer.Child = block;
        return outer;
    }

    // P2 RESUME: inline banner shown (idle only, N>0) offering to relaunch the fleet with --resume,
    // which re-queues the unfinished goals from the last run's durable ledger. Hidden while a run is
    // live or when N==0 (the row is simply not emitted in those cases).
    UIElement ResumeAffordance(int n, string signature)
    {
        var row = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(14, 8, 14, 8), Margin = new Thickness(8, 8, 8, 4) };
        var dp = new DockPanel { LastChildFill = true };

        // This banner is advisory, not a blocking error. Let the user dismiss this exact stale
        // resume state permanently; a new interrupted run has a different signature and returns.
        var close = IconButton("close", 14, _lang == 0 ? "未完了ランの通知を閉じる" : "Dismiss unfinished-run notice");
        close.Width = 28; close.Height = 28; close.Margin = new Thickness(8, 0, 0, 0);
        close.Padding = new Thickness(0); close.BorderThickness = new Thickness(0);
        close.Background = Brushes.Transparent; close.Foreground = Muted;
        close.Template = FlatButtonTemplate();
        close.ToolTip = _lang == 0 ? "この未完了ランを非表示" : "Hide this unfinished run";
        string capturedSignature = signature;
        close.Click += delegate (object sender, RoutedEventArgs e)
        { e.Handled = true; DismissResumeState(capturedSignature); };
        DockPanel.SetDock(close, Dock.Right); dp.Children.Add(close);

        var btn = new Button {
            Content = _lang == 0 ? "再開" : "Resume", Cursor = Cursors.Hand, FontSize = 12,
            FontWeight = FontWeights.SemiBold, Padding = new Thickness(12, 4, 12, 4),
            BorderThickness = new Thickness(1) };
        btn.Background = BtnBg; btn.Foreground = Fg; btn.BorderBrush = Border;
        btn.Template = FlatButtonTemplate();
        btn.Click += delegate
        {
            try
            {
                SpawnFleetResume();
                _lastSig = "";
                if (_startNote != null)
                    _startNote.Text = _lang == 0 ? "前回のランを再開しました。" : "Resumed the last run.";
            }
            catch (Exception ex)
            {
                if (_startNote != null) _startNote.Text = (_lang == 0 ? "再開に失敗: " : "Resume failed: ") + ex.Message;
            }
        };
        DockPanel.SetDock(btn, Dock.Right);
        dp.Children.Add(btn);

        var msg = new TextBlock {
            Text = _lang == 0 ? ("前回のランに未完了が " + n + " 件")
                              : ("Last run left " + n + " unfinished"),
            Foreground = Fg, FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis };
        dp.Children.Add(msg);

        row.Child = dp;
        return row;
    }

    // TASK 3: Directive band — shown only in the live/active branch, inserted above card list.
    // Reads goal from the first/primary worker (passed as `firstWorker`).
    // [REAL goal], [COMPUTED meta] — spec §5a. No fabrication; multi-goal case is honest.
    UIElement DirectiveBand(Dictionary<string, object> firstWorker)
    {
        bool ja = _lang == 0;

        // Gather goal texts from the ON-BOARD workers (History-cleared lanes excluded, set in
        // BuildRows) to determine single vs multi-goal.
        var goalTexts = new List<string>();
        var dbSrc = _directiveBandWorkers != null && _directiveBandWorkers.Count > 0 ? _directiveBandWorkers : _toolbarAll;
        foreach (Dictionary<string, object> tw in dbSrc)
        {
            string g = S(tw, "goal");
            if (!string.IsNullOrEmpty(g) && !goalTexts.Contains(g))
                goalTexts.Add(g);
        }

        // Section label: "DIRECTIVE" / "指示" when a single goal; "Goals (N)" / "ゴール (N)" for multiple.
        bool multiGoal = goalTexts.Count > 1;
        string sectionLabel = multiGoal
            ? (ja ? ("ゴール (" + goalTexts.Count + ")") : ("Goals (" + goalTexts.Count + ")"))
            : (ja ? "指示" : "DIRECTIVE");

        // Goal text: first goal + "(+N more lanes)" indicator for multi-goal.
        string primaryGoal = goalTexts.Count > 0 ? goalTexts[0] : "";
        string goalDisplay = primaryGoal;
        if (multiGoal && goalTexts.Count > 1)
        {
            int extras = goalTexts.Count - 1;
            goalDisplay = primaryGoal + (ja ? (" (他 " + extras + " lane)") : (" (+" + extras + " more lanes)"));
        }

        // Meta line: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
        string metaLine = _directiveBandMeta;

        var outer = new Border();
        outer.Margin = new Thickness(18, 0, 18, 0);
        outer.Padding = new Thickness(0, 8, 0, 0);
        outer.BorderThickness = new Thickness(0, 0, 0, 1);
        outer.BorderBrush = Theme.Br(Theme.Border(_dark));

        var col = new StackPanel();

        // Section label
        var lbl = new TextBlock();
        lbl.Text = sectionLabel;
        lbl.Foreground = Theme.Br(Theme.Muted(_dark));
        lbl.FontSize = Theme.FsMeta;
        lbl.FontWeight = FontWeights.SemiBold;
        lbl.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(lbl);

        // Separator line
        var sep = new Border();
        sep.Height = 1;
        sep.Background = Theme.Br(Theme.Border(_dark));
        sep.Margin = new Thickness(0, 0, 0, 6);
        col.Children.Add(sep);

        // Goal text — show even if empty (no fallback fabrication; empty just shows nothing)
        if (!string.IsNullOrEmpty(goalDisplay))
        {
            var goalTb = new TextBlock();
            goalTb.Text = goalDisplay;
            goalTb.Foreground = Theme.Br(Theme.Text(_dark));
            goalTb.FontSize = Theme.FsBody;
            goalTb.TextWrapping = TextWrapping.Wrap;
            goalTb.Margin = new Thickness(0, 0, 0, 4);
            col.Children.Add(goalTb);
        }

        // Meta line (started · elapsed · lanes)
        if (!string.IsNullOrEmpty(metaLine))
        {
            var metaTb = new TextBlock();
            metaTb.Text = metaLine;
            metaTb.Foreground = Theme.Br(Theme.Muted(_dark));
            metaTb.FontSize = Theme.FsMeta;
            metaTb.Margin = new Thickness(0, 0, 0, 8);
            col.Children.Add(metaTb);
        }

        outer.Child = col;
        return outer;
    }


    // Single source of truth for the toolbar's per-category counts. Returns an int[] so both the
    // toolbar render (BuildCardToolbar) and the row signature (RowSig) use IDENTICAL arithmetic --
    // otherwise a status-only change (pending->done) that keeps cntAll constant would not flip the
    // signature and the toolbar would render stale counts until the next forced re-render.
    // Index map: [0]=cntAll [1]=cntActive [2]=cntNeeds [3]=cntDone [4]=doneN [5]=maxN [6]=badN
    //            [7]=hiddenTerminal (terminal cards moved to History, excluded above -- folding this
    //            into the signature makes "send to history" also re-render the toolbar).
    int[] ToolbarCounts(List<Dictionary<string, object>> all)
    {
        int cntAll = 0, cntActive = 0, cntNeeds = 0, cntDone = 0;
        int doneN = 0, maxN = 0, badN = 0, hiddenTerminal = 0;
        string startedRootTb = _lastRoot != null ? S(_lastRoot, "started") : "";
        if (all != null)
        {
            foreach (Dictionary<string, object> w in all)
            {
                if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRootTb, w)))
                {
                    hiddenTerminal++;
                    continue;   // moved to History -- not on the board, don't count
                }
                cntAll++;
                string oc = S(w, "outcome");
                string st = S(w, "status");
                if (oc == "DONE") { doneN++; cntDone++; }
                else if (oc == "MAXTURNS") maxN++;
                else if (oc == "STUCK" || oc == "ERROR" || oc == "CANCELLED") badN++;
                if (st == "awaiting") cntNeeds++;
                if (!IsTerminalWorker(w) && st != "pending") cntActive++;
            }
        }
        return new int[] { cntAll, cntActive, cntNeeds, cntDone, doneN, maxN, badN, hiddenTerminal };
    }

    UIElement BuildCardToolbar(List<Dictionary<string, object>> all,
                               List<Dictionary<string, object>> shown)
    {
        // Per-tab counts from the ON-BOARD worker list = the full list MINUS the terminal cards
        // the user moved to History (_hiddenKeys). We don't use `shown` because that is also
        // narrowed by the active filter; we want totals-per-category independent of the filter,
        // but a card moved to History must not be counted (else "all 1 / done 1" persists after
        // everything was sent to history). Counts come from the shared ToolbarCounts() helper so
        // the render and RowSig can never diverge.
        int[] tc = ToolbarCounts(all);
        int cntAll = tc[0], cntActive = tc[1], cntNeeds = tc[2], cntDone = tc[3];
        int doneN = tc[4], maxN = tc[5], badN = tc[6];

        var bar = new Border();
        bar.BorderThickness = new Thickness(1); bar.BorderBrush = Border;
        bar.Background = CardBg; bar.CornerRadius = new CornerRadius(10);
        bar.Padding = new Thickness(12, 8, 12, 8); bar.Margin = new Thickness(8, 2, 8, 8);
        // clicks inside the toolbar must not bubble (it isn't a card, but stay safe)
        bar.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };

        var dp = new DockPanel();

        // right cluster: bulk MANUAL retry button (one-shot, respects the active filter)
        var rightCl = new StackPanel(); rightCl.Orientation = Orientation.Horizontal;
        rightCl.VerticalAlignment = VerticalAlignment.Center;

        // Only surface the bulk-retry button when there is at least one retry target
        // in `shown` (terminal AND outcome != DONE). Mirror RetryAllShown's selection
        // so the button never appears for an all-DONE run (nothing to retry).
        int retryTargets = 0;
        foreach (Dictionary<string, object> rw in shown)
        {
            if (!IsTerminalWorker(rw)) continue;
            if (S(rw, "outcome") == "DONE") continue;
            retryTargets++;
        }
        if (retryTargets > 0)
        {
            var retryAll = new Button();
            retryAll.Content = T("retry_all");
            retryAll.Background = Brushes.Transparent; retryAll.Foreground = Fg;
            retryAll.BorderThickness = new Thickness(1); retryAll.BorderBrush = Theme.Br(Theme.BorderStrong(_dark));
            retryAll.Padding = new Thickness(12, 4, 12, 4); retryAll.Cursor = Cursors.Hand;
            retryAll.FontSize = 12; retryAll.FontWeight = FontWeights.SemiBold;
            retryAll.Margin = new Thickness(10, 0, 0, 0);
            retryAll.VerticalAlignment = VerticalAlignment.Center;
            List<Dictionary<string, object>> shownCap = shown;
            retryAll.Click += delegate
            {
                RetryAllShown(shownCap);
                if (_toolbarNote != null) _toolbarNote.Text = RunIsLive() ? "" : T("retry_note");
            };
            rightCl.Children.Add(retryAll);
        }

        DockPanel.SetDock(rightCl, Dock.Right);
        dp.Children.Add(rightCl);

        // left: 4 spec tabs as a single segmented control container
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        left.VerticalAlignment = VerticalAlignment.Center;

        // Segmented container: one rounded Border holding all four tabs side-by-side.
        var seg = new Border();
        seg.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        seg.BorderBrush = Theme.Br(Theme.Border(_dark));
        seg.BorderThickness = new Thickness(1);
        seg.CornerRadius = new CornerRadius(6);
        seg.VerticalAlignment = VerticalAlignment.Center;
        var segRow = new StackPanel(); segRow.Orientation = Orientation.Horizontal;

        // Build all four tabs; insert thin dividers between them.
        string allLabel = T("flt_all") + " " + cntAll;
        string activeLabel = T("flt_active") + " " + cntActive;
        string needsLabel = T("flt_needs") + " " + cntNeeds;
        string doneLabel = T("flt_done") + " " + cntDone;

        segRow.Children.Add(SegFilterButton(allLabel, 0, false, 0, true, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(activeLabel, 1, false, 0, false, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(needsLabel, 2, true, cntNeeds, false, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(doneLabel, 3, false, 0, false, true));

        seg.Child = segRow;
        left.Children.Add(seg);

        // Task 5: only show the summary when there are failures; hide when nothing notable.
        int failTotal = badN + maxN;
        if (failTotal > 0)
        {
            var summary = new TextBlock();
            summary.Text = (_lang == 0 ? ("失敗 " + failTotal) : ("Failed " + failTotal));
            summary.Foreground = Theme.Br(Theme.Danger(_dark)); summary.FontSize = 12;
            summary.FontWeight = FontWeights.SemiBold;
            summary.VerticalAlignment = VerticalAlignment.Center;
            summary.Margin = new Thickness(14, 0, 0, 0);
            left.Children.Add(summary);
        }

        _toolbarNote = new TextBlock();
        _toolbarNote.Foreground = Muted; _toolbarNote.FontSize = 11.5;
        _toolbarNote.VerticalAlignment = VerticalAlignment.Center;
        _toolbarNote.Margin = new Thickness(14, 0, 0, 0);
        left.Children.Add(_toolbarNote);

        dp.Children.Add(left);
        bar.Child = dp;
        return bar;
    }
    TextBlock _toolbarNote;
    Button _autoRetryBtn;
    TextBlock _autoRetryCapVal;
    Button _reconnectChatBtn;   // settings-panel "チャット再接続"/"Reconnect chat" -- always available,
                                 // regardless of the Tool dot's state (see RunBridgeReconnectManual).

    // ON => calm success-tinted chip (AccentSoft bg with Success border + text); OFF => neutral muted.
    // Accent fill is reserved exclusively for the primary Start action; this is a status toggle chip.
    void PaintAutoRetryBtn()
    {
        if (_autoRetryBtn == null) return;
        _autoRetryBtn.Content = T("autoretry") + ": " + (_autoRetry ? "ON" : "OFF");
        if (_autoRetry)
        {
            // Subtle success treatment: light chip background, success-colored border, dark/fg text
            // In dark mode: AccentSoft bg is very dark (#3A2416) -- use SurfaceSubtle instead for
            // better readability as a success chip; pair with Success border and Success-tinted text.
            _autoRetryBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoRetryBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoRetryBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoRetryBtn.Background = BtnBg; _autoRetryBtn.Foreground = Muted; _autoRetryBtn.BorderBrush = Border;
        }
    }

    // P2 (c): auto-archive toggle chip -- same calm success/neutral treatment as PaintAutoRetryBtn.
    void PaintAutoArchiveBtn()
    {
        if (_autoArchiveBtn == null) return;
        _autoArchiveBtn.Content = T("autoarchive") + ": " + (_autoArchive ? "ON" : "OFF");
        if (_autoArchive)
        {
            _autoArchiveBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoArchiveBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoArchiveBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoArchiveBtn.Background = BtnBg; _autoArchiveBtn.Foreground = Muted; _autoArchiveBtn.BorderBrush = Border;
        }
    }

    void SetAutoRetryMax(int v)
    {
        _autoRetryMax = Math.Max(1, Math.Min(3, v));   // hard safety bound: 1..3, never unbounded
        SaveKey("autoretry_max", _autoRetryMax.ToString());
        if (_autoRetryCapVal != null) _autoRetryCapVal.Text = _autoRetryMax.ToString();
    }

    // Thin vertical divider between segmented filter tabs.
    UIElement SegDivider()
    {
        var d = new Border { Width = 1, Background = Theme.Br(Theme.Border(_dark)),
                             VerticalAlignment = VerticalAlignment.Stretch,
                             Margin = new Thickness(0, 4, 0, 4) };
        return d;
    }

    // One tab button inside the segmented filter container. Active tab gets a Surface-colored
    // rounded background; inactive tabs are transparent. `isNeeds` + amber treatment preserved.
    // `isFirst`/`isLast` clip corner radius so the active fill doesn't overflow the container border.
    Button SegFilterButton(string label, int val, bool isNeeds, int needsCount, bool isFirst, bool isLast)
    {
        var b = new Button();
        b.Content = label; b.Cursor = Cursors.Hand; b.FontSize = 12;
        b.Padding = new Thickness(10, 5, 10, 5);
        b.BorderThickness = new Thickness(0);   // no individual border -- container holds the border
        bool active = _cardFilter == val;
        CornerRadius cr = new CornerRadius(
            isFirst ? 5 : 0, isLast ? 5 : 0, isLast ? 5 : 0, isFirst ? 5 : 0);
        if (active)
        {
            if (isNeeds && needsCount > 0)
            {
                // Warning amber: active "Needs input" with items
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Theme.Br(Theme.Warning(_dark));
                b.FontWeight = FontWeights.SemiBold;
            }
            else
            {
                // Normal active: Surface fill, Fg text, semibold
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Fg;
                b.FontWeight = FontWeights.SemiBold;
            }
        }
        else
        {
            b.Background = Brushes.Transparent;
            b.Foreground = Muted;
            b.FontWeight = FontWeights.Normal;
        }
        // Use flat template so Background is respected (default Aero ignores it)
        var tmpl = new ControlTemplate(typeof(Button));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, cr);
        var cp2 = new FrameworkElementFactory(typeof(ContentPresenter));
        cp2.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        cp2.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        cp2.SetValue(ContentPresenter.MarginProperty, new Thickness(0));
        bd.AppendChild(cp2);
        tmpl.VisualTree = bd;
        b.Template = tmpl;
        int v = val;
        b.Click += delegate
        {
            if (_cardFilter == v) return;
            _cardFilter = v;
            _lastSig = ""; OnTick(null, null);
        };
        return b;
    }

    // Legacy FilterButton kept as private dead code to avoid breaking references elsewhere.
    // All callers now use SegFilterButton.
    Button FilterButton(string label, int val, bool isNeeds, int needsCount)
    {
        return SegFilterButton(label, val, isNeeds, needsCount, false, false);
    }

    // "⋮" kebab button for card secondary actions. A null label inserts a visual separator.
    // Uses a WPF ContextMenu
    // (not a manual Popup) so it survives the VirtualizingStackPanel recycling its host row
    // without causing layout/focus loops. A ContextMenu is hosted in its own popup root and
    // is NOT tied to the visual tree of the virtualized element; it handles detach gracefully.
    UIElement CardKebabBtn(string[] labels, Action[] actions, Dictionary<string, object> w)
    {
        var btn = new Button();
        // Draw three vertical dots as geometry
        var dotsPanel = new StackPanel { Orientation = Orientation.Vertical,
                                         VerticalAlignment = VerticalAlignment.Center,
                                         HorizontalAlignment = HorizontalAlignment.Center };
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse { Width = 3, Height = 3, Fill = Muted };
            if (i > 0) dot.Margin = new Thickness(0, 3, 0, 0);
            dotsPanel.Children.Add(dot);
        }
        btn.Content = dotsPanel;
        btn.Width = 28; btn.Height = 28; btn.Cursor = Cursors.Hand;
        btn.BorderThickness = new Thickness(0); btn.Background = Brushes.Transparent;
        btn.Padding = new Thickness(0);
        btn.ToolTip = _lang == 0 ? "その他の操作" : "More actions";
        btn.Template = FlatButtonTemplate();
        // Screen-reader name: include the worker name so each card's kebab is distinguishable.
        string kebabWorker = S(w, "name");
        string kebabName;
        if (!string.IsNullOrEmpty(kebabWorker))
            kebabName = _lang == 0 ? (kebabWorker + " その他の操作") : (kebabWorker + " more actions");
        else
            kebabName = _lang == 0 ? "その他の操作" : "More actions";
        System.Windows.Automation.AutomationProperties.SetName(btn, kebabName);

        // Build a ContextMenu. Unlike a manual Popup, a ContextMenu lives in its own HwndSource
        // and does not hold a reference that breaks when the virtualizing container is recycled.
        var cm = new ContextMenu();
        cm.Background = Theme.Br(Theme.Surface(_dark));
        cm.BorderBrush = Theme.Br(Theme.Border(_dark));
        cm.BorderThickness = new Thickness(1);
        cm.Padding = new Thickness(2);
        // The kebab sits at the far-right edge of a card. Shift the popup left so the complete
        // menu remains inside the Fleet window instead of growing off its right edge.
        cm.MinWidth = 240;
        cm.HorizontalOffset = -250;

        string[] lbls = labels;
        Action[] acts = actions;
        for (int i = 0; i < lbls.Length; i++)
        {
            int idx = i;
            if (lbls[idx] == null)
            {
                cm.Items.Add(new Separator { Margin = new Thickness(4, 3, 4, 3) });
                continue;
            }
            var mi = new MenuItem();
            mi.Header = lbls[idx];
            mi.Background = Brushes.Transparent;
            mi.Foreground = Theme.Br(Theme.Text(_dark));
            mi.BorderThickness = new Thickness(0);
            mi.Padding = new Thickness(12, 6, 12, 6);
            mi.FontSize = 12.5;
            Action a = acts[idx];
            mi.Click += delegate (object s2, RoutedEventArgs e2) { a(); };
            cm.Items.Add(mi);
        }
        btn.ContextMenu = cm;

        btn.Click += delegate (object s, RoutedEventArgs e)
        {
            e.Handled = true;
            if (btn.ContextMenu != null)
            {
                btn.ContextMenu.PlacementTarget = btn;
                btn.ContextMenu.Placement = PlacementMode.Bottom;
                btn.ContextMenu.IsOpen = true;
            }
        };
        // ButtonBase already handles its mouse-up. Do not mark PreviewMouseLeftButtonUp handled:
        // doing so prevents ButtonBase from raising Click, leaving this menu apparently dead.
        return btn;
    }

    // The history SECTION HEADER (Clear button + caption). Factored out of the old AppendHistory
    // so the virtualizing converter can build it for a Kind==2 row. The history ROWS are built
    // separately by HistoryRow() per Kind==3 row.
    UIElement HistoryHeader()
    {
        var head = new DockPanel();
        head.Margin = new Thickness(8, 18, 8, 4);
        var clear = new Button();
        clear.Content = T("clear_history") + " (" + _history.Count + ")";
        clear.Cursor = Cursors.Hand; clear.BorderThickness = new Thickness(1);
        clear.Background = BtnBg; clear.Foreground = Fg; clear.BorderBrush = Border;
        clear.Padding = new Thickness(10, 2, 10, 2); clear.FontSize = 12;
        clear.Click += delegate { ClearHistory(); };
        DockPanel.SetDock(clear, Dock.Right);
        head.Children.Add(clear);

        // P2 (b): live search box. Docked right of the caption; typing filters the rows below with a
        // ~300ms debounce (so it doesn't rebuild on every keystroke). Built ONCE and preserved across
        // renders (the header row's signature is static), so focus + caret survive a re-filter.
        if (_histSearchBox == null)
        {
            _histSearchBox = new TextBox();
            _histSearchBox.Text = _histQuery ?? "";
            _histSearchBox.Width = 160; _histSearchBox.FontSize = 12;
            _histSearchBox.Padding = new Thickness(6, 2, 6, 2);
            _histSearchBox.VerticalContentAlignment = VerticalAlignment.Center;
            _histSearchBox.BorderThickness = new Thickness(1);
            _histSearchBox.ToolTip = T("hist_search");
            _histSearchBox.TextChanged += delegate { OnHistSearchChanged(); };
        }
        _histSearchBox.Background = BtnBg; _histSearchBox.Foreground = Fg; _histSearchBox.BorderBrush = Border;
        // The box is cached so focus and caret survive a re-filter, but the wrapper around it is
        // not: this method runs again whenever the list re-realizes the header row (virtualization
        // recycling, DataContext change), and handing an element that still has a logical parent
        // to a second Border throws InvalidOperationException. That escapes through a binding
        // converter, where nothing catches it, and WPF aborts the process -- the cockpit was
        // dying mid-run. Disconnect from the previous wrapper first.
        var prevWrap = LogicalTreeHelper.GetParent(_histSearchBox) as Decorator;
        if (prevWrap != null) prevWrap.Child = null;
        var searchWrap = new Border { Child = _histSearchBox, Margin = new Thickness(8, 0, 8, 0) };
        DockPanel.SetDock(searchWrap, Dock.Right);
        head.Children.Add(searchWrap);

        var ht = new TextBlock();
        ht.Text = (_lang == 0 ? "履歴 — クリアするまで蓄積（クリックで会話を表示）" : "History — stacks until cleared (click to open)");
        ht.Foreground = Muted; ht.FontSize = 12.5; ht.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(ht);
        return head;
    }

    // Debounced history-search handler: stash the query, then (re)arm a ~300ms one-shot timer that
    // triggers a single re-render. Prevents a rebuild-per-keystroke thrash while staying live.
    void OnHistSearchChanged()
    {
        if (_histSearchBox == null) return;
        _histQuery = _histSearchBox.Text ?? "";
        if (_histSearchTimer == null)
        {
            _histSearchTimer = new DispatcherTimer();
            _histSearchTimer.Interval = TimeSpan.FromMilliseconds(300);
            _histSearchTimer.Tick += delegate { _histSearchTimer.Stop(); ForceRender(); };
        }
        _histSearchTimer.Stop();
        _histSearchTimer.Start();
    }

    // Divider that separates the live (active/queued) cards above from this run's TERMINAL workers
    // below -- so the top of the list is only what's still running.
    UIElement CompletedDivider()
    {
        int n = 0;
        if (_toolbarShown != null)
            foreach (Dictionary<string, object> w in _toolbarShown)
                if (IsTerminalWorker(w)) n++;
        var head = new DockPanel();
        head.Margin = new Thickness(8, 16, 8, 4);
        var all = new Button();
        all.Content = T("all_to_history");
        all.Cursor = Cursors.Hand; all.BorderThickness = new Thickness(1);
        all.Background = BtnBg; all.Foreground = Fg; all.BorderBrush = Border;
        all.Padding = new Thickness(10, 2, 10, 2); all.FontSize = 12;
        all.ToolTip = _lang == 0 ? "完了をすべて履歴へ移動" : "Move all completed to history";
        all.Click += delegate { ArchiveAllTerminal(); };
        DockPanel.SetDock(all, Dock.Right);
        head.Children.Add(all);
        var t = new TextBlock();
        t.Text = (_lang == 0 ? "完了 — " : "Completed — ") + n + (_lang == 0 ? " 件（このラン）" : " (this run)");
        t.Foreground = Muted; t.FontSize = 12.5; t.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(t);
        return head;
    }

    Border HistoryRow(Dictionary<string, object> e)
    {
        string status = S(e, "status");
        string ck = ColorKey(status);
        string conv = S(e, "conv_url");
        // Default COLLAPSED, exactly like a live card: a terminal worker that scrolls down into
        // History must NOT spring open. The disclosure key is the archive `key` (started#name),
        // which never collides with a live worker name (those carry no '#'), so it can ride the
        // same _expanded set / ToggleExpand path as live cards.
        string hkey = S(e, "key");
        bool isOpen = !string.IsNullOrEmpty(hkey) && _expanded.Contains(hkey);

        var row = new Border();
        row.BorderThickness = new Thickness(1);
        row.BorderBrush = Border; row.Background = CardBg;
        row.CornerRadius = new CornerRadius(9);
        row.Padding = new Thickness(14, 8, 14, 8); row.Margin = new Thickness(8, 3, 8, 3);

        var col = new StackPanel();
        var dp = new DockPanel();
        // chevron disclosure (only when there is a key to toggle)
        if (!string.IsNullOrEmpty(hkey))
        {
            var chev = ChevronToggle(hkey, isOpen);
            DockPanel.SetDock(chev, Dock.Left);
            dp.Children.Add(chev);
        }
        string hcanon = status == "ready" ? "waiting" : status;
        var pill = Pill(Theme.StatusLabel(hcanon, _lang), Theme.StatusRail(hcanon));
        pill.Margin = new Thickness(0, 0, 5, 0);
        DockPanel.SetDock(pill, Dock.Left);
        dp.Children.Add(pill);
        var turns = new TextBlock();
        turns.Text = T("turn") + " " + I(e, "turn");
        turns.Foreground = Muted; turns.FontSize = 11.5;
        turns.VerticalAlignment = VerticalAlignment.Center;
        turns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(turns, Dock.Right);
        dp.Children.Add(turns);
        // Collapsed: the concise headline (conv_title if captured, else derived) -- single line,
        // ellipsis-trimmed -- so History matches the live collapsed card instead of dumping the
        // full goal text. The long goal only appears when this row is explicitly expanded.
        var head = new TextBlock();
        head.Text = CardTitle(S(e, "conv_title"), S(e, "goal"));
        head.Foreground = Fg; head.FontSize = 13;
        head.VerticalAlignment = VerticalAlignment.Center;
        head.TextTrimming = TextTrimming.CharacterEllipsis;
        dp.Children.Add(head);
        col.Children.Add(dp);

        if (isOpen)
        {
            // expanded: full goal, wrapped, as muted selectable text (mirrors the live card)
            var g = new TextBox();
            g.Text = S(e, "goal");
            g.Foreground = Muted; g.FontSize = 12.5;
            g.IsReadOnly = true; g.BorderThickness = new Thickness(0);
            g.Background = Brushes.Transparent; g.Padding = new Thickness(0);
            g.IsTabStop = false; g.TextWrapping = TextWrapping.Wrap;
            g.Margin = new Thickness(0, 6, 0, 2);
            SwallowMouseUp(g);
            col.Children.Add(g);

            // "続ける" (Continue): send a FOLLOW-UP instruction to this finished task, carrying its
            // prior context. Launches a FRESH fleet run whose goal PREPENDS the prior goal + a note
            // to re-read the on-disk outputs (no reopenable URL for this agent). Capture e's values
            // into locals BEFORE the delegate for closure-safety (same pattern as the row handler).
            string contPrior = S(e, "goal");
            string contConv = S(e, "conv_url");
            var contBtn = new Button();
            contBtn.Content = _lang == 0 ? "続ける" : "Continue";
            contBtn.Cursor = Cursors.Hand;
            contBtn.FontSize = 12;
            contBtn.Padding = new Thickness(10, 3, 10, 3);
            contBtn.Margin = new Thickness(0, 6, 0, 0);
            contBtn.HorizontalAlignment = HorizontalAlignment.Right;
            contBtn.BorderThickness = new Thickness(1);
            contBtn.Background = BtnBg;
            contBtn.BorderBrush = Border;
            contBtn.Foreground = Fg;
            contBtn.Template = FlatButtonTemplate();
            contBtn.Click += delegate
            {
                string fu = PromptFollowup();
                if (string.IsNullOrEmpty(fu)) return;
                string goalText = BuildContinueGoal(contPrior, fu);
                // The continuation goal is MULTI-LINE; SpawnFleet's GoalsToJsonl now escapes a
                // plain multi-line goal string safely on its own, so no manual serialization is
                // needed for the common case. Only pre-serialize here when there's an EXTRA key
                // beyond "text" to carry (resume_conv) -- GoalsToJsonl detects an already-JSON
                // goal string and passes it through as-is instead of double-wrapping it.
                var glist = new List<string>();
                if (!string.IsNullOrEmpty(contConv))
                {
                    var gd = new Dictionary<string, object>();
                    gd["text"] = goalText;
                    gd["resume_conv"] = contConv;
                    glist.Add(_js.Serialize(gd));
                }
                else
                {
                    glist.Add(goalText);
                }
                SpawnFleet(glist, "continue_input.txt");
                if (_startNote != null)
                {
                    _startNote.Text = _lang == 0
                        ? "前回タスクの続きを開始しました（成果物を読み直してから追加指示を実行します）。"
                        : "Started a continuation of the prior task (it re-reads the saved outputs, then runs the follow-up).";
                }
            };
            col.Children.Add(contBtn);
        }

        row.Child = col;
        // A history row is openable when we have ANY way to reach its body: a real conv_url, the
        // disk transcript path, or the worker name (the chat resolves the .jsonl by name). conv_url
        // is empty in the final snapshot / for the new agent, so keying ONLY on it left completed
        // conversations un-clickable -- the body was on disk the whole time. (friction #20)
        string htx = S(e, "transcript"), hnm = S(e, "name");
        if (!string.IsNullOrEmpty(conv) || !string.IsNullOrEmpty(htx) || !string.IsNullOrEmpty(hnm))
        {
            row.Cursor = Cursors.Hand;
            string url = conv, tx = htx, nm = hnm;
            row.MouseLeftButtonUp += delegate { FlashOpen(row); OpenHistory(nm, url, tx); };
        }
        return row;
    }

    // Pass A2-1: true when a status is a terminal-but-unresolved attention lane (stuck/maxturns/error).
    // These get the "recovery surface" treatment on their collapsed row (§6 spec).
    static bool IsAttentionStatus(string status)
    {
        return status == "stuck" || status == "maxturns" || status == "error";
    }

    // P0: an INFRA_STUCK worker is NOT a task failure — the engine parked it because the infra
    // (Edge session / sign-in / connector) broke. It gets a distinct ORANGE インフラ待ち pill,
    // is excluded from failure/miss counts, and its `reason` (actionable) is shown as-is.
    static bool IsInfraStuck(Dictionary<string, object> w)
    {
        return string.Equals(S(w, "outcome"), "INFRA_STUCK", StringComparison.OrdinalIgnoreCase);
    }

    // Build a small outline button for the attention recovery row (§6: equal-weight neutral buttons).
    Button AttentionBtn(string label)
    {
        var b = new Button();
        b.Content = label;
        b.Cursor = Cursors.Hand;
        b.FontSize = 12;
        b.Padding = new Thickness(10, 3, 10, 3);
        b.Margin = new Thickness(0, 0, 6, 0);
        b.BorderThickness = new Thickness(1);
        b.Background = Brushes.Transparent;
        b.BorderBrush = Border;
        b.Foreground = Muted;
        b.Template = FlatButtonTemplate();
        return b;
    }

    // One canonical user-facing result string for the expanded result panel, clipboard, and
    // artifact detection. Keeping these entry points together prevents the menu from copying raw
    // runner chatter while the card displays the cleaned final answer.
    string WorkerResultText(Dictionary<string, object> w, bool terminal)
    {
        string displayResult = w != null ? S(w, "display_result") : "";
        if (!string.IsNullOrEmpty(displayResult)) return displayResult;

        string last = w != null ? S(w, "last") : "";
        if (!string.IsNullOrEmpty(last))
        {
            string cleaned = CleanAgentResultForUi(last);
            return !string.IsNullOrEmpty(cleaned)
                ? cleaned
                : (_lang == 0 ? "結果を受信しました" : "Result received");
        }

        string outcome = w != null ? S(w, "outcome") : "";
        return terminal ? OutcomeLabel(outcome) : (_lang == 0 ? "実行中…" : "Working…");
    }

    void CopyWorkerResult(string result)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(result)) throw new InvalidOperationException();
            Clipboard.SetText(result);
            ShowScaleToast(T("copy_result_done"));
        }
        catch (Exception) { ShowScaleToast(T("copy_result_fail")); }
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string rawStatus = S(w, "status");
        string status = rawStatus == "ready" ? "waiting" : rawStatus;   // canonical (runner emits fine states)
        string reason = S(w, "reason");
        string last = S(w, "last");
        string conv = S(w, "conv_url");
        string convTitle = S(w, "conv_title");
        int turn = I(w, "turn");
        int reviews = I(w, "verify_attempts");
        bool verifiedOk = string.Equals(S(w, "verified"), "True", StringComparison.OrdinalIgnoreCase);
        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                        || status == "error" || status == "cancelled";
        bool isOpen = _expanded.Contains(name);
        // P0: INFRA_STUCK is an infra pause (Edge/sign-in/connector broke), NOT a task failure. It
        // gets the distinct ORANGE インフラ待ち treatment: a warning rail/pill, its actionable reason
        // shown as-is, and a 再投入 re-queue action — NOT the red stuck/error recovery surface.
        bool isInfra = !closed && IsInfraStuck(w);
        // Attention lane: stuck/maxturns/error and NOT yet expanded -- gets recovery surface treatment.
        // INFRA_STUCK is carved out of the red attention lane (handled by its own infra branch).
        bool isAttention = !closed && !isInfra && IsAttentionStatus(status);

        string railKind = closed ? "neutral" : (isInfra ? "warning" : Theme.StatusRail(status));
        Brush statusBrush = Theme.Br(Theme.RailColor(railKind, _dark));
        // Chip color is computed SEPARATELY from the left rail. A completed worker that has been
        // released (closed) keeps status=="done"/outcome=="DONE", but `railKind` above forces neutral
        // (grey) for the rail. The chip, however, must match the History row's done chip
        // (Theme.StatusRail("done")=="success", green) so "完了" is the same green in both places.
        // We therefore base chipKind on the status (with an explicit DONE override), not on `closed`.
        bool isDone = status == "done" || string.Equals(S(w, "outcome"), "DONE", StringComparison.OrdinalIgnoreCase);
        string chipKind = isDone ? "success" : (isInfra ? "warning" : Theme.StatusRail(status));

        // Pass A2-1 TASK 1: demote the collapsed row to a LEDGER ROW.
        // - No rounded corners, no card background fill, no full border.
        // - Flat row on the app surface: bottom divider only (Theme.Border).
        // - 3px left status rail preserved at full row height.
        // - Tighter vertical padding so rows are denser (~64-76px collapsed).
        var card = new Border();
        card.Tag = name;                // lets a chevron toggle find & replace just this card
        // Ledger row: only a bottom hairline divider (no rounded card border).
        card.BorderThickness = new Thickness(0, 0, 0, 1);
        card.BorderBrush = Border;
        // Transparent background (inherits the flat app surface).
        card.Background = Brushes.Transparent;
        // No margin between rows; the bottom border IS the separator.
        card.Margin = new Thickness(0, 0, 0, 0);

        var shell = new Grid();
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });   // rail
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        // Rail spans full row height (no vertical margin) — the Margin(0,2,0,2) is gone.
        var rail = new Border { Width = Theme.RailW, Background = statusBrush };
        Grid.SetColumn(rail, 0); shell.Children.Add(rail);

        // Tighter padding: 10px horizontal, 7px vertical top/bottom.
        var col = new StackPanel { Margin = new Thickness(10, 7, 12, 7) };
        Grid.SetColumn(col, 1); shell.Children.Add(col);

        // ── line 1: [chevron] [chip] [title* ........] [Open] [primary action | kebab⋮] ──
        // Fixed right-action width (~112px) so long titles never collide with actions.
        var top = new Grid();
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });  // left: chevron+chip+title
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(112, GridUnitType.Pixel) }); // right: actions

        // Right action area (fixed 112px): Open + primary action or kebab.
        var right = new StackPanel { Orientation = Orientation.Horizontal,
                                     HorizontalAlignment = HorizontalAlignment.Right,
                                     VerticalAlignment = VerticalAlignment.Center };
        {
            var openLink = new TextBlock();
            openLink.Text = _lang == 0 ? "開く" : "Open";
            openLink.Foreground = Muted; openLink.FontSize = 12;
            openLink.VerticalAlignment = VerticalAlignment.Center; openLink.Cursor = Cursors.Hand;
            openLink.Margin = new Thickness(0, 0, 8, 0);
            openLink.ToolTip = _lang == 0 ? "この会話をメインチャットで開く" : "Open this conversation in the chat";
            string onm = name; string ourl = conv;
            openLink.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; FlashOpen(card); OpenWorker(onm, ourl); };
            right.Children.Add(openLink);
        }
        if (closed || terminal)
        {
            // Completed/released Fleet card menu: keep the kebab available after its agent tab is
            // released. The result and artifacts remain useful, and the same goal can be run again.
            // Reuse actions come first; the cleanup action sits after a separator.
            var wt2 = w;
            string menuResult = WorkerResultText(wt2, true);
            var artifactPaths = DetectExistingPaths(menuResult);
            var menuLabels = new List<string>();
            var menuActions = new List<Action>();

            string copyText = menuResult;
            menuLabels.Add(T("copy_result"));
            menuActions.Add(delegate { CopyWorkerResult(copyText); });

            if (artifactPaths.Count > 0)
            {
                string firstArtifact = artifactPaths[0];
                menuLabels.Add(T("reveal_artifacts"));
                menuActions.Add(delegate {
                    ShowScaleToast(RevealPath(firstArtifact) ? T("reveal_artifacts_done") : T("reveal_artifacts_fail"));
                });
            }

            menuLabels.Add(T("rerun_same"));
            menuActions.Add(delegate { RetryGoal(wt2); ShowScaleToast(T("rerun_started")); });

            menuLabels.Add(null);
            menuActions.Add(null);
            menuLabels.Add("→ " + T("to_history"));
            menuActions.Add(delegate { ArchiveAndHide(wt2); });
            right.Children.Add(CardKebabBtn(menuLabels.ToArray(), menuActions.ToArray(), w));
        }
        else
        {
            // running (non-terminal) card: release stays VISIBLE (single-click, never hidden)
            var relBtn = new Button();
            relBtn.Content = MakeReleaseContent();
            relBtn.Cursor = Cursors.Hand; relBtn.BorderThickness = new Thickness(1);
            relBtn.Background = BtnBg; relBtn.BorderBrush = Border; relBtn.Foreground = Fg;
            relBtn.Padding = new Thickness(6, 2, 8, 2);
            relBtn.ToolTip = _lang == 0 ? "このタスクを停止してタブを解放（fleet 停止中ならカードを片付け）"
                                        : "Stop this task and release its tab (clears the card if the fleet is stopped)";
            string nm = name; var wt2 = w;
            relBtn.Click += delegate {
                RequestClose(nm);
                if (_lastRoot != null
                    && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                    && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                    ArchiveAndHide(wt2);
            };
            right.Children.Add(relBtn);
        }
        Grid.SetColumn(right, 1); top.Children.Add(right);

        // left cluster: chevron + status chip, then the title fills the rest (1 line, ellipsis)
        var left = new DockPanel { LastChildFill = true };
        var chev = ChevronToggle(name, isOpen); DockPanel.SetDock(chev, Dock.Left); left.Children.Add(chev);
        // INFRA_STUCK -> distinct ORANGE インフラ待ち pill; otherwise the normal status label.
        var chip = Pill(isInfra ? T("infra_wait") : Theme.StatusLabel(status, _lang), chipKind);
        chip.Margin = new Thickness(2, 0, 5, 0);
        DockPanel.SetDock(chip, Dock.Left); left.Children.Add(chip);
        // AGENT BADGE (P0 feature 4): which agent this conversation is bound to. Green subtle badge
        // for the configured agent, WARNING-colored 既定Copilot badge for a plain /chat/ (default) url.
        var agentBadge = BuildAgentBadge(conv, convTitle);
        if (agentBadge != null) { DockPanel.SetDock(agentBadge, Dock.Left); left.Children.Add(agentBadge); }
        string headline = CardTitle(convTitle, goal);
        var ht = new TextBlock {
            Text = headline, Foreground = Fg, FontSize = 13.5, FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap
        };
        left.Children.Add(ht);
        Grid.SetColumn(left, 0); top.Children.Add(left);
        col.Children.Add(top);

        if (!isOpen)
        {
            // ── COLLAPSED ROW body (ledger row, not expanded drawer) ──────────────────────────
            if (isInfra)
            {
                // P0 INFRA_STUCK collapsed row: the reason text is actionable (e.g. "sign-in
                // required" / "default-Copilot fallback"), so render it verbatim, then offer a
                // 再投入 action reusing the existing retry path. NOT a red error surface.
                if (!string.IsNullOrEmpty(reason))
                {
                    var ir = new TextBlock
                    {
                        Text = OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(ir);
                }
                var infraRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24, 6, 0, 2) };
                infraRow.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };
                // 再投入 (Re-queue): reuse RetryGoal (add_goal if live, else respawn) — the same
                // affordance stopped goals already use, per spec ("reuse the existing retry path").
                var requeueBtn = AttentionBtn(T("infra_retry"));
                requeueBtn.ToolTip = _lang == 0
                    ? "インフラ復旧後にこのゴールを再投入します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Re-queue this goal after the infra recovers (add_goal if live, else respawn fleet)";
                Dictionary<string, object> wInfra = w;
                requeueBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wInfra); };
                infraRow.Children.Add(requeueBtn);
                // Open-conversation shortcut so the user can inspect the parked lane.
                var infraOpen = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                string infraNm = name; string infraUrl = conv;
                infraOpen.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(infraNm, infraUrl); };
                infraRow.Children.Add(infraOpen);
                col.Children.Add(infraRow);
            }
            else if (isAttention)
            {
                // Pass A2-1 TASK 2: RECOVERY ROW for attention lanes (stuck/maxturns/error).
                // §6: "Not an error card — a recovery surface."

                // Line 2: human recovery sentence from the `reason` field [REAL].
                // Calm lead-in + verbatim reason text. Omit when reason is empty.
                if (!string.IsNullOrEmpty(reason))
                {
                    string leadIn = _lang == 0 ? "停止理由: " : "Stopped: ";
                    var rl2 = new TextBlock
                    {
                        Text = leadIn + OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl2);
                }

                // Line 3: freshness — "最終更新 {N} 前" / "last update {N} ago" [COMPUTED].
                // Best proxy: transcript meta ts (start of this worker); fallback to status updated.
                {
                    string transcriptPath2 = S(w, "transcript");
                    double evidenceTs = ReadTranscriptStartTs(transcriptPath2);
                    // If transcript ts unavailable, fall back to the status.json top-level updated field.
                    if (evidenceTs <= 0 && _lastRoot != null)
                        evidenceTs = Dbl(_lastRoot, "updated");
                    string freshnessText;
                    if (evidenceTs > 0)
                    {
                        double age = NowUnix() - evidenceTs;
                        if (age < 0) age = 0;
                        freshnessText = _lang == 0
                            ? ("最終更新 " + Fmt(age) + " 前")
                            : ("last update " + Fmt(age) + " ago");
                    }
                    else
                    {
                        freshnessText = _lang == 0 ? "最終更新: 不明" : "last update: unknown";
                    }
                    var ml3 = new TextBlock
                    {
                        Text = freshnessText,
                        Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(ml3);
                }

                // Recovery actions (§6): [再開]/[Resume], [会話を開く]/[Open conversation], [停止]/[Stop].
                // Equal-weight outline buttons; NO accent color.
                var recov = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(24, 6, 0, 2)
                };
                recov.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };

                // [再開]/[Resume]: fall back to RetryGoal (re-queues or re-spawns via existing path).
                var resumeBtn = AttentionBtn(_lang == 0 ? "再開" : "Resume");
                resumeBtn.ToolTip = _lang == 0
                    ? "このタスクを再開・再試行します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Resume or retry this task (add_goal if fleet live, else respawn fleet)";
                Dictionary<string, object> wResume = w;
                resumeBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wResume); };
                recov.Children.Add(resumeBtn);

                // [会話を開く]/[Open conversation]: open the conversation/transcript in the main chat.
                var evidBtn = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                evidBtn.ToolTip = _lang == 0
                    ? "この会話を開いて最後のやり取りを確認する"
                    : "Open this conversation to review the last exchange";
                string evidNm = name; string evidUrl = conv;
                evidBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(evidNm, evidUrl); };
                recov.Children.Add(evidBtn);

                // [停止]/[Stop]: release/archive this lane via existing mechanisms.
                var stopBtn = AttentionBtn(_lang == 0 ? "停止" : "Stop");
                stopBtn.ToolTip = _lang == 0
                    ? "このレーンを停止して履歴へ移動する"
                    : "Stop this lane and move to history";
                string stopNm = name; Dictionary<string, object> wStop = w;
                stopBtn.Click += delegate (object s2, RoutedEventArgs e2)
                {
                    e2.Handled = true;
                    RequestClose(stopNm);
                    if (_lastRoot != null
                        && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                        && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                        ArchiveAndHide(wStop);
                    else
                        ArchiveAndHide(wStop);
                };
                recov.Children.Add(stopBtn);

                col.Children.Add(recov);
            }
            else
            {
                // ── Normal collapsed ledger row: line 2 + line 3 ────────────────────────────
                // Line 2: latest human-readable progress. Precedence: display_result > last > fallback.
                string collapsedDisplayResult = S(w, "display_result");
                string resultText;
                if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    resultText = collapsedDisplayResult;
                else if (!string.IsNullOrEmpty(last))
                    resultText = last;
                else if (terminal || closed)
                    resultText = "";
                else if (status == "pending")
                    resultText = (_lang == 0 ? "待機中…" : "Queued…");
                else
                    resultText = (_lang == 0 ? "実行中…" : "Working…");

                if (!string.IsNullOrEmpty(resultText))
                {
                    string displayLine;
                    if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    {
                        // display_result is already clean; just take the first line for the collapsed view
                        int nl2 = collapsedDisplayResult.IndexOf('\n');
                        string fl2 = nl2 >= 0 ? collapsedDisplayResult.Substring(0, nl2) : collapsedDisplayResult;
                        displayLine = OneLine(!string.IsNullOrEmpty(fl2) ? fl2 : collapsedDisplayResult);
                    }
                    else if (!string.IsNullOrEmpty(last))
                    {
                        string cleanedResult = CleanAgentResultForUi(last);
                        string firstLine = "";
                        if (!string.IsNullOrEmpty(cleanedResult))
                        {
                            int nl = cleanedResult.IndexOf('\n');
                            firstLine = nl >= 0 ? cleanedResult.Substring(0, nl) : cleanedResult;
                        }
                        displayLine = !string.IsNullOrEmpty(firstLine)
                            ? OneLine(firstLine)
                            : (_lang == 0 ? "結果を受信しました" : "Result received");
                    }
                    else
                    {
                        displayLine = OneLine(resultText);
                    }
                    var rl = new TextBlock
                    {
                        Text = displayLine, Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl);
                }

                // Line 3: meta — worker name · turn N · alive {freshness} ago · ✓verified [COMPUTED].
                var meta = new StringBuilder();
                string transcriptPath = S(w, "transcript");
                double startTs = ReadTranscriptStartTs(transcriptPath);
                if (startTs > 0)
                {
                    double cardElapsed = NowUnix() - startTs;
                    if (cardElapsed > 0)
                        meta.Append(Fmt(cardElapsed)).Append(" · ");
                }
                meta.Append(name.ToUpper());
                if (turn > 0) meta.Append(" · ").Append(T("turn")).Append(' ').Append(turn);
                if (reviews > 0) meta.Append(" · ").Append(_lang == 0 ? ("確認 " + reviews + " 回") : ("reviewed " + reviews));
                if (verifiedOk) meta.Append(" · ").Append(_lang == 0 ? "✓ 検証OK" : "✓ verified");
                var ml = new TextBlock
                {
                    Text = meta.ToString(), Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                    Margin = new Thickness(24, 3, 0, 0)
                };
                col.Children.Add(ml);

                // TASK 3 (Bucket C): next_step + self_confidence [REAL — agent-emitted, shown as-is].
                string nextStep = S(w, "next_step");
                string selfConf = S(w, "self_confidence");

                // "→ 次: {next_step}" / "→ next: {next_step}"  — muted, 1 line ellipsis.
                if (!string.IsNullOrEmpty(nextStep))
                {
                    string nextPrefix = _lang == 0 ? "→ 次: " : "→ next: ";
                    var nextTb = new TextBlock
                    {
                        Text = nextPrefix + nextStep,
                        Foreground = Theme.Br(Theme.Muted(_dark)),
                        FontSize = 12,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(nextTb);
                }

                // Self-confidence chip — clearly labeled "自己申告" (self-reported, not verified).
                // Neutral/muted styling; do NOT use a trust-signal color. Subtle level tint only.
                if (!string.IsNullOrEmpty(selfConf))
                {
                    string confLabel = _lang == 0
                        ? ("自己申告: " + selfConf)
                        : ("self-reported: " + selfConf);
                    // Subtle background tint by level (very muted — not a trust signal).
                    string tintHex;
                    if (selfConf == "high")        tintHex = _dark ? "#1a2a1a" : "#e8f5e8";
                    else if (selfConf == "medium")  tintHex = _dark ? "#2a2a1a" : "#f5f5e8";
                    else                            tintHex = _dark ? "#2a1a1a" : "#f5e8e8";  // low / unknown

                    var confChip = new Border();
                    confChip.CornerRadius = new CornerRadius(4);
                    confChip.Background = Theme.Br(tintHex);
                    confChip.Padding = new Thickness(6, 1, 6, 1);
                    confChip.Margin = new Thickness(24, 3, 0, 0);
                    confChip.HorizontalAlignment = HorizontalAlignment.Left;
                    var confTb = new TextBlock();
                    confTb.Text = confLabel;
                    confTb.FontSize = 11;
                    confTb.Foreground = Theme.Br(Theme.Muted(_dark));
                    confChip.Child = confTb;
                    col.Children.Add(confChip);
                }
            }

            // Feature 1: always-visible steer affordance -- reachable WITHOUT expanding the card.
            // Same terminal gate the expanded drawer's SteerRow/RetryRow/ContinueRow switch uses
            // (below): a terminal card (done/stuck/maxturns/error/cancelled) gets nothing here.
            if (!terminal) col.Children.Add(CollapsedSteerRow(name));
        }
        else
        {
            // ── EXPANDED DRAWER ───────────────────────────────────────────────────────────────
            // Expanded detail is organized into tabs (spec): Overview (default) / Conversation /
            // Review / Logs -- so raw transcript, refuter notes and internal fields no longer all
            // dump onto the surface at once. Heavy content is built ONLY when expanded.
            col.Children.Add(BuildCardTabs(w, name, goal, last, reason, terminal));
            // Actions live BELOW the tabs (not inside one) so steer/retry are always reachable.
            if (!terminal) col.Children.Add(SteerRow(name));
            else if (S(w, "outcome") != "DONE") col.Children.Add(RetryRow(w));
            else col.Children.Add(ContinueRow(name, goal, S(w, "conv_url")));
        }

        card.Child = shell;
        // FIX B: while a Stop is in flight (optimistic, not yet confirmed by the sweep), dim every
        // non-terminal card so the click reads as "taking effect" immediately. terminal cards
        // (already done/stuck/etc.) are left alone -- Stop doesn't touch them.
        card.Opacity = (_stopping && !terminal) ? 0.45 : 1.0;
        // Always clickable: open this worker in the main chat BY NAME, so it works even when the
        // Copilot conv_url was never captured (the main chat renders the live status.json snapshot
        // for this worker). conv_url is passed too so /history can fill in the full transcript
        // when it is available.
        card.Cursor = Cursors.Hand;
        string wname = name; string url = conv;
        card.MouseLeftButtonUp += delegate { FlashOpen(card); OpenWorker(wname, url); };
        card.ToolTip = _lang == 0 ? "クリックでこの会話をメインに表示" : "Click to open this conversation in the chat";
        return card;
    }

    // Per-card selected tab (0=Overview,1=Conversation,2=Review,3=Logs). Persisted so a streaming
    // worker's open tab doesn't snap back to Overview on each status re-render.
    Dictionary<string, int> _cardTab = new Dictionary<string, int>();

    // Expanded-card tab group (spec): Overview default; raw transcript/refuter/internal fields live
    // behind Conversation/Review/Logs instead of all dumping onto the surface. Clicking a tab flips
    // panel visibility in place (no re-render needed); the choice is remembered in _cardTab.
    UIElement BuildCardTabs(Dictionary<string, object> w, string name, string goal, string last,
                            string reason, bool terminal)
    {
        int sel = _cardTab.ContainsKey(name) ? _cardTab[name] : 0;
        string outcome = S(w, "outcome");
        bool done = outcome == "DONE";
        int reviews = I(w, "verify_attempts");
        bool verifiedOk = string.Equals(S(w, "verified"), "True", StringComparison.OrdinalIgnoreCase);
        string tpath = S(w, "transcript");

        var wrap = new StackPanel { Margin = new Thickness(24, 12, 0, 2) };

        var panels = new UIElement[] {
            TabOverview(goal, last, outcome, terminal, reviews, verifiedOk, tpath, w),
            TabConversation(tpath),
            TabReview(reason, done, terminal, reviews),
            TabLogs(w, reason)
        };
        string[] labels = _lang == 0 ? new string[] { "概要", "会話", "レビュー", "ログ" }
                                      : new string[] { "Overview", "Conversation", "Review", "Logs" };

        var strip = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 10) };
        var txts = new TextBlock[4];
        var unders = new Border[4];
        for (int i = 0; i < 4; i++)
        {
            int idx = i;
            var tcol = new StackPanel();
            var tt = new TextBlock { Text = labels[i], FontSize = 12.5, Margin = new Thickness(0, 0, 0, 4) };
            var un = new Border { Height = 2, CornerRadius = new CornerRadius(1) };
            tcol.Children.Add(tt); tcol.Children.Add(un);
            txts[i] = tt; unders[i] = un;
            var box = new Border { Child = tcol, Cursor = Cursors.Hand, Background = Brushes.Transparent, Margin = new Thickness(0, 0, 16, 0) };
            box.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
            {
                e.Handled = true;
                _cardTab[name] = idx;
                for (int j = 0; j < 4; j++)
                {
                    panels[j].Visibility = j == idx ? Visibility.Visible : Visibility.Collapsed;
                    PaintTab(txts[j], unders[j], j == idx);
                }
            };
            strip.Children.Add(box);
        }
        wrap.Children.Add(strip);
        for (int i = 0; i < 4; i++)
        {
            panels[i].Visibility = i == sel ? Visibility.Visible : Visibility.Collapsed;
            PaintTab(txts[i], unders[i], i == sel);
            wrap.Children.Add(panels[i]);
        }
        return wrap;
    }

    void PaintTab(TextBlock t, Border underline, bool active)
    {
        t.Foreground = active ? Fg : Muted;
        t.FontWeight = active ? FontWeights.SemiBold : FontWeights.Normal;
        underline.Background = active ? Theme.Br(Theme.Accent(_dark)) : Brushes.Transparent;
    }

    TextBlock SectLabel(string s)
    {
        return new TextBlock { Text = s, Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 11.5,
                               FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 10, 0, 4) };
    }

    // ── P1 ARTIFACT LINKS ───────────────────────────────────────────────────────
    // Detect Windows file/folder paths inside agent result text and make them clickable.
    // Matches: a drive-letter path (C:\... or C:/...) OR a UNC path (\\host\share\...),
    // with either separator, allowing Japanese and other non-space/non-quote characters in
    // segments, stopping at whitespace, quotes, or bracket delimiters. Trailing punctuation
    // (Japanese 。、 and ASCII . , ) ] ; :) is stripped by CleanPathTail after the match so a
    // path at the end of a sentence doesn't swallow the period. Parsing is capped by the caller
    // to the first ~4000 chars so a huge blob can't stall the UI thread.
    static readonly Regex _pathRe = new Regex(
        @"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s""'<>|]+[\\/])[^\s""'<>|(){}\[\]]+",
        RegexOptions.Compiled);
    const int PathScanCap = 4000;

    // Strip trailing sentence punctuation the regex may have greedily included.
    static string CleanPathTail(string p)
    {
        if (string.IsNullOrEmpty(p)) return p;
        int end = p.Length;
        while (end > 0)
        {
            char c = p[end - 1];
            if (c == '。' || c == '、' || c == '.' || c == ',' || c == ')' || c == ']'
                || c == ';' || c == ':' || c == '!' || c == '?' || c == '"' || c == '\'')
                end--;
            else break;
        }
        return p.Substring(0, end);
    }

    // Reveal a detected path: file -> explorer /select (highlight in its folder); directory ->
    // open the folder; neither exists -> no-op (the run is rendered non-interactive with a tooltip).
    static bool RevealPath(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                System.Diagnostics.Process.Start("explorer.exe", "/select,\"" + path + "\"");
                return true;
            }
            else if (Directory.Exists(path))
            {
                System.Diagnostics.Process.Start("explorer.exe", "\"" + path + "\"");
                return true;
            }
        }
        catch (Exception) { }
        return false;
    }

    // Distinct EXISTING paths (file or folder) found in `text`, in first-seen order, capped scan.
    List<string> DetectExistingPaths(string text)
    {
        var outp = new List<string>();
        if (string.IsNullOrEmpty(text)) return outp;
        string scan = text.Length > PathScanCap ? text.Substring(0, PathScanCap) : text;
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (Match m in _pathRe.Matches(scan))
        {
            string p = CleanPathTail(m.Value);
            if (p.Length < 4) continue;
            bool exists;
            try { exists = File.Exists(p) || Directory.Exists(p); } catch (Exception) { exists = false; }
            if (!exists) continue;
            if (seen.Add(p)) outp.Add(p);
        }
        return outp;
    }

    // A hyperlink-styled run for one path. Clickable when the path exists (reveal in folder / open
    // folder); disabled + "見つかりません" tooltip when it does not.
    Inline MakePathRun(string rawPath)
    {
        string path = CleanPathTail(rawPath);
        bool exists;
        try { exists = File.Exists(path) || Directory.Exists(path); } catch (Exception) { exists = false; }
        if (!exists)
        {
            var dead = new Run(rawPath);
            dead.Foreground = Muted;
            dead.ToolTip = T("path_missing");
            return dead;
        }
        var link = new Hyperlink(new Run(rawPath));
        link.Foreground = Theme.Br(Theme.Info(_dark));   // link color from tokens
        link.Cursor = Cursors.Hand;
        link.ToolTip = path;
        string captured = path;
        link.Click += delegate { RevealPath(captured); };
        return link;
    }

    // Result text as an inline-capable, wrapping, selectable block with any detected file/folder
    // paths rendered as clickable links. Falls back to the plain RoText TextBox when the text has
    // no detectable path, so wrapping / em-height stay identical to the old rendering. Uses the
    // SAME MaxHeight / scroll affordance so tall results still scroll rather than push the card.
    UIElement ResultText(string s, Brush fg, double size)
    {
        var paths = DetectPathSpans(s);
        if (paths.Count == 0) return RoText(s, fg, size);

        var rtb = new RichTextBox {
            IsReadOnly = true, IsDocumentEnabled = true, BorderThickness = new Thickness(0),
            Background = Brushes.Transparent, Padding = new Thickness(0), Foreground = fg,
            FontSize = size, IsTabStop = false, MaxHeight = 160,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        var para = new Paragraph { Margin = new Thickness(0), LineHeight = size * 1.35, Foreground = fg };
        int cur = 0;
        foreach (var span in paths)
        {
            if (span.Key > cur) para.Inlines.Add(new Run(s.Substring(cur, span.Key - cur)));
            string raw = s.Substring(span.Key, span.Value - span.Key);
            para.Inlines.Add(MakePathRun(raw));
            cur = span.Value;
        }
        if (cur < s.Length) para.Inlines.Add(new Run(s.Substring(cur)));
        rtb.Document = new FlowDocument(para) { PagePadding = new Thickness(0), Foreground = fg };
        // Same as SwallowMouseUp (which is TextBox-typed): stop the card's row click from firing when
        // the user selects text or clicks inside the result body.
        rtb.AddHandler(UIElement.MouseLeftButtonUpEvent,
            new MouseButtonEventHandler(delegate (object snd, MouseButtonEventArgs ev) { ev.Handled = true; }), true);
        return rtb;
    }

    // Character spans (start,end) of raw path substrings in the FULL text (only within the scan cap
    // so cost is bounded). Returns raw match spans (trailing punctuation NOT trimmed here so the
    // surrounding plain text keeps that punctuation); MakePathRun trims it for the click target.
    List<KeyValuePair<int, int>> DetectPathSpans(string text)
    {
        var outp = new List<KeyValuePair<int, int>>();
        if (string.IsNullOrEmpty(text)) return outp;
        int cap = Math.Min(text.Length, PathScanCap);
        foreach (Match m in _pathRe.Matches(text.Substring(0, cap)))
        {
            string cleaned = CleanPathTail(m.Value);
            if (cleaned.Length < 4) continue;
            // span covers only the cleaned path so trailing "。" etc. stays in the plain run
            outp.Add(new KeyValuePair<int, int>(m.Index, m.Index + cleaned.Length));
        }
        return outp;
    }

    // The compact 成果物 row on a COMPLETED card: up to 3 distinct existing files, each clickable
    // (reveal in folder), with "+N" when more were detected. No element when none found.
    UIElement ArtifactsRow(string resultText)
    {
        var files = new List<string>();
        foreach (string p in DetectExistingPaths(resultText))
        {
            bool isFile;
            try { isFile = File.Exists(p); } catch (Exception) { isFile = false; }
            if (isFile) files.Add(p);
        }
        if (files.Count == 0) return null;

        var sp = new StackPanel();
        sp.Children.Add(SectLabel(T("artifacts")));
        var wrap = new WrapPanel();
        int shown = Math.Min(3, files.Count);
        for (int i = 0; i < shown; i++)
        {
            string full = files[i];
            var chip = new Border {
                BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
                CornerRadius = new CornerRadius(6), Padding = new Thickness(8, 2, 8, 2),
                Margin = new Thickness(0, 0, 6, 4), Cursor = Cursors.Hand };
            var tb = new TextBlock {
                Text = Path.GetFileName(full), Foreground = Theme.Br(Theme.Info(_dark)),
                FontSize = 12, TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 240 };
            tb.ToolTip = full;
            chip.Child = tb;
            string captured = full;
            chip.MouseLeftButtonUp += delegate (object o, MouseButtonEventArgs ev) { ev.Handled = true; RevealPath(captured); };
            wrap.Children.Add(chip);
        }
        if (files.Count > shown)
            wrap.Children.Add(new TextBlock {
                Text = "+" + (files.Count - shown), Foreground = Muted, FontSize = 12,
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 0, 4) });
        sp.Children.Add(wrap);
        return sp;
    }

    TextBox RoText(string s, Brush fg, double size)
    {
        var t = new TextBox { Text = s, Foreground = fg, FontSize = size, IsReadOnly = true,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent, Padding = new Thickness(0),
            IsTabStop = false, TextWrapping = TextWrapping.Wrap, MaxHeight = 160,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        SwallowMouseUp(t);
        return t;
    }

    UIElement TabOverview(string goal, string last, string outcome, bool terminal, int reviews,
                          bool verifiedOk, string tpath, Dictionary<string, object> w)
    {
        var sp = new StackPanel();
        sp.Children.Add(SectLabel(_lang == 0 ? "結果" : "Result"));
        // Precedence: display_result (cleaned final answer from runner) > last > OutcomeLabel fallback.
        string displayResult = (w != null) ? S(w, "display_result") : "";
        string result = WorkerResultText(w, terminal);
        // P1: render the result with clickable file/folder paths (falls back to plain text when none).
        sp.Children.Add(ResultText(result, Fg, 13));

        // P1: compact 成果物 row on COMPLETED cards, listing the distinct existing files detected.
        if (terminal)
        {
            UIElement artRow = ArtifactsRow(result);
            if (artRow != null) sp.Children.Add(artRow);
        }

        var checks = new List<string>();
        // Prefer display_result evidence, then last, for the "final answer obtained" check.
        bool hasFinalAnswer = !string.IsNullOrEmpty(displayResult) || !string.IsNullOrEmpty(last);
        if (hasFinalAnswer)
            checks.Add(_lang == 0 ? "最終応答を取得" : "Final response obtained");
        int verifyAttempts = (w != null) ? I(w, "verify_attempts") : reviews;
        if (verifyAttempts > 0)
            checks.Add(_lang == 0 ? ("レビュー " + verifyAttempts + " 回実施") : ("Reviewed " + verifyAttempts + " time(s)"));
        if (verifiedOk)
            checks.Add(_lang == 0 ? "レビューで最終応答を確認 / Verified by review" : "Verified by review");
        // If terminal and no other evidence at all, fall back to the outcome label (e.g. CANCELLED).
        if (terminal && checks.Count == 0)
            checks.Add(OutcomeLabel(outcome));
        if (checks.Count > 0)
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "チェック" : "Checks"));
            foreach (var c in checks)
                sp.Children.Add(new TextBlock { Text = "・" + c, Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 1, 0, 1) });
        }

        // ── Timeline section ──────────────────────────────────────────────────────
        // Prefer phase_events (same source as Evidence Spine) when available; fall back to transcript.
        sp.Children.Add(SectLabel(_lang == 0 ? "タイムライン" : "Timeline"));
        var tsEvents = BuildTimelineEvents(tpath, outcome, terminal, reviews, w);
        foreach (string ev in tsEvents)
            sp.Children.Add(new TextBlock {
                Text = "・" + ev, Foreground = Muted, FontSize = 12,
                Margin = new Thickness(0, 1, 0, 1), TextWrapping = TextWrapping.Wrap });

        sp.Children.Add(SectLabel(_lang == 0 ? "指示" : "Goal"));
        sp.Children.Add(RoText(goal, Muted, 12.5));
        return sp;
    }

    // Build the ordered event list for the Timeline section.
    // Prefers phase_events from the worker dict (same source as Evidence Spine) when present.
    // Falls back to transcript-derived timestamps when phase_events is absent.
    List<string> BuildTimelineEvents(string tpath, string outcome, bool terminal, int reviews,
                                     Dictionary<string, object> w)
    {
        bool ja = _lang == 0;
        Func<double, string> fmtTs = delegate(double ts)
        {
            if (ts <= 0) return "";
            try
            {
                var dt = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime();
                return dt.ToString("HH:mm") + " ";
            }
            catch { return ""; }
        };

        // ── REAL mode: phase_events present on this worker ─────────────────────────
        if (w != null)
        {
            object peRaw;
            if (w.TryGetValue("phase_events", out peRaw) && peRaw is object[])
            {
                object[] peArr = (object[])peRaw;
                if (peArr.Length > 0)
                {
                    var evs2 = new List<string>();
                    foreach (object peObj in peArr)
                    {
                        var pe = peObj as Dictionary<string, object>;
                        if (pe == null) continue;
                        double peTs = 0;
                        object peTsRaw;
                        if (pe.TryGetValue("ts", out peTsRaw) && peTsRaw != null)
                        { try { peTs = Convert.ToDouble(peTsRaw); } catch { } }
                        string peEvent = "";
                        object peEvRaw;
                        if (pe.TryGetValue("event", out peEvRaw) && peEvRaw != null)
                            peEvent = peEvRaw.ToString();
                        // Use same label vocab as the Spine (Theme.StatusLabel)
                        string localLabel = Theme.StatusLabel(peEvent, _lang);
                        if (string.IsNullOrEmpty(localLabel) || localLabel == peEvent)
                        {
                            object peLblRaw;
                            if (pe.TryGetValue("label", out peLblRaw) && peLblRaw != null
                                && !string.IsNullOrEmpty(peLblRaw.ToString()))
                                localLabel = peLblRaw.ToString();
                        }
                        if (string.IsNullOrEmpty(localLabel)) localLabel = peEvent;
                        string timePrefix = peTs > 0 ? fmtTs(peTs) : "";
                        evs2.Add(timePrefix + localLabel);
                    }
                    if (evs2.Count > 0) return evs2;
                }
            }
        }

        // ── COMPUTED fallback: derive timestamps from transcript ──────────────────
        double metaTs = 0, firstTurnTs = 0;
        bool hasTs = false;
        try
        {
            if (!string.IsNullOrEmpty(tpath) && File.Exists(tpath))
            {
                string[] lines;
                using (var fsr = new FileStream(tpath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fsr, Encoding.UTF8))
                    lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
                foreach (var ln in lines)
                {
                    if (string.IsNullOrEmpty(ln)) continue;
                    Dictionary<string, object> obj;
                    try { obj = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                    if (obj == null) continue;
                    if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                    {
                        if (obj.ContainsKey("ts") && obj["ts"] != null)
                        { metaTs = Convert.ToDouble(obj["ts"]); hasTs = true; }
                        continue;
                    }
                    if (obj.ContainsKey("role") && obj.ContainsKey("ts") && obj["ts"] != null)
                    {
                        if (firstTurnTs == 0)
                            firstTurnTs = Convert.ToDouble(obj["ts"]);
                        if (firstTurnTs > 0) break;
                    }
                }
            }
        }
        catch { }

        var evs = new List<string>();
        string queuedTs = hasTs ? fmtTs(metaTs) : "";
        evs.Add(queuedTs + (ja ? "投入" : "Queued"));
        string startTs = (firstTurnTs > 0) ? fmtTs(firstTurnTs) : "";
        evs.Add(startTs + (ja ? "開始" : "Started"));
        if (reviews > 0)
            evs.Add(ja ? ("レビュー (" + reviews + "x)") : ("Reviewed (" + reviews + "x)"));
        if (terminal)
        {
            string outcomeEv;
            switch (outcome)
            {
                case "DONE":      outcomeEv = ja ? "完了" : "Completed"; break;
                case "MAXTURNS":  outcomeEv = ja ? "ターン上限" : "Max turns reached"; break;
                case "STUCK":     outcomeEv = ja ? "停滞" : "Stuck"; break;
                case "ERROR":     outcomeEv = ja ? "エラー" : "Error"; break;
                case "CANCELLED": outcomeEv = ja ? "停止" : "Cancelled"; break;
                default:          outcomeEv = string.IsNullOrEmpty(outcome) ? (ja ? "終了" : "Ended") : outcome; break;
            }
            evs.Add(outcomeEv);
        }
        return evs;
    }

    // Read the "ts" of the first turn in the transcript (meta or first role line) to compute
    // per-card elapsed. Returns 0 if no transcript or no ts field found.
    double ReadTranscriptStartTs(string tpath)
    {
        try
        {
            if (string.IsNullOrEmpty(tpath) || !File.Exists(tpath)) return 0;
            string[] lines;
            using (var fsr = new FileStream(tpath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (var ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> obj;
                try { obj = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                if (obj == null) continue;
                // meta line has "ts" -- this is the best proxy for "worker was queued"
                if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                {
                    if (obj.ContainsKey("ts") && obj["ts"] != null)
                        return Convert.ToDouble(obj["ts"]);
                }
            }
        }
        catch { }
        return 0;
    }

    UIElement TabConversation(string tpath)
    {
        if (!string.IsNullOrEmpty(tpath))
        {
            var mini = MiniThread(tpath);
            if (mini != null) return mini;
        }
        return new TextBlock { Text = _lang == 0 ? "（会話の履歴はまだありません）" : "(No conversation yet)",
                               Foreground = Muted, FontSize = 12.5 };
    }

    UIElement TabReview(string reason, bool done, bool terminal, int reviews)
    {
        var sp = new StackPanel();
        if (done)
        {
            // DONE: never surface raw "REFUTED" -- frame review as the check that it was.
            sp.Children.Add(new TextBlock {
                Text = _lang == 0 ? "レビューで内容を確認し、最終回答を確定しました。"
                                  : "Review checked the work and the final answer was confirmed.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap });
            if (reviews > 0)
                sp.Children.Add(new TextBlock {
                    Text = (_lang == 0 ? "確認回数: " : "Review passes: ") + reviews,
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12, Margin = new Thickness(0, 4, 0, 0) });
        }
        else if (terminal && !string.IsNullOrEmpty(reason))
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "レビュー指摘" : "Review note"));
            sp.Children.Add(RoText(reason, Muted, 12.5));
        }
        else
        {
            sp.Children.Add(new TextBlock {
                Text = _lang == 0 ? "（レビュー記録はまだありません）" : "(No review notes yet)",
                Foreground = Muted, FontSize = 12.5 });
        }
        return sp;
    }

    UIElement TabLogs(Dictionary<string, object> w, string reason)
    {
        var sb = new StringBuilder();
        sb.Append("status=").Append(S(w, "status")).Append("  outcome=").Append(S(w, "outcome"));
        sb.Append("  turn=").Append(S(w, "turn")).Append("  verify_attempts=").Append(S(w, "verify_attempts"));
        sb.Append("  verified=").Append(S(w, "verified")).Append('\n');
        if (!string.IsNullOrEmpty(reason)) sb.Append("\nreason:\n").Append(reason).Append('\n');
        var box = new Border { Background = QuoteBg, CornerRadius = new CornerRadius(8), Padding = new Thickness(12, 10, 12, 10) };
        var t = RoText(sb.ToString(), Muted, 12);
        t.FontFamily = new FontFamily(Theme.CodeFont);
        t.MaxHeight = 220;
        box.Child = t;
        return box;
    }

    string OutcomeLabel(string outcome)
    {
        bool ja = _lang == 0;
        switch (outcome)
        {
            case "DONE": return ja ? "完了" : "Done";
            case "MAXTURNS": return ja ? "ターン上限に到達" : "Hit turn limit";
            case "STUCK": return ja ? "停滞して終了" : "Stuck";
            case "ERROR": return ja ? "エラーで終了" : "Error";
            case "CANCELLED": return ja ? "停止されました" : "Cancelled";
            default: return string.IsNullOrEmpty(outcome) ? "" : outcome;
        }
    }

    // #14 mini-chat: the last few turns of a worker's disk transcript, as a compact scrollable
    // thread inside the EXPANDED card -- read recent context + steer from the cockpit without
    // switching to the chat window. Null when the transcript is empty/missing. Only expanded cards
    // call this, so the I/O is bounded to the one or two cards the user opened.
    UIElement MiniThread(string transcriptPath)
    {
        var turns = ReadLastTurns(transcriptPath, 4);
        if (turns.Count == 0) return null;
        var panel = new StackPanel();
        foreach (var t in turns)
        {
            bool user = t.Item1 == "U";
            var b = new Border { Background = QuoteBg, CornerRadius = new CornerRadius(6),
                                 Padding = new Thickness(10, 7, 10, 7), Margin = new Thickness(0, 0, 0, 5) };
            var sp = new StackPanel();
            var who = new TextBlock { Text = user ? (_lang == 0 ? "▶ 指示 / あなた" : "▶ Instruction / You") : (_lang == 0 ? "● エージェント" : "● Agent"), FontSize = 11,
                                      FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 3) };
            who.Foreground = user ? Accent : Muted;
            sp.Children.Add(who);
            var tb = new TextBox { Text = t.Item2, FontSize = 12, IsReadOnly = true,
                                   BorderThickness = new Thickness(0), Background = Brushes.Transparent,
                                   Padding = new Thickness(0), IsTabStop = false, TextWrapping = TextWrapping.Wrap,
                                   MaxHeight = 90, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            tb.Foreground = Fg;
            SwallowMouseUp(tb);
            sp.Children.Add(tb);
            b.Child = sp;
            panel.Children.Add(b);
        }
        return new ScrollViewer { MaxHeight = 240, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                                  Content = panel, Margin = new Thickness(0, 6, 0, 6) };
    }

    // Last `n` (role, text) turns from a jsonl transcript (skips meta / guid marker lines).
    List<Tuple<string, string>> ReadLastTurns(string path, int n)
    {
        var all = new List<Tuple<string, string>>();
        try
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) return all;
            string[] lines;
            using (var fsr = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (var ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> o;
                try { o = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                if (o == null || !o.ContainsKey("role")) continue;
                string role = o["role"] != null ? o["role"].ToString() : "assistant";
                string text = (o.ContainsKey("text") && o["text"] != null) ? o["text"].ToString() : "";
                all.Add(Tuple.Create(role.StartsWith("user") ? "U" : "A", text));
            }
        }
        catch { }
        if (all.Count > n) all = all.GetRange(all.Count - n, n);
        return all;
    }

    // Feature 1: slim always-visible steer affordance on a COLLAPSED non-terminal card -- mid-run
    // steering used to require expanding the card first. One line: a borderless underline TextBox
    // + a small "↳" send glyph. Reuses TrySteerSend (see above) for the actual command write.
    //
    // Keystroke-safety (the reason this is more than a two-line TextBox): a LIVE worker's `last`/
    // `turn`/etc. stream every ~700ms sweep, which changes this card's RowSig and makes Card(w)
    // rebuild the WHOLE card -- including this box -- independent of whether the user is mid-type.
    // Three things make that survivable:
    //   1. Text is seeded from _steerDraft[name] on every build, and TextChanged writes back to
    //      it -- so a torn-down-and-rebuilt box never starts empty.
    //   2. _steerBoxRef[name] is updated to the newest TextBox instance on every build, and
    //      RenderCards' post-SetRows Dispatcher pass (Loaded priority, mirroring the existing
    //      scroll-restore pass right above it) re-Focus()es + re-seats the caret on whichever
    //      instance is current, so a rebuild mid-type does not drop focus either.
    //   3. GotFocus/LostFocus maintain _steerFocusWorker so that pass knows there IS a focus to
    //      restore, and to WHICH worker. LostFocus defers its "did focus really leave" check to
    //      ContextIdle -- strictly after the Loaded-priority restore -- so a teardown-triggered
    //      LostFocus on the OLD box (which fires before the new box is focused) can't race ahead
    //      and clear the tracked worker before the restore runs.
    UIElement CollapsedSteerRow(string name)
    {
        var outer = new DockPanel();
        outer.Margin = new Thickness(24, 5, 0, 0);
        // must not bubble to the card's whole-row open-conversation click handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var send = new Button();
        send.Content = "↳";   // "↳" -- icon-only so the row stays one line
        send.ToolTip = _lang == 0 ? "送信" : "Send";
        send.Background = Brushes.Transparent; send.Foreground = Muted;
        send.BorderThickness = new Thickness(0); send.Cursor = Cursors.Hand; send.FontSize = 13;
        send.Padding = new Thickness(6, 0, 2, 0);
        DockPanel.SetDock(send, Dock.Right);
        outer.Children.Add(send);

        string nm = name;
        // input + placeholder watermark overlay (same idea as SteerRow's, compacted into one line)
        var inputGrid = new Grid();
        var tb = new TextBox();
        SwallowMouseUp(tb);   // selecting/typing must not trigger the card's open-conversation click
        tb.FontSize = 12; tb.Padding = new Thickness(4, 2, 4, 2);
        tb.BorderThickness = new Thickness(0, 0, 0, 1); tb.BorderBrush = Border;
        tb.Background = Brushes.Transparent; tb.Foreground = Fg; tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "回答待ち中でも割り込み指示を送れます（次のターンに最優先で反映）"
                                : "Inject a steering instruction (applied on the next turn)";
        string draft;
        tb.Text = _steerDraft.TryGetValue(nm, out draft) ? draft : "";
        _steerBoxRef[nm] = tb;   // newest realized instance for this worker (used by the focus-restore pass)

        var placeholder = new TextBlock();
        placeholder.Text = T("steer_collapsed_placeholder");
        placeholder.Foreground = Muted; placeholder.FontSize = 12;
        placeholder.Padding = new Thickness(4, 2, 4, 2);
        placeholder.IsHitTestVisible = false;
        placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);

        Func<bool> trySend = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) ShowScaleToast(failReason);
                return false;
            }
            tb.Text = "";
            _steerDraft.Remove(nm);
            ShowScaleToast(T("steer_collapsed_ack"));
            return true;
        };
        send.Click += delegate { trySend(); };
        tb.KeyDown += delegate (object s2, KeyEventArgs e2)
        {
            if (e2.Key == Key.Return) { trySend(); e2.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            _steerDraft[nm] = tb.Text;
            placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        };
        tb.GotFocus += delegate { _steerFocusWorker = nm; };
        tb.LostFocus += delegate
        {
            // Defer the "did focus really move away" check to ContextIdle -- strictly lower
            // priority than the Loaded-priority restore pass in RenderCards, so if this LostFocus
            // fired only because the container was rebuilt out from under the user (not because
            // they clicked elsewhere), the restore pass has already re-focused the NEW box by the
            // time this runs, and stillOnSteer below comes back true (no-op).
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != nm) return;
                TextBox cur = Keyboard.FocusedElement as TextBox;
                TextBox tracked;
                bool stillOnSteer = cur != null && _steerBoxRef.TryGetValue(nm, out tracked) && cur == tracked;
                if (!stillOnSteer) _steerFocusWorker = null;
            }), System.Windows.Threading.DispatcherPriority.ContextIdle);
        };
        outer.Children.Add(inputGrid);   // last child + LastChildFill -> fills remaining width
        return outer;
    }

    // ② steering: inject a mid-task instruction into this worker's conversation.
    // Task 6: restyled as a small composer -- rounded surfaceSubtle border, placeholder watermark,
    // neutral send button, post-send wording updated.
    UIElement SteerRow(string name)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 10, 0, 0);
        // clicks inside this row must not bubble to the card's open-conversation handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        // mini-composer wrapper border (matches bottom composer look, smaller)
        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(8);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(10, 8, 10, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "送信" : "Send";
        // Task 6: neutral by default (not accent)
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        // honest feedback when there is no live fleet to consume the steer
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        // input + placeholder watermark overlay
        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "回答待ち中でも割り込み指示を送れます（次のターンに最優先で反映）"
                                : "Inject a steering instruction (applied on the next turn)";
        // placeholder watermark text (hides when text is present)
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "このタスクに追加指示..." : "Add instruction to this task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string nm = name;
        // returns true iff the steer was actually sent; keeps the text + shows a note otherwise.
        // Reuses TrySteerSend (the ONE steer-send code path, shared with CollapsedSteerRow).
        Func<bool> trySteer = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) note.Text = failReason;
                return false;
            }
            tb.Text = "";
            // Task 6: updated post-send wording
            note.Text = _lang == 0 ? "次のターンに送信しました" : "Queued for the next turn";
            return true;
        };
        send.Click += delegate { trySteer(); };
        tb.KeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { trySteer(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }

    // "続ける" (Continue) inline composer for a FINISHED (done) card -- the SAME mini-composer UI as
    // SteerRow (割り込み). A done worker is terminal and can't take a steer, so instead of injecting
    // into a live worker this launches a FRESH continuation: BuildContinueGoal prepends the prior
    // goal + "re-read your on-disk outputs" so the agent recovers context, then runs the follow-up.
    // Written as ONE JSONL line by SpawnFleet's GoalsToJsonl (the prefix is multi-line; a plain
    // multi-line string would otherwise be split one bogus goal per source line).
    // No RunIsLive gate -- a continuation starts its own run.
    UIElement ContinueRow(string name, string goal, string conv)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 10, 0, 0);
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(8);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(10, 8, 10, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "続ける" : "Continue";
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "この完了タスクに追加指示（前回の成果物を読み直してから実行）"
                                : "Follow-up to this finished task (re-reads its saved outputs first)";
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "完了タスクに続けて指示..." : "Continue this finished task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string g = goal, c = conv;
        Func<bool> tryContinue = delegate
        {
            string t = (tb.Text ?? "").Trim();
            if (t.Length == 0) return false;
            string goalText = BuildContinueGoal(g, t);
            // SpawnFleet's GoalsToJsonl escapes a plain multi-line goal string safely on its
            // own; only pre-serialize here to carry the EXTRA resume_conv key (GoalsToJsonl
            // detects an already-JSON goal string and passes it through as-is).
            var glist = new List<string>();
            if (!string.IsNullOrEmpty(c))
            {
                var gd = new Dictionary<string, object>();
                gd["text"] = goalText;
                gd["resume_conv"] = c;
                glist.Add(_js.Serialize(gd));
            }
            else
            {
                glist.Add(goalText);
            }
            SpawnFleet(glist, "continue_input.txt");
            tb.Text = "";
            note.Text = _lang == 0 ? "続きを開始しました" : "Continuation started";
            return true;
        };
        send.Click += delegate { tryContinue(); };
        tb.KeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { tryContinue(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }

    // Feature A: a read-only TextBox inside a clickable card would still raise the card's
    // MouseLeftButtonUp -> open-conversation, so dragging to select text would yank you into
    // the chat. Swallow the mouse-up at the TextBox (handledEventsToo) so selection works and
    // the card click never fires.
    void SwallowMouseUp(TextBox tb)
    {
        tb.AddHandler(UIElement.MouseLeftButtonUpEvent,
            new MouseButtonEventHandler(delegate(object s, MouseButtonEventArgs e) { e.Handled = true; }),
            true);
    }

    // Feature C: per-card "Retry" on a terminal non-DONE worker. Re-queues this exact goal
    // (with its acceptance checks + cwd) at priority via add_goal. Clicks must not bubble to
    // the card's open-conversation handler.
    UIElement RetryRow(Dictionary<string, object> w)
    {
        var dp = new DockPanel();
        dp.Margin = new Thickness(0, 10, 0, 0);
        dp.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted;
        note.VerticalAlignment = VerticalAlignment.Center;
        note.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(note);
        var btn = new Button();
        btn.Content = T("retry");
        btn.Background = Accent; btn.Foreground = White; btn.BorderThickness = new Thickness(0);
        btn.Padding = new Thickness(12, 4, 12, 4); btn.Cursor = Cursors.Hand; btn.FontSize = 12;
        btn.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(btn, Dock.Right);
        Dictionary<string, object> wkr = w;
        btn.Click += delegate
        {
            RetryGoal(wkr);
            note.Text = RunIsLive() ? "" : T("retry_note");
        };
        dp.Children.Add(btn);
        return dp;
    }

    // Build one add_goal entry { text, checks, cwd, priority } for a worker dict. checks is
    // passed through as-is from the worker (whatever the fleet stored -- a list).
    Dictionary<string, object> RetryEntry(Dictionary<string, object> w)
    {
        var item = new Dictionary<string, object>();
        item["text"] = S(w, "goal");
        object checks = null;
        if (w.ContainsKey("checks")) checks = w["checks"];
        item["checks"] = checks;
        item["cwd"] = S(w, "cwd");
        item["priority"] = true;
        return item;
    }

    // Re-run the worker's goal. TWO honest paths, picked by whether a run is LIVE:
    //  * LIVE  -> append to commands.json's add_goal list (MERGE writer); the running fleet
    //            consumes it on its next sweep and re-runs it WITH its acceptance gate (checks+cwd).
    //  * FINISHED/stale -> nothing alive would ever drain add_goal, so instead SPAWN a fresh fleet
    //            for this goal text (SpawnFleet). The relaunched run picks it up and re-runs it.
    // The auto-retry scanner only calls this while live, so its add_goal path is unchanged.
    void RetryGoal(Dictionary<string, object> w)
    {
        if (RunIsLive())
        {
            var cmd = ReadCommands();
            var adds = new List<object>();
            if (cmd.ContainsKey("add_goal") && cmd["add_goal"] is object[])
                foreach (object o in (object[])cmd["add_goal"]) adds.Add(o);
            adds.Add(RetryEntry(w));
            cmd["add_goal"] = adds;
            WriteCommands(cmd);
            return;
        }
        string goal = S(w, "goal");
        if (string.IsNullOrEmpty(goal)) return;
        try { SpawnFleet(new List<string> { goal }, "retry_input.txt"); _lastSig = ""; } catch (Exception) { }
    }

    // Feature C bulk: re-run EVERY currently-shown terminal non-DONE worker (respecting the active
    // filter). LIVE -> one merged add_goal list; FINISHED/stale -> ONE relaunched fleet carrying
    // all the retried goal texts (mirrors RetryGoal's live/finished split).
    void RetryAllShown(List<Dictionary<string, object>> shown)
    {
        bool live = RunIsLive();
        var cmd = ReadCommands();
        var adds = new List<object>();
        if (cmd.ContainsKey("add_goal") && cmd["add_goal"] is object[])
            foreach (object o in (object[])cmd["add_goal"]) adds.Add(o);
        var goalTexts = new List<string>();
        int n = 0;
        foreach (Dictionary<string, object> w in shown)
        {
            if (!IsTerminalWorker(w)) continue;
            if (S(w, "outcome") == "DONE") continue;
            adds.Add(RetryEntry(w));
            string g = S(w, "goal");
            if (!string.IsNullOrEmpty(g)) goalTexts.Add(g);
            n++;
        }
        if (n == 0) return;
        if (live)
        {
            cmd["add_goal"] = adds;
            WriteCommands(cmd);
        }
        else if (goalTexts.Count > 0)
        {
            try { SpawnFleet(goalTexts, "retry_input.txt"); _lastSig = ""; } catch (Exception) { }
        }
    }

    static bool IsTerminalWorker(Dictionary<string, object> w)
    {
        string status = S(w, "status");
        return status == "done" || status == "stuck" || status == "maxturns"
               || status == "error" || status == "cancelled";
    }

    // Severity rank for the "unfinished only" sort: failures first, then max-turns, then
    // cancelled, then still-running/other (stable within a rank).
    static int SeverityRank(Dictionary<string, object> w)
    {
        string oc = S(w, "outcome");
        if (oc == "STUCK" || oc == "ERROR") return 0;
        if (oc == "MAXTURNS") return 1;
        if (oc == "CANCELLED") return 2;
        return 3;   // still-running / other
    }

    UIElement MakeReleaseContent()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.Children.Add(MakeIcon("close", 13, Fg));
        var t = new TextBlock(); t.Text = "  " + T("release");
        t.FontSize = 12; t.Foreground = Fg; t.VerticalAlignment = VerticalAlignment.Center;
        sp.Children.Add(t);
        return sp;
    }

    // Status chip (spec): a small OUTLINED pill -- colored border + colored text on a transparent
    // fill, never a saturated block. `railKind` is one of neutral/info/success/warning/danger.
    // P0 AGENT BADGE (feature 4): show which agent a worker's conversation is bound to, read
    // from its conv_url in status.json.
    //   • conv_url carries "/agent/<id>" AND <id> matches the configured agent -> subtle Muted
    //     badge with the agent's short name (12-char title, else "agent").
    //   • conv_url carries "/agent/<otherid>" -> subtle badge (still an agent, just not ours).
    //   • conv_url is a plain "/chat/" url with NO "/agent/" segment -> WARNING 既定Copilot badge
    //     (a default-Copilot conversation has no MCP connector — the incident case).
    //   • no conv_url yet (worker just started) -> null (no badge; don't accuse prematurely).
    Border BuildAgentBadge(string convUrl, string convTitle)
    {
        if (string.IsNullOrEmpty(convUrl)) return null;
        string lo = convUrl.ToLowerInvariant();
        bool hasAgentSeg = lo.Contains("/agent/");
        if (hasAgentSeg)
        {
            // Agent-bound conversation. Prefer a short name from the title; fall back to "agent".
            string shortName = "agent";
            if (!string.IsNullOrEmpty(convTitle))
            {
                string tt = OneLine(convTitle);
                shortName = tt.Length > 12 ? tt.Substring(0, 12) : tt;
            }
            bool mine = !string.IsNullOrEmpty(_agentMarkerId)
                        && lo.Contains(_agentMarkerId.ToLowerInvariant());
            // Subtle (Muted) badge either way; the configured-agent case is the expected/quiet state.
            return AgentBadge(shortName, mine ? "neutral" : "neutral");
        }
        // A conversation URL that is a plain /chat/ (no /agent/ segment) — default Copilot.
        if (lo.Contains("/chat/") || lo.Contains("/conversation/"))
            return AgentBadge(T("badge_default_copilot"), "warning");
        return null;
    }

    // Small outlined badge. kind: "neutral" (subtle) | "warning" (default-Copilot alert).
    Border AgentBadge(string text, string kind)
    {
        Brush color = kind == "warning" ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Muted(_dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        b.CornerRadius = new CornerRadius(4);
        b.Padding = new Thickness(5, 0, 5, 0);
        b.Margin = new Thickness(0, 0, 5, 0);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 10.5; t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        b.Child = t;
        b.ToolTip = kind == "warning"
            ? (_lang == 0 ? "既定Copilotの会話（MCPコネクタ無し）。エージェントに接続し直してください。"
                          : "Default-Copilot conversation (no MCP connector). Reconnect to the agent.")
            : (_lang == 0 ? "この会話はエージェントに接続されています" : "This conversation is bound to the agent");
        return b;
    }

    Border Pill(string text, string railKind)
    {
        var color = Theme.Br(Theme.RailColor(railKind, _dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        // Tight rounded-rect tag (not a CornerRadius=999 stadium oval). The oval shape left
        // empty space inside the frame on each side of a short label like "完了", which read as a
        // floating "枠"/gap between the chip and the goal text. A small radius hugs the label.
        b.CornerRadius = new CornerRadius(4);
        b.Padding = new Thickness(6, 1, 6, 1);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }

    // Feature 2: Pill() itself carries no external spacing (callers position it inside a DockPanel/
    // StackPanel that already spaces siblings). A WrapPanel does not, so give each header chip a
    // small right/bottom margin -- small helper instead of repeating the Thickness at every call site.
    Border ChipMargin(Border pill)
    {
        pill.Margin = new Thickness(0, 0, 6, 4);
        return pill;
    }

    // Collapse newlines/tabs/runs of whitespace into single spaces -- for the one-line card result.
    static string OneLine(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length);
        bool lastSpace = false;
        foreach (char c in s)
        {
            char ch = (c == '\n' || c == '\r' || c == '\t') ? ' ' : c;
            if (ch == ' ') { if (lastSpace) continue; lastSpace = true; }
            else lastSpace = false;
            sb.Append(ch);
        }
        return sb.ToString().Trim();
    }

    // Task 3: clean agent result text for display only (never touch logs/transcript).
    // Normalizes line endings, trims lines, drops empty and preamble-only lines,
    // removes adjacent duplicate lines, and trims a trailing lone "DONE" if redundant.
    static readonly string[] _resultPreambleTokens = {
        "desktopfile操作", "browser操作", "computeruse", "Copilot", "エージェント"
    };
    static string CleanAgentResultForUi(string raw)
    {
        if (string.IsNullOrEmpty(raw)) return "";
        // Normalize CRLF/CR to LF, split, trim each line, drop empty.
        string normalized = raw.Replace("\r\n", "\n").Replace("\r", "\n");
        string[] parts = normalized.Split('\n');
        var lines = new List<string>();
        foreach (string p in parts)
        {
            string t = p.Trim();
            if (t.Length == 0) continue;
            // drop lines that are only a known preamble token (case-insensitive exact match)
            bool isPreamble = false;
            foreach (string tok in _resultPreambleTokens)
            {
                if (string.Equals(t, tok, StringComparison.OrdinalIgnoreCase))
                { isPreamble = true; break; }
            }
            if (isPreamble) continue;
            lines.Add(t);
        }
        // Remove repeated identical adjacent lines.
        var deduped = new List<string>();
        for (int i = 0; i < lines.Count; i++)
        {
            if (deduped.Count == 0 || lines[i] != deduped[deduped.Count - 1])
                deduped.Add(lines[i]);
        }
        // If the final line is exactly "DONE" and previous line already ends with "DONE", drop it.
        if (deduped.Count >= 2
            && deduped[deduped.Count - 1] == "DONE"
            && deduped[deduped.Count - 2].EndsWith("DONE"))
        {
            deduped.RemoveAt(deduped.Count - 1);
        }
        if (deduped.Count == 0) return "";
        var sb2 = new StringBuilder();
        for (int i = 0; i < deduped.Count; i++)
        {
            if (i > 0) sb2.Append('\n');
            sb2.Append(deduped[i]);
        }
        return sb2.ToString();
    }

    UIElement Dots()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.VerticalAlignment = VerticalAlignment.Center; sp.Margin = new Thickness(8, 0, 0, 0);
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse();
            dot.Width = 6; dot.Height = 6; dot.Fill = Muted; dot.Margin = new Thickness(2, 0, 0, 0);
            var anim = new DoubleAnimation(0.25, 1.0, new Duration(TimeSpan.FromMilliseconds(600)));
            anim.AutoReverse = true; anim.RepeatBehavior = RepeatBehavior.Forever;
            anim.BeginTime = TimeSpan.FromMilliseconds(i * 150);
            dot.BeginAnimation(UIElement.OpacityProperty, anim);
            sp.Children.Add(dot);
        }
        return sp;
    }

    // Disclosure chevron: '>' collapsed, 'v' expanded -- drawn as vector geometry to match the
    // Material-Symbols-as-paths aesthetic (the glyph subset has no chevron). Clicking toggles
    // this worker's detail and re-renders immediately. e.Handled stops the click from bubbling
    // to the card's open-conversation handler.
    UIElement ChevronToggle(string name, bool expanded)
    {
        var hit = new Border();
        hit.Background = Brushes.Transparent;            // whole padded area is the hit target
        hit.Padding = new Thickness(8, 8, 6, 8);
        hit.MinWidth = 24; hit.MinHeight = 28;           // ~24x28 px target -- tighter caret->chip, still easy to click
        hit.Cursor = Cursors.Hand;
        hit.VerticalAlignment = VerticalAlignment.Center;
        hit.HorizontalAlignment = HorizontalAlignment.Center;
        hit.ToolTip = _lang == 0
            ? (expanded ? "詳細を閉じる" : "詳細を開く（最新の進捗・割り込み）")
            : (expanded ? "Collapse details" : "Expand (latest progress + steer)");

        var path = new System.Windows.Shapes.Path();
        path.Data = Geometry.Parse("M 0,0 L 4,4 L 0,8");   // a '>' caret
        path.Stroke = Muted; path.StrokeThickness = 1.6;
        path.StrokeStartLineCap = PenLineCap.Round;
        path.StrokeEndLineCap = PenLineCap.Round;
        path.StrokeLineJoin = PenLineJoin.Round;
        path.Width = 7; path.Height = 11; path.Stretch = Stretch.None;
        path.VerticalAlignment = VerticalAlignment.Center;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        if (expanded)
        {
            path.RenderTransformOrigin = new Point(0.5, 0.5);
            path.RenderTransform = new RotateTransform(90);   // '>' -> 'v'
        }
        hit.Child = path;

        string nm = name;
        // Fire on button-DOWN so the chevron beats the ListBoxItem's selection handling, which
        // captures the mouse on DOWN and would otherwise swallow our UP. PreviewMouseLeftButtonDown
        // runs in the tunnel before the item sees it; e.Handled stops the open-conversation handler.
        hit.PreviewMouseLeftButtonDown += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
            ToggleExpand(nm);
        };
        // Also swallow the button-UP: the toggle drives on DOWN, but the card opens the conversation
        // on MouseLeftButtonUp -- a DOWN-only Handled does NOT stop the separate UP event, so the UP
        // would bubble to card.MouseLeftButtonUp -> OpenWorker and wrongly open main. Consume it here
        // (same pattern as openLink / SwallowMouseUp elsewhere in this file).
        hit.PreviewMouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
        };
        return hit;
    }

    // Flip one worker's detail and refresh the virtualizing list. With virtualization the old
    // "find the card in _cards.Children" trick no longer applies (only ~10-20 cards are realized),
    // so we rebuild the lightweight row models and reassign -- cheap, because only visible rows
    // realize. Scroll offset is preserved via ListScroller(), and _lastSig is re-synced to the
    // new expanded state so the next 700ms tick doesn't immediately trigger a redundant rebuild.
    void ToggleExpand(string name)
    {
        if (_expanded.Contains(name)) _expanded.Remove(name);
        else _expanded.Add(name);
        if (_lastRoot == null) { _lastSig = ""; OnTick(null, null); return; }

        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;
        SetRows(BuildRows(_lastRoot));
        if (off > 0.0)
        {
            double target = off;
            // Restore the (pixel-based) offset AFTER the VirtualizingStackPanel has re-measured the
            // full scroll extent. At Loaded priority the extent isn't final yet, so ScrollTo clamps
            // to ~0 -> the big upward jump when expanding a low card. Run at Background (after
            // layout), force a layout pass, then re-apply clamped to the scrollable range so the
            // toggled card stays put and its detail appears in place.
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 == null) return;
                if (_list != null) _list.UpdateLayout();
                double max = s2.ScrollableHeight;
                double t = (target < 0.0) ? 0.0 : (target > max ? max : target);
                s2.ScrollToVerticalOffset(t);
            }), System.Windows.Threading.DispatcherPriority.Background);
        }
        _lastSig = Sig(_lastRoot);
    }

    // ── cockpit -> fleet control channel ─────────────────────────────────────────
    // Merge into the pending command file so concurrent commands don't clobber each
    // other before the fleet (polling ~1s) consumes them.
    Dictionary<string, object> ReadCommands()
    {
        try
        {
            if (File.Exists(_commandsPath))
            {
                var ex = _js.DeserializeObject(File.ReadAllText(_commandsPath, Encoding.UTF8)) as Dictionary<string, object>;
                if (ex != null) return ex;
            }
        }
        catch (Exception) { }
        return new Dictionary<string, object>();
    }
    void WriteCommands(Dictionary<string, object> cmd)
    {
        try { File.WriteAllText(_commandsPath, _js.Serialize(cmd), new UTF8Encoding(false)); }
        catch (Exception) { }
    }

    void RequestClose(string name)
    {
        var cmd = ReadCommands();
        var closes = new List<object>();
        if (cmd.ContainsKey("close") && cmd["close"] is object[])
            foreach (object o in (object[])cmd["close"]) closes.Add(o);
        if (!closes.Contains(name)) closes.Add(name);
        cmd["close"] = closes;
        WriteCommands(cmd);
    }

    void RequestSetMaxtabs(int n)
    {
        var cmd = ReadCommands();
        cmd["set_maxtabs"] = n;
        WriteCommands(cmd);
    }

    // Live autoscale control: {"set_autoscale":{"on":0|1,"default":N,"max":M}}. Merged into
    // commands.json via the SAME writer RequestSetMaxtabs uses, so concurrent commands
    // (close/steer/set_maxtabs) aren't clobbered before the fleet (polling ~1s) consumes them.
    void RequestSetAutoscale(bool on, int def, int max)
    {
        var cmd = ReadCommands();
        var sa = new Dictionary<string, object>();
        sa["on"] = on ? 1 : 0;
        sa["default"] = def;
        sa["max"] = max;
        cmd["set_autoscale"] = sa;
        WriteCommands(cmd);
    }

    void RequestSteer(string name, string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        var cmd = ReadCommands();
        var steers = new List<object>();
        if (cmd.ContainsKey("steer") && cmd["steer"] is object[])
            foreach (object o in (object[])cmd["steer"]) steers.Add(o);
        var item = new Dictionary<string, object>();
        item["worker"] = name; item["text"] = text;
        steers.Add(item);
        cmd["steer"] = steers;
        WriteCommands(cmd);
    }

    // Feature 1: the ONE steer-send code path, shared by SteerRow (expanded drawer) and
    // CollapsedSteerRow (always-visible mini box on a collapsed non-terminal card). Neither
    // caller re-implements the RequestSteer/commands.json write -- both funnel through here.
    // Returns true iff the steer was actually queued; otherwise failReason carries the reason
    // text (steer_dead) so the caller can surface it however fits its layout (inline note vs toast).
    bool TrySteerSend(string name, string text, out string failReason)
    {
        failReason = null;
        string t = (text ?? "").Trim();
        if (t.Length == 0) return false;
        if (!RunIsLive()) { failReason = T("steer_dead"); return false; }
        RequestSteer(name, t);
        return true;
    }

    // FIX A: immediate LOCAL feedback on an open-conversation click. The main chat only notices
    // open.json on its own ~800ms poll, so without this the click reads as dead for up to 0.8s.
    // Briefly flips the clicked card's border/background to the accent colour and back; a
    // DispatcherTimer one-shot reverts it (mirrors the PaintPause optimistic-feedback pattern, and
    // the ShowScaleToast fade-timer style already used elsewhere in this file). Does NOT touch the
    // .fleet/open.json file-signal protocol -- purely a local visual cue.
    // Safe if `card` is later disposed/rebuilt mid-flash: it's just a property set on a UIElement
    // that may no longer be in the visual tree, which WPF tolerates, and the null check covers the
    // case where no card reference exists at all (e.g. a future caller passes null).
    void FlashOpen(Border card)
    {
        if (card == null) return;
        try
        {
            Brush origBorder = card.BorderBrush;
            Brush origBg = card.Background;
            card.BorderBrush = Theme.Br(Theme.Accent(_dark));
            card.Background = Theme.Br(Theme.AccentSoft(_dark));
            var t = new DispatcherTimer();
            t.Interval = TimeSpan.FromMilliseconds(350);
            t.Tick += delegate
            {
                t.Stop();
                if (card == null) return;
                card.BorderBrush = origBorder;
                card.Background = origBg;
            };
            t.Start();
        }
        catch (Exception) { }
    }

    // ① groundwork: ask the chat to open this conversation (it polls open.json).
    void OpenConv(string url)
    {
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["url"] = url; o["ts"] = _openSeq;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a worker in the main chat by NAME (robust click target). The chat polls open.json;
    // it resolves the worker by name in status.json and shows its live snapshot, and uses url
    // for /history when a real Copilot conv_url was captured.
    void OpenWorker(string name, string url)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? ""; o["ts"] = _openSeq;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // Open a HISTORY (archived) conversation. Carries the exact disk transcript path so the chat
    // shows the full body straight from the .jsonl even when conv_url is empty -- plus the worker
    // name as a fallback (the chat resolves the newest .jsonl by name). Fixes the completed-
    // conversation "本文はまだ取得できません" dead-end.
    void OpenHistory(string name, string url, string transcript)
    {
        try
        {
            _openSeq++;
            var o = new Dictionary<string, object>();
            o["worker"] = name ?? ""; o["url"] = url ?? "";
            o["transcript"] = transcript ?? ""; o["ts"] = _openSeq;
            File.WriteAllText(_openPath, _js.Serialize(o), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    // A concise card/conversation title: the Copilot-generated conv_title when present, else the
    // issue heading derived from the goal (the first real line after the "== ... issue ==" marker,
    // else the first non-boilerplate line), trimmed so long goal text never wrecks readability.
    string CardTitle(string convTitle, string goal)
    {
        // A generic Copilot auto-title ("会話" / "Chat" / "新しいチャット") carries no information,
        // so prefer the goal-derived heading -- otherwise every SWE worker card just reads "会話"
        // and w0..w7 are indistinguishable at a glance.
        string ct = (convTitle ?? "").Trim();
        bool genericCt = ct.Length <= 4 || ct == "会話" || ct == "新しいチャット"
                         || ct.Equals("Chat", StringComparison.OrdinalIgnoreCase)
                         || ct.Equals("New chat", StringComparison.OrdinalIgnoreCase);
        if (!string.IsNullOrEmpty(ct) && !genericCt) return Trunc(ct, 90);
        if (string.IsNullOrEmpty(goal)) return string.IsNullOrEmpty(ct) ? "" : Trunc(ct, 90);
        string[] lines = goal.Replace("\r", "").Split('\n');
        for (int i = 0; i < lines.Length; i++)
        {
            string ln = lines[i].Trim();
            if (ln.StartsWith("==") && ln.IndexOf("issue", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                for (int j = i + 1; j < lines.Length; j++)
                {
                    string h = lines[j].Trim();
                    if (h.Length > 0) return Trunc(h, 90);
                }
            }
        }
        foreach (string l in lines)
        {
            string t = l.Trim();
            if (t.Length > 0 && !t.StartsWith("==") && !t.StartsWith("あなたは")
                && !t.StartsWith("対象") && !t.StartsWith("この"))
                return Trunc(t, 90);
        }
        foreach (string l in lines) { string t = l.Trim(); if (t.Length > 0) return Trunc(t, 90); }
        return "";
    }

    string Trunc(string s, int n)
    {
        s = (s ?? "").Trim();
        return s.Length <= n ? s : s.Substring(0, n - 1) + "…";
    }

    // ── persistent history: finished/released tasks stack until cleared ───────────
    void LoadHistory()
    {
        _history = new List<object>();
        _archivedKeys = new System.Collections.Generic.HashSet<string>();
        try
        {
            if (!File.Exists(_historyPath)) return;
            var arr = _js.DeserializeObject(File.ReadAllText(_historyPath, Encoding.UTF8)) as object[];
            if (arr == null) return;
            foreach (object o in arr)
            {
                _history.Add(o);
                var d = o as Dictionary<string, object>;
                if (d != null && d.ContainsKey("key")) _archivedKeys.Add(S(d, "key"));
            }
        }
        catch (Exception) { }
    }
    void SaveHistory()
    {
        try { File.WriteAllText(_historyPath, _js.Serialize(_history), new UTF8Encoding(false)); }
        catch (Exception) { }
    }
    // The STABLE per-run+worker key (run identity + worker name). `started` is absent from
    // some per-worker dicts, so it's read from the root snapshot. Matches the archive scheme.
    static string WorkerKey(string started, Dictionary<string, object> w)
    { return (started ?? "") + "#" + S(w, "name"); }

    void LoadHidden()
    {
        _hiddenKeys = new System.Collections.Generic.HashSet<string>();
        try
        {
            if (!File.Exists(_hiddenPath)) return;
            var arr = _js.DeserializeObject(File.ReadAllText(_hiddenPath, Encoding.UTF8)) as object[];
            if (arr == null) return;
            foreach (object o in arr)
                if (o != null) _hiddenKeys.Add(o.ToString());
        }
        catch (Exception) { }
    }

    void SaveHidden()
    {
        try
        {
            var list = new List<object>();
            foreach (string k in _hiddenKeys) list.Add(k);
            File.WriteAllText(_hiddenPath, _js.Serialize(list), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    void ClearHistory()
    {
        _history.Clear(); _archivedKeys.Clear();
        try { if (File.Exists(_historyPath)) File.Delete(_historyPath); } catch (Exception) { }
        try
        {
            var st = ReadStatus();
            bool runningFlag = st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                               && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]));
            // A LIVE run updates status.json ~once/second. If "running" is true but the
            // file is STALE (the run crashed / was killed without writing a final snapshot,
            // e.g. a send failure), it is not actually live -- so allow clearing it.
            // Otherwise a dead run's card sticks forever and Clear appears to do nothing.
            double updated = 0;
            try { if (st != null && st.ContainsKey("updated")) updated = Convert.ToDouble(st["updated"]); }
            catch (Exception) { }
            bool stale = updated > 0 && (NowUnix() - updated) > 15;
            bool live = runningFlag && !stale;

            if (!live)
            {
                // Run is finished/dead: a clean reset -- blank the snapshot AND forget hides.
                File.WriteAllText(_statusPath,
                    "{\"total\":0,\"done_count\":0,\"running\":false,\"idle\":true,\"workers\":[]}",
                    new UTF8Encoding(false));
                _hiddenKeys.Clear();
                SaveHidden();
            }
            else
            {
                // Run is LIVE: the runner rewrites status.json each second, so we can't blank it
                // (it would just reappear, and would clobber running workers). Instead HIDE the
                // TERMINAL cards persistently -- the runner regenerating them won't un-hide them.
                // Running (non-terminal) workers are NEVER hidden: a live task vanishing is a hazard.
                object wo; string started = S(st, "started");
                if (st != null && st.TryGetValue("workers", out wo) && wo is object[])
                {
                    bool any = false;
                    foreach (object o in (object[])wo)
                    {
                        var w = o as Dictionary<string, object>;
                        if (w == null || !IsTerminalWorker(w)) continue;
                        if (_hiddenKeys.Add(WorkerKey(started, w))) any = true;
                    }
                    if (any) SaveHidden();
                }
            }
        }
        catch (Exception) { }
        ForceRender();
    }
    void ArchiveTerminal(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        string started = S(root, "started");
        bool added = false;
        foreach (object o in (object[])wo)
        {
            var w = (Dictionary<string, object>)o;
            string status = S(w, "status");
            bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                            || status == "error" || status == "cancelled";
            if (!terminal) continue;
            string conv = S(w, "conv_url");
            // STABLE key per run+worker (conv_url is absent in the final snapshot, so
            // keying on it would double-archive the same task).
            string key = started + "#" + S(w, "name");
            if (_archivedKeys.Contains(key)) continue;
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = status;
            e["conv_title"] = S(w, "conv_title");
            e["outcome"] = S(w, "outcome"); e["conv_url"] = conv;
            // Carry the disk transcript path + worker name so a HISTORY row can still show the
            // full body: conv_url is empty in the final snapshot (and for the new agent), so
            // without these a completed conversation strands on "本文はまだ取得できません" even
            // though the jsonl transcript exists on disk.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            _history.Add(e);
            added = true;
        }
        if (added) SaveHistory();
    }

    // Client-side archive of ONE worker into the persisted history + hide its card. Unlike the
    // per-worker 解放 (which writes commands.json and needs a LIVE fleet to consume it), this works
    // with the fleet stopped -- so a finished or stale card can be moved to history any time.
    void _archiveOne(Dictionary<string, object> w)
    {
        if (w == null) return;
        string started = _lastRoot != null ? S(_lastRoot, "started") : "";
        string key = started + "#" + S(w, "name");
        if (!_archivedKeys.Contains(key))
        {
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = S(w, "status");
            e["conv_title"] = S(w, "conv_title"); e["outcome"] = S(w, "outcome");
            e["conv_url"] = S(w, "conv_url");
            // see _archiveTerminal: carry transcript path + name so the history row can show the
            // full disk transcript even when conv_url is empty.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            _history.Add(e);
        }
        _hiddenKeys.Add(WorkerKey(started, w));
    }

    void ArchiveAndHide(Dictionary<string, object> w) { _archiveOne(w); SaveHistory(); SaveHidden(); ForceRender(); }

    // Bulk: move every TERMINAL worker currently shown into history (the 完了-divider button).
    void ArchiveAllTerminal()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            if (IsTerminalWorker(w)) _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }

    // Bulk-clear EVERY shown worker -- used by Stop-all when the run has gone STALE (no live fleet to
    // consume a stop command). Unlike ArchiveAllTerminal this clears non-terminal cards too, because a
    // stale run's "running" workers are frozen leftovers, not actually executing. Makes Stop-all do
    // something visible even with the driver dead, instead of silently writing an unread commands.json.
    void ArchiveAllStale()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }
}
