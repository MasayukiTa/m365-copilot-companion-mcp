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
        card.CornerRadius    = new CornerRadius(Theme.RadPopover);
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
        iconWrap.Margin = new Thickness(0, 0, 0, 16);
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
        // ORDER IS AN ARGUMENT ABOUT WHAT MATTERS, and this one used to open on the audit
        // log. The archive is what the loop is FOR -- the solvers it has actually adopted --
        // and it sat last, as two integers, while the record of permission changes took the
        // first screen every time. The integrity checks still have to be reachable without
        // hunting, which is why they collapse to one line rather than moving out of sight,
        // and why they expand themselves when a check fails.
        _body.Children.Clear();
        var pending = BuildPending(state);
        if (pending != null) _body.Children.Add(pending);
        _body.Children.Add(BuildArchive(state));
        _body.Children.Add(BuildScorecard(state));
        _body.Children.Add(BuildAbHistory(state));
        _body.Children.Add(BuildPassTrend(state));
        _body.Children.Add(BuildUsage(state));
        _body.Children.Add(BuildBurnedLedger(state));
        var decided = BuildDecisions();
        if (decided != null) _body.Children.Add(decided);
        _body.Children.Add(BuildAuthority());
    }

}
