// FleetCockpit.cs -- native Windows (WPF) LIVE cockpit for the parallel relay fleet.
//
// relay/fleet_runner.py drives N autonomous Copilot conversations at once and writes
// a live snapshot to .fleet/status.json after every round-robin sweep. This window
// tails that JSON and renders one live card per goal -- status pill, turn x/max, the
// streaming last response -- so you can WATCH N agents work in parallel. That is the
// visible Cowork-beating bit: Cowork shows you one autonomous track; this shows N.
//
//   [ fleet_runner.py ] --(atomic write)--> .fleet/status.json --(poll)--> [ this ]
//
// Palette = ShuttleScope Design Language v1.1 (grayscale-first slate; saturated status
// pills always carry #fff text; single E_EMPHASIS orange accent). No Node, no browser.
// Build with the Windows-only csc.exe (legacy C# 5): see ui\build_cockpit.bat
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

    // ShuttleScope palette -- theme-dependent brushes (swapped on light/dark toggle).
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg;
    static readonly Brush Accent = new SolidColorBrush(C("#ea580c"));   // E_EMPHASIS (const)
    static readonly Brush White  = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    Button _themeBtn;
    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "copilot-bridge", "settings.txt");

    readonly string _statusPath;
    StackPanel _cards;
    TextBlock _header, _sub;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();

    void ApplyThemeBrushes()
    {
        if (_dark)
        {
            Bg = new SolidColorBrush(C("#0f172a")); CardBg = new SolidColorBrush(C("#1e293b"));
            Border = new SolidColorBrush(C("#334155")); Fg = new SolidColorBrush(C("#f8fafc"));
            Muted = new SolidColorBrush(C("#94a3b8")); QuoteBg = new SolidColorBrush(C("#0b1220"));
        }
        else
        {
            Bg = new SolidColorBrush(C("#ffffff")); CardBg = new SolidColorBrush(C("#f8fafc"));
            Border = new SolidColorBrush(C("#e2e8f0")); Fg = new SolidColorBrush(C("#0f172a"));
            Muted = new SolidColorBrush(C("#64748b")); QuoteBg = new SolidColorBrush(C("#f1f5f9"));
        }
    }

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
        _dark = LoadDark();                 // honor the shared chat/cockpit theme setting
        ApplyThemeBrushes();
        Title = "並列自律フリート — Cockpit";
        Width = 1080; Height = 760;
        Background = Bg;
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
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;        // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "status.json"));
    }

    Border _headBar;
    ScrollViewer _sv;

    void BuildChrome()
    {
        var root = new DockPanel();
        _headBar = new Border();
        _headBar.Padding = new Thickness(28, 22, 22, 8);
        DockPanel.SetDock(_headBar, Dock.Top);

        var headRow = new DockPanel();
        // theme toggle, pinned right
        _themeBtn = new Button();
        _themeBtn.Content = _dark ? "☀" : "☾";
        _themeBtn.FontSize = 16; _themeBtn.Width = 38; _themeBtn.Height = 32;
        _themeBtn.Cursor = Cursors.Hand; _themeBtn.BorderThickness = new Thickness(1);
        _themeBtn.VerticalAlignment = VerticalAlignment.Top;
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveDark(); ApplyTheme(); };
        DockPanel.SetDock(_themeBtn, Dock.Right);
        headRow.Children.Add(_themeBtn);

        var head = new StackPanel();
        _header = new TextBlock();
        _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.Text = "🛰  並列自律フリート";
        head.Children.Add(_header);

        _sub = new TextBlock();
        _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 0, 0);
        _sub.Text = "status.json を待機中…  " + _statusPath;
        head.Children.Add(_sub);
        headRow.Children.Add(head);
        _headBar.Child = headRow;
        root.Children.Add(_headBar);

        _sv = new ScrollViewer();
        _sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _sv.Padding = new Thickness(20, 6, 20, 24);
        _cards = new StackPanel();
        _sv.Content = _cards;
        root.Children.Add(_sv);
        Content = root;
        PaintChrome();
    }

    // Apply the current theme brushes to the persistent chrome (window, header bar,
    // texts, toggle). Cards pick up the brushes when they are (re)built in Render().
    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        _header.Foreground = Fg;
        _sub.Foreground = Muted;
        _themeBtn.Content = _dark ? "☀" : "☾";
        _themeBtn.Background = CardBg;
        _themeBtn.Foreground = Fg;
        _themeBtn.BorderBrush = Border;
        if (_sv != null) _sv.Background = Bg;
    }

    // Live theme switch: swap brushes, repaint chrome, force a full card rebuild.
    void ApplyTheme()
    {
        ApplyThemeBrushes();
        PaintChrome();
        _lastSig = "";                 // invalidate so the next tick re-renders cards
        Dictionary<string, object> root = ReadStatus();
        if (root != null) { _lastSig = Sig(root); Render(root); }
    }

    bool LoadDark()
    {
        try
        {
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                    if (ln.StartsWith("dark=")) return ln.Substring(5).Trim() != "0";
        }
        catch (Exception) { }
        return true;       // default dark
    }

    void SaveDark()
    {
        try
        {
            var lines = new List<string>();
            bool found = false;
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    if (ln.StartsWith("dark=")) { lines.Add("dark=" + (_dark ? "1" : "0")); found = true; }
                    else lines.Add(ln);
                }
            if (!found) lines.Add("dark=" + (_dark ? "1" : "0"));
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile));
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", Encoding.UTF8);
        }
        catch (Exception) { }
    }

    void OnTick(object sender, EventArgs e)
    {
        Dictionary<string, object> root = ReadStatus();
        if (root == null)
        {
            _sub.Text = "fleet 未起動。 python -m relay.fleet_runner で開始すると、ここに表示されます。";
            return;
        }
        string sig = Sig(root);
        if (sig == _lastSig) return;     // nothing changed -> skip rebuild (no flicker)
        _lastSig = sig;
        Render(root);
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
    {
        if (d.ContainsKey(k) && d[k] != null) return d[k].ToString();
        return "";
    }
    static int I(Dictionary<string, object> d, string k)
    {
        try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { }
        return 0;
    }
    static double Dbl(Dictionary<string, object> d, string k)
    {
        try { if (d.ContainsKey(k) && d[k] != null) return Convert.ToDouble(d[k]); } catch (Exception) { }
        return 0;
    }

    string Sig(Dictionary<string, object> root)
    {
        var sb = new StringBuilder();
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

    Brush BrushFor(string colorKey)
    {
        if (colorKey == "good") return new SolidColorBrush(C("#3b4cc0"));   // running (A_GOOD)
        if (colorKey == "done") return new SolidColorBrush(C("#16a34a"));   // finished clean
        if (colorKey == "bad")  return new SolidColorBrush(C("#b40426"));   // stuck/error (B_BAD)
        return new SolidColorBrush(C("#475569"));                          // muted slate
    }

    void Render(Dictionary<string, object> root)
    {
        int total = I(root, "total");
        int done = I(root, "done_count");
        bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
        double started = Dbl(root, "started");
        double updated = Dbl(root, "updated");
        double elapsed = root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                         : (updated > 0 && started > 0 ? updated - started : 0);

        _header.Text = "🛰  並列自律フリート — " + done + " / " + total + " 完了";
        string state = running ? "実行中" : "完了";
        int maxc = I(root, "max_concurrent");
        int openTabs = I(root, "open_tabs");
        int availMb = I(root, "avail_mb");
        string mem = "";
        if (maxc > 0)
            mem = "    タブ " + openTabs + "/" + maxc + " 同時"
                + (availMb > 0 ? "（空きRAM " + availMb + "MB・完了で即解放）" : "");
        _sub.Text = state + "    経過 " + Fmt(elapsed) + "    " + total + " ゴール" + mem;

        _cards.Children.Clear();
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        foreach (object o in (object[])wo)
            _cards.Children.Add(Card((Dictionary<string, object>)o));
    }

    static string Fmt(double sec)
    {
        int s = (int)sec;
        if (s < 60) return s + "s";
        return (s / 60) + "m " + (s % 60) + "s";
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string status = S(w, "status");
        string pill = S(w, "pill");
        string color = S(w, "color");
        string reason = S(w, "reason");
        string last = S(w, "last");
        int turn = I(w, "turn");
        int maxt = I(w, "max_turns");

        var card = new Border();
        card.Background = CardBg;
        card.BorderBrush = Border;
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(12);
        card.Padding = new Thickness(18, 14, 18, 14);
        card.Margin = new Thickness(8, 8, 8, 8);

        var col = new StackPanel();

        // top row: name + pill + turn counter
        var top = new DockPanel();
        var nm = new TextBlock();
        nm.Text = name.ToUpper();
        nm.Foreground = Accent; nm.FontWeight = FontWeights.Bold; nm.FontSize = 13;
        nm.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(nm, Dock.Left);
        top.Children.Add(nm);

        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        var turns = new TextBlock();
        turns.Text = (closed ? "🗙 タブ解放　" : "") + "ターン " + turn + " / " + maxt;
        turns.Foreground = Muted; turns.FontSize = 12;
        turns.HorizontalAlignment = HorizontalAlignment.Right;
        turns.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(turns, Dock.Right);
        top.Children.Add(turns);

        var pillWrap = new StackPanel();
        pillWrap.Orientation = Orientation.Horizontal;
        pillWrap.HorizontalAlignment = HorizontalAlignment.Right;
        pillWrap.Margin = new Thickness(0, 0, 14, 0);
        pillWrap.Children.Add(Pill(pill, color));
        if (status == "waiting") pillWrap.Children.Add(Dots());
        top.Children.Add(pillWrap);

        col.Children.Add(top);

        // goal
        var g = new TextBlock();
        g.Text = goal;
        g.Foreground = Fg; g.FontSize = 14; g.TextWrapping = TextWrapping.Wrap;
        g.Margin = new Thickness(0, 10, 0, 8);
        col.Children.Add(g);

        // last response snippet (streaming) or reason
        string body = !string.IsNullOrEmpty(last) ? last : reason;
        if (!string.IsNullOrEmpty(body))
        {
            var quote = new Border();
            quote.Background = QuoteBg;
            quote.BorderBrush = Border; quote.BorderThickness = new Thickness(0, 0, 0, 0);
            quote.CornerRadius = new CornerRadius(8);
            quote.Padding = new Thickness(12, 10, 12, 10);
            var bt = new TextBlock();
            bt.Text = body;
            bt.Foreground = Muted; bt.FontSize = 12.5; bt.TextWrapping = TextWrapping.Wrap;
            bt.MaxHeight = 120; bt.TextTrimming = TextTrimming.CharacterEllipsis;
            quote.Child = bt;
            col.Children.Add(quote);
        }

        card.Child = col;
        return card;
    }

    Border Pill(string text, string colorKey)
    {
        var b = new Border();
        b.Background = BrushFor(colorKey);
        b.CornerRadius = new CornerRadius(999);
        b.Padding = new Thickness(11, 3, 11, 3);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text;
        t.Foreground = White;            // saturated bg -> always white text (contrast rule)
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }

    UIElement Dots()
    {
        var sp = new StackPanel();
        sp.Orientation = Orientation.Horizontal;
        sp.VerticalAlignment = VerticalAlignment.Center;
        sp.Margin = new Thickness(8, 0, 0, 0);
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse();
            dot.Width = 6; dot.Height = 6; dot.Fill = Muted;
            dot.Margin = new Thickness(2, 0, 0, 0);
            var anim = new DoubleAnimation(0.25, 1.0, new Duration(TimeSpan.FromMilliseconds(600)));
            anim.AutoReverse = true; anim.RepeatBehavior = RepeatBehavior.Forever;
            anim.BeginTime = TimeSpan.FromMilliseconds(i * 150);
            dot.BeginAnimation(UIElement.OpacityProperty, anim);
            sp.Children.Add(dot);
        }
        return sp;
    }
}
