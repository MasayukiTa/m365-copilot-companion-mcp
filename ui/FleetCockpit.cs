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
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using System.Web.Script.Serialization;

class CockpitProgram
{
    [STAThread]
    static void Main(string[] args)
    {
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
    long _settingsMtime = 0;

    readonly string _statusPath, _commandsPath;
    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "copilot-bridge", "settings.txt");

    StackPanel _cards;
    TextBlock _header, _sub;
    Button _themeBtn, _langBtn;
    TextBlock _maxLbl;
    Border _headBar;
    ScrollViewer _sv;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();

    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
        _commandsPath = Path.Combine(Path.GetDirectoryName(_statusPath), "commands.json");
        LoadGlyphs();
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
    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "title") return ja ? "並列実行" : "Parallel execution";
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
        if (k == "idle") return ja ? "未実行 — python -m relay.fleet_runner で並列実行を開始するとここに表示されます。"
                                   : "Not running — start with python -m relay.fleet_runner to see goals here.";
        if (k == "stale") return ja ? "更新が止まっています（フリート停止？）" : "no updates (fleet stopped?)";
        if (k == "applies_next") return ja ? "次回起動から適用" : "applies next run";
        if (k == "start") return ja ? "並列実行を開始" : "Start parallel run";
        if (k == "goalhint") return ja ? "1行に1ゴール（複数可）" : "One goal per line";
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
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", Encoding.UTF8);
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

        var headRow = new DockPanel();

        // right controls: max-tabs stepper, language, theme
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;
        DockPanel.SetDock(ctrls, Dock.Right);

        ctrls.Children.Add(MaxTabsStepper());
        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); Relabel(); ForceRender(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyTheme(); };
        ctrls.Children.Add(_themeBtn);
        headRow.Children.Add(ctrls);

        // title block (icon + title + sub)
        var titleRow = new StackPanel(); titleRow.Orientation = Orientation.Horizontal;
        _iconHost = new ContentControl(); _iconHost.VerticalAlignment = VerticalAlignment.Center;
        _iconHost.Margin = new Thickness(0, 0, 10, 0);
        titleRow.Children.Add(_iconHost);
        var titleCol = new StackPanel();
        _header = new TextBlock(); _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        titleCol.Children.Add(_header);
        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 0, 0);
        titleCol.Children.Add(_sub);
        titleRow.Children.Add(titleCol);
        headRow.Children.Add(titleRow);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);
        root.Children.Add(BuildInputBar());

        _sv = new ScrollViewer();
        _sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _sv.Padding = new Thickness(18, 6, 18, 24);
        _cards = new StackPanel();
        _sv.Content = _cards;
        root.Children.Add(_sv);
        Content = root;
        PaintChrome();
    }

    TextBox _goalInput;
    Button _startBtn;
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
        Grid.SetColumn(_goalInput, 0); grid.Children.Add(_goalInput);

        var rightCol = new StackPanel();
        rightCol.Margin = new Thickness(10, 0, 0, 0);
        _startBtn = new Button();
        _startBtn.Cursor = Cursors.Hand; _startBtn.BorderThickness = new Thickness(0);
        _startBtn.Height = 40; _startBtn.MinWidth = 150; _startBtn.FontWeight = FontWeights.SemiBold;
        _startBtn.Padding = new Thickness(14, 0, 14, 0);
        _startBtn.Click += delegate { StartFleet(); };
        rightCol.Children.Add(_startBtn);
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

            string repo = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            // hand the goals to the fleet via a UTF-8 file (avoids any arg-encoding issues)
            string goalsFile = Path.Combine(Path.GetDirectoryName(_statusPath), "goals_input.txt");
            File.WriteAllText(goalsFile, string.Join("\n", goals.ToArray()) + "\n", new UTF8Encoding(false));

            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m relay.fleet_runner --goals-file \"" + goalsFile + "\"";
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            System.Diagnostics.Process.Start(psi);
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

    ContentControl _iconHost;
    Button _maxMinus, _maxPlus;

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
    void SetMaxTabs(int v)
    {
        _maxtabs = Math.Max(1, Math.Min(8, v));
        SaveKey("maxtabs", _maxtabs.ToString());
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
    }

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
        if (_sv != null) _sv.Background = Bg;
        _iconHost.Content = MakeIcon("satellite_alt", 26, Accent);
        // restyle the header buttons for the theme
        foreach (Button b in new Button[] { _themeBtn, _langBtn, _maxMinus, _maxPlus })
            if (b != null) { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        _langBtn.Content = MakeIcon("translate", 18, Fg);
        if (_maxLbl != null) _maxLbl.Foreground = Muted;
        if (_maxValue != null) _maxValue.Foreground = Fg;
        if (_inBar != null) _inBar.Background = Bg;
        if (_goalInput != null)
        {
            _goalInput.Background = BtnBg; _goalInput.Foreground = Fg;
            _goalInput.BorderBrush = Border; _goalInput.CaretBrush = Fg;
        }
        if (_startBtn != null) { _startBtn.Background = Accent; _startBtn.Foreground = White; }
        if (_startNote != null) _startNote.Foreground = Muted;
        Relabel();
    }

    void Relabel()
    {
        if (_maxLbl != null) _maxLbl.Text = T("maxtabs") + " (" + T("applies_next") + ")";
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        if (_startBtn != null) _startBtn.Content = T("start");
        if (_goalInput != null) _goalInput.ToolTip = T("goalhint");
        if (_startNote != null && string.IsNullOrEmpty(_startNote.Text)) _startNote.Text = T("goalhint");
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
            if (_lastSig != "IDLE") { _cards.Children.Clear(); _lastSig = "IDLE"; }
            return;
        }
        UpdateHeader(root);                 // live elapsed every tick
        string sig = Sig(root);
        if (sig == _lastSig) return;
        _lastSig = sig;
        RenderCards(root);
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
        sb.Append(S(root, "done_count")).Append('/').Append(S(root, "total")).Append('|');
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
            {
                var w = (Dictionary<string, object>)o;
                sb.Append(S(w, "name")).Append(S(w, "status")).Append(S(w, "turn"))
                  .Append('#').Append((S(w, "last")).Length).Append(';');
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
        _sub.Text = state + "    " + T("elapsed") + " " + Fmt(elapsed) + "    " + total + " " + T("goals") + mem;
    }

    static string Fmt(double sec)
    {
        if (sec < 0) sec = 0;
        int s = (int)sec;
        if (s < 60) return s + "s";
        return (s / 60) + "m " + (s % 60) + "s";
    }

    void RenderCards(Dictionary<string, object> root)
    {
        _cards.Children.Clear();
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        foreach (object o in (object[])wo)
            _cards.Children.Add(Card((Dictionary<string, object>)o));
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string status = S(w, "status");
        string reason = S(w, "reason");
        string last = S(w, "last");
        int turn = I(w, "turn");
        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                        || status == "error" || status == "cancelled";

        string ck = ColorKey(status);
        Color sc = StatusColor(ck);

        var card = new Border();
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
        if (closed || terminal)
        {
            var rel = new TextBlock();
            rel.Text = T("released");
            rel.Foreground = Muted; rel.FontSize = 12; rel.VerticalAlignment = VerticalAlignment.Center;
            right.Children.Add(rel);
        }
        else
        {
            var relBtn = new Button();
            relBtn.Content = MakeReleaseContent();
            relBtn.Cursor = Cursors.Hand; relBtn.BorderThickness = new Thickness(1);
            relBtn.Background = BtnBg; relBtn.BorderBrush = Border; relBtn.Foreground = Fg;
            relBtn.Padding = new Thickness(8, 2, 10, 2);
            relBtn.ToolTip = _lang == 0 ? "このタスクを停止してタブを解放" : "Stop this task and release its tab";
            string nm = name;
            relBtn.Click += delegate { RequestClose(nm); };
            right.Children.Add(relBtn);
        }
        DockPanel.SetDock(right, Dock.Right);
        top.Children.Add(right);

        // left cluster: name + pill (+ dots when running)
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        var nm2 = new TextBlock();
        nm2.Text = name.ToUpper();
        nm2.Foreground = Accent; nm2.FontWeight = FontWeights.Bold; nm2.FontSize = 13;
        nm2.VerticalAlignment = VerticalAlignment.Center; nm2.Margin = new Thickness(0, 0, 10, 0);
        left.Children.Add(nm2);
        left.Children.Add(Pill(StatusLabel(status), ck));
        if (status == "waiting") left.Children.Add(Dots());
        top.Children.Add(left);

        col.Children.Add(top);

        var g = new TextBlock();
        g.Text = goal;
        g.Foreground = Fg; g.FontSize = 14; g.TextWrapping = TextWrapping.Wrap;
        g.Margin = new Thickness(0, 10, 0, 8);
        col.Children.Add(g);

        string body = !string.IsNullOrEmpty(last) ? last : reason;
        if (!string.IsNullOrEmpty(body))
        {
            var quote = new Border();
            quote.Background = QuoteBg; quote.CornerRadius = new CornerRadius(8);
            quote.Padding = new Thickness(12, 10, 12, 10);
            var bt = new TextBlock();
            bt.Text = body; bt.Foreground = Muted; bt.FontSize = 12.5;
            bt.TextWrapping = TextWrapping.Wrap; bt.MaxHeight = 120;
            bt.TextTrimming = TextTrimming.CharacterEllipsis;
            quote.Child = bt;
            col.Children.Add(quote);
        }

        card.Child = col;
        return card;
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

    // ── cockpit -> fleet control channel ─────────────────────────────────────────
    void RequestClose(string name)
    {
        try
        {
            var closes = new List<object>();
            if (File.Exists(_commandsPath))
            {
                try
                {
                    var ex = (Dictionary<string, object>)_js.DeserializeObject(File.ReadAllText(_commandsPath, Encoding.UTF8));
                    if (ex != null && ex.ContainsKey("close") && ex["close"] is object[])
                        foreach (object o in (object[])ex["close"]) closes.Add(o);
                }
                catch (Exception) { }
            }
            if (!closes.Contains(name)) closes.Add(name);
            var cmd = new Dictionary<string, object>();
            cmd["close"] = closes;
            File.WriteAllText(_commandsPath, _js.Serialize(cmd), Encoding.UTF8);
        }
        catch (Exception) { }
    }
}
