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
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using System.Web.Script.Serialization;

class SelfImproveDashboardWindow : Window
{
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
    TextBlock _header, _sub, _freshnessLine;
    Button _themeBtn, _langBtn, _refreshBtn;
    Border _divider1, _divider2;   // vertical dividers between icon buttons
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
        Title = "Self-Improvement";
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

    // ── i18n ────────────────────────────────────────────────────────────────────
    string T(string k)
    {
        bool ja = _lang == 0;
        // window header
        if (k == "win_title")  return ja ? "自己改善 / Self-Improvement" : "Self-Improvement / 自己改善";
        if (k == "win_sub")    return ja
            ? "エージェントが自分の解決スキャフォルドをどう改善しているか（実タスクの完了率・A/Bテスト・採用履歴）"
            : "How the agent is improving its own solving scaffold — real-task completion, A/B tests, and what it kept.";

        // no-data friendly message
        if (k == "nodata_title") return ja ? "まだデータがありません" : "No data yet";
        if (k == "nodata_body")  return ja
            ? "自己改善ループが走ると、ここに指標が表示されます。\n\npython -m relay.selfimprove.dashboard を実行するとデータが生成されます。"
            : "Metrics will appear here once the self-improvement loop runs.\n\nRun: python -m relay.selfimprove.dashboard";

        // section labels (shown in caps)
        if (k == "usage_sec")     return ja ? "実利用" : "Live usage";
        if (k == "scorecard_sec") return ja ? "スコアカード" : "Scorecard";
        if (k == "ab_sec")        return ja ? "A/B 履歴" : "A/B history";
        if (k == "burned_sec")    return ja ? "Burned 台帳" : "Burned ledger";
        if (k == "trend_sec")     return ja ? "Pass@1 推移" : "Pass@1 trend";
        if (k == "archive_sec")   return ja ? "アーカイブ" : "Archive";

        // section explanations (one-liner below the label)
        if (k == "usage_exp")  return ja
            ? "あなたの実際のタスクから算出。ベンチマーク不要 — 普段の完了率と傾向を示します。"
            : "From your real runs, no benchmark — shows day-to-day completion rate and trend.";
        if (k == "scorecard_exp") return ja
            ? "最新世代の解決性能のスナップショット。A/B テストの結果と採用されたスキャフォルドの数。"
            : "Snapshot of the latest generation's solving performance, the latest A/B result, and how many scaffolds have been adopted.";
        if (k == "ab_exp")     return ja
            ? "新しいスキャフォルド案を旧版と比較した結果（新しい順）。採用=緑、示唆的=黄、却下=赤。"
            : "Results of testing a new scaffold idea against the current one, newest first. Keep=green, suggestive=yellow, rejected=red.";
        if (k == "burned_exp") return ja
            ? "評価に使われたため再利用できない問題の台帳。理由別の内訳。"
            : "Problems that can no longer be reused for evaluation because they were consumed during testing — broken down by reason.";
        if (k == "trend_exp")  return ja
            ? "各改善サイクル後の pass@1 の推移（新しい順・最大24件）。"
            : "Pass@1 after each improvement cycle, newest first — up to 24 points shown.";
        if (k == "archive_exp") return ja
            ? "採用されたゲノム（解決スクリプトの変種）の多様性アーカイブ。QDセルは問題タイプごとのスロット数。"
            : "Diversity archive of adopted genomes (scaffold variants). QD cells = slots by problem type.";

        // metric labels
        if (k == "u_completion")  return ja ? "完了率" : "Completion";
        if (k == "u_recent")      return ja ? "直近完了率" : "Recent";
        if (k == "u_turns")       return ja ? "中央ターン数" : "Median turns";
        if (k == "u_tasks")       return ja ? "タスク数" : "Tasks";
        if (k == "u_trend")       return ja ? "完了率の推移（古い→新しい）" : "Completion trend (old → new)";
        if (k == "latest_pass")   return ja ? "最新 pass@1" : "Latest pass@1";
        if (k == "latest_ab")     return ja ? "最新 A/B" : "Latest A/B";
        if (k == "burned_total")  return ja ? "Burned 合計" : "Burned total";
        if (k == "archive_count") return ja ? "採用ゲノム数" : "Archive count";
        if (k == "qd_cells")      return ja ? "QD セル" : "QD cells";
        if (k == "genomes")       return ja ? "ゲノム" : "Genomes";
        if (k == "keep")          return ja ? "採用" : "Keep";
        if (k == "none")          return ja ? "なし" : "none";
        if (k == "total")         return ja ? "合計" : "Total";
        if (k == "by_reason")     return ja ? "理由別内訳" : "By reason";
        return k;
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
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);
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

