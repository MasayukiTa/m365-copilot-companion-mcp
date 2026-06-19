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
// ui/assets/material_glyphs.json (NO emoji). Palette = ShuttleScope slate; status shows
// as a soft band border + faint tint (not a harsh fill) plus a saturated pill. Theme and
// language follow the shared settings.txt live, so toggling the chat retints this too.
// Build: ui\build_cockpit.bat  (Windows csc.exe, legacy C# 5).
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
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
        string path = args.Length > 0 ? args[0] : null;
        new Application().Run(new CockpitWindow(path));
    }
}

class CockpitWindow : Window
{
    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    static double NowUnix() { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    // theme-dependent brushes
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    static readonly Brush Accent = new SolidColorBrush(C("#ea580c"));
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;          // 0 = Japanese, 1 = English
    int _maxtabs = 3;
    bool _autoscale = false;   // RAM-aware autoscale on/off (NEW)
    int _autoMax = 4;          // ceiling (上限) tabs may grow to under autoscale (NEW)
    string _effort = "auto";   // effort mode min|max|ultra|auto -> settings.txt effort= (NEW)
    bool _paused = false;      // local fleet pause/resume toggle state (NEW)
    long _settingsMtime = 0;

    readonly string _statusPath, _commandsPath, _historyPath, _openPath;
    string _convsPath, _hiddenPath;
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
    Button _themeBtn, _langBtn, _mainBtn;
    TextBlock _maxLbl;
    Border _headBar;
    ListBox _list;                 // virtualizing host for the card/history rows
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

    // Per-worker disclosure state (Claude-Code-style "> / v"). Collapsed is the default:
    // a collapsed card renders only a lightweight summary line -- no progress quote, no steer
    // TextBox -- so a fleet of 100+ tasks stays scrollable. Only an EXPANDED card builds the
    // heavy detail and enables steering. Keyed by worker name; survives re-renders.
    HashSet<string> _expanded = new HashSet<string>();
    // last status snapshot rendered, kept so a chevron toggle can rebuild ONLY its one card
    // in place (no full 164-card re-render) -- that's what makes expand/collapse feel instant.
    Dictionary<string, object> _lastRoot;

    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    // Feature B: terminal-task view filter. 0=all, 1=unfinished only (hide DONE), 2=done only.
    int _cardFilter = 0;

    // Opt-in, CAPPED auto-retry (default OFF). When on, a newly-stopped non-DONE goal is
    // re-queued at most _autoRetryMax times -- bounded so a deterministically-failing task
    // (e.g. the tool-denial ones) can NEVER loop forever. Counted by goal TEXT, so a re-queued
    // copy (which gets a new worker name) shares the original goal's budget. Manual retry is
    // unaffected -- this only governs the automatic re-queue.
    bool _autoRetry = false;
    int _autoRetryMax = 1;
    Dictionary<string, int> _autoRetryCount = new Dictionary<string, int>();

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
        string dir = Path.GetDirectoryName(_statusPath);
        _commandsPath = Path.Combine(dir, "commands.json");
        _historyPath = Path.Combine(dir, "history.json");
        _openPath = Path.Combine(dir, "open.json");
        _convsPath = Path.Combine(dir, "conversations.json");
        _hiddenPath = Path.Combine(dir, "cockpit_hidden.json");
        LoadGlyphs();
        LoadHistory();
        LoadHidden();
        LoadSettings();
        ApplyThemeBrushes();
        Width = 1080; Height = 760;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(700);
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        OnTick(null, null);
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
        if (k == "applies_next") return ja ? "次回起動から適用" : "applies next run";
        if (k == "start") return ja ? "並列実行を開始" : "Start parallel run";
        if (k == "goalhint") return ja ? "1行に1ゴール（複数可）・Ctrl+Enter で開始" : "One goal per line · Ctrl+Enter to start";
        if (k == "folder") return ja ? "自律コーディング (フォルダ)" : "Autonomous coding (folder)";
        // Feature B: view-filter toolbar
        if (k == "flt_all") return ja ? "すべて" : "All";
        if (k == "flt_unfinished") return ja ? "未完了のみ" : "Unfinished only";
        if (k == "flt_done") return ja ? "完了のみ" : "Done only";
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
        if (k == "pause") return ja ? "一時停止" : "Pause";
        if (k == "resume") return ja ? "再開" : "Resume";
        if (k == "stopall") return ja ? "全停止" : "Stop all";
        return k;
    }
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
                else if (ln.StartsWith("maxtabs=") && int.TryParse(ln.Substring(8).Trim(), out v)) _maxtabs = Math.Max(1, Math.Min(8, v));
                else if (ln.StartsWith("autoscale_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoMax = Math.Max(1, Math.Min(8, v));
                else if (ln.StartsWith("autoscale=")) _autoscale = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoretry_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoRetryMax = Math.Max(1, Math.Min(3, v));
                else if (ln.StartsWith("autoretry=")) _autoRetry = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("effort="))
                {
                    string ef = ln.Substring(7).Trim();
                    if (ef == "min" || ef == "max" || ef == "ultra" || ef == "auto") _effort = ef;
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
        if (_dark)
        {
            Bg = new SolidColorBrush(C("#0f172a")); CardBg = new SolidColorBrush(C("#1e293b"));
            Border = new SolidColorBrush(C("#334155")); Fg = new SolidColorBrush(C("#f8fafc"));
            Muted = new SolidColorBrush(C("#94a3b8")); QuoteBg = new SolidColorBrush(C("#0b1220"));
            BtnBg = new SolidColorBrush(C("#1e293b"));
        }
        else
        {
            Bg = new SolidColorBrush(C("#ffffff")); CardBg = new SolidColorBrush(C("#f8fafc"));
            Border = new SolidColorBrush(C("#e2e8f0")); Fg = new SolidColorBrush(C("#0f172a"));
            Muted = new SolidColorBrush(C("#64748b")); QuoteBg = new SolidColorBrush(C("#f1f5f9"));
            BtnBg = new SolidColorBrush(C("#f1f5f9"));
        }
    }

    Color BgColor() { return _dark ? C("#0f172a") : C("#ffffff"); }
    Color CardColor() { return _dark ? C("#1e293b") : C("#f8fafc"); }
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
        if (ck == "good") return C("#3b4cc0");
        if (ck == "done") return C("#16a34a");
        if (ck == "bad") return C("#b40426");
        return _dark ? C("#64748b") : C("#94a3b8");
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

        // right controls: autoscale group, language, theme  (row 0, col 1)
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;
        ctrls.HorizontalAlignment = HorizontalAlignment.Right;

        ctrls.Children.Add(AutoscaleControls());
        ctrls.Children.Add(EffortControl());
        ctrls.Children.Add(FleetControls());
        _mainBtn = IconButton("chat", 18);
        _mainBtn.ToolTip = _lang == 0 ? "メイン (チャット) を開く" : "Open main chat";
        _mainBtn.Click += delegate { OpenMain(); };
        ctrls.Children.Add(_mainBtn);
        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); Relabel(); ForceRender(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyTheme(); };
        ctrls.Children.Add(_themeBtn);
        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headRow.Children.Add(ctrls);

