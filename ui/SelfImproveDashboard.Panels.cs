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
        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int i = 0; i < 4; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var goodBrush = new SolidColorBrush(StatusColorFor("good", _dark));
        // ORDINARY WORK IS THE HEADLINE, because that is what "how is the fleet doing" means.
        // These cells used to show the blend of ordinary goals and benchmark workers, whose
        // completion rates differ by about ten times -- so the number moved with whatever bulk
        // job was queued. Measured 2026-09-12: this row read 0.39 / 0.20 while ordinary work
        // stood at 0.45 / 0.89 and a SWE-bench arm filled 41 of the last 50 rows.
        var wl   = Obj(u, "workload");
        var ord_ = Obj(wl, "ordinary");
        var bch  = Obj(wl, "bench");
        grid.Children.Add(MetricCell(T("u_completion"), Pct(ord_, "completion_rate"),  goodBrush, 0));
        grid.Children.Add(MetricCell(T("u_recent"),     Pct(ord_, "recent_completion_rate"), Fg, 1));
        grid.Children.Add(MetricCell(T("u_turns"),
            Num(u.ContainsKey("median_turns") ? u["median_turns"] : null, "0.#"), Fg, 2));
        grid.Children.Add(MetricCell(T("u_tasks"), I(u, "n_tasks").ToString(), Fg, 3));
        col.Children.Add(grid);

        // EVERY RATE WITH ITS OWN DENOMINATOR, and the blend still visible. A rate over nine
        // rows and a rate over forty-one must not be read as the same kind of thing, and a
        // number that is hidden is one the next person re-derives wrongly.
        var split = new TextBlock {
            Text = string.Format("{0}  —  n={1} / {2}   ·   bench {3} (n={4} / {5})   ·   blended {6} / {7}",
                                 T("u_split"),
                                 I(ord_, "n"), I(ord_, "recent_n"),
                                 Pct(bch, "completion_rate"), I(bch, "n"), I(bch, "recent_n"),
                                 Pct(u, "completion_rate"), Pct(u, "recent_completion_rate")),
            TextWrapping = TextWrapping.Wrap, FontSize = 11.5, Margin = new Thickness(2, 6, 2, 0) };
        split.Foreground = Muted;
        col.Children.Add(split);

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
            mixLine.Margin = new Thickness(0, 12, 0, 0);
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

        // persona / advice leak rate — quality metric measured from real usage (no benchmark).
        BuildPersonaLeak(u, col);

        return card;
    }

    // persona_leak_rate : float|null (share of outputs leaking coaching/lecture/ego),
    // quality_scored : int (how many runs could be scored),
    // persona_flagged : [{key, signals[], excerpt}, ...] (leak examples, may be empty).
    // null leak rate => show "計測前" (no fabricated number). Flagged shown only when non-empty.
    void BuildPersonaLeak(Dictionary<string, object> u, StackPanel col)
    {
        // divider above the quality block to set it apart from the benchmark-free usage counts
        col.Children.Add(HRule());

        // label row: metric name + value (value coloured by whether any leak)
        var labelRow = new StackPanel();
        labelRow.Orientation = Orientation.Horizontal;
        labelRow.Margin = new Thickness(0, 8, 0, 0);

        var nameTb = new TextBlock();
        nameTb.Text = T("u_persona") + ": ";
        nameTb.Foreground = Muted; nameTb.FontSize = 12.5;
        nameTb.VerticalAlignment = VerticalAlignment.Center;
        labelRow.Children.Add(nameTb);

        object rateObj = u.ContainsKey("persona_leak_rate") ? u["persona_leak_rate"] : null;
        int scored = I(u, "quality_scored");

        var valTb = new TextBlock();
        valTb.FontSize = 15; valTb.FontWeight = FontWeights.SemiBold;
        valTb.VerticalAlignment = VerticalAlignment.Center;

        if (rateObj == null)
        {
            // null => not yet measured; do NOT invent a number
            valTb.Text = T("u_persona_pre");
            valTb.Foreground = Muted;
        }
        else
        {
            double rate = 0.0;
            bool ok = true;
            try { rate = Convert.ToDouble(rateObj); } catch (Exception) { ok = false; }
            if (!ok)
            {
                valTb.Text = T("u_persona_pre");
                valTb.Foreground = Muted;
            }
            else
            {
                valTb.Text = (rate * 100.0).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) + "%";
                // green when clean (0%), warning otherwise
                valTb.Foreground = new SolidColorBrush(StatusColorFor(rate <= 0.0 ? "good" : "warn", _dark));
            }
        }
        labelRow.Children.Add(valTb);

        if (scored > 0)
        {
            var nTb = new TextBlock();
            nTb.Text = "  (" + scored.ToString() + T("u_persona_n") + ")";
            nTb.Foreground = Muted; nTb.FontSize = 12;
            nTb.VerticalAlignment = VerticalAlignment.Center;
            labelRow.Children.Add(nTb);
        }
        col.Children.Add(labelRow);

        // helper line: stresses this number is from real usage, not a benchmark
        col.Children.Add(SectionExplanation(T("u_persona_exp")));

        // flagged excerpts (real leak examples) — only when non-empty
        object[] flagged = Arr(u, "persona_flagged");
        if (flagged != null && flagged.Length > 0)
        {
            var egLbl = new TextBlock();
            egLbl.Text = T("u_persona_eg");
            egLbl.Foreground = Muted; egLbl.FontSize = 11;
            egLbl.FontWeight = FontWeights.SemiBold;
            egLbl.Margin = new Thickness(0, 8, 0, 3);
            col.Children.Add(egLbl);

            int shown = 0;
            for (int i = 0; i < flagged.Length && shown < 3; i++)
            {
                var fr = flagged[i] as Dictionary<string, object>;
                if (fr == null) continue;
                string excerpt = S(fr, "excerpt");
                if (string.IsNullOrEmpty(excerpt)) continue;

                var quote = new Border();
                quote.Background      = QuoteBg;
                quote.CornerRadius    = new CornerRadius(Theme.RadSmall);
                // The tinted ground already says "this is quoted material"; the bar only made
                // it a sticky note.
                quote.Padding         = new Thickness(8, 5, 8, 5);
                quote.Margin          = new Thickness(0, 3, 0, 0);

                var qt = new TextBlock();
                qt.Text = excerpt;
                qt.Foreground = Muted; qt.FontSize = 11.5;
                qt.TextWrapping = TextWrapping.Wrap;
                quote.Child = qt;
                col.Children.Add(quote);
                shown++;
            }
        }
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
        card.BorderThickness = new Thickness(2);
        card.CornerRadius    = new CornerRadius(Theme.RadPopover);
        card.Padding         = new Thickness(16, 16, 16, 16);
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
            chip.Margin = new Thickness(12, 0, 0, 0);
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

            // No accent strip: the verdict is already stated twice in the row, as the chip at
            // the end and as the colour on net_pp. A third copy as a bar down the left edge
            // adds no information and is the look the operator has asked not to see.
            var strip = new Border();
            strip.Padding = new Thickness(0, 5, 0, 5);

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
        totalRow.Margin = new Thickness(0, 8, 0, 12);
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

            // A SUPERSEDED POINT IS NOT A STEP IN A SERIES. The archive holds one genome
            // measured twice -- 0.34, then 0.50 after the first grade turned out to be a
            // host artifact -- and drawn plainly this series says the loop improved by 16
            // points. It did not; the same scaffold was measured again. Labelled and dimmed
            // rather than dropped, because the loop did produce that number and the record
            // of the correction is what makes it auditable.
            bool superseded = false;
            try
            {
                if (entry.ContainsKey("superseded") && entry["superseded"] != null)
                    superseded = Convert.ToBoolean(entry["superseded"]);
            }
            catch (Exception) { }

            // The recorded reason beats the generic label whenever there is one: "(replaced by
            // a re-measurement)" says a row was replaced, and the reader's next question is
            // always why.
            string note = entry.ContainsKey("note") && entry["note"] != null
                        ? Convert.ToString(entry["note"]) : "";
            string tag = note.Length > 0 ? note : T("superseded");
            var barRow = BarRow(superseded ? ts + "   " + tag : ts, -1, -1, pass,
                Num(entry.ContainsKey("pass_at_1") ? entry["pass_at_1"] : null, "0.000"));
            if (superseded)
            {
                var fe = barRow as FrameworkElement;
                if (fe != null) fe.Opacity = 0.45;
            }
            col.Children.Add(barRow);
        }
        return card;
    }

    // ── (5) ARCHIVE ──────────────────────────────────────────────────────────────
    // Genome count + QD cells.
    // Highest pass@1 across adopted genomes. The scorecard's "latest" answers a different
    // question -- what the last cycle scored -- and the two diverge exactly when a cycle
    // regresses, which is the case worth being able to see.
    string BestPass(Dictionary<string, object> arc)
    {
        object[] gs = arc != null ? Arr(arc, "genomes") : null;
        if (gs == null || gs.Length == 0) return "?";
        double best = double.MinValue;
        foreach (var o in gs)
        {
            var g = o as Dictionary<string, object>;
            if (g == null || !g.ContainsKey("pass_at_1") || g["pass_at_1"] == null) continue;
            try { double v = Convert.ToDouble(g["pass_at_1"]); if (v > best) best = v; }
            catch (Exception) { }
        }
        return best == double.MinValue ? "?" : best.ToString("0.###");
    }
    UIElement BuildArchive(Dictionary<string, object> state)
    {
        var card = SectionCard("archive_sec", "archive_exp");
        var col  = (StackPanel)card.Child;
        var arc  = Obj(state, "archive");
        var sum  = Obj(state, "summary");

        var grid = new Grid(); grid.Margin = new Thickness(0, 12, 0, 0);
        for (int c = 0; c < 3; c++)
        {
            var cd = new ColumnDefinition(); cd.Width = new GridLength(1, GridUnitType.Star);
            grid.ColumnDefinitions.Add(cd);
        }
        grid.Children.Add(MetricCell(T("genomes"),  I(arc, "count").ToString(),    Fg, 0));
        grid.Children.Add(MetricCell(T("qd_cells"), I(arc, "qd_cells").ToString(), Fg, 1));
        grid.Children.Add(MetricCell(T("g_best"), BestPass(arc), Fg, 2));
        col.Children.Add(grid);

        // THE GENOMES THEMSELVES. The feed has carried id, parent, pass@1, the gate verdict
        // and the behavioural descriptors for every adopted genome all along; the card showed
        // two integers derived from that list and threw the list away. Nothing here needed new
        // data -- only for the screen to stop discarding what it was already given.
        object[] gs = arc != null ? Arr(arc, "genomes") : null;
        if (gs == null || gs.Length == 0) { col.Children.Add(MuteRow(T("none"))); return card; }

        var table = new Grid(); table.Margin = new Thickness(0, 16, 0, 0);
        double[] w = { 1.1, 0.7, 1.0, 2.0, 1.0 };
        for (int c = 0; c < w.Length; c++)
        {
            var cd = new ColumnDefinition(); cd.Width = new GridLength(w[c], GridUnitType.Star);
            table.ColumnDefinitions.Add(cd);
        }
        var hr = new RowDefinition(); hr.Height = GridLength.Auto;
        table.RowDefinitions.Add(hr);
        string[] heads = { T("g_id"), T("g_pass"), T("g_gate"), T("g_desc"), T("g_parent") };
        for (int c = 0; c < heads.Length; c++)
        {
            var h = ColHeader(heads[c], c);
            Grid.SetRow((UIElement)h, 0);
            table.Children.Add((UIElement)h);
        }

        int shownG = 0;
        for (int i = gs.Length - 1; i >= 0 && shownG < 12; i--)
        {
            var g = gs[i] as Dictionary<string, object>;
            if (g == null) continue;
            var rd = new RowDefinition(); rd.Height = GridLength.Auto;
            table.RowDefinitions.Add(rd);
            int row = table.RowDefinitions.Count - 1;

            string gid    = S(g, "id");
            string gate   = S(g, "gate_verdict");
            object parent = g.ContainsKey("parent_id") ? g["parent_id"] : null;
            string ptxt   = parent == null ? T("g_root") : Convert.ToString(parent);

            var desc = Obj(g, "descriptors");
            var dparts = new List<string>();
            if (desc != null)
                foreach (var kv in desc)
                    dparts.Add(Convert.ToString(kv.Value));
            string dtxt = string.Join("  \u00b7  ", dparts.ToArray());

            int nMeas = 1;
            try
            {
                if (g.ContainsKey("measurements") && g["measurements"] != null)
                    nMeas = Convert.ToInt32(g["measurements"]);
            }
            catch (Exception) { }

            // Deduplicating the rows must not erase the fact that there were several. The id
            // is a content hash, so a repeat means the same scaffold was measured again --
            // which is exactly what the reader needs to know before reading the number.
            var idTb = RowCell(gid + (nMeas > 1 ? "  ×" + nMeas : ""), Fg, true, 0) as TextBlock;
            if (idTb != null)
            {
                idTb.FontFamily = new FontFamily(Theme.CodeFont);
                if (nMeas > 1) idTb.ToolTip = T("measured_n") + ": " + nMeas;
            }
            var cells = new UIElement[] {
                (UIElement)idTb,
                (UIElement)RowCell(Num(g.ContainsKey("pass_at_1") ? g["pass_at_1"] : null, "0.###"),
                                   Fg, true, 1),
                (UIElement)RowCell(gate, Muted, false, 2),
                (UIElement)RowCell(dtxt, Muted, false, 3),
                (UIElement)RowCell(ptxt, Theme.Br(Theme.Faint(_dark)), false, 4)
            };
            for (int c = 0; c < cells.Length; c++)
            {
                var fe = cells[c] as FrameworkElement;
                if (fe != null) fe.Margin = new Thickness(c == 0 ? 0 : 4, 6, 0, 0);
                Grid.SetRow(cells[c], row);
                table.Children.Add(cells[c]);
            }
            shownG++;
        }
        col.Children.Add(table);
        if (gs.Length > shownG)
            col.Children.Add(MuteRow("+" + (gs.Length - shownG) + (_lang == 0 ? " 件" : " more")));
        return card;
    }
}