    // ── theme ────────────────────────────────────────────────────────────────────
    void ApplyThemeBrushes()
    {
        Bg      = Theme.Br(Theme.Bg(_dark));
        CardBg  = Theme.Br(Theme.Surface(_dark));
        Border  = Theme.Br(Theme.Border(_dark));
        Fg      = Theme.Br(Theme.Text(_dark));
        Muted   = Theme.Br(Theme.Muted(_dark));
        QuoteBg = Theme.Br(Theme.SurfaceSubtle(_dark));
        BtnBg   = Theme.Br(Theme.SurfaceSubtle(_dark));
        Accent  = Theme.Br(Theme.Accent(_dark));
    }
    Color BgColor()   { return Theme.Col(Theme.Bg(_dark)); }
    Color CardColor() { return Theme.Col(Theme.Surface(_dark)); }
    static Color Mix(Color a, Color b, double t)
    {
        return Color.FromRgb((byte)(a.R * t + b.R * (1 - t)),
                             (byte)(a.G * t + b.G * (1 - t)),
                             (byte)(a.B * t + b.B * (1 - t)));
    }

    static Color StatusColorFor(string ck, bool dark)
    {
        if (ck == "good") return Theme.Col(Theme.Success(dark));
        if (ck == "warn") return Theme.Col(Theme.Warning(dark));
        if (ck == "bad")  return Theme.Col(Theme.Danger(dark));
        return Theme.Col(Theme.Muted(dark));
    }
    string VerdictKey(object keep, string verdict)
    {
        if (AsBool(keep)) return "good";
        if (!string.IsNullOrEmpty(verdict) && verdict.ToLower() == "suggestive") return "warn";
        return "bad";
    }

    // ── chrome ───────────────────────────────────────────────────────────────────
    void BuildChrome()
    {
        var root = new DockPanel();

        // ---- header bar ----
        _headBar = new Border();
        _headBar.Padding = new Thickness(26, 18, 18, 14);
        DockPanel.SetDock(_headBar, Dock.Top);

        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // title row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // subtitle row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // separator

        // controls (top-right): refresh | lang | theme  (FIX 1b + FIX 2a: dividers between them)
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;

        // FIX 1b: Refresh button (no matching glyph in 8-glyph set; use Unicode ⟳ TextBlock)
        _refreshBtn = RefreshButton();
        _refreshBtn.ToolTip = _lang == 0 ? "更新" : "Refresh";
        _refreshBtn.Click += delegate { RegenerateFeed(); };
        ctrls.Children.Add(_refreshBtn);

        // FIX 2a: vertical divider between refresh and lang
        _divider1 = MakeVDivider();
        ctrls.Children.Add(_divider1);

        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_langBtn);

