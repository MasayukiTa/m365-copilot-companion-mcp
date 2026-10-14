// FleetCockpit.FanoutGroups.cs -- fan-out group lines and the budget bars.
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

    // ── Split group (分割グループ) line on a fan-out parent card ─────────────────────────────
    // `groups` in status.json is written by relay/family_view.py (contract in its docstring).
    // OWNER RULES: never the long goal text -- only the capped `ledger` strings it carries;
    // plain Japanese wording; one small line, no popup, no toast. Read-only.
    Dictionary<string, object> GroupOfParent(Dictionary<string, object> w)
    {
        if (w == null || _lastRoot == null) return null;
        object go;
        if (!_lastRoot.TryGetValue("groups", out go) || !(go is object[])) return null;
        string nm = S(w, "name");
        if (nm.Length == 0) return null;
        foreach (object o in (object[])go)
        {
            var g = o as Dictionary<string, object>;
            var par = g != null ? Obj(g, "parent") : null;
            if (par != null && S(par, "name") == nm) return g;
        }
        return null;
    }

    static string GroupCount(Dictionary<string, object> ch, string key, string label)
    {
        int n = I(ch, key);
        return n > 0 ? label + " " + n : "";
    }

    // One line: 分割グループ 子N件: 待機 a · 実行中 b · ... / 統合: <label> [/ 下位グループ n件]
    // The words live in GroupTreeView (EffortPolicy.cs, tested by a compiled harness).
    string GroupLineText(Dictionary<string, object> w)
    {
        return GroupTreeView.Line(GroupOfParent(w), _lang == 0);
    }

    // The second line of a root group: tree usage + the depth-cap note. "" when not reported.
    string GroupExtraText(Dictionary<string, object> w, out int level)
    {
        level = 0;
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return "";
        return GroupTreeView.ExtraText(_lastRoot, g, _lang == 0, out level) ?? "";
    }

    string GroupExtraSig(Dictionary<string, object> w)
    {
        int lv;
        string x = GroupExtraText(w, out lv);
        return x.Length == 0 ? "" : "|x" + lv + x;
    }

    // Cards whose usage line the person collapsed (click the group line). Not persisted.
    readonly HashSet<string> _groupExtraHidden = new HashSet<string>();

    UIElement BuildGroupLine(Dictionary<string, object> w)
    {
        string text = GroupLineText(w);
        if (text.Length == 0) return null;
        var g = GroupOfParent(w);
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 3, 0, 0) };
        int xLevel;
        string extra = GroupExtraText(w, out xLevel);
        TextBlock extraTb = null;
        UIElement barRow = null;
        string cardName = S(w, "name");
        row.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2)
        {
            e2.Handled = true;
            if (extraTb == null) return;
            bool hide = extraTb.Visibility == Visibility.Visible;
            extraTb.Visibility = hide ? Visibility.Collapsed : Visibility.Visible;
            if (barRow != null) barRow.Visibility = extraTb.Visibility;
            if (hide) _groupExtraHidden.Add(cardName); else _groupExtraHidden.Remove(cardName);
        };
        row.Children.Add(new TextBlock {
            Text = text, Foreground = Muted, FontSize = 12,
            VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis,
            TextWrapping = TextWrapping.NoWrap, MaxWidth = 560 });
        // Constraint tokens (dates, quoted names, amounts) from the capped ledger: small chips.
        var led = Obj(g, "ledger");
        int shown = 0;
        var tip = new List<string>();
        if (led != null)
        {
            if (S(led, "task").Length > 0) tip.Add(S(led, "task"));
            object co, to;
            if (led.TryGetValue("constraints", out co) && co is object[])
                foreach (object c in (object[])co) tip.Add("- " + (c != null ? c.ToString() : ""));
            if (led.TryGetValue("tokens", out to) && to is object[])
                foreach (object t in (object[])to)
                {
                    string ts = t != null ? t.ToString() : "";
                    if (ts.Length == 0 || shown >= 4) continue;
                    var chip = Pill(ts, "neutral");
                    chip.Margin = new Thickness(8, 0, 0, 0);
                    row.Children.Add(chip);
                    shown++;
                }
        }
        if (tip.Count > 0) row.ToolTip = string.Join("\n", tip.ToArray());
        if (extra.Length == 0) return row;
        extraTb = new TextBlock {
            Text = extra, FontSize = 12, Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 1, 0, 0),
            Foreground = xLevel >= 2 ? Theme.Br(Theme.Danger(_dark))
                         : xLevel == 1 ? Theme.Br(Theme.Warning(_dark)) : Muted,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap, MaxWidth = 560,
            Visibility = _groupExtraHidden.Contains(cardName) ? Visibility.Collapsed : Visibility.Visible };
        var both = new StackPanel { Orientation = Orientation.Vertical };
        both.Children.Add(row);
        both.Children.Add(extraTb);
        var barsAll = new StackPanel { Orientation = Orientation.Vertical, Visibility = extraTb.Visibility };
        var turnsBar = BuildBudgetBar(w, Visibility.Visible);
        if (turnsBar != null) barsAll.Children.Add(turnsBar);
        // The other ledger dimensions (workers, concurrent, minutes), additive under the turns bar.
        int[] moreIdx = { 0, 1, 3 };
        for (int mi = 0; mi < moreIdx.Length; mi++)
        {
            var more = BuildBudgetBarAt(w, moreIdx[mi]);
            if (more != null) barsAll.Children.Add(more);
        }
        if (barsAll.Children.Count > 0) { barRow = barsAll; both.Children.Add(barRow); }
        return both;
    }

    // Turns used against the tree's turn cap: a thin bar and "turns 12 / 40". Display only; the
    // words and the fraction come from GroupTreeView.BudgetBar. null when no cap is reported.
    UIElement BuildBudgetBar(Dictionary<string, object> w, Visibility vis)
    {
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return null;
        double frac; int level;
        string text = GroupTreeView.BudgetBar(_lastRoot, g, _lang == 0, out frac, out level);
        if (text == null) return null;
        var fill = level >= 2 ? Theme.Br(Theme.Danger(_dark))
                   : level == 1 ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Info(_dark));
        const double width = 120;
        var track = new Grid { Width = width, Height = 6, VerticalAlignment = VerticalAlignment.Center };
        track.Children.Add(new Border { Background = Theme.Br(Theme.Border(_dark)), CornerRadius = new CornerRadius(3) });
        track.Children.Add(new Border {
            Background = fill, CornerRadius = new CornerRadius(3),
            Width = Math.Max(frac > 0 ? 3 : 0, width * frac), HorizontalAlignment = HorizontalAlignment.Left });
        var bar = new StackPanel {
            Orientation = Orientation.Horizontal, Visibility = vis,
            Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 2, 0, 0) };
        bar.Children.Add(track);
        bar.Children.Add(new TextBlock {
            Text = text, FontSize = 12, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted,
            VerticalAlignment = VerticalAlignment.Center });
        System.Windows.Automation.AutomationProperties.SetAutomationId(bar, "budgetBar_turns");
        return bar;
    }

    // One more ledger dimension as a bar (idx: 0 total, 1 active, 3 minutes), same drawing and
    // colouring as the turns bar. Display only; null when that dimension has no cap reported.
    UIElement BuildBudgetBarAt(Dictionary<string, object> w, int idx)
    {
        var g = GroupOfParent(w);
        if (g == null || _lastRoot == null) return null;
        double frac; int level;
        string text = GroupTreeView.BudgetBarAt(_lastRoot, g, idx, _lang == 0, out frac, out level);
        if (text == null) return null;
        var fill = level >= 2 ? Theme.Br(Theme.Danger(_dark))
                   : level == 1 ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Info(_dark));
        const double width = 120;
        var track = new Grid { Width = width, Height = 6, VerticalAlignment = VerticalAlignment.Center };
        track.Children.Add(new Border { Background = Theme.Br(Theme.Border(_dark)), CornerRadius = new CornerRadius(3) });
        track.Children.Add(new Border {
            Background = fill, CornerRadius = new CornerRadius(3),
            Width = Math.Max(frac > 0 ? 3 : 0, width * frac), HorizontalAlignment = HorizontalAlignment.Left });
        var bar = new StackPanel {
            Orientation = Orientation.Horizontal,
            Margin = new Thickness(24 + GroupTreeView.IndentPx(g), 2, 0, 0) };
        bar.Children.Add(track);
        bar.Children.Add(new TextBlock {
            Text = text, FontSize = 12, Margin = new Thickness(8, 0, 0, 0), Foreground = Muted,
            VerticalAlignment = VerticalAlignment.Center });
        System.Windows.Automation.AutomationProperties.SetAutomationId(bar, "budgetBar_" + FanoutBudgetView.ReportKeys[idx]);
        return bar;
    }
}
