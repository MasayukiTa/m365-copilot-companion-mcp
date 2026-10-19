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
    // ── widget helpers ────────────────────────────────────────────────────────────

    // Section card: returns a themed Border whose .Child is a StackPanel with the
    // section label + one-line explanation already prepended; callers append content rows.
    // titleKey and expKey are T() keys (not raw strings) so the card re-renders on lang toggle.
    Border SectionCard(string titleKey, string expKey)
    {
        var card = new Border();
        card.BorderThickness = new Thickness(1);
        card.CornerRadius    = new CornerRadius(Theme.RadPopover);
        card.Padding         = new Thickness(16, 16, 16, 16);
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
        // Muted, not Accent, for the same reason the past-task eyebrow changed: Theme.cs reserves
        // that colour for a primary action and says ONLY. A rule with a silent exception at one
        // call site is read as a loose rule by the next person, who adds the second exception.
        // The caps and the weight stay -- the offence was the hue, not the device.
        t.Foreground  = Muted;
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
        track.Height = 8; track.CornerRadius = new CornerRadius(Theme.RadChip);   // = height/2. 999 renders as a lens, not a bar.
        track.Background = QuoteBg;
        track.VerticalAlignment = VerticalAlignment.Center;
        track.Margin = new Thickness(0, 0, 12, 0);
        var bargrid = new Grid();
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac,       GridUnitType.Star) });
        bargrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.0 - frac, GridUnitType.Star) });
        var fill = new Border();
        // Graphite rather than the action hue. A meter is a passive quantity, and in this app's
        // vocabulary orange means "the primary thing to press" -- a bar wearing it out-shouts the
        // buttons beside it while claiming nothing. Strong neutral against the subtle track is
        // legible without making that claim.
        fill.Height = 8; fill.CornerRadius = new CornerRadius(Theme.RadChip);
        fill.Background = Theme.Br(Theme.Secondary(_dark));
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

    // Status chip. Theme.cs states the design target in as many words -- status is "a thin
    // left rail + small chip (never a full-card fill)" -- and this was a saturated full fill
    // at radius 999, which is pixel-identical to the cockpit's RUNNING indicator. Three of
    // them in a row over a static integrity check therefore read as three things spinning.
    // Shape carries meaning whether or not that was intended.
    //
    // Now: soft tint, small radius, the status colour carried by the TEXT. The tint is mixed
    // against the card, so the chip never becomes a dark ground and the white-on-dark rule
    // has nothing to bind to. "muted" spends no colour at all -- a normal state should not
    // cost a chip, and if everything is normal the caller should not be building one.
    Border Pill(string text, string ck)
    {
        var b = new Border();
        b.CornerRadius = new CornerRadius(Theme.RadSmall);
        b.Padding      = new Thickness(8, 2, 8, 2);
        b.Margin       = new Thickness(0, 0, 6, 0);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = string.IsNullOrEmpty(text) ? "?" : text;
        t.FontSize = Theme.FsChip; t.FontWeight = FontWeights.SemiBold;
        if (ck == "good" || ck == "warn" || ck == "bad")
        {
            Color c = StatusColorFor(ck, _dark);
            b.Background      = new SolidColorBrush(Mix(c, CardColor(), 0.14));
            b.BorderBrush     = new SolidColorBrush(Mix(c, CardColor(), 0.45));
            b.BorderThickness = new Thickness(1);
            t.Foreground      = new SolidColorBrush(c);
        }
        else
        {
            t.Foreground = Muted;
            b.Padding    = new Thickness(0, 2, 8, 2);
        }
        b.Child = t;
        return b;
    }

    // "3日前" / "3 d ago" from a unix timestamp. The ledger has carried a ts on every record
    // from the beginning and the history displayed none of it: a list of acts with no times
    // is not a history, and it is the single largest reason the section could not be read.
    string AgoText(double ts, double now)
    {
        bool ja = _lang == 0;
        double sec = now - ts;
        if (sec < 90) return ja ? "たった今" : "just now";
        if (sec < 3600) return ((int)(sec / 60)).ToString() + (ja ? "分前" : " min ago");
        if (sec < 86400) return ((int)(sec / 3600)).ToString() + (ja ? "時間前" : " hr ago");
        return ((int)(sec / 86400)).ToString() + (ja ? "日前" : " d ago");
    }

    // A DERIVED SUMMARY, NOT THE RECORD. The reason a record carries is prose the agent typed
    // in whichever language it was working in, so toggling this window to English left those
    // lines in Japanese -- they are not interface text. Shortening and translating one needs a
    // model, and that is the only thing on this screen a model does.
    //
    // Read from a cache keyed by the record's own hash, so a summary cannot attach to a
    // different record and an edited record simply misses. Missing means the raw reason, which
    // is exactly what this window showed before summaries existed: every failure lands there.
    Dictionary<string, object> LoadSummaries()
    {
        try
        {
            string p = Path.Combine(RepoRoot(), ".fleet", "selfimprove", "record_summaries.json");
            if (!File.Exists(p)) return null;
            return _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8))
                   as Dictionary<string, object>;
        }
        catch (Exception) { return null; }
    }

    string SummaryFor(Dictionary<string, object> row, Dictionary<string, object> cache)
    {
        try
        {
            if (cache == null || row == null) return "";
            object h; row.TryGetValue("hash", out h);
            string key = Convert.ToString(h);
            if (key.Length == 0) return "";
            object e; cache.TryGetValue(key, out e);
            var entry = e as Dictionary<string, object>;
            if (entry == null) return "";
            object v; entry.TryGetValue(_lang == 0 ? "ja" : "en", out v);
            return Convert.ToString(v);
        }
        catch (Exception) { return ""; }
    }

    // THE HEADLINE IS DERIVED, NOT TYPED. The first line used to be the actor -- a module
    // path like "relay.selfimprove.frozen CLI", which takes two values across the whole ledger
    // and tells a reader nothing -- and the line that actually read as the title was the raw
    // --reason string the agent had typed. A record whose headline is its own input text
    // cannot be scanned.
    //
    // Every part of this is computable from the record, so none of it is generated: the event
    // maps to a fixed verb, the target is the basenames of `changed`. Substituting a language
    // model for a lookup table is the same design fault this system has committed before.
    string EventVerb(string kind)
    {
        if (kind == "rebless")           return T("ev_rebless");
        if (kind == "baseline_mismatch") return T("ev_mismatch");
        if (kind == "rebless_revoke")    return T("ev_revoke");
        if (kind == "genome_apply")      return T("ev_apply");
        if (kind == "genome_revert")     return T("ev_revert");
        if (kind == "branch_create")     return T("ev_branch_new");
        if (kind == "branch_delete")     return T("ev_branch_del");
        return kind;
    }

    // "UNPINNED:" marks a file that was not yet in the baseline when the drift was detected.
    // It is a state marker on the same path, so a mismatch and the re-signing that closed it
    // disagree on the string while naming the same file. Compared raw, 2 of 21 pairs looked
    // like "detected something other than what was approved" -- which is a real anomaly and
    // must never be hidden, so the comparison has to be able to tell the two cases apart.
    static string StripPin(string path)
    {
        return path != null && path.StartsWith("UNPINNED:") ? path.Substring(9) : path;
    }

    static List<string> ChangedPaths(Dictionary<string, object> row)
    {
        var out_ = new List<string>();
        object changed; row.TryGetValue("changed", out changed);
        var files = changed as Dictionary<string, object>;
        if (files != null) foreach (var k in files.Keys) out_.Add(StripPin(k));
        out_.Sort();
        return out_;
    }

    static string BaseNames(List<string> paths)
    {
        var seen = new List<string>();
        foreach (var p in paths)
        {
            string b = p;
            int i = b.LastIndexOfAny(new char[] { '/', '\\' });
            if (i >= 0) b = b.Substring(i + 1);
            if (b.Length > 0 && !seen.Contains(b)) seen.Add(b);
        }
        return string.Join(", ", seen.ToArray());
    }

    // A record written by a test run: everything it touched is under a temp directory. 20 of
    // the 78 records in the live ledger are these -- test genome applies and reverts that were
    // written to the operator's real ledger rather than a redirected one. They are real rows
    // and stay in the file; they are simply not events in this repository's life.
    static bool IsTestRecord(Dictionary<string, object> row)
    {
        var paths = new List<string>();
        object changed; row.TryGetValue("changed", out changed);
        var files = changed as Dictionary<string, object>;
        if (files == null || files.Count == 0) return false;
        foreach (var k in files.Keys)
        {
            // ABSOLUTE MEANS ELSEWHERE. Matching the substring "tmp" would fold a real record
            // about a repository file whose path merely contains it -- and folding a real act
            // out of sight is the failure this section spent the morning fixing. Every path a
            // genuine record names is repository-relative; every one a test run wrote is an
            // absolute path into a temp directory.
            string p = StripPin(k);
            bool absolute = (p.Length > 1 && p[1] == ':')
                         || p.StartsWith("\\") || p.StartsWith("/");
            if (!absolute) return false;
        }
        return true;
    }

    static bool SameTargets(Dictionary<string, object> a, Dictionary<string, object> b)
    {
        var pa = ChangedPaths(a); var pb = ChangedPaths(b);
        if (pa.Count != pb.Count) return false;
        for (int i = 0; i < pa.Count; i++) if (pa[i] != pb[i]) return false;
        return true;
    }

    string GapText(double a, double b)
    {
        double sec = Math.Abs(b - a);
        bool ja = _lang == 0;
        if (sec < 90) return ja ? "すぐ" : "moments";
        if (sec < 3600) return ((int)(sec / 60)).ToString() + (ja ? "分" : " min");
        if (sec < 86400) return ((int)(sec / 3600)).ToString() + (ja ? "時間" : " hr");
        return ((int)(sec / 86400)).ToString() + (ja ? "日" : " d");
    }

    // The colour a record spends, spent on the event NAME rather than on a strip beside it.
    //
    // NO COLOURED LEFT RAILS. This was written with one, on the strength of a line in Theme.cs
    // recommending them, and the operator's response was that they have said repeatedly they
    // dislike the look -- a thick coloured bar down the left edge reads as a sticky note. The
    // line in Theme.cs has been corrected too, because a stale recommendation will simply be
    // followed again by whoever reads it next.
    //
    // Re-signing is the routine event here (24 in a week), so it stays neutral; the anomalies
    // are what get colour, which is the same rule the header follows.
    Brush KindBrush(string kind)
    {
        if (kind == "baseline_mismatch" || kind == "revoke")
            return new SolidColorBrush(StatusColorFor("bad", _dark));
        return Fg;
    }

    static void SetClip(TextBlock t, bool open)
    {
        if (t == null) return;
        t.TextWrapping = open ? TextWrapping.Wrap : TextWrapping.NoWrap;
        t.TextTrimming = open ? TextTrimming.None : TextTrimming.CharacterEllipsis;
    }

    // Long single lines, clipped rather than wrapped: eight records that each wrap to four
    // lines is the same unreadable wall in a different shape. The full text is on the tooltip
    // and one click on the record opens every clipped line in it.
    TextBlock ClipLine(string text, Brush fg, double size, bool mono)
    {
        var t = new TextBlock();
        t.Text = text; t.Foreground = fg; t.FontSize = size;
        if (mono) t.FontFamily = new FontFamily(Theme.CodeFont);
        t.TextWrapping = TextWrapping.NoWrap;
        t.TextTrimming = TextTrimming.CharacterEllipsis;
        if (!string.IsNullOrEmpty(text)) t.ToolTip = text;
        return t;
    }
}