        // FIX 2a: vertical divider between lang and theme
        _divider2 = MakeVDivider();
        ctrls.Children.Add(_divider2);

        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18);
        _themeBtn.ToolTip = "テーマ (ダーク/ライト)";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyThemeBrushes(); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_themeBtn);

        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headGrid.Children.Add(ctrls);

        // title row: icon + text
        var titleRow = new DockPanel { LastChildFill = true };
        titleRow.VerticalAlignment = VerticalAlignment.Center;
        titleRow.Margin = new Thickness(0, 0, 12, 0);
        _iconHost = new ContentControl();
        _iconHost.VerticalAlignment = VerticalAlignment.Center;
        _iconHost.Margin = new Thickness(0, 0, 10, 0);
        DockPanel.SetDock(_iconHost, Dock.Left);
        titleRow.Children.Add(_iconHost);
        _header = new TextBlock();
        _header.FontSize = 20;
        _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis;
        _header.TextWrapping = TextWrapping.NoWrap;
        titleRow.Children.Add(_header);
        Grid.SetColumn(titleRow, 0); Grid.SetRow(titleRow, 0);
        headGrid.Children.Add(titleRow);

        // subtitle (one-line explanation of what this window IS)
        _sub = new TextBlock();
        _sub.FontSize = 12.5;
        _sub.Margin = new Thickness(36, 5, 12, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        headGrid.Children.Add(_sub);

        // FIX 1c: freshness line (row 1 col 1, right-aligned, under the controls)
        _freshnessLine = new TextBlock();
        _freshnessLine.FontSize = 11;
        _freshnessLine.Margin = new Thickness(0, 5, 0, 0);
        _freshnessLine.TextAlignment = TextAlignment.Right;
        _freshnessLine.VerticalAlignment = VerticalAlignment.Top;
        Grid.SetColumn(_freshnessLine, 1); Grid.SetRow(_freshnessLine, 1);
        headGrid.Children.Add(_freshnessLine);

        // separator line below the header
        var sep = new Border();
        sep.Height = 1;
        sep.Margin = new Thickness(0, 12, 0, 0);
        sep.BorderThickness = new Thickness(0);
        Grid.SetColumn(sep, 0); Grid.SetColumnSpan(sep, 2); Grid.SetRow(sep, 2);
        headGrid.Children.Add(sep);

        _headBar.Child = headGrid;
        root.Children.Add(_headBar);

        // ---- scrolling body ----
        _sv = new ScrollViewer();
        _sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _sv.Padding = new Thickness(20, 12, 20, 28);
        _body = new StackPanel();
        _sv.Background = Bg; _body.Background = Bg;
        _sv.Content = _body;
        root.Children.Add(_sv);

        Content = root;
        PaintChrome();
    }

    Button IconButton(string glyph, double size)
    {
        var b = new Button();
        b.Width = 36; b.Height = 30;
        b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1);
        b.Margin = new Thickness(2, 0, 2, 0);
        b.Padding = new Thickness(5, 3, 5, 3);
        b.Content = MakeIcon(glyph, size, Fg);
        b.Tag = glyph;
        // FIX 2b: hover affordance -- faint background tint on mouse enter/leave
        b.MouseEnter += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = Theme.Br(Theme.Hover(_dark));
        };
        b.MouseLeave += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = BtnBg;
        };
        return b;
    }

    // FIX 1b: Refresh button using Unicode ⟳ (no refresh/sync glyph in the 8-glyph set)
    Button RefreshButton()
    {
        var b = new Button();
        b.Width = 36; b.Height = 30;
        b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1);
        b.Margin = new Thickness(2, 0, 2, 0);
        b.Padding = new Thickness(5, 3, 5, 3);
        var t = new TextBlock();
        t.Text = "⟳";   // ⟳  CLOCKWISE GAPPED CIRCLE ARROW
        t.FontSize = 16;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.HorizontalAlignment = HorizontalAlignment.Center;
        b.Content = t;
        b.Tag = "_refresh";
        b.MouseEnter += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = Theme.Br(Theme.Hover(_dark));
        };
        b.MouseLeave += delegate(object s, MouseEventArgs e)
        {
            var btn = (Button)s;
            btn.Background = BtnBg;
        };
        return b;
    }

    // FIX 2a: thin vertical divider between icon buttons
    Border MakeVDivider()
    {
        var d = new Border();
        d.Width = 1;
        d.Height = 17;
        d.VerticalAlignment = VerticalAlignment.Center;
        d.Margin = new Thickness(6, 0, 6, 0);
        d.Background = Muted;   // visible divider (user: make the line darker, not the faint Border)
        return d;
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        if (_sv != null)   _sv.Background   = Bg;
        if (_body != null) _body.Background = Bg;
        _header.Foreground = Fg;
        _header.Text = T("win_title");
        _sub.Foreground = Muted;
        _sub.Text = T("win_sub");
        _iconHost.Content = MakeIcon("account_tree", 24, Fg);

        // FIX 2c: repaint all icon buttons + dividers
        foreach (Button b in new Button[] { _themeBtn, _langBtn, _refreshBtn })
        {
            if (b != null)
            {
                b.Background  = BtnBg;
                b.Foreground  = Fg;
                b.BorderBrush = Border;
            }
        }
        if (_themeBtn   != null) _themeBtn.Content  = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        if (_langBtn    != null) _langBtn.Content   = MakeIcon("translate", 18, Fg);
        // _refreshBtn content is a TextBlock; repaint its Foreground
        if (_refreshBtn != null)
        {
            _refreshBtn.ToolTip = _lang == 0 ? "更新" : "Refresh";
            var tb = _refreshBtn.Content as TextBlock;
            if (tb != null) tb.Foreground = Fg;
        }
        // repaint dividers with current border brush
        if (_divider1 != null) _divider1.Background = Muted;
        if (_divider2 != null) _divider2.Background = Muted;

        // FIX 1c: freshness line is updated on each tick; just set colour here
        if (_freshnessLine != null) _freshnessLine.Foreground = Muted;
    }

    void ForceRender() { _lastSig = ""; OnTick(null, null); }

    // ── poll loop ─────────────────────────────────────────────────────────────────
    void OnTick(object sender, EventArgs e)
    {
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

        // FIX 1c: update freshness line every tick regardless of state change
        UpdateFreshnessLine();

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

    // FIX 1c: compute freshness from File.GetLastWriteTime(_jsonPath) and display as
    // "更新: 10:48 (たった今 / N分前)"  /  "Updated 10:48 (just now / N min ago)"
    void UpdateFreshnessLine()
    {
        if (_freshnessLine == null) return;
        try
        {
            if (!File.Exists(_jsonPath)) { _freshnessLine.Text = ""; return; }
            DateTime mtime = File.GetLastWriteTime(_jsonPath);
            double mins = (DateTime.Now - mtime).TotalMinutes;
            string timeStr = mtime.ToString("HH:mm");
            string ago;
            bool ja = _lang == 0;
            if (mins < 1.0)
                ago = ja ? "たった今" : "just now";
            else if (mins < 60.0)
                ago = ja ? ((int)mins).ToString() + "分前" : ((int)mins).ToString() + " min ago";
            else
            {
                int h = (int)(mins / 60);
                ago = ja ? h.ToString() + "時間前" : h.ToString() + " hr ago";
            }
            _freshnessLine.Text = (ja ? "更新: " : "Updated ") + timeStr + " (" + ago + ")";
        }
        catch (Exception) { _freshnessLine.Text = ""; }
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

    // ── JSON-safe accessors ────────────────────────────────────────────────────────
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

    static string Num(object o, string fmt)
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString(fmt, System.Globalization.CultureInfo.InvariantCulture); }
        catch (Exception) { return "n/a"; }
    }
    static string Pp(object o)
    {
        if (o == null) return "n/a";
        try { return Convert.ToDouble(o).ToString("+0.0;-0.0", System.Globalization.CultureInfo.InvariantCulture) + "pp"; }
        catch (Exception) { return "n/a"; }
    }
    static string Pct(Dictionary<string, object> d, string k)
    {
        if (d == null || !d.ContainsKey(k) || d[k] == null) return "—";
        try { return (Convert.ToDouble(d[k]) * 100.0).ToString("0.0") + "%"; } catch (Exception) { return "—"; }
    }

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

    // Empty / no-data state: friendly message in a single centered card, not a broken panel.
    void RenderNoData()
    {
        Background = Bg; _headBar.Background = Bg;
        _body.Children.Clear();

        // a calm, centred placeholder card
        var card = new Border();
        card.BorderThickness = new Thickness(1);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(32, 28, 32, 28);
        card.Margin          = new Thickness(0, 24, 0, 0);
        card.BorderBrush     = Border;
        card.Background      = CardBg;
        card.HorizontalAlignment = HorizontalAlignment.Center;
        card.MaxWidth        = 560;

        var col = new StackPanel();
        col.HorizontalAlignment = HorizontalAlignment.Center;

        // icon  (use a neutral "info" look)
        var iconWrap = new ContentControl();
        iconWrap.Content = MakeIcon("auto_awesome", 36, Muted);
        iconWrap.HorizontalAlignment = HorizontalAlignment.Center;
        iconWrap.Margin = new Thickness(0, 0, 0, 14);
        col.Children.Add(iconWrap);

        var heading = new TextBlock();
        heading.Text = T("nodata_title");
        heading.Foreground = Fg;
        heading.FontSize = 15;
        heading.FontWeight = FontWeights.SemiBold;
        heading.TextAlignment = TextAlignment.Center;
        heading.Margin = new Thickness(0, 0, 0, 8);
        col.Children.Add(heading);

        var body = new TextBlock();
        body.Text = T("nodata_body");
        body.Foreground = Muted;
        body.FontSize = 12.5;
        body.TextWrapping = TextWrapping.Wrap;
        body.TextAlignment = TextAlignment.Center;
        body.LineHeight = 20;
        col.Children.Add(body);

        card.Child = col;
        _body.Children.Add(card);
    }

    void Render(Dictionary<string, object> state)
    {
        _body.Children.Clear();
        _body.Children.Add(BuildUsage(state));
        _body.Children.Add(BuildScorecard(state));
        _body.Children.Add(BuildAbHistory(state));
        _body.Children.Add(BuildBurnedLedger(state));
        _body.Children.Add(BuildPassTrend(state));
        _body.Children.Add(BuildArchive(state));
    }

    // ── (0) LIVE USAGE — general-user lens ───────────────────────────────────────
    // Real-run completion / turns / trend, no benchmark.
    UIElement BuildUsage(Dictionary<string, object> state)
    {
        var u    = Obj(state, "usage");
        var card = SectionCard("usage_sec", "usage_exp");
        var col  = (StackPanel)card.Child;

        if (u == null || I(u, "n_tasks") == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // metrics row
        var grid = new Grid(); grid.Margin = new Thickness(0, 10, 0, 0);
        for (int i = 0; i < 4; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var goodBrush = new SolidColorBrush(StatusColorFor("good", _dark));
        grid.Children.Add(MetricCell(T("u_completion"), Pct(u, "completion_rate"),  goodBrush, 0));
        grid.Children.Add(MetricCell(T("u_recent"),     Pct(u, "recent_completion_rate"), Fg, 1));
        grid.Children.Add(MetricCell(T("u_turns"),
            Num(u.ContainsKey("median_turns") ? u["median_turns"] : null, "0.#"), Fg, 2));
        grid.Children.Add(MetricCell(T("u_tasks"), I(u, "n_tasks").ToString(), Fg, 3));
        col.Children.Add(grid);

        // status mix (counts by outcome)
        var mix = Obj(u, "status_mix");
        if (mix != null && mix.Count > 0)
        {
            var parts = new List<string>();
            foreach (KeyValuePair<string, object> kv in mix)
                parts.Add(kv.Key + ": " + kv.Value);
            var mixLine = new TextBlock();
            mixLine.Text = string.Join("    ·    ", parts.ToArray());
            mixLine.Foreground = Muted;
            mixLine.FontSize = 12;
            mixLine.Margin = new Thickness(0, 10, 0, 0);
            mixLine.TextWrapping = TextWrapping.Wrap;
            col.Children.Add(mixLine);
        }

        // completion trend
        object[] tr = Arr(u, "trend");
        if (tr != null && tr.Length > 1)
        {
            var label = new TextBlock();
            label.Text = T("u_trend");
            label.Foreground = Muted;
            label.FontSize = 11;
            label.Margin = new Thickness(0, 12, 0, 3);
            col.Children.Add(label);

            var sb = new StringBuilder();
            for (int i = 0; i < tr.Length; i++)
            {
                double v = 0.0;
                try { if (tr[i] != null) v = Convert.ToDouble(tr[i]); } catch (Exception) { }
                if (i > 0) sb.Append("  →  ");
                sb.Append(((int)System.Math.Round(v * 100)).ToString() + "%");
            }
            var trendLine = new TextBlock();
            trendLine.Text = sb.ToString();
            trendLine.Foreground = Fg;
            trendLine.FontSize = 13;
            trendLine.FontWeight = FontWeights.SemiBold;
            col.Children.Add(trendLine);
        }

        return card;
    }

    // ── (1) SCORECARD ────────────────────────────────────────────────────────────
    // Latest pass@1, latest A/B verdict, burned count, archive count.
    UIElement BuildScorecard(Dictionary<string, object> state)
    {
        var sum = Obj(state, "summary");
        var ab  = sum != null ? Obj(sum, "latest_ab") : null;

        string vk = ab != null ? VerdictKey(ab.ContainsKey("keep") ? ab["keep"] : null, S(ab, "verdict")) : "muted";
        Color  sc = StatusColorFor(vk, _dark);

        // scorecard uses a subtle tinted border to convey the latest verdict at a glance
        var card = new Border();
        card.BorderThickness = new Thickness(1.4);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(18, 14, 16, 16);
        card.Margin          = new Thickness(0, 8, 0, 8);
        card.BorderBrush     = new SolidColorBrush(Mix(sc, BgColor(), 0.45));
        card.Background      = new SolidColorBrush(Mix(sc, CardColor(), 0.07));

        var col = new StackPanel();

        // section label + verdict chip on one row
        var topRow = new DockPanel { LastChildFill = false };
        topRow.Margin = new Thickness(0, 0, 0, 2);

        var sLabel = SectionLabel(T("scorecard_sec"));
        DockPanel.SetDock(sLabel, Dock.Left);
        topRow.Children.Add(sLabel);

        if (ab != null)
        {
            string vtext = string.IsNullOrEmpty(S(ab, "verdict")) ? "?" : S(ab, "verdict");
            var chip = Pill(vtext, vk);
            chip.Margin = new Thickness(10, 0, 0, 0);
            DockPanel.SetDock(chip, Dock.Left);
            topRow.Children.Add(chip);
        }
        col.Children.Add(topRow);

        // explanation
        col.Children.Add(SectionExplanation(T("scorecard_exp")));

        // metrics row
        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int i = 0; i < 4; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        string pass1 = sum != null ? Num(sum.ContainsKey("latest_pass_at_1") ? sum["latest_pass_at_1"] : null, "0.000") : "n/a";
        grid.Children.Add(MetricCell(T("latest_pass"), pass1, Fg, 0));

        string abVal;
        if (ab != null)
        {
            string verdict = string.IsNullOrEmpty(S(ab, "verdict")) ? "n/a" : S(ab, "verdict");
            abVal = verdict + "  " + Pp(ab.ContainsKey("net_pp") ? ab["net_pp"] : null)
                            + "  p=" + Num(ab.ContainsKey("p") ? ab["p"] : null, "0.000");
        }
        else
        {
            abVal = "n/a";
        }
        grid.Children.Add(MetricCell(T("latest_ab"), abVal, new SolidColorBrush(sc), 1));
        grid.Children.Add(MetricCell(T("burned_total"),  I(sum, "burned_total").ToString(),  Fg, 2));
        grid.Children.Add(MetricCell(T("archive_count"), I(sum, "archive_count").ToString(), Fg, 3));
        col.Children.Add(grid);

        card.Child = col;
        return card;
    }

    // ── (2) A/B HISTORY ──────────────────────────────────────────────────────────
    // Newest-first list: toggle name, sample size, net pp, p-value, verdict chip.
    UIElement BuildAbHistory(Dictionary<string, object> state)
    {
        var card = SectionCard("ab_sec", "ab_exp");
        var col  = (StackPanel)card.Child;
        object[] hist = Arr(state, "ab_history");

        if (hist == null || hist.Length == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // column header labels
        var hdr = new Grid(); hdr.Margin = new Thickness(0, 8, 0, 2);
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        hdr.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(80) });
        hdr.Children.Add(ColHeader(_lang == 0 ? "トグル" : "Toggle",        0));
        hdr.Children.Add(ColHeader("n",                                      1));
        hdr.Children.Add(ColHeader(_lang == 0 ? "差分" : "Net",             2));
        hdr.Children.Add(ColHeader("p-value",                                3));
        hdr.Children.Add(ColHeader(_lang == 0 ? "採否" : "Verdict",         4));
        col.Children.Add(hdr);
        col.Children.Add(HRule());

        // newest-first
        for (int i = hist.Length - 1; i >= 0; i--)
        {
            var r = hist[i] as Dictionary<string, object>;
            if (r == null) continue;
            string vk = VerdictKey(r.ContainsKey("keep") ? r["keep"] : null, S(r, "verdict"));
            Color  sc = StatusColorFor(vk, _dark);

            // left accent strip = verdict color
            var strip = new Border();
            strip.BorderThickness = new Thickness(3, 0, 0, 0);
            strip.BorderBrush     = new SolidColorBrush(sc);
            strip.Padding         = new Thickness(10, 4, 0, 4);

            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(80) });

            string toggle = string.IsNullOrEmpty(S(r, "toggle")) ? "?" : S(r, "toggle");
            string nVal   = r.ContainsKey("n") && r["n"] != null ? r["n"].ToString() : "?";
            string netVal = Pp(r.ContainsKey("net_pp") ? r["net_pp"] : null);
            string pVal   = Num(r.ContainsKey("p") ? r["p"] : null, "0.000");
            string vtext  = string.IsNullOrEmpty(S(r, "verdict")) ? "?" : S(r, "verdict");

            row.Children.Add(RowCell(toggle, Fg,   false, 0));
            row.Children.Add(RowCell(nVal,   Muted, false, 1));
            row.Children.Add(RowCell(netVal, new SolidColorBrush(sc), true, 2));
            row.Children.Add(RowCell(pVal,   Muted, false, 3));

            // verdict chip
            var chip = Pill(vtext, vk);
            chip.VerticalAlignment = VerticalAlignment.Center;
            chip.Margin = new Thickness(4, 0, 0, 0);
            Grid.SetColumn(chip, 4);
            row.Children.Add(chip);

            strip.Child = row;
            col.Children.Add(strip);
        }
        return card;
    }

    // ── (3) BURNED LEDGER ────────────────────────────────────────────────────────
    // Total count of burned problems + breakdown by reason (bar chart).
    UIElement BuildBurnedLedger(Dictionary<string, object> state)
    {
        var card = SectionCard("burned_sec", "burned_exp");
        var col  = (StackPanel)card.Child;
        var bl   = Obj(state, "burned_ledger");
        int total = I(bl, "total");

        // total count as a prominent metric
        var totalRow = new StackPanel(); totalRow.Orientation = Orientation.Horizontal;
        totalRow.Margin = new Thickness(0, 8, 0, 10);
        var tLabel = new TextBlock();
        tLabel.Text = T("burned_total") + ": ";
        tLabel.Foreground = Muted; tLabel.FontSize = 13;
        tLabel.VerticalAlignment = VerticalAlignment.Center;
        totalRow.Children.Add(tLabel);
        var tValue = new TextBlock();
        tValue.Text = total.ToString();
        tValue.Foreground = Fg; tValue.FontSize = 16; tValue.FontWeight = FontWeights.SemiBold;
        tValue.VerticalAlignment = VerticalAlignment.Center;
        totalRow.Children.Add(tValue);
        col.Children.Add(totalRow);

        var byReason = bl != null ? Obj(bl, "by_reason") : null;
        if (byReason == null || byReason.Count == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        // sub-label
        var subLbl = new TextBlock();
        subLbl.Text = T("by_reason");
        subLbl.Foreground = Muted; subLbl.FontSize = 11;
        subLbl.FontWeight = FontWeights.SemiBold;
        subLbl.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(subLbl);

        int max = 1;
        foreach (KeyValuePair<string, object> kv in byReason)
        { int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { } if (v > max) max = v; }

        foreach (KeyValuePair<string, object> kv in byReason)
        {
            int v = 0; try { v = Convert.ToInt32(kv.Value); } catch (Exception) { }
            col.Children.Add(BarRow(kv.Key, v, max, (double)v / max));
        }
        return card;
    }

    // ── (4) PASS@1 TREND ─────────────────────────────────────────────────────────
    // Timestamped bar list, newest at top, capped at 24 entries.
    UIElement BuildPassTrend(Dictionary<string, object> state)
    {
        var card = SectionCard("trend_sec", "trend_exp");
        var col  = (StackPanel)card.Child;
        object[] pt = Arr(state, "pass1_trend");

        if (pt == null || pt.Length == 0)
        {
            col.Children.Add(MuteRow(T("none")));
            return card;
        }

        int shown = 0;
        for (int i = pt.Length - 1; i >= 0 && shown < 24; i--, shown++)
        {
            var entry = pt[i] as Dictionary<string, object>;
            if (entry == null) continue;
            double pass = 0.0;
            try
            {
                if (entry.ContainsKey("pass_at_1") && entry["pass_at_1"] != null)
                    pass = Convert.ToDouble(entry["pass_at_1"]);
            }
            catch (Exception) { }
            if (pass < 0) pass = 0;
            if (pass > 1) pass = 1;
            string ts = entry.ContainsKey("ts") && entry["ts"] != null ? entry["ts"].ToString() : "?";
            col.Children.Add(BarRow(ts, -1, -1, pass,
                Num(entry.ContainsKey("pass_at_1") ? entry["pass_at_1"] : null, "0.000")));
        }
        return card;
    }

    // ── (5) ARCHIVE ──────────────────────────────────────────────────────────────
    // Genome count + QD cells.
    UIElement BuildArchive(Dictionary<string, object> state)
    {
        var card = SectionCard("archive_sec", "archive_exp");
        var col  = (StackPanel)card.Child;
        var arc  = Obj(state, "archive");

        var grid = new Grid(); grid.Margin = new Thickness(0, 10, 0, 0);
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(2, GridUnitType.Star) });
        grid.Children.Add(MetricCell(T("genomes"),  I(arc, "count").ToString(),    Fg, 0));
        grid.Children.Add(MetricCell(T("qd_cells"), I(arc, "qd_cells").ToString(), Fg, 1));
        col.Children.Add(grid);
        return card;
    }

    // ── widget helpers ────────────────────────────────────────────────────────────

    // Section card: returns a themed Border whose .Child is a StackPanel with the
    // section label + one-line explanation already prepended; callers append content rows.
    // titleKey and expKey are T() keys (not raw strings) so the card re-renders on lang toggle.
    Border SectionCard(string titleKey, string expKey)
    {
        var card = new Border();
        card.BorderThickness = new Thickness(1);
        card.CornerRadius    = new CornerRadius(10);
        card.Padding         = new Thickness(18, 14, 16, 16);
        card.Margin          = new Thickness(0, 0, 0, 12);
        card.BorderBrush     = Border;
        card.Background      = CardBg;

        var col = new StackPanel();

        // section label (small, semibold, caps)
        col.Children.Add(SectionLabel(T(titleKey)));

        // one-line explanation (muted, wraps)
        col.Children.Add(SectionExplanation(T(expKey)));

        card.Child = col;
        return card;
    }

    TextBlock SectionLabel(string text)
    {
        var t = new TextBlock();
        t.Text = text.ToUpper();
        t.Foreground  = Accent;
        t.FontSize    = 11;
        t.FontWeight  = FontWeights.SemiBold;
        t.Margin      = new Thickness(0, 0, 0, 0);
        return t;
    }

    TextBlock SectionExplanation(string text)
    {
        var t = new TextBlock();
        t.Text = text;
        t.Foreground   = Muted;
        t.FontSize     = 11.5;
        t.TextWrapping = TextWrapping.Wrap;
        t.Margin       = new Thickness(0, 3, 0, 0);
        return t;
    }

    // Metric cell: label above, value below, placed in a Grid column.
    UIElement MetricCell(string label, string value, Brush valueBrush, int colIdx)
    {
        var sp = new StackPanel();
        sp.Margin = new Thickness(0, 0, 12, 0);
        var l = new TextBlock();
        l.Text = label; l.Foreground = Muted; l.FontSize = 11;
        l.TextTrimming = TextTrimming.CharacterEllipsis;
        sp.Children.Add(l);
        var v = new TextBlock();
        v.Text = value; v.Foreground = valueBrush; v.FontSize = 15;
        v.FontWeight = FontWeights.SemiBold;
        v.TextWrapping = TextWrapping.Wrap;
        v.Margin = new Thickness(0, 3, 0, 0);
        sp.Children.Add(v);
        Grid.SetColumn(sp, colIdx);
        return sp;
    }

    // Plain muted row used for "none" placeholders.
    TextBlock MuteRow(string text)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = Muted; t.FontSize = 12.5;
        t.Margin = new Thickness(0, 8, 0, 0);
        return t;
    }

    // Table column header (small, muted, semibold).
    UIElement ColHeader(string text, int col)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = Muted; t.FontSize = 11;
        t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.Margin = new Thickness(col == 0 ? 0 : 4, 0, 0, 0);
        Grid.SetColumn(t, col);
        return t;
    }

    // Table data cell.
    UIElement RowCell(string text, Brush brush, bool semibold, int col)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = brush; t.FontSize = 12.5;
        if (semibold) t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        t.TextTrimming = TextTrimming.CharacterEllipsis;
        t.Margin = new Thickness(col == 0 ? 0 : 4, 0, 0, 0);
        Grid.SetColumn(t, col);
        return t;
    }

    // Thin horizontal rule divider.
    UIElement HRule()
    {
        var b = new Border();
        b.Height = 1; b.Background = Border;
        b.Margin = new Thickness(0, 2, 0, 4);
        return b;
    }

    // Bar row: label | proportional bar | value. Used for burned ledger (counts) and pass@1 trend (ratio).
    UIElement BarRow(string label, int v, int max, double frac) { return BarRow(label, v, max, frac, null); }
    UIElement BarRow(string label, int v, int max, double frac, string valueText)
    {
        if (frac < 0) frac = 0;
        if (frac > 1) frac = 1;

        var grid = new Grid(); grid.Margin = new Thickness(0, 3, 0, 3);
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(160) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });

        var lbl = new TextBlock();
        lbl.Text = label; lbl.Foreground = Muted; lbl.FontSize = 12;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        lbl.TextTrimming = TextTrimming.CharacterEllipsis;
        Grid.SetColumn(lbl, 0); grid.Children.Add(lbl);

        // bar: a two-star Grid inside a pill-shaped track
        var track = new Border();
        track.Height = 8; track.CornerRadius = new CornerRadius(999);
        track.Background = QuoteBg;
        track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 10, 0);
        var bargrid = new Grid();
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac,       GridUnitType.Star) });
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.0 - frac, GridUnitType.Star) });
        var fill = new Border();
        fill.Height = 8; fill.CornerRadius = new CornerRadius(999); fill.Background = Accent;
        Grid.SetColumn(fill, 0); bargrid.Children.Add(fill);
        track.Child = bargrid;
        Grid.SetColumn(track, 1); grid.Children.Add(track);

        var val = new TextBlock();
        val.Text = valueText != null ? valueText : v.ToString();
        val.Foreground = Fg; val.FontSize = 12; val.FontWeight = FontWeights.SemiBold;
        val.VerticalAlignment = VerticalAlignment.Center;
        val.TextAlignment = TextAlignment.Right;
        Grid.SetColumn(val, 2); grid.Children.Add(val);

        return grid;
    }

    // Pill badge (saturated bg, white text).
    Border Pill(string text, string ck)
    {
        var b = new Border();
        b.Background   = new SolidColorBrush(StatusColorFor(ck, _dark));
        b.CornerRadius = new CornerRadius(999);
        b.Padding      = new Thickness(9, 2, 9, 2);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = string.IsNullOrEmpty(text) ? "?" : text;
        t.Foreground = White;
        t.FontSize = 11; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }
}
