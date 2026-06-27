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
    TextBlock _workerChip;       // live "N workers" neutral chip in the header controls row
    Border _workerChipBorder;   // the Border wrapping _workerChip (for PaintChrome re-theming)
    Button _themeBtn, _langBtn, _mainBtn, _siBtn;
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
        Title = "Fleet Cockpit";   // also lets the taskbar / alt-tab / automation name this window
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
        if (k == "pause") return ja ? "一時停止" : "Pause";
        if (k == "resume") return ja ? "再開" : "Resume";
        if (k == "stopall") return ja ? "全停止" : "Stop all";
        if (k == "steer_dead") return ja ? "走行が停止中のため割り込めません（再開後にどうぞ）" : "No run live — can't steer (resume the fleet first)";
        // Capacity-wait banner (admission gate) + force-start
        // Settings panel (gear popup) -- consolidates the scattered toolbar controls
        if (k == "settings") return ja ? "設定" : "Settings";
        if (k == "set_tabs_section") return ja ? "並列タブ" : "Parallel tabs";
        if (k == "set_retry_section") return ja ? "自動再試行" : "Auto-retry";
        if (k == "set_capacity_section") return ja ? "容量ガード" : "Capacity guard";
        if (k == "disk_floor") return ja ? "実行下限ディスク (GB)" : "Disk floor (GB)";
        if (k == "disk_floor_hint") return ja ? "空きディスクがこの値を下回るとタブ開放を待機します。" : "Pauses opening tabs when free disk drops below this.";
        if (k == "ram_floor") return ja ? "確保する空きRAM (MB)" : "RAM floor (MB)";
        if (k == "force_start") return ja ? "今すぐ開始" : "Start now";
        if (k == "floor_restore") return ja ? "容量制限を戻す" : "Restore limit";
        if (k == "floor_off") return ja ? "容量制限を一時解除しています" : "Capacity limit paused";
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
                else if (ln.StartsWith("maxtabs=") && int.TryParse(ln.Substring(8).Trim(), out v)) _maxtabs = Math.Max(1, Math.Min(100, v));
                else if (ln.StartsWith("autoscale_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoMax = Math.Max(1, Math.Min(100, v));
                else if (ln.StartsWith("autoscale=")) _autoscale = ln.Substring(10).Trim() == "1";
                else if (ln.StartsWith("autoretry_max=") && int.TryParse(ln.Substring(14).Trim(), out v)) _autoRetryMax = Math.Max(1, Math.Min(3, v));
                else if (ln.StartsWith("autoretry=")) _autoRetry = ln.Substring(10).Trim() == "1";
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

        // right controls: autoscale group, language, theme  (row 0, col 1)
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;
        ctrls.HorizontalAlignment = HorizontalAlignment.Right;

        // "N workers" neutral info chip (live count updated in OnTick; shows maxtabs until first tick)
        _workerChip = new TextBlock();
        _workerChip.FontSize = 12; _workerChip.VerticalAlignment = VerticalAlignment.Center;
        _workerChip.Margin = new Thickness(0, 0, 12, 0);
        _workerChip.Padding = new Thickness(10, 3, 10, 3);
        UpdateWorkerChip(0, false);   // initial paint
        _workerChipBorder = new Border();
        _workerChipBorder.Child = _workerChip;
        _workerChipBorder.BorderThickness = new Thickness(1);
        _workerChipBorder.CornerRadius = new CornerRadius(Theme.RadSmall);
        _workerChipBorder.Padding = new Thickness(0);
        _workerChipBorder.VerticalAlignment = VerticalAlignment.Center;
        _workerChipBorder.Margin = new Thickness(0, 0, 12, 0);
        PaintWorkerChipBorder(_workerChipBorder);
        ctrls.Children.Add(_workerChipBorder);

        ctrls.Children.Add(AutoscaleControls());
        ctrls.Children.Add(EffortControl());
        ctrls.Children.Add(ApprovalControl());
        ctrls.Children.Add(FleetControls());
        // gear -> settings popup consolidating the scattered start/上限/retry/disk-floor controls.
        ctrls.Children.Add(SettingsControl());
        _mainBtn = IconButton("chat", 18);
        _mainBtn.ToolTip = _lang == 0 ? "メイン (チャット) を開く" : "Open main chat";
        _mainBtn.Click += delegate { OpenMain(); };
        ctrls.Children.Add(_mainBtn);
        _siBtn = IconButton("account_tree", 18);
        _siBtn.ToolTip = _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement dashboard";
        _siBtn.Click += delegate { new SelfImproveDashboardWindow().Show(); };
        ctrls.Children.Add(_siBtn);
        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); RebuildChrome(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = _lang == 0 ? "テーマ (ダーク/ライト)" : "Theme (dark/light)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyTheme(); };
        ctrls.Children.Add(_themeBtn);
        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headRow.Children.Add(ctrls);

        // title -- row 0, col 0 (satellite icon removed per UX feedback; the title now sits flush left)
        var titleRow = new DockPanel { LastChildFill = true };
        titleRow.VerticalAlignment = VerticalAlignment.Center;
        titleRow.Margin = new Thickness(0, 0, 12, 0);
        _header = new TextBlock(); _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis; _header.TextWrapping = TextWrapping.NoWrap;
        titleRow.Children.Add(_header);    // fills the rest of col 0
        Grid.SetColumn(titleRow, 0); Grid.SetRow(titleRow, 0);
        headRow.Children.Add(titleRow);

        // subtitle -- its OWN row spanning BOTH columns, so the long elapsed+ETA line uses the full
        // width and is never clipped by the controls column. Wrap (not ellipsis) so it's never hidden.
        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 18, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        headRow.Children.Add(_sub);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);
        root.Children.Add(BuildMtBanner());
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
    Border _composerBox;             // the rounded composer surface (themed in PaintChrome)
    TextBlock _composerHint;         // footer "one goal per line · / for commands"
    TextBlock _composerWatermark;    // placeholder shown when the textarea is empty

    // ④ task-injection: a bottom-docked composer (spec: agent-workspace, not a form). Type goals
    // (one per line) and launch a fleet. The big top textarea + right-side vertical button stack
    // are gone; the box now sits at the bottom with an inline footer (folder=secondary, start=primary).
    UIElement BuildInputBar()
    {
        _inBar = new Border();
        _inBar.Padding = new Thickness(Theme.PadApp, 6, Theme.PadApp, Theme.PadApp);
        DockPanel.SetDock(_inBar, Dock.Bottom);

        _composerBox = new Border();
        _composerBox.CornerRadius = new CornerRadius(Theme.RadComposer);
        _composerBox.BorderThickness = new Thickness(1);
        _composerBox.Padding = new Thickness(12, 10, 12, 10);

        var col = new StackPanel();

        // ── textarea + watermark overlay (a Grid so they stack in the same cell) ──
        var taGrid = new Grid();
        _goalInput = new TextBox();
        _goalInput.AcceptsReturn = true; _goalInput.TextWrapping = TextWrapping.Wrap;
        _goalInput.MinHeight = 64; _goalInput.MaxHeight = 180;   // spec: min 64, max 180 internal scroll
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
            { e.Handled = true; StartFleet(); }
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
        footer.Margin = new Thickness(0, 8, 0, 0);

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
        _startBtn.Click += delegate { StartFleet(); };
        btns.Children.Add(_startBtn);
        DockPanel.SetDock(btns, Dock.Right);
        footer.Children.Add(btns);

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

    // Focus ring: thicken/tint the composer border while the goal box has keyboard focus.
    void PaintComposerFocus(bool focused)
    {
        if (_composerBox == null) return;
        _composerBox.BorderBrush = focused ? Theme.Br(Theme.Accent(_dark)) : Border;
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
                _goalInput.Text = GoalHelpText();
                _goalInput.CaretIndex = _goalInput.Text.Length;
                _startNote.Text = _lang == 0 ? "コマンド一覧を入力欄に表示しました。" : "Command help expanded in the goal box.";
                return;
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

    // Spawn a fresh `python -m relay.fleet_runner` for the given goal texts. Factored out of
    // StartFleet so RETRY can reuse it: when a run has FINISHED, the cockpit relaunches a fleet
    // (instead of writing an add_goal that nothing alive would ever consume). The agent URL is
    // NOT passed -- the runner resolves it from MCP_FLEET_AGENT_URL / .env, exactly as a manual
    // Start does -- and --state-dir is the same .fleet dir this cockpit tails, so the relaunched
    // run shows up live here. Goals are handed over via a UTF-8 file to dodge arg-encoding issues.
    // Returns true if the process started. `goalsFileName` lets callers use a distinct file so a
    // retry spawn never clobbers the manual Start input file (or vice-versa).
    bool SpawnFleet(List<string> goals, string goalsFileName, bool planMode = false)
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
        if (planMode) psi.Arguments += " --plan";
        psi.WorkingDirectory = repo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
        System.Diagnostics.Process.Start(psi);
        return true;
    }

    // --- slash-command autocomplete for the goal box (parity with the main chat) ---
    Popup _gcmdPopup; ListBox _gcmdList;
    static readonly string[][] _goalCommandsJa = {
        new[]{"/help","コマンド一覧を表示"},
        new[]{"/code","<機能> を実装し、pytest テストも書いて通す"},
        new[]{"/fix","<ファイル> の <不具合> を直し、テストを通す"},
        new[]{"/test","<対象> の pytest テストを書く"},
        new[]{"/refactor","<対象> を読みやすくリファクタする(挙動は変えない)"},
        new[]{"/doc","<対象> の README/説明 を書く"},
        new[]{"/review","<対象> をレビューして問題点を箇条書きで挙げる"},
        new[]{"/research","<問い> を Claude で深掘り調査する"},
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
            if (sp != null && sp.Children.Count > 0 && sp.Children[0] is TextBlock
                && ((TextBlock)sp.Children[0]).Text == "/help")
                template = GoalHelpText();
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
        if (_lang != 0)
            return "Fleet goal-box commands:\n"
                + "/code <feature> - implement + pytest tests\n"
                + "/fix <target> - fix a bug + verify\n"
                + "/test <target> - add pytest tests\n"
                + "/refactor <target> - tidy up without changing behavior\n"
                + "/doc <target> - write README / docs\n"
                + "/review <target> - review and list issues\n"
                + "/research <question> - deep research\n"
                + "\nTop-bar settings:\n"
                + "Reasoning = min/max/ultra/auto\n"
                + "Approval = run/plan/auto (run=run now, plan=wait for plan approval, auto=plain fleet waits for plan approval; folder autonomy uses GO/ASK/STOP)";
        return "フリート入力欄コマンド:\n"
            + "/code <機能> - 実装と pytest テスト\n"
            + "/fix <対象> - 不具合修正と検証\n"
            + "/test <対象> - pytest テスト追加\n"
            + "/refactor <対象> - 挙動を変えず整理\n"
            + "/doc <対象> - README/説明を書く\n"
            + "/review <対象> - 問題点レビュー\n"
            + "/research <問い> - 深掘り調査\n"
            + "\n上部設定:\n"
            + "推論=min/max/ultra/auto\n"
            + "承認=run/plan/auto（run=即実行、plan=計画承認待ち、auto=通常フリートは計画承認待ち、自律コーディング(フォルダ)はGO/ASK/STOPゲート）";
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

    // Autoscale control GROUP (header): just [ RAM自動調整: ON/OFF ]. The 開始(デフォルト) and 上限
    // steppers were moved OUT of the header into the settings panel (gear popup); only the toggle
    // remains here. The toggle live-applies via RequestSetAutoscale/RequestSetMaxtabs; the steppers
    // in settings route through SetMaxTabs/SetAutoMax.
    UIElement AutoscaleControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 0, 12, 0);
        // GAP5: explain this dense cluster -- the two −/+ steppers were indistinguishable.
        group.ToolTip = _lang == 0
            ? "RAM自動調整=ON なら空きRAMに応じて並列タブ数を自動増減。開始タブ数・上限は設定（⚙）内。"
            : "Autoscale ON auto-adjusts parallel tabs to free RAM. Start count & ceiling live in Settings (⚙).";

        // a. autoscale ON/OFF toggle (simple themed button whose label flips)
        _autoToggle = new Button();
        _autoToggle.ToolTip = _lang == 0 ? "RAM空きに応じて並列タブ数を自動調整 (ON/OFF)" : "Auto-adjust parallel tabs to free RAM (ON/OFF)";
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

        // b+c. Both the start (開始/デフォルト) and ceiling (上限) steppers were RELOCATED into the
        //    settings panel (gear popup) to declutter the header. BuildSettingsPanel builds both and
        //    keeps the _max*/_auto* field refs, so PaintChrome/UpdateAutoEnabled/SetMaxTabs/SetAutoMax
        //    keep driving them. Only the autoscale ON/OFF toggle remains in the header.
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
        _gearBtn = IconButton("settings", 18);
        _gearBtn.ToolTip = _lang == 0 ? "設定（タブ数・再試行・容量床）" : "Settings (tabs / retry / disk floor)";
        _settingsPopup = new System.Windows.Controls.Primitives.Popup();
        _settingsPopup.PlacementTarget = _gearBtn;
        _settingsPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
        _settingsPopup.StaysOpen = false;          // click-away closes it
        _settingsPopup.AllowsTransparency = true;
        _gearBtn.Click += delegate
        {
            if (_settingsPopup.IsOpen) { _settingsPopup.IsOpen = false; return; }
            _settingsPopup.Child = BuildSettingsPanel();   // rebuild so theme/lang/values are fresh
            _settingsPopup.IsOpen = true;
        };
        return _gearBtn;
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

        card.Child = col;
        UpdateAutoEnabled();   // grey the ceiling stepper if autoscale is off
        return card;
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
        FillComboWithHelp(_effortBox, _effortModes, EffortHelp(), _effort);  // per-option hover help
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

    // Approval selector: compact run/plan/auto mode next to effort. This avoids adding another
    // header button in the already-dense fleet toolbar.
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
            ? "承認モード: run=すぐ実行、plan=計画承認待ち、auto=通常フリートは計画承認待ち/フォルダ自律はGO-ASK-STOP判定"
            : "Approval mode: run=run now, plan=wait for approval, auto=plain fleet waits for plan approval; folder autonomy uses GO/ASK/STOP";
        _approvalBox.Cursor = Cursors.Hand; _approvalBox.FontSize = 12;
        _approvalBox.FontWeight = FontWeights.SemiBold; _approvalBox.MinWidth = 74;
        _approvalBox.Padding = new Thickness(8, 2, 4, 2);
        _approvalBox.VerticalAlignment = VerticalAlignment.Center;
        FillComboWithHelp(_approvalBox, _approvalModes, ApprovalHelp(), _approval);  // per-option hover help
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
        if (_approvalLbl != null) { _approvalLbl.Text = T("approval"); _approvalLbl.Foreground = Muted; }
        if (_approvalBox == null) return;
        if (!Equals(ComboVal(_approvalBox), _approval)) ComboSelectVal(_approvalBox, _approval);
        _approvalBox.Background = BtnBg; _approvalBox.Foreground = Fg; _approvalBox.BorderBrush = Border;
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
        _pauseBtn.Cursor = Cursors.Hand; _pauseBtn.BorderThickness = new Thickness(1);
        _pauseBtn.Width = 32; _pauseBtn.Height = 32; _pauseBtn.Padding = new Thickness(0);
        _pauseBtn.Margin = new Thickness(0, 0, 8, 0);
        _pauseBtn.VerticalAlignment = VerticalAlignment.Center;
        _pauseIcon = new System.Windows.Shapes.Path { Stretch = Stretch.None, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        _pauseBtn.Content = _pauseIcon;
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
        _stopBtn.Cursor = Cursors.Hand; _stopBtn.BorderThickness = new Thickness(1);
        _stopBtn.Width = 32; _stopBtn.Height = 32; _stopBtn.Padding = new Thickness(0);
        _stopBtn.VerticalAlignment = VerticalAlignment.Center;
        _stopIcon = new System.Windows.Shapes.Path { Stretch = Stretch.None, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center,
            Data = Geometry.Parse("M3,3 H13 V13 H3 Z") };   // a stop square
        _stopBtn.Content = _stopIcon;
        _stopBtn.Click += delegate
        {
            var cmd = ReadCommands();
            cmd["stop"] = true;
            WriteCommands(cmd);
            // No live fleet to consume the stop (status went stale, e.g. a run was killed and its
            // last status froze with workers still shown as "running") -> clear every card NOW so the
            // button is never a no-op. Mirrors the per-card release stale path (search ArchiveAndHide).
            if (_lastRoot != null
                && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                ArchiveAllStale();
        };
        group.Children.Add(_stopBtn);

        PaintPause();
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
        if (_stopIcon != null) _stopIcon.Fill = Fg;
    }
    // Per-tick: Pause is enabled only when a run is LIVE (something to pause). When no run is live we
    // also drop a stale "paused" state so the label can never sit on "Resume" over a dead/absent run.
    void RefreshPauseEnabled(Dictionary<string, object> root)
    {
        if (_pauseBtn == null) return;
        bool live = Liveness(root) == 1;
        _pauseBtn.IsEnabled = live;
        _pauseBtn.Opacity = live ? 1.0 : 0.5;
        if (!live && _paused) { _paused = false; PaintPause(); }
    }

    // MaxTabsStepper() removed: the 開始(デフォルト) stepper now lives only in the settings panel
    // (BuildSettingsPanel builds it inline and assigns _maxMinus/_maxPlus/_maxValue).
    TextBlock _maxValue;

    // Toggle label/colour: ON => accent border + accent soft bg (clearly colored),
    // OFF => muted neutral. Task 2: ON state must be visually distinct with color.
    void PaintAutoToggle()
    {
        if (_autoToggle == null) return;
        _autoToggle.Content = _autoscale ? "Auto" : (_lang == 0 ? "Auto · 切" : "Auto · off");
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
    ControlTemplate FlatButtonTemplate()
    {
        var t = new ControlTemplate(typeof(Button));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.BorderBrushProperty, new TemplateBindingExtension(Control.BorderBrushProperty));
        bd.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new TemplateBindingExtension(Control.BorderThicknessProperty));
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(4));
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        cp.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        bd.AppendChild(cp);
        t.VisualTree = bd;
        return t;
    }
    Button IconButton(string glyph, double size)
    {
        var b = new Button(); b.Width = 36; b.Height = 30; b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1); b.Margin = new Thickness(4, 0, 0, 0);
        b.Content = MakeIcon(glyph, size, Fg); b.Tag = glyph;
        return b;
    }

    // Update the "N workers" chip text.  liveCount = 0 when idle (falls back to maxtabs).
    // isLive = true when status.json shows an active run (show actual worker count).
    void UpdateWorkerChip(int liveCount, bool isLive)
    {
        if (_workerChip == null) return;
        int n = isLive ? liveCount : _maxtabs;
        string label = _lang == 0 ? (n + " タブ") : (n + " workers");
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
        if (_gearBtn != null) _gearBtn.Content = MakeIcon("settings", 18, Fg);
        _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        _langBtn.Content = MakeIcon("translate", 18, Fg);
        if (_maxValue != null) _maxValue.Foreground = Fg;
        if (_autoLbl != null) _autoLbl.Foreground = Muted;
        if (_autoValue != null) _autoValue.Foreground = Fg;
        if (_workerChip != null) UpdateWorkerChip(0, false);
        PaintWorkerChipBorder(_workerChipBorder);
        PaintAutoToggle();
        UpdateAutoEnabled();
        PaintEffort();
        PaintApproval();
        PaintPause();
        if (_stopBtn != null) { _stopBtn.Background = BtnBg; _stopBtn.Foreground = Fg; _stopBtn.BorderBrush = Border; }
        if (_inBar != null) _inBar.Background = Bg;
        if (_composerBox != null) { _composerBox.Background = BtnBg; _composerBox.BorderBrush = Border; }
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
            _mtBanner.BorderThickness = new Thickness(3, 1, 1, 1);
            _mtBanner.BorderBrush = warn;
            if (_mtBannerLbl != null) _mtBannerLbl.Foreground = Fg;
        }
        if (_mtApplyNow != null) { _mtApplyNow.Background = Brushes.Transparent; _mtApplyNow.Foreground = warn; _mtApplyNow.BorderBrush = warn; _mtApplyNow.BorderThickness = new Thickness(1); }
        if (_mtLater != null) { _mtLater.Background = Brushes.Transparent; _mtLater.Foreground = Muted; _mtLater.BorderBrush = Border; }
        if (_capBanner != null)
        {
            _capBanner.Background = CardBg;
            _capBanner.BorderThickness = new Thickness(3, 1, 1, 1);
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
        PaintPause();
        // _stopBtn / _pauseBtn now render drawn icons (PaintPause), not text labels.
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
        BuildChrome();
        if (goalText != null && _goalInput != null) _goalInput.Text = goalText;
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
                    bool d0 = _dark; int l0 = _lang;
                    LoadSettings();
                    if (d0 != _dark) { ApplyThemeBrushes(); PaintChrome(); _lastSig = ""; }
                    else if (l0 != _lang) { RebuildChrome(); }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> root = ReadStatus();
        RefreshPauseEnabled(root);          // Pause is only meaningful for a live run; grey it out otherwise
        UpdateCapBanner(root);              // TASK 1: surface the admission-gate wait reactively each tick
        bool idle = root == null || I(root, "total") == 0
                    || (root.ContainsKey("idle") && Convert.ToBoolean(root["idle"]));
        if (idle)
        {
            _header.Text = "Fleet";
            _sub.Text = "";                // the empty-state block in the body carries the message now
            UpdateWorkerChip(0, false);    // show maxtabs while idle
            string isig = "IDLE" + _history.Count + (_dark ? "D" : "L") + _lang;
            if (_lastSig != isig)
            {
                _lastRoot = null;
                var rows = new List<object>();
                AppendHistoryRows(rows);   // history header + rows, if any
                if (rows.Count == 0) rows.Add(MkRow(5, null, null));   // empty state when nothing to show
                SetRows(rows);
                _lastSig = isig;
            }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        // Update the "N workers" chip with the actual total worker count each tick.
        {
            int wCount = 0;
            object wo3;
            if (root.TryGetValue("workers", out wo3) && wo3 is object[])
                wCount = ((object[])wo3).Length;
            UpdateWorkerChip(wCount, true);
        }
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
        string triple = ja2
            ? (cntRunning + " 実行中 · " + cntQueued + " 待機 · " + cntDoneW + " 完了")
            : (cntRunning + " running · " + cntQueued + " queued · " + cntDoneW + " done");

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

        if (running && updated > 0 && (NowUnix() - updated) > 8)
            triple = triple + " — " + T("stale");

        _sub.Text = triple + "    " + T("elapsed") + " " + Fmt(elapsed) + eta;
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
        rows.Add(MkRow(0, null, null));               // toolbar
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
            case 5: return "ES|" + g;                          // empty state (static chrome)
            case 3:                                            // history row: stable per entry
                return "h|" + g + "|" + (hist != null ? RuntimeHelpers.GetHashCode(hist) : 0);
            default:                                           // kind 1: worker card
                string nm = S(w, "name");
                var sb = new StringBuilder("c|");
                sb.Append(g).Append('|').Append(nm)
                  .Append(S(w, "status")).Append(S(w, "turn")).Append(S(w, "outcome"));
                // The collapsed card now shows a result line + meta (turn/reviews/verified), so its
                // signature must track `last`/reviews/verified too -- otherwise the at-a-glance line
                // would freeze while the worker streams. Length-only keeps it cheap.
                sb.Append(_expanded.Contains(nm) ? "#E" : "#C")
                  .Append((S(w, "last")).Length).Append(':').Append(S(w, "verify_attempts")).Append(S(w, "verified"));
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

    UIElement BuildCardToolbar(List<Dictionary<string, object>> all,
                               List<Dictionary<string, object>> shown)
    {
        // Compute per-tab counts from the FULL worker list (not filtered shown).
        int cntAll = 0, cntActive = 0, cntNeeds = 0, cntDone = 0;
        int doneN = 0, maxN = 0, badN = 0;
        foreach (Dictionary<string, object> w in all)
        {
            cntAll++;
            string oc = S(w, "outcome");
            string st = S(w, "status");
            if (oc == "DONE") { doneN++; cntDone++; }
            else if (oc == "MAXTURNS") maxN++;
            else if (oc == "STUCK" || oc == "ERROR" || oc == "CANCELLED") badN++;
            if (st == "awaiting") cntNeeds++;
            if (!IsTerminalWorker(w) && st != "pending") cntActive++;
        }

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

        DockPanel.SetDock(rightCl, Dock.Right);
        dp.Children.Add(rightCl);

        // left: 4 spec tabs (All / Active / Needs input / Done) with count badges
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        left.VerticalAlignment = VerticalAlignment.Center;

        // Tab 0: All (show total, no badge required but include count)
        string allLabel = T("flt_all") + " " + cntAll;
        left.Children.Add(FilterButton(allLabel, 0, false, 0));

        // Tab 1: Active with count
        string activeLabel = T("flt_active") + " " + cntActive;
        left.Children.Add(FilterButton(activeLabel, 1, false, 0));

        // Tab 2: Needs input with count; warning treatment when selected AND count>0
        string needsLabel = T("flt_needs") + " " + cntNeeds;
        left.Children.Add(FilterButton(needsLabel, 2, true, cntNeeds));

        // Tab 3: Done with count
        string doneLabel = T("flt_done") + " " + cntDone;
        left.Children.Add(FilterButton(doneLabel, 3, false, 0));

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

    // One segmented filter button. The active one gets the subtle-filled treatment; inactive ones
    // are neutral. `isNeeds` = this is the "Needs input" tab; when it IS selected AND needsCount>0
    // it uses an amber warning foreground/border instead of the normal active style.
    Button FilterButton(string label, int val, bool isNeeds, int needsCount)
    {
        var b = new Button();
        b.Content = label; b.Cursor = Cursors.Hand; b.FontSize = 12;
        b.Padding = new Thickness(10, 3, 10, 3); b.Margin = new Thickness(0, 0, 6, 0);
        b.BorderThickness = new Thickness(1);
        bool active = _cardFilter == val;
        if (active)
        {
            if (isNeeds && needsCount > 0)
            {
                // Warning amber: active "Needs input" with items
                b.Background = BtnBg;
                b.Foreground = Theme.Br(Theme.Warning(_dark));
                b.BorderBrush = Theme.Br(Theme.Warning(_dark));
                b.FontWeight = FontWeights.SemiBold;
            }
            else
            {
                // Normal active: subtle fill + stronger border
                b.Background = BtnBg; b.Foreground = Fg;
                b.BorderBrush = Theme.Br(Theme.BorderStrong(_dark));
                b.FontWeight = FontWeights.SemiBold;
            }
        }
        else
        {
            b.Background = Brushes.Transparent; b.Foreground = Muted; b.BorderBrush = Border;
        }
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
        string hcanon = status == "ready" ? "waiting" : status;
        var pill = Pill(Theme.StatusLabel(hcanon, _lang), Theme.StatusRail(hcanon));
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

        string railKind = closed ? "neutral" : Theme.StatusRail(status);
        Brush statusBrush = Theme.Br(Theme.RailColor(railKind, _dark));

        // Outer card: a plain surface with a thin border and a 3px status RAIL on the left.
        // No status fill / tint anywhere (spec): the rail + chip carry the meaning, so a successful
        // card never looks like a green alert block.
        var card = new Border();
        card.Tag = name;                // lets a chevron toggle find & replace just this card
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadCard);
        card.BorderBrush = Border; card.Background = CardBg;
        card.Margin = new Thickness(8, Theme.CardGap / 2, 8, Theme.CardGap / 2);

        var shell = new Grid();
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });   // rail
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var rail = new Border { Width = Theme.RailW, CornerRadius = new CornerRadius(2),
                                Background = statusBrush, Margin = new Thickness(0, 2, 0, 2) };
        Grid.SetColumn(rail, 0); shell.Children.Add(rail);

        var col = new StackPanel { Margin = new Thickness(14, 11, 14, 11) };
        Grid.SetColumn(col, 1); shell.Children.Add(col);

        // ── line 1: [chevron] [chip] [title .........] [Open] [release/archive] ──
        var top = new DockPanel();

        var right = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        {
            var openLink = new TextBlock();
            openLink.Text = _lang == 0 ? "開く" : "Open";
            openLink.Foreground = Muted; openLink.FontSize = 12;
            openLink.VerticalAlignment = VerticalAlignment.Center; openLink.Cursor = Cursors.Hand;
            openLink.Margin = new Thickness(0, 0, 10, 0);
            openLink.ToolTip = _lang == 0 ? "この会話をメインチャットで開く" : "Open this conversation in the chat";
            string onm = name; string ourl = conv;
            openLink.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; OpenWorker(onm, ourl); };
            right.Children.Add(openLink);
        }
        if (closed)
        {
            var rel = new TextBlock { Text = T("released"), Foreground = Muted, FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
            right.Children.Add(rel);
        }
        else if (terminal)
        {
            // finished card -> a working button that moves it to HISTORY client-side.
            var arch = new Button();
            arch.Content = new TextBlock { Text = "→ " + T("to_history"), Foreground = Fg, FontSize = 12 };
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
                if (_lastRoot != null
                    && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                    && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                    ArchiveAndHide(wt);
            };
            right.Children.Add(relBtn);
        }
        DockPanel.SetDock(right, Dock.Right);
        top.Children.Add(right);

        // left cluster: chevron + status chip, then the title fills the rest (1 line, ellipsis)
        var left = new DockPanel { LastChildFill = true };
        var chev = ChevronToggle(name, isOpen); DockPanel.SetDock(chev, Dock.Left); left.Children.Add(chev);
        var chip = Pill(Theme.StatusLabel(status, _lang), railKind);
        chip.Margin = new Thickness(2, 0, 10, 0);
        DockPanel.SetDock(chip, Dock.Left); left.Children.Add(chip);
        string headline = CardTitle(convTitle, goal);
        var ht = new TextBlock {
            Text = headline, Foreground = Fg, FontSize = 13.5, FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap
        };
        left.Children.Add(ht);
        top.Children.Add(left);
        col.Children.Add(top);

        // ── line 2: latest human-readable progress (the clean `last`, NOT the refuter reason -- a
        // DONE card must never surface "REFUTED" as its result). Hidden when there's nothing to say. ──
        string resultText = !string.IsNullOrEmpty(last) ? last : (terminal || closed ? "" : (_lang == 0 ? "実行中…" : "Working…"));
        if (!string.IsNullOrEmpty(resultText))
        {
            string resultPrefix = _lang == 0 ? "結果: " : "Result: ";
            // Task 3: clean the result for display; fall back to a neutral label if cleaning
            // strips everything (e.g. result was only preamble tokens).
            string cleanedResult = !string.IsNullOrEmpty(last) ? CleanAgentResultForUi(last) : "";
            string displayLine;
            if (!string.IsNullOrEmpty(last))
            {
                // result from agent: use first non-empty cleaned line, or fallback
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
            var rl = new TextBlock {
                Text = resultPrefix + displayLine, Foreground = Muted, FontSize = 12.5,
                TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                Margin = new Thickness(24, 5, 0, 0)
            };
            col.Children.Add(rl);
        }

        // ── line 3: meta -- [elapsed] · worker name · turn · reviews · verified ──
        // Per-card elapsed: the transcript jsonl meta line carries "ts" (epoch) = file creation
        // time (when the worker was queued). We compute elapsed from that to now.
        var meta = new StringBuilder();
        string transcriptPath = S(w, "transcript");
        double startTs = ReadTranscriptStartTs(transcriptPath);
        if (startTs > 0)
        {
            double cardElapsed = NowUnix() - startTs;
            if (cardElapsed > 0)
            {
                meta.Append(Fmt(cardElapsed)).Append(" · ");
            }
        }
        meta.Append(name.ToUpper());
        if (turn > 0) meta.Append(" · ").Append(T("turn")).Append(' ').Append(turn);
        if (reviews > 0) meta.Append(" · ").Append(_lang == 0 ? ("確認 " + reviews + " 回") : ("reviewed " + reviews));
        if (verifiedOk) meta.Append(" · ").Append(_lang == 0 ? "検証OK" : "verified");
        var ml = new TextBlock {
            Text = meta.ToString(), Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
            Margin = new Thickness(24, 4, 0, 0)
        };
        col.Children.Add(ml);

        // Expanded detail is organized into tabs (spec): Overview (default) / Conversation /
        // Review / Logs -- so raw transcript, refuter notes and internal fields no longer all
        // dump onto the surface at once. Heavy content is built ONLY when expanded.
        if (isOpen)
        {
            col.Children.Add(BuildCardTabs(w, name, goal, last, reason, terminal));
            // Actions live BELOW the tabs (not inside one) so steer/retry are always reachable.
            if (!terminal) col.Children.Add(SteerRow(name));
            else if (S(w, "outcome") != "DONE") col.Children.Add(RetryRow(w));
        }

        card.Child = shell;
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
        // Task 3: clean agent result for display; if cleaning yields empty, show fallback label.
        string resultRaw = !string.IsNullOrEmpty(last) ? last
                           : (terminal ? OutcomeLabel(outcome) : (_lang == 0 ? "実行中…" : "Working…"));
        string result;
        if (!string.IsNullOrEmpty(last))
        {
            string cleaned = CleanAgentResultForUi(last);
            result = !string.IsNullOrEmpty(cleaned)
                ? cleaned
                : (_lang == 0 ? "結果を受信しました" : "Result received");
        }
        else
        {
            result = resultRaw;
        }
        sp.Children.Add(RoText(result, Fg, 13));

        var checks = new List<string>();
        if (!string.IsNullOrEmpty(last)) checks.Add(_lang == 0 ? "エージェント応答を取得" : "Agent response captured");
        if (reviews > 0) checks.Add(_lang == 0 ? ("レビュー " + reviews + " 回実施") : ("Reviewed " + reviews + " time(s)"));
        if (verifiedOk) checks.Add(_lang == 0 ? "検証OK" : "Verified");
        if (terminal) checks.Add(OutcomeLabel(outcome));
        if (checks.Count > 0)
        {
            sp.Children.Add(SectLabel(_lang == 0 ? "チェック" : "Checks"));
            foreach (var c in checks)
                sp.Children.Add(new TextBlock { Text = "・" + c, Foreground = Muted, FontSize = 12.5, Margin = new Thickness(0, 1, 0, 1) });
        }

        // ── Timeline section ──────────────────────────────────────────────────────
        // Events are derived from available data; timestamps come from the transcript jsonl
        // "ts" field (epoch float). Each line: {"meta":true,"ts":...} or {"role":..., "ts":...}.
        sp.Children.Add(SectLabel(_lang == 0 ? "タイムライン" : "Timeline"));
        var tsEvents = BuildTimelineEvents(tpath, outcome, terminal, reviews);
        foreach (string ev in tsEvents)
            sp.Children.Add(new TextBlock {
                Text = "・" + ev, Foreground = Muted, FontSize = 12,
                Margin = new Thickness(0, 1, 0, 1), TextWrapping = TextWrapping.Wrap });

        sp.Children.Add(SectLabel(_lang == 0 ? "指示" : "Goal"));
        sp.Children.Add(RoText(goal, Muted, 12.5));
        return sp;
    }

    // Build the ordered event list for the Timeline section. Reads the transcript jsonl to
    // extract real "ts" (epoch) timestamps. Each entry already has "ts" on every line
    // (meta line + every user/assistant turn). Returns a list of display strings.
    List<string> BuildTimelineEvents(string tpath, string outcome, bool terminal, int reviews)
    {
        bool ja = _lang == 0;
        // Try to read ts values from the transcript: meta ts = queued/started, first user turn ts.
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
                    // meta line
                    if (obj.ContainsKey("meta") && Convert.ToBoolean(obj["meta"]))
                    {
                        if (obj.ContainsKey("ts") && obj["ts"] != null)
                        { metaTs = Convert.ToDouble(obj["ts"]); hasTs = true; }
                        continue;
                    }
                    // first turn line (role present, ts present)
                    if (obj.ContainsKey("role") && obj.ContainsKey("ts") && obj["ts"] != null)
                    {
                        if (firstTurnTs == 0)
                            firstTurnTs = Convert.ToDouble(obj["ts"]);
                        // we only need the first turn ts; stop after finding it
                        if (firstTurnTs > 0) break;
                    }
                }
            }
        }
        catch { }

        var evs = new List<string>();
        // Helper: format a unix epoch as "HH:mm" if we have it.
        // Returns "" when ts==0 (no timestamp available).
        // C# 5: no local functions -> use a Func delegate
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

        // Event 1: Queued (meta ts = file creation = worker was queued)
        string queuedTs = hasTs ? fmtTs(metaTs) : "";
        evs.Add(queuedTs + (ja ? "投入" : "Queued"));

        // Event 2: Started (first turn ts = actual first interaction)
        string startTs = (firstTurnTs > 0) ? fmtTs(firstTurnTs) : "";
        evs.Add(startTs + (ja ? "開始" : "Started"));

        // Event 3: Reviewed (if verify_attempts > 0)
        if (reviews > 0)
            evs.Add(ja ? ("レビュー (" + reviews + "x)") : ("Reviewed (" + reviews + "x)"));

        // Event 4: terminal outcome
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
        Func<bool> trySteer = delegate
        {
            string t = (tb.Text ?? "").Trim();
            if (t.Length == 0) return false;
            if (!RunIsLive()) { note.Text = T("steer_dead"); return false; }
            RequestSteer(nm, t); tb.Text = "";
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
    Border Pill(string text, string railKind)
    {
        var color = Theme.Br(Theme.RailColor(railKind, _dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        b.CornerRadius = new CornerRadius(999);
        b.Padding = new Thickness(9, 1.5, 9, 1.5);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
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