        // title (icon + title) -- row 0, col 0
        var titleRow = new DockPanel { LastChildFill = true };
        titleRow.VerticalAlignment = VerticalAlignment.Center;
        titleRow.Margin = new Thickness(0, 0, 12, 0);
        _iconHost = new ContentControl(); _iconHost.VerticalAlignment = VerticalAlignment.Center;
        _iconHost.Margin = new Thickness(0, 0, 10, 0);
        DockPanel.SetDock(_iconHost, Dock.Left);
        titleRow.Children.Add(_iconHost);
        _header = new TextBlock(); _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis; _header.TextWrapping = TextWrapping.NoWrap;
        titleRow.Children.Add(_header);    // fills the rest of col 0
        Grid.SetColumn(titleRow, 0); Grid.SetRow(titleRow, 0);
        headRow.Children.Add(titleRow);

        // subtitle -- its OWN row spanning BOTH columns, so the long elapsed+ETA line uses the full
        // width and is never clipped by the controls column. Wrap (not ellipsis) so it's never hidden.
        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(38, 4, 18, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        headRow.Children.Add(_sub);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);
        root.Children.Add(BuildInputBar());
        root.Children.Add(BuildMtBanner());

        // Virtualizing card list. A ListBox brings its own ScrollViewer + VirtualizingStackPanel,
        // so with 100-164 cards only the ~10-20 visible rows are realized -> scrolling and
        // window maximize/restore stay fast. The container chrome is templated away so it reads
        // as a plain scrolling list (no selection highlight / mouse-over / focus rect).
        _list = new ListBox();
        _list.BorderThickness = new Thickness(0);
        _list.Background = Brushes.Transparent;
        _list.Padding = new Thickness(18, 6, 18, 24);
        ScrollViewer.SetVerticalScrollBarVisibility(_list, ScrollBarVisibility.Auto);
        ScrollViewer.SetHorizontalScrollBarVisibility(_list, ScrollBarVisibility.Disabled);
        ScrollViewer.SetCanContentScroll(_list, true);
        VirtualizingPanel.SetIsVirtualizing(_list, true);
        VirtualizingPanel.SetVirtualizationMode(_list, VirtualizationMode.Recycling);
        VirtualizingPanel.SetScrollUnit(_list, ScrollUnit.Pixel);
        _list.Focusable = false;
        _list.IsTabStop = false;
        KeyboardNavigation.SetDirectionalNavigation(_list, KeyboardNavigationMode.None);
        _list.ItemContainerStyle = BuildItemContainerStyle();
        _list.ItemTemplate = BuildRowTemplate();
        _list.ItemsSource = _rows;     // bound ONCE; SetRows mutates _rows in place (no Reset)
        root.Children.Add(_list);
        Content = root;
        PaintChrome();
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

    // ④ task-injection: type goals (one per line) and launch a fleet from here.
    UIElement BuildInputBar()
    {
        _inBar = new Border();
        _inBar.Padding = new Thickness(26, 2, 18, 10);
        DockPanel.SetDock(_inBar, Dock.Top);

        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        _goalInput = new TextBox();
        _goalInput.AcceptsReturn = true; _goalInput.TextWrapping = TextWrapping.Wrap;
        _goalInput.MinHeight = 40; _goalInput.MaxHeight = 120;
        _goalInput.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _goalInput.FontSize = 13; _goalInput.Padding = new Thickness(10, 8, 10, 8);
        _goalInput.BorderThickness = new Thickness(1);
        _goalInput.VerticalContentAlignment = VerticalAlignment.Top;
        BuildGoalCmdPopup();
        _goalInput.TextChanged += delegate { UpdateGoalCmdPopup(); };
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
            { e.Handled = true; StartFleet(); }
        };
        Grid.SetColumn(_goalInput, 0); grid.Children.Add(_goalInput);
        // Placeholder hint advertising slash commands + how to start (the goal box had no visible
        // hint and its slash palette was invisible until you typed "/"). (friction #8)
        var goalHint = new TextBlock
        {
            Text = T("goalhint") + (_lang == 0 ? "  ·「/」でコマンド" : "  · \"/\" for commands"),
            IsHitTestVisible = false, FontSize = 12.5, Foreground = Muted,
            Margin = new Thickness(13, 9, 12, 0), VerticalAlignment = VerticalAlignment.Top,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        Grid.SetColumn(goalHint, 0); grid.Children.Add(goalHint);
        _goalInput.TextChanged += delegate { goalHint.Visibility = string.IsNullOrEmpty(_goalInput.Text) ? Visibility.Visible : Visibility.Collapsed; };

        var rightCol = new StackPanel();
        rightCol.Margin = new Thickness(10, 0, 0, 0);
        _startBtn = new Button();
        _startBtn.Cursor = Cursors.Hand; _startBtn.BorderThickness = new Thickness(0);
        _startBtn.Height = 40; _startBtn.MinWidth = 150; _startBtn.FontWeight = FontWeights.SemiBold;
        _startBtn.Padding = new Thickness(14, 0, 14, 0);
        _startBtn.Click += delegate { StartFleet(); };
        rightCol.Children.Add(_startBtn);
        _folderBtn = new Button();
        _folderBtn.Cursor = Cursors.Hand; _folderBtn.BorderThickness = new Thickness(1);
        _folderBtn.Height = 30; _folderBtn.MinWidth = 150; _folderBtn.FontSize = 12;
        _folderBtn.Margin = new Thickness(0, 6, 0, 0); _folderBtn.Padding = new Thickness(10, 0, 10, 0);
        _folderBtn.Click += delegate { FolderToGoals(); };
        rightCol.Children.Add(_folderBtn);
        _startNote = new TextBlock();
        _startNote.FontSize = 11; _startNote.Margin = new Thickness(2, 4, 0, 0);
        _startNote.TextWrapping = TextWrapping.Wrap; _startNote.MaxWidth = 150;
        rightCol.Children.Add(_startNote);
        Grid.SetColumn(rightCol, 1); grid.Children.Add(rightCol);

        _inBar.Child = grid;
        return _inBar;
    }
    Border _inBar;

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

