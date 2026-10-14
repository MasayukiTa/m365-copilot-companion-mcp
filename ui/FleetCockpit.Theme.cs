// FleetCockpit.Theme.cs -- theme brushes, icons/glyphs and the flat button primitives.
// Part of `partial class CockpitWindow` (the other parts: ui/FleetCockpit.cs and ui/FleetCockpit.*.cs).
// Moved verbatim out of ui/FleetCockpit.cs: no code was changed. Every part is listed on the
// Build "FleetCockpit" line of ui/rebuild_ui.ps1, which is the one list of what is compiled.
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

partial class CockpitWindow
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
        AccentFg = Theme.Br(Theme.AccentFg(_dark));
        AccentFill = Theme.Br(Theme.AccentFill(_dark));
        SuccessFill = Theme.Br(Theme.SuccessFill(_dark));
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
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(Theme.RadChip));
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
    //: One height for every control in the header strip. Measured on screen before this
    //: existed: the icon buttons were 30 tall, pause and stop 26, the dropdowns 22 and the
    //: approval chip 20 -- four heights in one 400px row, each vertically centred, so no two
    //: of them shared a top or a bottom edge with anything.
    internal const double HeaderCtrlH = 30;

    Button IconButton(string glyph, double size, string autoName)
    {
        var b = new Button(); b.Width = 36; b.Height = HeaderCtrlH; b.Cursor = Cursors.Hand;
        b.BorderThickness = new Thickness(1); b.Margin = new Thickness(4, 0, 0, 0);
        // THE SAME BOX AS THE ONES BESIDE THEM. These four ran on the stock Aero template, which
        // draws square corners, while pause and stop next door use FlatButtonTemplate and are
        // rounded to RadSmall -- six buttons in one strip, two different shapes, and once their
        // heights were made to match the difference was all that was left to notice.
        b.Template = FlatButtonTemplate();
        b.Background = BtnBg; b.BorderBrush = Border;
        b.Content = MakeIcon(glyph, size, Fg); b.Tag = glyph;
        if (!string.IsNullOrEmpty(autoName))
            System.Windows.Automation.AutomationProperties.SetName(b, autoName);
        return b;
    }
}
