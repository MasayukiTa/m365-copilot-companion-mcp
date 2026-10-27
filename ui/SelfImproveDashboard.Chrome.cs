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

partial class SelfImproveDashboardWindow
{
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
            // WRITE the new shared location (repo .config), never the retired %APPDATA% copy,
            // so a theme/language change here is read by every coordinator and by the other
            // panels. Reading above still honours the old file while a machine migrates.
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFileForWrite));
            File.WriteAllText(SettingsFileForWrite, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
            _settingsMtime = File.GetLastWriteTimeUtc(SettingsFileForWrite).Ticks;
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
        _headBar.Padding = new Thickness(24, 16, 16, 16);
        DockPanel.SetDock(_headBar, Dock.Top);

        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // title row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // subtitle row
        headGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });  // separator

        // controls (top-right): refresh | lang | theme
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Top;

        // Refresh button (no matching glyph in 8-glyph set; use Unicode ⟳ TextBlock)
        _refreshBtn = RefreshButton();
        _refreshBtn.ToolTip = _lang == 0 ? "更新" : "Refresh";
        _refreshBtn.Click += delegate { RegenerateFeed(); };
        ctrls.Children.Add(_refreshBtn);

        _langBtn = IconButton("translate", 18);
        _langBtn.ToolTip = "日本語 / English";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); PaintChrome(); ForceRender(); };
        ctrls.Children.Add(_langBtn);

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
        _iconHost.Margin = new Thickness(0, 0, 12, 0);
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

    // The subset HAS a refresh glyph now -- the note that used to sit here said it did not, and
    // that absence is the whole reason a Unicode character was used. Adding the glyph was cheaper
    // than the inconsistency it bought.
    Button RefreshButton()
    {
        var b = new Button();
        b.Width = 36; b.Height = 30;
        b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1);
        b.Margin = new Thickness(2, 0, 2, 0);
        b.Padding = new Thickness(5, 3, 5, 3);
        b.Content = MakeIcon("refresh", 16, Fg);
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
            // Rebuilt rather than re-tinted: the content is drawn geometry now, and the old code
            // cast it to TextBlock -- which would simply return null and silently skip the repaint.
            _refreshBtn.Content = MakeIcon("refresh", 16, Fg);
        }
        // There were two vertical dividers between the icon buttons, and a repaint for them
        // here. Nothing ever called the builder and nothing ever assigned the fields, so the
        // toolbar has never had a divider and this branch could not run -- csc has been saying
        // so, unheard, because the build check was compiling a source list that left this file
        // out. Removed rather than wired up: the toolbar reads fine without them.

        // FIX 1c: freshness line is updated on each tick; just set colour here
        if (_freshnessLine != null) _freshnessLine.Foreground = Muted;
    }

    void ForceRender() { _lastSig = ""; OnTick(null, null); }
}
