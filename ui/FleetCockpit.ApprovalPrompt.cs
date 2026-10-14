// FleetCockpit.ApprovalPrompt.cs -- ApprovalPromptWindow: the native approval prompt launched beside a toast (a separate top-level class, moved whole).
// Moved verbatim out of ui/FleetCockpit.cs: no code was changed. Compiled with the other parts
// (Build "FleetCockpit" line of ui/rebuild_ui.ps1).
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

// Small native action surface launched alongside an approval toast. It is intentionally part of
// FleetCockpit.exe: no installer, protocol registration, browser, admin rights, or extra runtime is
// required. One process drains all pending gates so a burst of workers never creates a window storm.
class ApprovalPromptWindow : Window
{
    readonly JavaScriptSerializer _js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
    string _gateDir;
    string _currentPath;
    //: Why the path was refused, kept so the window can say it. It used to close itself in
    //: `Loaded` before painting, so a refused gate and a working one looked identical from the
    //: outside: the notification was clicked and nothing happened at all.
    string _gateWhy;
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
    // AccentFg is the foreground for text on ANY saturated fill, not only the accent one: white
    // reads on both fills in the light theme (5.14 on the orange, 5.02 on the green) and on
    // neither in the dark theme (2.80 and 2.28), so the choice is a property of the theme, not of
    // the hue. Nine buttons here used a literal white brush and could not follow it -- including
    // the one that starts a run, the largest control on the screen.
    Brush Bg, Surface, SurfaceSubtle, Line, Fg, Muted, Accent, AccentFg, AccentFill, Danger, Warning;

