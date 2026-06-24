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
// Theme/cards/glyphs match FleetCockpit.cs exactly: ShuttleScope slate palette, single orange
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
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using System.Web.Script.Serialization;

class SelfImproveDashboardWindow : Window
{
    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }

    // theme-dependent brushes (same palette as FleetCockpit)
    Brush Bg, CardBg, Border, Fg, Muted, QuoteBg, BtnBg;
    static readonly Brush Accent = new SolidColorBrush(C("#ea580c"));
    static readonly Brush White = new SolidColorBrush(C("#ffffff"));

    bool _dark = true;
    int _lang = 0;             // 0 = Japanese, 1 = English
    long _settingsMtime = 0;

    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "copilot-bridge", "settings.txt");

    readonly string _jsonPath;
    DispatcherTimer _timer;
    string _lastSig = "";
    JavaScriptSerializer _js = new JavaScriptSerializer();
    double _upm = 960;
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();

    ContentControl _iconHost;
    TextBlock _header, _sub;
    Button _themeBtn, _langBtn;
    StackPanel _body;          // the scrolling content column we rebuild each change
    ScrollViewer _sv;          // scroll host -- themed explicitly so its fill matches the toolbar
    Border _headBar;

    public SelfImproveDashboardWindow() : this(null) { }

    public SelfImproveDashboardWindow(string path)
    {
        _jsonPath = ResolvePath(path);
        LoadGlyphs();
        LoadSettings();
        ApplyThemeBrushes();
        Title = "Self-Improvement";
        Width = 920; Height = 720;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        BuildChrome();
        _timer = new DispatcherTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(1000);     // ~1s tail, per spec
        _timer.Tick += new EventHandler(OnTick);
        _timer.Start();
        OnTick(null, null);
    }

    static string ResolvePath(string path)
    {
        if (!string.IsNullOrEmpty(path)) return path;
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;          // ...\ui\
        return Path.GetFullPath(Path.Combine(exeDir, "..", ".fleet", "selfimprove_dashboard.json"));
    }

    // ── i18n ────────────────────────────────────────────────────────────────────
    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "title") return ja ? "自己改善" : "Self-Improvement";
        if (k == "nodata") return ja ? "自己改善データはまだありません — python -m relay.selfimprove.dashboard で生成されます。"
                                     : "No self-improvement data yet — produced by python -m relay.selfimprove.dashboard.";
        if (k == "scorecard") return ja ? "スコアカード" : "Scorecard";
        if (k == "latest_pass") return ja ? "最新 pass@1" : "Latest pass@1";
        if (k == "latest_ab") return ja ? "最新 A/B" : "Latest A/B";
        if (k == "burned") return ja ? "burned 合計" : "burned total";
        if (k == "archive") return ja ? "アーカイブ" : "archive";
        if (k == "ab_history") return ja ? "A/B 履歴" : "A/B history";
        if (k == "burned_ledger") return ja ? "burned 台帳" : "Burned ledger";
        if (k == "pass_trend") return ja ? "pass@1 推移" : "pass@1 trend";
        if (k == "archive_sec") return ja ? "アーカイブ概要" : "Archive";
        if (k == "qd_cells") return ja ? "QDセル" : "QD cells";
        if (k == "genomes") return ja ? "ゲノム" : "genomes";
        if (k == "none") return ja ? "なし" : "none";
        if (k == "keep") return ja ? "採用" : "keep";
        if (k == "total") return ja ? "合計" : "total";
        // Live usage lens (general-user, no benchmark)
        if (k == "usage_sec") return ja ? "実利用（あなたのタスク）" : "Live — your usage";
        if (k == "usage_caption") return ja ? "実際の実行から算出。ベンチ不要 — これが普段の使い心地の指標です。"
                                            : "From your real runs — no benchmark. This is how it performs day to day.";
        if (k == "u_completion") return ja ? "完了率" : "Completion";
        if (k == "u_recent") return ja ? "直近" : "Recently";
        if (k == "u_turns") return ja ? "中央ターン" : "Median turns";
        if (k == "u_tasks") return ja ? "タスク数" : "Tasks";
        if (k == "u_trend") return ja ? "完了率の推移（古い→新しい）" : "Completion over time (old → new)";
        if (k == "u_verify") return ja ? "自己検証通過" : "Self-verified";
        return k;
    }

    // ── Material Symbols glyphs (vector, no emoji) -- mirrors FleetCockpit ────────
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
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);  // font y-up -> WPF y-down
        path.Data = geo; path.Fill = fill; path.Stretch = Stretch.None;
        path.Width = size; path.Height = size;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        path.VerticalAlignment = VerticalAlignment.Center;
        return path;
    }

    // ── settings (shared with the chat / cockpit) ─────────────────────────────────
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

    // ── theme (identical palette to FleetCockpit) ─────────────────────────────────
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

    // Verdict -> color key. Spec: GREEN when keep==true, YELLOW when verdict=="suggestive",
    // RED otherwise. "good"/"warn"/"bad"/"muted" mirror FleetCockpit's status palette.
    static Color StatusColorFor(string ck, bool dark)
    {
        if (ck == "good") return C("#16a34a");   // green  (keep)
        if (ck == "warn") return C("#d97706");   // amber  (suggestive)
        if (ck == "bad") return C("#b40426");    // red    (not kept)
        return dark ? C("#64748b") : C("#94a3b8");
    }
    string VerdictKey(object keep, string verdict)
    {
        if (AsBool(keep)) return "good";
        if (!string.IsNullOrEmpty(verdict) && verdict.ToLower() == "suggestive") return "warn";
        return "bad";
    }

    void BuildChrome()
    {
        var root = new DockPanel();

        _headBar = new Border();
        _headBar.Padding = new Thickness(26, 20, 18, 12);
        DockPanel.SetDock(_headBar, Dock.Top);

        var headRow = new Grid();
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        // right controls: language, theme (read-only view -> no write controls)
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;
        ctrls.HorizontalAlignment = HorizontalAlignment.Right;
        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyThemeBrushes(); PaintChrome(); ForceRender(); };
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
        titleRow.Children.Add(_header);
        Grid.SetColumn(titleRow, 0); Grid.SetRow(titleRow, 0);
        headRow.Children.Add(titleRow);

        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(38, 4, 18, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        headRow.Children.Add(_sub);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);

        // scrolling body
        _sv = new ScrollViewer();
        _sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _sv.Padding = new Thickness(18, 4, 18, 24);
        _body = new StackPanel();
        // Explicitly theme the scroll host + body. Without this the ScrollViewer/StackPanel inherit
        // a system-default (non-theme) fill, which in LIGHT mode read as a dark panel that didn't
        // match the toolbar -- the "dashboard background differs" complaint. Repainted on theme toggle.
        _sv.Background = Bg; _body.Background = Bg;
        _sv.Content = _body;
        root.Children.Add(_sv);

        Content = root;
        PaintChrome();
    }

    Button IconButton(string glyph, double size)
    {
        var b = new Button(); b.Width = 36; b.Height = 30; b.Cursor = System.Windows.Input.Cursors.Hand;
        b.BorderThickness = new Thickness(1); b.Margin = new Thickness(4, 0, 0, 0);
        b.Content = MakeIcon(glyph, size, Fg); b.Tag = glyph;
        return b;
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        if (_sv != null) _sv.Background = Bg;
        if (_body != null) _body.Background = Bg;
        _header.Foreground = Fg;
        _header.Text = T("title");
        _sub.Foreground = Muted;
        _iconHost.Content = MakeIcon("account_tree", 26, Fg);   // match the other header icons (was Accent = the odd-one-out orange the user flagged)
        foreach (Button b in new Button[] { _themeBtn, _langBtn })
            if (b != null) { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        if (_themeBtn != null) _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        if (_langBtn != null) _langBtn.Content = MakeIcon("translate", 18, Fg);
    }

    void ForceRender() { _lastSig = ""; OnTick(null, null); }

    // ── poll loop ─────────────────────────────────────────────────────────────────
    void OnTick(object sender, EventArgs e)
    {
        // follow external theme/lang edits (e.g. the chat toggled the theme)
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
                    else if (l0 != _lang) { PaintChrome(); _lastSig = ""; }
                }
            }
        }
        catch (Exception) { }

        Dictionary<string, object> state = ReadState();
        if (state == null)
        {
            if (_lastSig != "NODATA" + (_dark ? "D" : "L") + _lang)
            {
                RenderNoData();
                _lastSig = "NODATA" + (_dark ? "D" : "L") + _lang;
            }
            return;
        }

        string sig = Sig(state);
        if (sig == _lastSig) return;
        _lastSig = sig;
        Render(state);
    }

    Dictionary<string, object> ReadState()
    {
        try
        {
            if (!File.Exists(_jsonPath)) return null;
            string text;
            using (var fs = new FileStream(_jsonPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8))
                text = sr.ReadToEnd();
            if (string.IsNullOrEmpty(text)) return null;
            return _js.DeserializeObject(text) as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    // ── JSON-safe accessors (mirror FleetCockpit's S/I/Dbl, plus dict/array/bool) ──
    static string S(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k) && d[k] != null) return d[k].ToString(); return ""; }
    static int I(Dictionary<string, object> d, string k)
    { try { if (d != null && d.ContainsKey(k) && d[k] != null) return Convert.ToInt32(d[k]); } catch (Exception) { } return 0; }
    static Dictionary<string, object> Obj(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k)) return d[k] as Dictionary<string, object>; return null; }
    static object[] Arr(Dictionary<string, object> d, string k)
    { if (d != null && d.ContainsKey(k)) return d[k] as object[]; return null; }
    static bool AsBool(object o)
    { try { return o != null && Convert.ToBoolean(o); } catch (Exception) { return false; } }

    // number -> string, "n/a" when the value is absent/null
    static string Num(object o, string fmt)
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString(fmt, System.Globalization.CultureInfo.InvariantCulture); }
        catch (Exception) { return "n/a"; }
    }
    static string Pp(object o)   // signed percentage points, e.g. +8.8pp
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString("+0.0;-0.0", System.Globalization.CultureInfo.InvariantCulture) + "pp"; }
        catch (Exception) { return "n/a"; }
    }

    // Render signature: change-detect so we don't rebuild the tree every second when nothing moved.
    string Sig(Dictionary<string, object> state)
    {
        var sb = new StringBuilder();
        sb.Append(_dark ? "D" : "L").Append(_lang).Append('|');
        var sum = Obj(state, "summary");
        if (sum != null)
        {
            sb.Append(S(sum, "latest_pass_at_1")).Append('|');
            sb.Append(S(sum, "burned_total")).Append('|').Append(S(sum, "archive_count")).Append('|');
            var ab = Obj(sum, "latest_ab");
            if (ab != null) sb.Append(S(ab, "net_pp")).Append(S(ab, "p")).Append(S(ab, "verdict")).Append(S(ab, "keep"));
        }
        object[] hist = Arr(state, "ab_history");
        sb.Append('|').Append(hist != null ? hist.Length : 0);
        object[] pt = Arr(state, "pass1_trend");
        sb.Append('|').Append(pt != null ? pt.Length : 0);
        return sb.ToString();
    }

    // ── rendering ─────────────────────────────────────────────────────────────────
    void RenderNoData()
    {
        Background = Bg; _headBar.Background = Bg;
        _header.Text = T("title");
        _sub.Text = "";
        _body.Children.Clear();
        var card = SectionCard(T("title"));
        var t = new TextBlock();
        t.Text = T("nodata");
        t.Foreground = Muted; t.FontSize = 13.5; t.TextWrapping = TextWrapping.Wrap;
        t.Margin = new Thickness(0, 6, 0, 0);
        ((StackPanel)card.Child).Children.Add(t);
        _body.Children.Add(card);
    }

    void Render(Dictionary<string, object> state)
    {
        _sub.Text = "";
        _body.Children.Clear();
        _body.Children.Add(BuildUsage(state));        // (0) general-user lens first
        _body.Children.Add(BuildScorecard(state));
        _body.Children.Add(BuildAbHistory(state));
        _body.Children.Add(BuildBurnedLedger(state));
        _body.Children.Add(BuildPassTrend(state));
        _body.Children.Add(BuildArchive(state));
    }

    // (0) LIVE USAGE -- the general-user lens. Real-run completion / turns / trend from the persisted
    // history + live snapshot, NO benchmark. This is what a normal user (who never runs a bench) sees.
    UIElement BuildUsage(Dictionary<string, object> state)
    {
        var u = Obj(state, "usage");
        var card = SectionCard(T("usage_sec"));
        var col = (StackPanel)card.Child;
        if (u == null || I(u, "n_tasks") == 0) { col.Children.Add(EmptyLine()); return card; }

        var cap = new TextBlock { Text = T("usage_caption"), Foreground = Muted, FontSize = 11.5,
                                  TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 3, 0, 0) };
        col.Children.Add(cap);

        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var good = new SolidColorBrush(StatusColorFor("good", _dark));
        grid.Children.Add(Metric(T("u_completion"), Pct(u, "completion_rate"), good, 0));
        grid.Children.Add(Metric(T("u_recent"), Pct(u, "recent_completion_rate"), Fg, 1));
        grid.Children.Add(Metric(T("u_turns"), Num(u.ContainsKey("median_turns") ? u["median_turns"] : null, "0.#"), Fg, 2));
        grid.Children.Add(Metric(T("u_tasks"), I(u, "n_tasks").ToString(), Fg, 3));
        col.Children.Add(grid);

        var mix = Obj(u, "status_mix");
        if (mix != null && mix.Count > 0)
        {
            var parts = new List<string>();
            foreach (var kv in mix) parts.Add(kv.Key + " " + kv.Value);
            col.Children.Add(new TextBlock { Text = string.Join("    ·    ", parts.ToArray()), Foreground = Muted,
                                             FontSize = 12, Margin = new Thickness(0, 10, 0, 0), TextWrapping = TextWrapping.Wrap });
        }

        object[] tr = Arr(u, "trend");
        if (tr != null && tr.Length > 1)
        {
            col.Children.Add(new TextBlock { Text = T("u_trend"), Foreground = Muted, FontSize = 11.5, Margin = new Thickness(0, 11, 0, 4) });
            var sb = new StringBuilder();
            for (int i = 0; i < tr.Length; i++)
            {
                double v = 0.0; try { if (tr[i] != null) v = Convert.ToDouble(tr[i]); } catch (Exception) { }
                if (i > 0) sb.Append("  →  ");
                sb.Append(((int)System.Math.Round(v * 100)).ToString() + "%");
            }
            col.Children.Add(new TextBlock { Text = sb.ToString(), Foreground = Fg, FontSize = 13,
                                             FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 0) });
        }
        return card;
    }

    // percent string from a 0..1 fraction stored at d[k]; "—" when absent/null.
    static string Pct(Dictionary<string, object> d, string k)
    {
        if (d == null || !d.ContainsKey(k) || d[k] == null) return "—";
        try { return (Convert.ToDouble(d[k]) * 100.0).ToString("0.0") + "%"; } catch (Exception) { return "—"; }
    }

    // (1) SCORECARD header card: latest pass@1, latest A/B verdict (colored), burned_total, archive_count.
    UIElement BuildScorecard(Dictionary<string, object> state)
    {
        var sum = Obj(state, "summary");
        var ab = sum != null ? Obj(sum, "latest_ab") : null;

        string verdictKey = ab != null ? VerdictKey(ab["keep"], S(ab, "verdict")) : "muted";
        Color sc = StatusColorFor(verdictKey, _dark);

        var card = new Border();
        card.BorderThickness = new Thickness(1.4);
        card.CornerRadius = new CornerRadius(12);
        card.Padding = new Thickness(18, 14, 16, 14);
        card.Margin = new Thickness(8, 7, 8, 7);
        card.BorderBrush = new SolidColorBrush(Mix(sc, BgColor(), 0.55));
        card.Background = new SolidColorBrush(Mix(sc, CardColor(), 0.10));

        var col = new StackPanel();

        var top = new StackPanel(); top.Orientation = Orientation.Horizontal;
        var title = new TextBlock();
        title.Text = T("scorecard").ToUpper();
        title.Foreground = Accent; title.FontWeight = FontWeights.Bold; title.FontSize = 13;
        title.VerticalAlignment = VerticalAlignment.Center; title.Margin = new Thickness(0, 0, 10, 0);
        top.Children.Add(title);
        if (ab != null) top.Children.Add(Pill(S(ab, "verdict"), verdictKey));
        col.Children.Add(top);

        // big metric line
        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        grid.Children.Add(Metric(T("latest_pass"), sum != null ? Num(sum.ContainsKey("latest_pass_at_1") ? sum["latest_pass_at_1"] : null, "0.000") : "n/a", Fg, 0));
        string abVal = "n/a";
        if (ab != null)
            abVal = (string.IsNullOrEmpty(S(ab, "verdict")) ? "n/a" : S(ab, "verdict"))
                  + "  " + Pp(ab.ContainsKey("net_pp") ? ab["net_pp"] : null)
                  + "  p=" + Num(ab.ContainsKey("p") ? ab["p"] : null, "0.000");
        grid.Children.Add(Metric(T("latest_ab"), abVal, new SolidColorBrush(sc), 1));
        grid.Children.Add(Metric(T("burned"), I(sum, "burned_total").ToString(), Fg, 2));
        grid.Children.Add(Metric(T("archive"), I(sum, "archive_count").ToString(), Fg, 3));
        col.Children.Add(grid);

        card.Child = col;
        return card;
    }

    UIElement Metric(string label, string value, Brush valueBrush, int colIdx)
    {
        var sp = new StackPanel(); sp.Margin = new Thickness(0, 0, 12, 0);
        var l = new TextBlock();
        l.Text = label; l.Foreground = Muted; l.FontSize = 11.5;
        l.TextTrimming = TextTrimming.CharacterEllipsis;
        sp.Children.Add(l);
        var v = new TextBlock();
        v.Text = value; v.Foreground = valueBrush; v.FontSize = 15; v.FontWeight = FontWeights.SemiBold;
        v.TextWrapping = TextWrapping.Wrap; v.Margin = new Thickness(0, 3, 0, 0);
        sp.Children.Add(v);
        Grid.SetColumn(sp, colIdx);
        return sp;
    }

    // (2) A/B HISTORY list (newest-first): toggle, n, net_pp, p, verdict, keep.
    UIElement BuildAbHistory(Dictionary<string, object> state)
    {
        var card = SectionCard(T("ab_history"));
        var col = (StackPanel)card.Child;
        object[] hist = Arr(state, "ab_history");
        if (hist == null || hist.Length == 0)
        {
            col.Children.Add(EmptyLine());
            return card;
        }
        // newest-first: ab_history is oldest->newest, so iterate in reverse.
        for (int i = hist.Length - 1; i >= 0; i--)
        {
            var r = hist[i] as Dictionary<string, object>;
            if (r == null) continue;
            string vk = VerdictKey(r.ContainsKey("keep") ? r["keep"] : null, S(r, "verdict"));
            Color sc = StatusColorFor(vk, _dark);

            var row = new DockPanel(); row.Margin = new Thickness(0, 5, 0, 5);

            // pill on the right
            var pill = Pill(string.IsNullOrEmpty(S(r, "verdict")) ? "?" : S(r, "verdict"), vk);
            pill.HorizontalAlignment = HorizontalAlignment.Right;
            DockPanel.SetDock(pill, Dock.Right);
            row.Children.Add(pill);

            var line = new TextBlock();
            line.Foreground = Fg; line.FontSize = 13; line.VerticalAlignment = VerticalAlignment.Center;
            line.TextTrimming = TextTrimming.CharacterEllipsis;
            string toggle = string.IsNullOrEmpty(S(r, "toggle")) ? "?" : S(r, "toggle");
            line.Text = toggle
                + "    n=" + (r.ContainsKey("n") && r["n"] != null ? r["n"].ToString() : "?")
                + "    net=" + Pp(r.ContainsKey("net_pp") ? r["net_pp"] : null)
                + "    p=" + Num(r.ContainsKey("p") ? r["p"] : null, "0.000")
                + "    " + T("keep") + "=" + (AsBool(r.ContainsKey("keep") ? r["keep"] : null) ? "true" : "false");
            row.Children.Add(line);

            // colored left accent strip via a bordered wrapper
            var wrap = new Border();
            wrap.BorderThickness = new Thickness(3, 0, 0, 0);
            wrap.BorderBrush = new SolidColorBrush(sc);
            wrap.Padding = new Thickness(10, 0, 0, 0);
            wrap.Child = row;
            col.Children.Add(wrap);
        }
        return card;
    }

    // (3) BURNED LEDGER: total + by_reason breakdown.
    UIElement BuildBurnedLedger(Dictionary<string, object> state)
    {
        var card = SectionCard(T("burned_ledger"));
        var col = (StackPanel)card.Child;
        var bl = Obj(state, "burned_ledger");
        int total = I(bl, "total");

        var tot = new TextBlock();
        tot.Text = T("total") + ": " + total;
        tot.Foreground = Fg; tot.FontSize = 14; tot.FontWeight = FontWeights.SemiBold;
        tot.Margin = new Thickness(0, 6, 0, 6);
        col.Children.Add(tot);

        var byReason = bl != null ? Obj(bl, "by_reason") : null;
        if (byReason == null || byReason.Count == 0)
        {
            col.Children.Add(EmptyLine());
            return card;
        }
        // find max for a tiny inline bar
        int max = 1;
        foreach (KeyValuePair<string, object> kv in byReason) { int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { } if (v > max) max = v; }
        foreach (KeyValuePair<string, object> kv in byReason)
        {
            int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { }
            col.Children.Add(BarRow(kv.Key, v, max, (double)v / max));
        }
        return card;
    }

    // (4) PASS@1 TREND mini-list (ts, pass_at_1) -- simple textual/bar list, no charting library.
    UIElement BuildPassTrend(Dictionary<string, object> state)
    {
        var card = SectionCard(T("pass_trend"));
        var col = (StackPanel)card.Child;
        object[] pt = Arr(state, "pass1_trend");
        if (pt == null || pt.Length == 0)
        {
            col.Children.Add(EmptyLine());
            return card;
        }
        // newest at the top, capped to the last 24 points so a long run stays readable.
        int shown = 0;
        for (int i = pt.Length - 1; i >= 0 && shown < 24; i--, shown++)
        {
            var e = pt[i] as Dictionary<string, object>;
            if (e == null) continue;
            double pass = 0.0;
            try { if (e.ContainsKey("pass_at_1") && e["pass_at_1"] != null) pass = Convert.ToDouble(e["pass_at_1"]); } catch (Exception) { }
            if (pass < 0) pass = 0; if (pass > 1) pass = 1;
            string ts = e.ContainsKey("ts") && e["ts"] != null ? e["ts"].ToString() : "?";
            col.Children.Add(BarRow(ts, -1, -1, pass, Num(e.ContainsKey("pass_at_1") ? e["pass_at_1"] : null, "0.000")));
        }
        return card;
    }

    // (5) ARCHIVE summary: count, qd_cells.
    UIElement BuildArchive(Dictionary<string, object> state)
    {
        var card = SectionCard(T("archive_sec"));
        var col = (StackPanel)card.Child;
        var arc = Obj(state, "archive");

        var grid = new Grid(); grid.Margin = new Thickness(0, 6, 0, 0);
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.Children.Add(Metric(T("genomes"), I(arc, "count").ToString(), Fg, 0));
        grid.Children.Add(Metric(T("qd_cells"), I(arc, "qd_cells").ToString(), Fg, 1));
        col.Children.Add(grid);
        return card;
    }

    // A labelled bar row used by the burned ledger (count bar) and the pass@1 trend (ratio bar).
    // When `valueText` is null the count `v` is shown; otherwise `valueText` is shown verbatim.
    UIElement BarRow(string label, int v, int max, double frac) { return BarRow(label, v, max, frac, null); }
    UIElement BarRow(string label, int v, int max, double frac, string valueText)
    {
        if (frac < 0) frac = 0; if (frac > 1) frac = 1;
        var grid = new Grid(); grid.Margin = new Thickness(0, 3, 0, 3);
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });

        var l = new TextBlock();
        l.Text = label; l.Foreground = Muted; l.FontSize = 12.5;
        l.VerticalAlignment = VerticalAlignment.Center;
        l.TextTrimming = TextTrimming.CharacterEllipsis;
        Grid.SetColumn(l, 0); grid.Children.Add(l);

        // bar track + fill
        var track = new Border();
        track.Height = 10; track.CornerRadius = new CornerRadius(999);
        track.Background = QuoteBg; track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 10, 0);
        var inner = new Grid();
        inner.HorizontalAlignment = HorizontalAlignment.Left;
        var fill = new Border();
        fill.Height = 10; fill.CornerRadius = new CornerRadius(999);
        fill.Background = Accent;
        // width is set via a viewbox-free proportional trick: a Grid with two star columns
        var bargrid = new Grid();
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac, GridUnitType.Star) });
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1 - frac, GridUnitType.Star) });
        Grid.SetColumn(fill, 0); bargrid.Children.Add(fill);
        track.Child = bargrid;
        Grid.SetColumn(track, 1); grid.Children.Add(track);

        var val = new TextBlock();
        val.Text = valueText != null ? valueText : v.ToString();
        val.Foreground = Fg; val.FontSize = 12.5; val.FontWeight = FontWeights.SemiBold;
        val.VerticalAlignment = VerticalAlignment.Center; val.TextAlignment = TextAlignment.Right;
        Grid.SetColumn(val, 2); grid.Children.Add(val);

        return grid;
    }

    // A section card shell: titled, soft-bordered, themed -- returns a Border whose Child is the
    // content StackPanel (with the title already added), so callers append their rows to it.
    Border SectionCard(string titleText)
    {
        var card = new Border();
        card.BorderThickness = new Thickness(1.4);
        card.CornerRadius = new CornerRadius(12);
        card.Padding = new Thickness(18, 13, 16, 13);
        card.Margin = new Thickness(8, 7, 8, 7);
        card.BorderBrush = Border; card.Background = CardBg;

        var col = new StackPanel();
        var title = new TextBlock();
        title.Text = titleText.ToUpper();
        title.Foreground = Accent; title.FontWeight = FontWeights.Bold; title.FontSize = 13;
        col.Children.Add(title);
        card.Child = col;
        return card;
    }

    TextBlock EmptyLine()
    {
        var t = new TextBlock();
        t.Text = T("none"); t.Foreground = Muted; t.FontSize = 13;
        t.Margin = new Thickness(0, 6, 0, 0);
        return t;
    }

    // Pill badge -- identical to FleetCockpit's: saturated bg + white text.
    Border Pill(string text, string ck)
    {
        var b = new Border();
        b.Background = new SolidColorBrush(StatusColorFor(ck, _dark));
        b.CornerRadius = new CornerRadius(999);
        b.Padding = new Thickness(11, 3, 11, 3);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = string.IsNullOrEmpty(text) ? "?" : text;
        t.Foreground = White;                 // saturated bg -> white text
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }
}
