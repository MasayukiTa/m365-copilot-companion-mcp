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
// Palette = the sibling app Design Language v1.1 (grayscale-first slate; saturated status
// pills always carry #fff text; single E_EMPHASIS orange accent). No Node, no browser.
// Build with the Windows-only csc.exe (legacy C# 5): see ui\build_cockpit.bat
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
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

    // the sibling app slate palette
    static readonly Brush Bg     = new SolidColorBrush(C("#0f172a"));
    static readonly Brush CardBg = new SolidColorBrush(C("#1e293b"));
    static readonly Brush Border = new SolidColorBrush(C("#334155"));
    static readonly Brush Fg     = new SolidColorBrush(C("#f8fafc"));
    static readonly Brush Muted  = new SolidColorBrush(C("#94a3b8"));
    static readonly Brush Accent = new SolidColorBrush(C("#ea580c"));   // E_EMPHASIS
    static readonly Brush White  = new SolidColorBrush(C("#ffffff"));

    readonly string _statusPath;
    StackPanel _cards;
    TextBlock _header, _sub;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();

    public CockpitWindow(string path)
    {
        _statusPath = ResolvePath(path);
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

    void BuildChrome()
    {
        var root = new DockPanel();
        var head = new StackPanel();
        head.Margin = new Thickness(28, 22, 28, 8);
        DockPanel.SetDock(head, Dock.Top);

        _header = new TextBlock();
        _header.Foreground = Fg;
        _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.Text = "🛰  並列自律フリート";
        head.Children.Add(_header);

        _sub = new TextBlock();
        _sub.Foreground = Muted;
        _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 0, 0);
        _sub.Text = "status.json を待機中…  " + _statusPath;
        head.Children.Add(_sub);
        root.Children.Add(head);

        var sv = new ScrollViewer();
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        sv.Padding = new Thickness(20, 6, 20, 24);
        _cards = new StackPanel();
        sv.Content = _cards;
        root.Children.Add(sv);
        Content = root;
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
            quote.Background = new SolidColorBrush(C("#0b1220"));
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