    static double NowUnix()
    { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    //: WHERE THE SETTINGS LIVE, in both places, new first.
    //:
    //: %APPDATA% is redirected for a process running inside an MSIX package, so the same
    //: absolute path resolved to the operator's file from one context and to a private copy
    //: from another. On 2026-09-16 the panel showed 1 GB while every fleet coordinator
    //: reserved 4 GB, for a month, and neither side could see the other's file. The
    //: repository is the one directory every context agrees about.
    //:
    //: READS take the new location when it exists and the old one otherwise, so a machine
    //: mid-migration keeps working and one that never migrates behaves exactly as before.
    //: WRITES always go to the new location. Kept in step with tools/settings_path.py --
    //: test_the_settings_path_is_the_same_in_every_language pins the two together.
    static string SettingsFileNew
    {
        get { return Path.Combine(RepoRootForSettings(), ".config", "settings.txt"); }
    }

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

    //: The repository root as seen from the running executable: ui\ sits directly under it.
    static string RepoRootForSettings()
    {
        try
        {
            string exe = System.Reflection.Assembly.GetExecutingAssembly().Location;
            string dir = Path.GetDirectoryName(exe);
            DirectoryInfo d = new DirectoryInfo(dir);
            while (d != null && !Directory.Exists(Path.Combine(d.FullName, ".fleet")))
                d = d.Parent;
            if (d != null) return d.FullName;
        }
        catch (Exception) { }
        return Directory.GetCurrentDirectory();
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
            // THE SAME QUESTION THE WRITER ASKED. Not "is this folder called .companion_gates"
            // -- gate_ops.py writes to MCP_GATE_DIR when it is set, and a cockpit that only
            // recognises the default name can never open what it wrote. Still a whitelist, and
            // still exactly one directory: an arbitrary path handed to --approval-gate is
            // refused as firmly as before, it is simply the RIGHT directory now.
            string full = Path.GetFullPath(initialGatePath);
            _gateDir = Path.GetDirectoryName(full);
            string allowed = CockpitWindow.ResolveGateDirectory("", "");
            if (!Path.GetFileName(full).StartsWith("gate_", StringComparison.OrdinalIgnoreCase) ||
                !Path.GetExtension(full).Equals(".json", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("not an approval gate file: " + full);
            // ASK THE FILESYSTEM, DO NOT COMPARE SPELLINGS. The first version compared
            // Path.GetFullPath of both directories as strings, and failed the moment they were
            // spelled differently -- measured with a gate under %TEMP%, which Windows hands back
            // in its 8.3 short form, against the same directory written long. The window closed
            // and the operator saw nothing. GetFullPath does not expand a short name, and
            // neither does any string comparison; enumerating the allowed directory answers "is
            // this file in there" in whatever spelling either side used.
            if (!Directory.Exists(allowed) ||
                Directory.GetFiles(allowed, Path.GetFileName(full)).Length == 0)
                throw new InvalidOperationException(
                    "approval gate is not in the gate directory: " + _gateDir +
                    " (expected " + allowed + ")");
            _currentPath = full;
        }
        catch (Exception ex) { _gateDir = null; _currentPath = null; _gateWhy = ex.Message; }

        Build();
        Loaded += delegate
        {
            if (_gateDir == null)
            {
                // A REFUSAL IS AN ANSWER AND HAS TO BE GIVEN. Closing here is what made a
                // clicked notification do nothing at all -- no window, no message, no log.
                MessageBox.Show(this,
                    L("承認ゲートを開けませんでした。", "The approval gate could not be opened.")
                    + "\n\n" + (_gateWhy ?? ""),
                    L("承認が必要です", "Approval required"),
                    MessageBoxButton.OK, MessageBoxImage.Warning);
                Close();
                return;
            }
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
        AccentFill = Theme.Br(Theme.AccentFill(_dark));
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
        _deny = new Button { Content = L("拒否", "Deny"), Padding = new Thickness(16, 7, 16, 7),
            Margin = new Thickness(0, 0, 8, 0), Background = Brushes.Transparent,
            Foreground = Danger, BorderBrush = Danger, BorderThickness = new Thickness(1), Cursor = Cursors.Hand };
        _deny.Template = FlatButtonTemplate();
        _deny.Click += delegate { Answer("denied"); }; DockPanel.SetDock(_deny, Dock.Right); actions.Children.Add(_deny);
        _approve = new Button { Content = L("承認", "Approve"), Padding = new Thickness(24, 8, 24, 8),
            Margin = new Thickness(0, 0, 8, 0), Background = AccentFill, Foreground = AccentFg,
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
            Padding = new Thickness(16, 12, 16, 12) };
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
            CornerRadius = new CornerRadius(Theme.RadCard), Padding = new Thickness(16, 12, 16, 12), Margin = new Thickness(0, Theme.SectionGap, 0, 0) };
        var policyCol = new StackPanel();
        policyCol.Children.Add(new TextBlock { Text = L("今後の操作承認", "Approval policy"), Foreground = Fg,
            FontSize = Theme.FsSection, FontWeight = FontWeights.SemiBold });
        _policy = new ComboBox { Margin = new Thickness(0, 8, 0, 0), MinWidth = 220,
            HorizontalAlignment = HorizontalAlignment.Left, Background = Surface, Foreground = Fg };
        // ORDER AND LABELS FOLLOW WHAT IS ACTUALLY RECOMMENDED. "Confirm" was first and was
        // labelled 推奨 / recommended; it is the mode that asks about every first-seen class
        // and keeps asking, which is how an approval queue becomes something nobody reads.
        // Auto refuses a prohibited pattern outright -- stricter than Confirm, which puts the
        // same operation to a person who can approve it -- and differs only on operations the
        // deterministic classifier finds clean.
        AddPolicyItem(L("自動（推奨）", "Auto (recommended)"), "auto");
        AddPolicyItem(L("バイパス", "Bypass"), "bypass");
        AddPolicyItem(L("毎回確認（非推奨）", "Confirm every time (not recommended)"), "default");
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
    { var item = _policy.SelectedItem as ComboBoxItem; return item == null ? "auto" : (string)item.Tag; }

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
        else _policyHelp.Text = L("初回の操作クラスを毎回確認します。非推奨: 確認が多すぎると内容が読まれなくなります。",
            "Asks about every first-seen operation class. Not recommended: an approval that is always there stops being read.");
    }

    public static string ReadPolicy()
    {
        // Matches tools/approval_policy.FALLBACK_APPROVAL_MODE. Two readers of one setting
        // that disagree about its default would show the operator a mode the relay is not
        // using.
        string fallback = (Environment.GetEnvironmentVariable("TASK_JOB_APPROVAL_MODE") ?? "auto").Trim().ToLowerInvariant();
        if (fallback != "auto" && fallback != "bypass" && fallback != "default") fallback = "auto";
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
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileForWrite));
            File.WriteAllText(SettingsFileForWrite, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
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
        // EVERY PATH OUT OF HERE ENDS AT LoadNext(). This used to `return` twice without it --
        // once when the gate file was missing and once when the write threw -- leaving
        // _current set and the same request on screen. Reported 2026-09-12: 承認 pressed
        // repeatedly on a gate whose file had been removed elsewhere, and nothing ever
        // happened. A button that does nothing teaches people the window is broken, and after
        // that they stop reading what it says, which is the whole failure an approval queue
        // exists to avoid.
        var gate = ReadGate(_currentPath);
        if (gate == null)
        {
            // GONE IS RESOLVED, NOT BROKEN. The file is removed when a request is answered
            // from the console, from another window, or by a cleanup -- so there is nothing
            // to decide here any more. Move on quietly; saying "it vanished" about something
            // the operator did themselves is noise.
            _current = null; _currentPath = null; LoadNext(); return;
        }
        try
        {
            gate.Remove("path"); gate["answered"] = true; gate["answer"] = verdict; gate["answered_at"] = NowUnix();
            string tmp = _currentPath + ".tmp";
            File.WriteAllText(tmp, _js.Serialize(gate), new UTF8Encoding(false));
            try { File.Replace(tmp, _currentPath, null); }
            catch { File.Copy(tmp, _currentPath, true); try { File.Delete(tmp); } catch { } }
        }
        catch (Exception ex)
        {
            // A WRITE THAT FAILED IS NOT THE SAME AS A GATE THAT VANISHED, and the operator
            // has to hear about this one: the operation they just approved will NOT proceed,
            // because the thing that reads the answer never sees it.
            try
            {
                MessageBox.Show(this,
                    L("この承認を記録できませんでした。操作は実行されません。\n\n",
                      "This answer could not be recorded, so the operation will not proceed.\n\n")
                    + ex.Message,
                    L("承認の記録に失敗", "Could not record the answer"),
                    MessageBoxButton.OK, MessageBoxImage.Warning);
            }
            catch { }
            _current = null; _currentPath = null; LoadNext(); return;
        }
        _current = null; _currentPath = null; LoadNext();
    }
}