            SpawnFleet(goals, "goals_input.txt");
            _goalInput.Text = "";
            _startNote.Text = (_lang == 0 ? "開始しました（" : "Started (") + goals.Count
                              + (_lang == 0 ? " 件）" : " goals)");
            _lastSig = "";   // force a re-render once status.json starts updating
        }
        catch (Exception ex)
        {
            _startNote.Text = (_lang == 0 ? "起動失敗: " : "Failed: ") + ex.Message;
        }
    }

    // Spawn a fresh `python -m relay.fleet_runner` for the given goal texts. Factored out of
    // StartFleet so RETRY can reuse it: when a run has FINISHED, the cockpit relaunches a fleet
    // (instead of writing an add_goal that nothing alive would ever consume). The agent URL is
    // NOT passed -- the runner resolves it from MCP_FLEET_AGENT_URL / .env, exactly as a manual
    // Start does -- and --state-dir is the same .fleet dir this cockpit tails, so the relaunched
    // run shows up live here. Goals are handed over via a UTF-8 file to dodge arg-encoding issues.
    // Returns true if the process started. `goalsFileName` lets callers use a distinct file so a
    // retry spawn never clobbers the manual Start input file (or vice-versa).
    bool SpawnFleet(List<string> goals, string goalsFileName)
    {
        string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(py)) py = "python";
        string stateDir = Path.GetDirectoryName(_statusPath);
        string goalsFile = Path.Combine(stateDir, goalsFileName);
        File.WriteAllText(goalsFile, string.Join("\n", goals.ToArray()) + "\n", new UTF8Encoding(false));

        var psi = new System.Diagnostics.ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = "-m relay.fleet_runner --goals-file \"" + goalsFile + "\""
                        + " --state-dir \"" + stateDir + "\" --effort " + _effort;
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // --- slash-command autocomplete for the goal box (parity with the main chat) ---
    Popup _gcmdPopup; ListBox _gcmdList;
    static readonly string[][] _goalCommands = {
        new[]{"/code","<機能> を実装し、pytest テストも書いて通す"},
        new[]{"/fix","<ファイル> の <不具合> を直し、テストを通す"},
        new[]{"/test","<対象> の pytest テストを書く"},
        new[]{"/refactor","<対象> を読みやすくリファクタする(挙動は変えない)"},
        new[]{"/doc","<対象> の README/説明 を書く"},
        new[]{"/review","<対象> をレビューして問題点を箇条書きで挙げる"},
        new[]{"/research","<問い> を Claude で深掘り調査する"},
    };
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
            int ls; string line; CurrentGoalLine(out ls, out line);
            string txt = _goalInput.Text ?? ""; int caret = _goalInput.CaretIndex;
            if (caret > txt.Length) caret = txt.Length;
            _goalInput.Text = txt.Substring(0, ls) + template + txt.Substring(caret);
            _goalInput.CaretIndex = ls + template.Length;
        }
        if (_gcmdPopup != null) _gcmdPopup.IsOpen = false;
        _goalInput.Focus();
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

            // plan-first? Yes -> propose a numbered plan and pause as 承認待ち; you approve
            // or edit it with a steer (the W card's steer box) to start execution.
            var mb = System.Windows.MessageBox.Show(this,
                _lang == 0 ? "先に実行計画を提示して承認を待ちますか？\n\nはい = 計画提示 → 承認待ち（カードに steer で承認/修正を送ると実行）\nいいえ = すぐ実行"
                           : "Propose a plan first and wait for your approval?\n\nYes = plan -> 承認待ち (steer the card to approve/edit)\nNo = run now",
                _lang == 0 ? "コーディング起動" : "Start coding",
                System.Windows.MessageBoxButton.YesNoCancel, System.Windows.MessageBoxImage.Question);
            if (mb == System.Windows.MessageBoxResult.Cancel) return;
            bool plan = mb == System.Windows.MessageBoxResult.Yes;

            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            string stateDir = Path.GetDirectoryName(_statusPath);

            // code_task: natural language in, auto-verified autonomous run out. Writes the
            // same status.json this cockpit already tails, so the task shows up live.
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.code_task -i \"" + instr.Replace("\"", "'")
                + "\" -f \"" + folder + "\" --state-dir \"" + stateDir + "\""
                + (plan ? " --plan" : "");
            psi.WorkingDirectory = repo; psi.UseShellExecute = false; psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }

            var p = new System.Diagnostics.Process();
            p.StartInfo = psi;
            p.Start();
            _startNote.Text = _lang == 0
                ? (plan ? "コーディング(計画モード)を開始。計画提示後「承認待ち」になります。カードに承認/修正を steer で送ってください。"
                        : "コーディングタスクを開始（検証方法は自動検出。テスト/コンパイルが通るまで完了しません）。")
                : (plan ? "Coding (plan mode) started -> it will pause at 承認待ち; steer the card to approve/edit."
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

    ContentControl _iconHost;
    Button _maxMinus, _maxPlus;
    ComboBox _effortBox;
    Button _pauseBtn, _stopBtn;
    Button _autoToggle;
    Button _autoMinus, _autoPlus;
    TextBlock _autoLbl, _autoValue;

    // Autoscale control GROUP: [ RAM自動調整: ON/OFF ]  [開始(デフォルト) − N +]  [上限 − M +].
    // Replaces the old single max-tabs stepper. The ceiling stepper greys out when autoscale
    // is off (it only matters under autoscale). All three live-apply via SetMaxTabs/SetAutoMax/
    // the toggle handler, which route to RequestSetAutoscale or RequestSetMaxtabs as appropriate.
    UIElement AutoscaleControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);

        // a. autoscale ON/OFF toggle (simple themed button whose label flips)
        _autoToggle = new Button();
        _autoToggle.Cursor = Cursors.Hand; _autoToggle.BorderThickness = new Thickness(1);
        _autoToggle.Padding = new Thickness(10, 3, 10, 3); _autoToggle.FontSize = 12;
        _autoToggle.FontWeight = FontWeights.SemiBold;
        _autoToggle.Margin = new Thickness(0, 0, 12, 0);
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

        // b. the EXISTING max-tabs stepper, relabeled as the default/start.
        group.Children.Add(MaxTabsStepper());

        // c. NEW ceiling stepper (mirrors MaxTabsStepper's −/+ pattern).
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
        group.Children.Add(wrap);

        return group;
    }

    // Effort selector: a [推論] label + a DROPDOWN (ComboBox) listing min/max/ultra/auto.
    // Picking a mode persists effort= to settings.txt; the fleet runner reads it at launch
    // (governs both fleet and single runs). The ComboBox is the single source of truth.
    static readonly string[] _effortModes = { "min", "max", "ultra", "auto" };
    TextBlock _effortLbl;
    UIElement EffortControl()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);

        _effortLbl = new TextBlock(); _effortLbl.VerticalAlignment = VerticalAlignment.Center;
        _effortLbl.FontSize = 12; _effortLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_effortLbl);

        _effortBox = new ComboBox();
        _effortBox.Cursor = Cursors.Hand; _effortBox.FontSize = 12;
        _effortBox.FontWeight = FontWeights.SemiBold; _effortBox.MinWidth = 78;
        _effortBox.Padding = new Thickness(8, 2, 4, 2);
        _effortBox.VerticalAlignment = VerticalAlignment.Center;
        foreach (string m in _effortModes) _effortBox.Items.Add(m);
        _effortBox.SelectedItem = _effort;
        _effortBox.SelectionChanged += delegate
        {
            string sel = _effortBox.SelectedItem as string;
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
        if (!Equals(_effortBox.SelectedItem, _effort)) _effortBox.SelectedItem = _effort;
        _effortBox.Background = BtnBg; _effortBox.Foreground = Fg; _effortBox.BorderBrush = Border;
    }

    // Fleet-wide controls: Pause/Resume toggle + Stop-all. Both write into commands.json
    // via WriteCommands, merging with ReadCommands first so a queued close/steer/add isn't
    // clobbered. fleet_runner._drain_commands consumes {"pause":bool}/{"stop":true} each sweep.
    UIElement FleetControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);

        _pauseBtn = new Button();
        _pauseBtn.Cursor = Cursors.Hand; _pauseBtn.BorderThickness = new Thickness(1);
        _pauseBtn.Padding = new Thickness(10, 3, 10, 3); _pauseBtn.FontSize = 12;
        _pauseBtn.FontWeight = FontWeights.SemiBold;
        _pauseBtn.Margin = new Thickness(0, 0, 8, 0);
        _pauseBtn.VerticalAlignment = VerticalAlignment.Center;
        _pauseBtn.Click += delegate
        {
            _paused = !_paused;
            var cmd = ReadCommands();
            cmd["pause"] = _paused;
            WriteCommands(cmd);
            PaintPause();
        };
        group.Children.Add(_pauseBtn);

        _stopBtn = new Button();
        _stopBtn.Cursor = Cursors.Hand; _stopBtn.BorderThickness = new Thickness(1);
        _stopBtn.Padding = new Thickness(10, 3, 10, 3); _stopBtn.FontSize = 12;
        _stopBtn.FontWeight = FontWeights.SemiBold;
        _stopBtn.VerticalAlignment = VerticalAlignment.Center;
        _stopBtn.Content = T("stopall");
        _stopBtn.Click += delegate
        {
            var cmd = ReadCommands();
            cmd["stop"] = true;
            WriteCommands(cmd);
        };
        group.Children.Add(_stopBtn);

        PaintPause();
        return group;
    }
    // Paused => accent bg + white text (contrast rule), label shows Resume; otherwise neutral, Pause.
    void PaintPause()
    {
        if (_pauseBtn == null) return;
        _pauseBtn.Content = _paused ? T("resume") : T("pause");
        if (_paused) { _pauseBtn.Background = Accent; _pauseBtn.Foreground = White; _pauseBtn.BorderBrush = Accent; }
        else { _pauseBtn.Background = BtnBg; _pauseBtn.Foreground = Fg; _pauseBtn.BorderBrush = Border; }
    }

    UIElement MaxTabsStepper()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);
        _maxLbl = new TextBlock(); _maxLbl.VerticalAlignment = VerticalAlignment.Center;
        _maxLbl.FontSize = 12; _maxLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_maxLbl);
        _maxMinus = MiniButton("−");
        _maxMinus.Click += delegate { SetMaxTabs(_maxtabs - 1); };
        wrap.Children.Add(_maxMinus);
        _maxValue = new TextBlock(); _maxValue.VerticalAlignment = VerticalAlignment.Center;
        _maxValue.FontSize = 13; _maxValue.FontWeight = FontWeights.SemiBold;
        _maxValue.Margin = new Thickness(8, 0, 8, 0); _maxValue.MinWidth = 14;
        _maxValue.TextAlignment = TextAlignment.Center;
        wrap.Children.Add(_maxValue);
        _maxPlus = MiniButton("+");
        _maxPlus.Click += delegate { SetMaxTabs(_maxtabs + 1); };
        wrap.Children.Add(_maxPlus);
        return wrap;
    }
    TextBlock _maxValue;

    // Toggle label/colour: ON => accent bg + white text (contrast rule for saturated bg),
    // OFF => neutral themed button.
    void PaintAutoToggle()
    {
        if (_autoToggle == null) return;
        _autoToggle.Content = T("autoscale") + ": " + (_autoscale ? T("auto_on") : T("auto_off"));
        if (_autoscale) { _autoToggle.Background = Accent; _autoToggle.Foreground = White; _autoToggle.BorderBrush = Accent; }
        else { _autoToggle.Background = BtnBg; _autoToggle.Foreground = Fg; _autoToggle.BorderBrush = Border; }
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
        _autoMax = Math.Max(1, Math.Min(8, v));
        SaveKey("autoscale_max", _autoMax.ToString());
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        // live-apply only while autoscale is on (ceiling is meaningless otherwise).
        if (_autoscale) RequestSetAutoscaleIfLive();
    }

    // true iff a run is currently LIVE (running and not idle).
    bool RunIsLive()
    {
        try
        {
            var st = ReadStatus();
            return st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                   && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]));
        }
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
        _maxtabs = Math.Max(1, Math.Min(8, v));
        SaveKey("maxtabs", _maxtabs.ToString());
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        // if a run is live, offer to apply now vs next run
        bool running = RunIsLive();
        if (running && _autoscale)
        {
            // under autoscale, the default reseats the live cap immediately (push now,
            // no banner -- the ceiling still governs growth).
            RequestSetAutoscale(true, Math.Min(_maxtabs, _autoMax), _autoMax);
            if (_mtBanner != null) _mtBanner.Visibility = Visibility.Collapsed;
        }
        else if (running && _mtBanner != null)
        {
            _mtBannerLbl.Text = (_lang == 0 ? "最大タブを " : "Max tabs -> ") + _maxtabs
                + (_lang == 0 ? " に変更しました。" : ".");
            _mtBanner.Visibility = Visibility.Visible;
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

    Button MiniButton(string txt)
    {
        var b = new Button(); b.Content = txt; b.Width = 26; b.Height = 26;
        b.FontSize = 15; b.Cursor = Cursors.Hand; b.BorderThickness = new Thickness(1);
        b.Padding = new Thickness(0); b.VerticalContentAlignment = VerticalAlignment.Center;
        return b;
    }
    Button IconButton(string glyph, double size)
    {
        var b = new Button(); b.Width = 36; b.Height = 30; b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1); b.Margin = new Thickness(4, 0, 0, 0);
        b.Content = MakeIcon(glyph, size, Fg); b.Tag = glyph;
        return b;
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        _header.Foreground = Fg;
        _sub.Foreground = Muted;
        if (_list != null) _list.Background = Bg;
        _iconHost.Content = MakeIcon("satellite_alt", 26, Accent);
        // restyle the header buttons for the theme
        foreach (Button b in new Button[] { _mainBtn, _themeBtn, _langBtn, _maxMinus, _maxPlus, _autoMinus, _autoPlus })
            if (b != null) { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        _langBtn.Content = MakeIcon("translate", 18, Fg);
        if (_maxLbl != null) _maxLbl.Foreground = Muted;
        if (_maxValue != null) _maxValue.Foreground = Fg;
        if (_autoLbl != null) _autoLbl.Foreground = Muted;
        if (_autoValue != null) _autoValue.Foreground = Fg;
        PaintAutoToggle();
        UpdateAutoEnabled();
        PaintEffort();
        PaintPause();
        if (_stopBtn != null) { _stopBtn.Background = BtnBg; _stopBtn.Foreground = Fg; _stopBtn.BorderBrush = Border; }
        if (_inBar != null) _inBar.Background = Bg;
        if (_goalInput != null)
        {
            _goalInput.Background = BtnBg; _goalInput.Foreground = Fg;
            _goalInput.BorderBrush = Border; _goalInput.CaretBrush = Fg;
        }
        if (_startBtn != null) { _startBtn.Background = Accent; _startBtn.Foreground = White; }
        if (_folderBtn != null) { _folderBtn.Background = BtnBg; _folderBtn.Foreground = Fg; _folderBtn.BorderBrush = Border; }
        if (_startNote != null) _startNote.Foreground = Muted;
        if (_mtBanner != null)
        {
            _mtBanner.Background = new SolidColorBrush(Mix(C("#ea580c"), CardColor(), 0.12));
            _mtBanner.BorderBrush = Accent;
            if (_mtBannerLbl != null) _mtBannerLbl.Foreground = Fg;
        }
        if (_mtApplyNow != null) { _mtApplyNow.Background = Accent; _mtApplyNow.Foreground = White; }
        if (_mtLater != null) { _mtLater.Background = BtnBg; _mtLater.Foreground = Fg; _mtLater.BorderBrush = Border; }
        Relabel();
    }

    void Relabel()
    {
        if (_maxLbl != null) _maxLbl.Text = T("def_tabs");
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        if (_autoLbl != null) _autoLbl.Text = T("max_tabs2");
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        PaintAutoToggle();
        PaintEffort();
        PaintPause();
        if (_stopBtn != null) _stopBtn.Content = T("stopall");
        if (_startBtn != null) _startBtn.Content = T("start");
        if (_folderBtn != null) _folderBtn.Content = T("folder");
        if (_goalInput != null) _goalInput.ToolTip = T("goalhint");
        if (_startNote != null && string.IsNullOrEmpty(_startNote.Text)) _startNote.Text = T("goalhint");
        if (_mtApplyNow != null) _mtApplyNow.Content = _lang == 0 ? "今すぐ反映" : "Apply now";
        if (_mtLater != null) _mtLater.Content = _lang == 0 ? "次回起動から" : "Next run";
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
                    bool d0 = _dark; int l0 = _lang;
                    LoadSettings();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { Relabel(); _lastSig = ""; }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> root = ReadStatus();
        bool idle = root == null || I(root, "total") == 0
                    || (root.ContainsKey("idle") && Convert.ToBoolean(root["idle"]));
        if (idle)
        {
            _header.Text = T("title");
            _sub.Text = T("idle");
            string isig = "IDLE" + _history.Count + (_dark ? "D" : "L") + _lang;
            if (_lastSig != isig)
            {
                _lastRoot = null;
                var rows = new List<object>();
                AppendHistoryRows(rows);   // history header + rows, if any
                SetRows(rows);
                _lastSig = isig;
            }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        // only archive while the run is LIVE -- otherwise the finished run's final
        // snapshot would re-add cleared tasks every tick (Clear would never stick).
        bool runningNow = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        if (runningNow) ArchiveTerminal(root);
        // opt-in auto-retry runs every tick (before the sig short-circuit) so it catches a
        // stopped goal even when nothing else changed. Bounded by _autoRetryMax per goal text.
        if (runningNow && _autoRetry) AutoRetryScan(root);
        string sig = Sig(root);
        if (sig == _lastSig) return;
        _lastSig = sig;
        RenderCards(root);
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
    static int I(Dictionary<string, object> d, string k)
    { try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { } return 0; }
    static double Dbl(Dictionary<string, object> d, string k)
    { try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToDouble(d[k]); } catch (Exception) { } return 0; }

    string Sig(Dictionary<string, object> root)
    {
        var sb = new StringBuilder();
        sb.Append(_dark ? "D" : "L").Append(_lang).Append('|');
        sb.Append("h").Append(_history.Count).Append('|');
        sb.Append(S(root, "done_count")).Append('/').Append(S(root, "total")).Append('|');
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var w = (Dictionary<string, object>)o;
                string nm = S(w, "name");
                sb.Append(nm).Append(S(w, "status")).Append(S(w, "turn"));
                // only an EXPANDED card shows live progress text, so only its `last`-length
                // changes need to force a re-render. Collapsed cards stay put while their
                // worker streams -- that's what keeps a 164-task fleet from thrashing.
                if (_expanded.Contains(nm)) sb.Append('#').Append((S(w, "last")).Length);
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

        _header.Text = T("title") + " — " + done + " / " + total + " " + T("done");

        int maxc = I(root, "max_concurrent");
        int openTabs = I(root, "open_tabs");
        int availMb = I(root, "avail_mb");
        string mem = maxc > 0 ? ("    " + openTabs + "/" + maxc + " " + T("concurrent")
                     + (availMb > 0 ? "（" + T("freeram") + " " + availMb + "MB・" + T("freed") + "）" : "")) : "";
        string state = running ? T("running") : T("done");
        if (running && updated > 0 && (NowUnix() - updated) > 8)
            state = T("running") + " — " + T("stale");
        // Live ETA: project remaining work from the throughput observed SO FAR (done/elapsed). This
        // implicitly tracks concurrency -- when 2 run in parallel, done climbs faster -> rate up ->
        // ETA drops. Recomputed every tick (and finish-clock uses DateTime.Now), so it's the
        // "constantly-moving number". Needs >=1 done to have a rate; until then show 計測中.
        string eta = "";
        if (running && total > 0 && done < total)
        {
            if (done > 0 && elapsed > 1.0)
            {
                double etaS = (total - done) * (elapsed / done);   // remaining / (done/elapsed)
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
        _sub.Text = state + "    " + T("elapsed") + " " + Fmt(elapsed) + eta + "    " + total + " " + T("goals") + mem;
        // The subtitle is ellipsis-trimmed at the column edge; expose the full text on hover.
        _sub.ToolTip = _sub.Text;
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

        if (off > 0.0)
        {
            double target = off;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 != null) s2.ScrollToVerticalOffset(target);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }
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
            if (_cardFilter == 1 && oc == "DONE") continue;
            if (_cardFilter == 2 && oc != "DONE") continue;
            shown.Add(w);
        }
        // Feature B: under "unfinished only", group failures together by severity (stable).
        // Otherwise use the default display order: active worker(s) at the top, queued in the
        // middle, completed sinking to the bottom (the live work is what the user wants to see).
        if (_cardFilter == 1) shown = StableBySeverity(shown);
        else shown = StableByDisplayRank(shown);

        // stash for the converter (it rebuilds the toolbar row from these when a container recycles)
        _toolbarAll = workers;
        _toolbarShown = shown;

        var rows = new List<object>();
        rows.Add(MkRow(0, null, null));               // toolbar
        // Default view: the live area shows only ACTIVE/queued work; terminal (done/failed) workers
        // drop below a "完了 (this run)" divider so the top is just what's running -- they are not
        // deleted (still inspectable below), only moved out of the active list. Filters 1/2 are
        // explicit views, so they don't re-partition.
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
        AppendHistoryRows(rows);
        return rows;
    }

    // Append a history-header row + one history row per entry (newest first) to the row model.
    void AppendHistoryRows(List<object> rows)
    {
        if (_history.Count == 0) return;
        rows.Add(MkRow(2, null, null));               // history header
        for (int i = _history.Count - 1; i >= 0; i--)
        {
            var e = _history[i] as Dictionary<string, object>;
            if (e == null) continue;
            rows.Add(MkRow(3, null, e));             // one history row per entry
        }
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
            case 0:  // toolbar: reflects global counts + the local control state it renders
                return "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                       + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
            case 2: return "HH|" + g;                          // history header (static chrome)
            case 4: return "DV|" + g;                          // "完了 (this run)" divider
            case 3:                                            // history row: stable per entry
                return "h|" + g + "|" + (hist != null ? RuntimeHelpers.GetHashCode(hist) : 0);
            default:                                           // kind 1: worker card
                string nm = S(w, "name");
                var sb = new StringBuilder("c|");
                sb.Append(g).Append('|').Append(nm)
                  .Append(S(w, "status")).Append(S(w, "turn")).Append(S(w, "outcome"));
                // only an EXPANDED card shows live progress text, so only then does `last` length
                // matter; a collapsed card stays put while its worker streams (no thrash).
                if (_expanded.Contains(nm)) sb.Append("#E").Append((S(w, "last")).Length);
                else sb.Append("#x");
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
    UIElement BuildCardToolbar(List<Dictionary<string, object>> all,
                               List<Dictionary<string, object>> shown)
    {
        int doneN = 0, maxN = 0, badN = 0;
        foreach (Dictionary<string, object> w in all)
        {
            string oc = S(w, "outcome");
            if (oc == "DONE") doneN++;
            else if (oc == "MAXTURNS") maxN++;
            else if (oc == "STUCK" || oc == "ERROR" || oc == "CANCELLED") badN++;
        }

        var bar = new Border();
        bar.BorderThickness = new Thickness(1); bar.BorderBrush = Border;
        bar.Background = CardBg; bar.CornerRadius = new CornerRadius(10);
        bar.Padding = new Thickness(12, 8, 12, 8); bar.Margin = new Thickness(8, 2, 8, 8);
        // clicks inside the toolbar must not bubble (it isn't a card, but stay safe)
        bar.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };

        var dp = new DockPanel();

        // right cluster: [auto-retry toggle (opt-in)] [cap −N+] [bulk retry]
        var rightCl = new StackPanel(); rightCl.Orientation = Orientation.Horizontal;
        rightCl.VerticalAlignment = VerticalAlignment.Center;

        // opt-in, CAPPED auto-retry toggle. Default OFF. Never infinite (see _autoRetryMax).
        _autoRetryBtn = new Button();
        _autoRetryBtn.BorderThickness = new Thickness(1); _autoRetryBtn.Cursor = Cursors.Hand;
        _autoRetryBtn.Padding = new Thickness(10, 4, 10, 4); _autoRetryBtn.FontSize = 12;
        _autoRetryBtn.FontWeight = FontWeights.SemiBold; _autoRetryBtn.Margin = new Thickness(0, 0, 8, 0);
        _autoRetryBtn.VerticalAlignment = VerticalAlignment.Center;
        _autoRetryBtn.ToolTip = _lang == 0
            ? "停止したゴールを自動で再投入（上限まで・既定OFF）。無限ループは起きません。"
            : "Auto re-queue stopped goals (up to the cap; default OFF). Never loops forever.";
        PaintAutoRetryBtn();
        _autoRetryBtn.Click += delegate { _autoRetry = !_autoRetry; SaveKey("autoretry", _autoRetry ? "1" : "0"); PaintAutoRetryBtn(); };
        rightCl.Children.Add(_autoRetryBtn);

        // per-goal retry cap (1..3) -- the safety bound
        var capLbl = new TextBlock(); capLbl.Text = T("cap"); capLbl.Foreground = Muted;
        capLbl.FontSize = 11.5; capLbl.VerticalAlignment = VerticalAlignment.Center; capLbl.Margin = new Thickness(0, 0, 6, 0);
        rightCl.Children.Add(capLbl);
        var capMinus = MiniButton("−"); capMinus.Click += delegate { SetAutoRetryMax(_autoRetryMax - 1); };
        rightCl.Children.Add(capMinus);
        _autoRetryCapVal = new TextBlock(); _autoRetryCapVal.Text = _autoRetryMax.ToString();
        _autoRetryCapVal.Foreground = Fg; _autoRetryCapVal.FontSize = 13; _autoRetryCapVal.FontWeight = FontWeights.SemiBold;
        _autoRetryCapVal.Margin = new Thickness(6, 0, 6, 0); _autoRetryCapVal.MinWidth = 12;
        _autoRetryCapVal.TextAlignment = TextAlignment.Center; _autoRetryCapVal.VerticalAlignment = VerticalAlignment.Center;
        rightCl.Children.Add(_autoRetryCapVal);
        var capPlus = MiniButton("+"); capPlus.Click += delegate { SetAutoRetryMax(_autoRetryMax + 1); };
        rightCl.Children.Add(capPlus);

        // bulk MANUAL retry button (one-shot, respects the active filter)
        var retryAll = new Button();
        retryAll.Content = T("retry_all");
        retryAll.Background = Accent; retryAll.Foreground = White; retryAll.BorderThickness = new Thickness(0);
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

        DockPanel.SetDock(rightCl, Dock.Right);
        dp.Children.Add(rightCl);

        // left: segmented filter buttons + summary + (optional) live-only note
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        left.VerticalAlignment = VerticalAlignment.Center;
        left.Children.Add(FilterButton(T("flt_all"), 0));
        left.Children.Add(FilterButton(T("flt_unfinished"), 1));
        left.Children.Add(FilterButton(T("flt_done"), 2));

        var summary = new TextBlock();
        summary.Text = (_lang == 0
            ? ("完了 " + doneN + " ・ 上限 " + maxN + " ・ 停止/失敗 " + badN)
            : ("Done " + doneN + " · MaxTurns " + maxN + " · Stuck/Err " + badN));
        summary.Foreground = Muted; summary.FontSize = 12;
        summary.VerticalAlignment = VerticalAlignment.Center;
        summary.Margin = new Thickness(14, 0, 0, 0);
        left.Children.Add(summary);

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

    // ON => accent fill + white text (contrast); OFF => neutral. Rebuilt with the toolbar.
    void PaintAutoRetryBtn()
    {
        if (_autoRetryBtn == null) return;
        _autoRetryBtn.Content = T("autoretry") + ": " + (_autoRetry ? "ON" : "OFF");
        if (_autoRetry)
        {
            _autoRetryBtn.Background = Accent; _autoRetryBtn.Foreground = White; _autoRetryBtn.BorderBrush = Border;
        }
        else
        {
            _autoRetryBtn.Background = BtnBg; _autoRetryBtn.Foreground = Fg; _autoRetryBtn.BorderBrush = Border;
        }
    }

    void SetAutoRetryMax(int v)
    {
        _autoRetryMax = Math.Max(1, Math.Min(3, v));   // hard safety bound: 1..3, never unbounded
        SaveKey("autoretry_max", _autoRetryMax.ToString());
        if (_autoRetryCapVal != null) _autoRetryCapVal.Text = _autoRetryMax.ToString();
    }

    // One segmented filter button. The active one gets the accent fill (white text for
    // contrast); inactive ones are neutral themed. Changing the filter forces a re-render.
    Button FilterButton(string label, int val)
    {
        var b = new Button();
        b.Content = label; b.Cursor = Cursors.Hand; b.FontSize = 12;
        b.Padding = new Thickness(10, 3, 10, 3); b.Margin = new Thickness(0, 0, 6, 0);
        b.BorderThickness = new Thickness(1);
        if (_cardFilter == val)
        { b.Background = Accent; b.Foreground = White; b.BorderBrush = Accent; b.FontWeight = FontWeights.SemiBold; }
        else
        { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        int v = val;
        b.Click += delegate
        {
            if (_cardFilter == v) return;
            _cardFilter = v;
            _lastSig = ""; OnTick(null, null);
        };
        return b;
    }

    // The history SECTION HEADER (Clear button + caption). Factored out of the old AppendHistory
    // so the virtualizing converter can build it for a Kind==2 row. The history ROWS are built
    // separately by HistoryRow() per Kind==3 row.
    UIElement HistoryHeader()
    {
        var head = new DockPanel();
        head.Margin = new Thickness(8, 18, 8, 4);
        var clear = new Button();
        clear.Content = (_lang == 0 ? "クリア (" : "Clear (") + _history.Count + ")";
        clear.Cursor = Cursors.Hand; clear.BorderThickness = new Thickness(1);
        clear.Background = BtnBg; clear.Foreground = Fg; clear.BorderBrush = Border;
        clear.Padding = new Thickness(10, 2, 10, 2); clear.FontSize = 12;
        clear.Click += delegate { ClearHistory(); };
        DockPanel.SetDock(clear, Dock.Right);
        head.Children.Add(clear);
        var ht = new TextBlock();
        ht.Text = (_lang == 0 ? "履歴 — クリアするまで蓄積（クリックで会話を表示）" : "History — stacks until cleared (click to open)");
        ht.Foreground = Muted; ht.FontSize = 12.5; ht.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(ht);
        return head;
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
        var pill = Pill(StatusLabel(status), ck);
        pill.Margin = new Thickness(0, 0, 10, 0);
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
            row.MouseLeftButtonUp += delegate { OpenHistory(nm, url, tx); };
        }
        return row;
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string status = S(w, "status");
        string reason = S(w, "reason");
        string last = S(w, "last");
        string conv = S(w, "conv_url");
        string convTitle = S(w, "conv_title");
        int turn = I(w, "turn");
        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                        || status == "error" || status == "cancelled";

        string ck = ColorKey(status);
        Color sc = StatusColor(ck);

        var card = new Border();
        card.Tag = name;                // lets a chevron toggle find & replace just this card
        card.BorderThickness = new Thickness(1.4);
        card.CornerRadius = new CornerRadius(12);
        card.Padding = new Thickness(18, 13, 16, 13);
        card.Margin = new Thickness(8, 7, 8, 7);
        if (ck == "muted")
        {
            card.BorderBrush = Border; card.Background = CardBg;
        }
        else
        {
            // soft frame + faint tint -- never a harsh fill
            card.BorderBrush = new SolidColorBrush(Mix(sc, BgColor(), 0.55));
            card.Background = new SolidColorBrush(Mix(sc, CardColor(), 0.10));
        }

        var col = new StackPanel();

        // top row: [name] [pill] [dots] ........... [turn] [release]
        var top = new DockPanel();

        // right cluster first (DockPanel right docks)
        var right = new StackPanel(); right.Orientation = Orientation.Horizontal;
        right.HorizontalAlignment = HorizontalAlignment.Right;
        var turns = new TextBlock();
        turns.Text = T("turn") + " " + turn;
        turns.Foreground = Muted; turns.FontSize = 12;
        turns.VerticalAlignment = VerticalAlignment.Center;
        turns.Margin = new Thickness(0, 0, 10, 0);
        right.Children.Add(turns);
        if (closed)
        {
            var rel = new TextBlock();
            rel.Text = T("released");
            rel.Foreground = Muted; rel.FontSize = 12; rel.VerticalAlignment = VerticalAlignment.Center;
            right.Children.Add(rel);
        }
        else if (terminal)
        {
            // finished card -> a working button that moves it to HISTORY client-side (the old
            // "解放済" text was a dead end; this needs no live fleet).
            var arch = new Button();
            var al = new TextBlock { Text = "→ " + T("to_history"), Foreground = Fg, FontSize = 12 };
            arch.Content = al;
            arch.Cursor = Cursors.Hand; arch.BorderThickness = new Thickness(1);
            arch.Background = BtnBg; arch.BorderBrush = Border; arch.Foreground = Fg;
            arch.Padding = new Thickness(8, 2, 10, 2);
            arch.ToolTip = _lang == 0 ? "このカードを履歴へ移動" : "Move this card to history";
            var wt = w;
            arch.Click += delegate { ArchiveAndHide(wt); };
            right.Children.Add(arch);
        }
        else
        {
            var relBtn = new Button();
            relBtn.Content = MakeReleaseContent();
            relBtn.Cursor = Cursors.Hand; relBtn.BorderThickness = new Thickness(1);
            relBtn.Background = BtnBg; relBtn.BorderBrush = Border; relBtn.Foreground = Fg;
            relBtn.Padding = new Thickness(8, 2, 10, 2);
            relBtn.ToolTip = _lang == 0 ? "このタスクを停止してタブを解放（fleet 停止中ならカードを片付け）"
                                        : "Stop this task and release its tab (clears the card if the fleet is stopped)";
            string nm = name; var wt = w;
            relBtn.Click += delegate {
                RequestClose(nm);
                // No live fleet to consume the close command (status went stale) -> clear it now so
                // a frozen worker (e.g. a verifying tab left by a stopped run) doesn't stick forever.
                if (_lastRoot != null
                    && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                    && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                    ArchiveAndHide(wt);
            };
            right.Children.Add(relBtn);
        }
        DockPanel.SetDock(right, Dock.Right);
        top.Children.Add(right);

        // left cluster: [chevron] name + pill (+ dots when running)
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        bool isOpen = _expanded.Contains(name);
        left.Children.Add(ChevronToggle(name, isOpen));
        var nm2 = new TextBlock();
        nm2.Text = name.ToUpper();
        nm2.Foreground = Accent; nm2.FontWeight = FontWeights.Bold; nm2.FontSize = 13;
        nm2.VerticalAlignment = VerticalAlignment.Center; nm2.Margin = new Thickness(0, 0, 10, 0);
        left.Children.Add(nm2);
        left.Children.Add(Pill(StatusLabel(status), ck));
        if (status == "waiting") left.Children.Add(Dots());
        top.Children.Add(left);

        col.Children.Add(top);

        // Concise headline title (Copilot conv_title if captured, else derived from the goal) --
        // ALWAYS shown so the card stays readable. The long goal text only appears when expanded,
        // so a wall of issue text never wrecks visibility (the reported problem).
        string headline = CardTitle(convTitle, goal);
        if (!string.IsNullOrEmpty(headline))
        {
            var ht = new TextBlock();
            ht.Text = headline;
            ht.Foreground = Fg; ht.FontSize = 14; ht.FontWeight = FontWeights.SemiBold;
            ht.TextTrimming = TextTrimming.CharacterEllipsis;
            ht.Margin = new Thickness(0, 6, 0, 0);
            col.Children.Add(ht);
        }

        if (isOpen)
        {
            // expanded: full goal, wrapped, as secondary (muted) text -- selectable/copyable
            // read-only TextBox (Feature A). The headline above is the primary label.
            var g = new TextBox();
            g.Text = goal;
            g.Foreground = Muted; g.FontSize = 12.5;
            g.IsReadOnly = true; g.BorderThickness = new Thickness(0);
            g.Background = Brushes.Transparent; g.Padding = new Thickness(0);
            g.IsTabStop = false; g.TextWrapping = TextWrapping.Wrap;
            g.Margin = new Thickness(0, 8, 0, 8);
            SwallowMouseUp(g);
            col.Children.Add(g);
        }

        // Heavy detail (live progress quote + steer TextBox) ONLY when expanded. This is the
        // whole point: a collapsed fleet of 100+ tasks builds no quotes and no input controls,
        // and -- via Sig() -- doesn't even re-render while a collapsed worker streams.
        if (isOpen)
        {
            string body = !string.IsNullOrEmpty(last) ? last : reason;
            if (!string.IsNullOrEmpty(body))
            {
                var quote = new Border();
                quote.Background = QuoteBg; quote.CornerRadius = new CornerRadius(8);
                quote.Padding = new Thickness(12, 10, 12, 10);
                quote.Margin = new Thickness(0, 0, 0, 0);
                // selectable/copyable read-only TextBox (Feature A)
                var bt = new TextBox();
                bt.Text = body; bt.Foreground = Muted; bt.FontSize = 12.5;
                bt.IsReadOnly = true; bt.BorderThickness = new Thickness(0);
                bt.Background = Brushes.Transparent; bt.Padding = new Thickness(0);
                bt.IsTabStop = false; bt.TextWrapping = TextWrapping.Wrap; bt.MaxHeight = 120;
                bt.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
                SwallowMouseUp(bt);
                quote.Child = bt;
                col.Children.Add(quote);
            }

            if (!terminal) col.Children.Add(SteerRow(name));
            // Feature C: retry stopped tasks -- terminal & NOT DONE gets a Retry button.
            else if (S(w, "outcome") != "DONE") col.Children.Add(RetryRow(w));
        }

        card.Child = col;
        // Always clickable: open this worker in the main chat BY NAME, so it works even when the
        // Copilot conv_url was never captured (the main chat renders the live status.json snapshot
        // for this worker). conv_url is passed too so /history can fill in the full transcript
        // when it is available.
        card.Cursor = Cursors.Hand;
        string wname = name; string url = conv;
        card.MouseLeftButtonUp += delegate { OpenWorker(wname, url); };
        card.ToolTip = _lang == 0 ? "クリックでこの会話をメインに表示" : "Click to open this conversation in the chat";
        return card;
    }

    // ② steering: inject a mid-task instruction into this worker's conversation.
    UIElement SteerRow(string name)
    {
        var dp = new DockPanel();
        dp.Margin = new Thickness(0, 10, 0, 0);
        // clicks inside this row must not bubble to the card's open-conversation handler
        dp.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var send = new Button();
        send.Content = _lang == 0 ? "割り込み" : "Steer";
        send.Background = Accent; send.Foreground = White; send.BorderThickness = new Thickness(0);
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(8, 5, 8, 5);
        tb.BorderThickness = new Thickness(1); tb.Background = BtnBg; tb.Foreground = Fg;
        tb.BorderBrush = Border; tb.CaretBrush = Fg; tb.Margin = new Thickness(0, 0, 8, 0);
        tb.ToolTip = _lang == 0 ? "回答待ち中でも割り込み指示を送れます（次のターンに最優先で反映）"
                                : "Inject a steering instruction (applied on the next turn)";
        string nm = name;
        send.Click += delegate
        {
            string t = (tb.Text ?? "").Trim();
            if (t.Length > 0) { RequestSteer(nm, t); tb.Text = ""; }
        };
        tb.KeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return)
            {
                string t = (tb.Text ?? "").Trim();
                if (t.Length > 0) { RequestSteer(nm, t); tb.Text = ""; }
                e.Handled = true;
            }
        };
        dp.Children.Add(tb);
        return dp;
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

    Border Pill(string text, string ck)
    {
        var b = new Border();
        b.Background = new SolidColorBrush(StatusColor(ck));
        b.CornerRadius = new CornerRadius(999);
        b.Padding = new Thickness(11, 3, 11, 3);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = White;          // saturated bg -> white text
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
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
        hit.Padding = new Thickness(8, 8, 12, 8);
        hit.MinWidth = 28; hit.MinHeight = 28;           // ~28x28 px target -- far easier to click
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
        if (!string.IsNullOrEmpty(convTitle)) return Trunc(convTitle, 90);
        if (string.IsNullOrEmpty(goal)) return "";
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
}
