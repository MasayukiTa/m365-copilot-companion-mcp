// FleetCockpit.SettingsControls.cs -- settings controls: effort, fan-out, budget, depth, merge, auto-resume, tool probe, approval mode.
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

    // THE FALLBACK BUTTON. Writes {"reunlock": "<name-or-empty>"} through the SAME command writer
    // SetDiskFloor/SetRamFloor use -- one file per command, so it cannot clobber a concurrent
    // close/steer/set_maxtabs. CARRIES NO SECRET: fleet_runner.py's
    // apply_reunlock reads the unlock password locally, on the coordinator's own machine, from
    // that machine's .env -- never from this file, which is plain text read by several
    // processes. `target` blank means every live worker (fleet_runner treats "" the same as the
    // steer broadcast rule it is built on; "*" is accepted there too for the same reason).
    //
    // The two methods that served the re-unlock control were removed with it. One wrote a
    // command for a person to send; the other turned the receipt back into a note on a panel.
    // Neither has a caller now: the harness delivers unlocks itself every sweep, and the
    // receipt is read where records are read rather than shown while someone is looking.

    // Effort selector: ComboBox dropdown for min/max/ultra/auto.
    // Persists effort= to settings.txt; the fleet runner reads it at launch.
    static readonly string[] _effortModes = { "min", "max", "ultra", "auto" };
    UIElement EffortControl()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);

        _effortLbl = new TextBlock(); _effortLbl.VerticalAlignment = VerticalAlignment.Center;
        _effortLbl.FontSize = 12; _effortLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_effortLbl);

        _effortBox = new ComboBox();
        _effortBox.ToolTip = _lang == 0 ? "推論の深さ（各ワーカーの調査/反論の強度）" : "Reasoning effort (research/refute depth per worker)";
        _effortBox.Cursor = Cursors.Hand; _effortBox.FontSize = 12;
        _effortBox.FontWeight = FontWeights.SemiBold; _effortBox.MinWidth = 78;
        _effortBox.Padding = new Thickness(8, 2, 4, 2);
        _effortBox.VerticalAlignment = VerticalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(_effortBox, _lang == 0 ? "推論" : "Reasoning");
        FillComboWithHelp(_effortBox, _effortModes, EffortHelp(), _effort);  // per-option hover help
        _effortBox.DropDownOpened += delegate { CloseHeaderPopups("effort"); };
        _effortBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_effortBox);
            if (string.IsNullOrEmpty(sel) || sel == _effort) return;
            _effort = sel;
            SaveKey("effort", _effort);
        };
        wrap.Children.Add(_effortBox);

        PaintEffort();
        return wrap;
    }
    // Effort policy selector (off|shadow|on). Persists effort_policy= to settings.txt, which
    // relay/effort_policy.py re-reads at every decision (each_gate). This control never passes
    // MCP_EFFORT_POLICY itself; what is REALLY in effect is what the runner reports in
    // status.json (see PaintEffortPolicyInEffect), and an inherited env override is shown as a
    // conflict. All wording/parsing lives in the WPF-free EffortPolicy.cs.
    // Layout shared by the combo rows that live in the gear popup (effort policy, fan-out, depth,
    // hierarchical merge, write scope): label left / combo right, and the runner's "in effect" and
    // "pending" lines wrapped underneath as the row's secondary text.
    UIElement SettingsComboBlock(TextBlock lbl, ComboBox box, TextBlock now, TextBlock pending)
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Vertical;
        wrap.Margin = new Thickness(0, 4, 0, 4);
        var row = new DockPanel(); row.LastChildFill = false;
        DockPanel.SetDock(box, Dock.Right); row.Children.Add(box);
        lbl.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(lbl, Dock.Left); row.Children.Add(lbl);
        wrap.Children.Add(row);
        foreach (TextBlock t in new[] { now, pending })
        {
            t.Margin = new Thickness(0, 2, 0, 0); t.MaxWidth = 300; t.TextWrapping = TextWrapping.Wrap;
            wrap.Children.Add(t);
        }
        return wrap;
    }
    UIElement EffortPolicyControl()
    {
        _effortPolicyLbl = new TextBlock(); _effortPolicyLbl.VerticalAlignment = VerticalAlignment.Center;
        _effortPolicyLbl.FontSize = 12;

        _effortPolicyBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_effortPolicyBox, "effortPolicyBox");
        _effortPolicyBox.ToolTip = EffortPolicyView.Help(_lang == 0) + "\n" + EffortPolicyView.TakeEffectTip(_lang == 0);
        _effortPolicyBox.Cursor = Cursors.Hand; _effortPolicyBox.FontSize = 12;
        _effortPolicyBox.FontWeight = FontWeights.SemiBold; _effortPolicyBox.MinWidth = 78;
        _effortPolicyBox.Padding = new Thickness(8, 2, 4, 2);
        _effortPolicyBox.VerticalAlignment = VerticalAlignment.Center;
        var epHelp = new Dictionary<string, string>();
        foreach (string m in EffortPolicyView.Modes) epHelp[m] = EffortPolicyView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_effortPolicyBox, EffortPolicyView.Modes, epHelp, _effortPolicy);
        _effortPolicyBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _effortPolicyBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_effortPolicyBox);
            if (!EffortPolicyView.IsMode(sel) || sel == _effortPolicy) return;
            _effortPolicy = sel;
            SaveKey(EffortPolicyView.Key, _effortPolicy);
            PaintEffortPolicyInEffect(_lastRoot);
        };

        _effortPolicyNow = new TextBlock(); _effortPolicyNow.VerticalAlignment = VerticalAlignment.Center;
        _effortPolicyNow.FontSize = 11.5;
        _effortPolicyWarn = new TextBlock(); _effortPolicyWarn.VerticalAlignment = VerticalAlignment.Center;
        _effortPolicyWarn.FontSize = 11.5; _effortPolicyWarn.FontWeight = FontWeights.SemiBold;
        _effortPolicyWarn.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_effortPolicyLbl, _effortPolicyBox, _effortPolicyNow, _effortPolicyWarn);
        PaintEffortPolicy();
        return wrap;
    }
    void PaintEffortPolicy()
    {
        if (_effortPolicyLbl != null) { _effortPolicyLbl.Text = T("effort_policy"); _effortPolicyLbl.Foreground = Muted; }
        if (_effortPolicyBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_effortPolicyBox), _effortPolicy)) ComboSelectVal(_effortPolicyBox, _effortPolicy);
        _effortPolicyBox.Background = BtnBg; _effortPolicyBox.Foreground = Fg; _effortPolicyBox.BorderBrush = Border;
        StyleFlatCombo(_effortPolicyBox);
        PaintEffortPolicyInEffect(_lastRoot);
    }
    // What the RUNNER says is in effect (status.json "effort_policy"), beside the combo. No
    // report (old runner, no run yet) -> nothing shown, never a guess from the combo.
    void PaintEffortPolicyInEffect(Dictionary<string, object> root)
    {
        if (_effortPolicyNow == null || _effortPolicyWarn == null) return;
        bool ja = _lang == 0;
        string now = null, warn = null;
        Dictionary<string, object> ep = root != null ? Obj(root, "effort_policy") : null;
        if (ep != null)
        {
            string mode = S(ep, "mode");
            object cf;
            bool conflict = ep.TryGetValue("conflict", out cf) && cf is bool && (bool)cf;
            now = EffortPolicyView.Describe(mode, S(ep, "source"), conflict, ja);
            warn = EffortPolicyView.ConflictText(mode, _effortPolicy, conflict, ja);
        }
        _effortPolicyNow.Text = now ?? "";
        _effortPolicyNow.Foreground = Muted;
        _effortPolicyNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _effortPolicyWarn.Text = warn ?? "";
        _effortPolicyWarn.Foreground = Theme.Br(Theme.Warning(_dark));
        _effortPolicyWarn.Visibility = warn != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Fan-out selector (on|off), the visible GUI path for what used to be reachable only by typing
    // /fanout. Persists fanout= to settings.txt (SaveKey), which every coordinator start reads
    // (sweep_start). Absent key = ON. What the coordinator was REALLY started with comes from
    // status.json "fanout_run" (PaintFanoutInEffect); its words live in FanoutView (EffortPolicy.cs).
    UIElement FanoutControl()
    {
        _fanoutLbl = new TextBlock(); _fanoutLbl.VerticalAlignment = VerticalAlignment.Center;
        _fanoutLbl.FontSize = 12;

        _fanoutBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_fanoutBox, "fanoutBox");
        _fanoutBox.ToolTip = FanoutView.Help(_lang == 0) + "\n" + FanoutView.TakeEffectTip(_lang == 0);
        _fanoutBox.Cursor = Cursors.Hand; _fanoutBox.FontSize = 12;
        _fanoutBox.FontWeight = FontWeights.SemiBold; _fanoutBox.MinWidth = 64;
        _fanoutBox.Padding = new Thickness(8, 2, 4, 2);
        _fanoutBox.VerticalAlignment = VerticalAlignment.Center;
        var foHelp = new Dictionary<string, string>();
        foreach (string m in FanoutView.Modes) foHelp[m] = FanoutView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_fanoutBox, FanoutView.Modes, foHelp, FanoutView.Token(_fanout));
        _fanoutBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _fanoutBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_fanoutBox);
            if ((sel != "on" && sel != "off") || sel == FanoutView.Token(_fanout)) return;
            _fanout = (sel == "on");
            SaveKey("fanout", _fanout ? "on" : "off");
            PaintFanoutInEffect(_lastRoot);
        };

        _fanoutNow = new TextBlock(); _fanoutNow.VerticalAlignment = VerticalAlignment.Center;
        _fanoutNow.FontSize = 11.5;
        _fanoutPending = new TextBlock(); _fanoutPending.VerticalAlignment = VerticalAlignment.Center;
        _fanoutPending.FontSize = 11.5; _fanoutPending.FontWeight = FontWeights.SemiBold;
        _fanoutPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_fanoutLbl, _fanoutBox, _fanoutNow, _fanoutPending);
        PaintFanout();
        return wrap;
    }
    void PaintFanout()
    {
        if (_fanoutLbl != null) { _fanoutLbl.Text = FanoutView.Label(_lang == 0); _fanoutLbl.Foreground = Muted; }
        if (_fanoutBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_fanoutBox), FanoutView.Token(_fanout))) ComboSelectVal(_fanoutBox, FanoutView.Token(_fanout));
        _fanoutBox.Background = BtnBg; _fanoutBox.Foreground = Fg; _fanoutBox.BorderBrush = Border;
        StyleFlatCombo(_fanoutBox);
        PaintFanoutInEffect(_lastRoot);
        PaintFanoutBudget();
        PaintFanoutDepth();
        PaintHierarchicalMerge();
        PaintMergeConversation();
        PaintWriteScope();
        PaintLoadRouting();
        PaintTeamMode();
        PaintRelayFindings();
        PaintAgentBoard();
        PaintOrgVersion();
        PaintOrgProposal();
        PaintOrgShadow();
        PaintOrgAb();
        PaintSupervisorSelfRestart();
        PaintToolProbe();
        PaintToolRoute();
        PaintConsentApprove();
        PaintResidentTools();
    }
    // What the COORDINATOR says it was started with (status.json "fanout_run"), beside the combo.
    // No report (old runner, no run yet) -> nothing shown, never a guess from the combo.
    void PaintFanoutInEffect(Dictionary<string, object> root)
    {
        if (_fanoutNow == null || _fanoutPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> fr = root != null ? Obj(root, "fanout_run") : null;
        if (fr != null)
        {
            object ev;
            bool has = fr.TryGetValue("enabled", out ev) && ev is bool;
            bool enabled = has && (bool)ev;
            now = FanoutView.Describe(has, enabled, S(fr, "source"), ja);
            pend = FanoutView.PendingText(has, enabled, _fanout, ja);
        }
        _fanoutNow.Text = now ?? "";
        _fanoutNow.Foreground = Muted;
        _fanoutNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _fanoutPending.Text = pend ?? "";
        _fanoutPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _fanoutPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Per-tree fan-out budget: four numeric boxes (total / active / turns / minutes) beside the
    // fan-out selector. Each persists through SaveKey only, when the operator commits (Enter or
    // leaving the box); the runner re-reads the keys at every split (each_gate). What the
    // COORDINATOR applies comes from status.json "fanout_budget" (PaintFanoutBudgetInEffect);
    // the words, bounds and parsing live in FanoutBudgetView (EffortPolicy.cs).
    UIElement FanoutBudgetControl()
    {
        // popup layout: the group label, then ONE compact row of four cells (caption over box)
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Vertical;
        wrap.Margin = new Thickness(0, 4, 0, 4);

        _fbLbl = new TextBlock(); _fbLbl.VerticalAlignment = VerticalAlignment.Center;
        _fbLbl.FontSize = 12; _fbLbl.Margin = new Thickness(0, 0, 0, 2);
        wrap.Children.Add(_fbLbl);

        var grid = new Grid();
        for (int c = 0; c < 4; c++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        wrap.Children.Add(grid);

        for (int i = 0; i < 4; i++)
        {
            int idx = i;
            var cell = new StackPanel(); cell.Orientation = Orientation.Vertical;
            cell.Margin = new Thickness(i == 0 ? 0 : 6, 0, 0, 0);
            Grid.SetColumn(cell, i); grid.Children.Add(cell);

            _fbCap[i] = new TextBlock(); _fbCap[i].VerticalAlignment = VerticalAlignment.Center;
            _fbCap[i].FontSize = 11.5; _fbCap[i].Margin = new Thickness(0, 0, 0, 2);
            cell.Children.Add(_fbCap[i]);

            var tb = new TextBox();
            tb.HorizontalAlignment = HorizontalAlignment.Stretch; tb.FontSize = 12; tb.Padding = new Thickness(4, 2, 4, 2);
            tb.VerticalAlignment = VerticalAlignment.Center;
            tb.HorizontalContentAlignment = HorizontalAlignment.Right;
            tb.MaxLength = 7;
            tb.Text = _fbVals[i].ToString();
            tb.LostFocus += delegate { CommitFanoutBudget(idx); };
            tb.KeyDown += delegate(object s, KeyEventArgs e)
            {
                if (e.Key == Key.Enter) { CommitFanoutBudget(idx); e.Handled = true; }
            };
            _fbBox[i] = tb;
            cell.Children.Add(tb);
        }

        _fbNow = new TextBlock(); _fbNow.VerticalAlignment = VerticalAlignment.Center;
        _fbNow.FontSize = 11.5; _fbNow.Margin = new Thickness(0, 2, 0, 0);
        _fbNow.MaxWidth = 300; _fbNow.TextWrapping = TextWrapping.Wrap;
        wrap.Children.Add(_fbNow);
        _fbPending = new TextBlock(); _fbPending.VerticalAlignment = VerticalAlignment.Center;
        _fbPending.FontSize = 11.5; _fbPending.FontWeight = FontWeights.SemiBold;
        _fbPending.Margin = new Thickness(0, 2, 0, 0);
        _fbPending.MaxWidth = 300; _fbPending.TextWrapping = TextWrapping.Wrap;
        _fbPending.Visibility = Visibility.Collapsed;
        wrap.Children.Add(_fbPending);

        PaintFanoutBudget();
        return wrap;
    }
    // Commit one box: a non-number reverts to the held value, a number is clamped to the key's
    // bounds, and an UNCHANGED value is not written (so leaving a box never rewrites the file).
    void CommitFanoutBudget(int idx)
    {
        TextBox tb = _fbBox[idx];
        if (tb == null) return;
        int v;
        if (!FanoutBudgetView.TryParseInput(idx, tb.Text, out v)) { tb.Text = _fbVals[idx].ToString(); return; }
        tb.Text = v.ToString();
        if (v == _fbVals[idx]) return;
        _fbVals[idx] = v;
        SaveFanoutBudgetKey(idx, v);
        PaintFanoutBudgetInEffect(_lastRoot);
    }
    void SaveFanoutBudgetKey(int idx, int v)
    {
        string s = v.ToString(System.Globalization.CultureInfo.InvariantCulture);
        switch (idx)
        {
            case 0: SaveKey("fanout_max_total", s); break;
            case 1: SaveKey("fanout_max_active", s); break;
            case 2: SaveKey("fanout_max_turns", s); break;
            case 3: SaveKey("fanout_max_wall_min", s); break;
        }
    }
    void PaintFanoutBudget()
    {
        bool ja = _lang == 0;
        if (_fbLbl != null) { _fbLbl.Text = FanoutBudgetView.GroupLabel(ja); _fbLbl.Foreground = Muted; }
        for (int i = 0; i < 4; i++)
        {
            if (_fbCap[i] != null)
            {
                _fbCap[i].Text = FanoutBudgetView.ShortLabel(i, ja); _fbCap[i].Foreground = Muted;
            }
            TextBox tb = _fbBox[i];
            if (tb == null) continue;
            tb.ToolTip = FanoutBudgetView.Help(i, ja) + "\n" + FanoutBudgetView.TakeEffectTip(ja);
            // assign only when different and not being typed in, so a repaint never fights the operator
            if (!tb.IsKeyboardFocused && tb.Text != _fbVals[i].ToString()) tb.Text = _fbVals[i].ToString();
            tb.Background = BtnBg; tb.Foreground = Fg; tb.BorderBrush = Border;
        }
        PaintFanoutBudgetInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "fanout_budget"), beside the boxes. No
    // report (old runner, no run yet) -> nothing shown, never a guess from the boxes.
    void PaintFanoutBudgetInEffect(Dictionary<string, object> root)
    {
        if (_fbNow == null || _fbPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> fb = root != null ? Obj(root, "fanout_budget") : null;
        if (fb != null)
        {
            bool ok = true;
            int[] lim = new int[4];
            for (int i = 0; i < 4; i++)
            {
                if (!fb.ContainsKey(FanoutBudgetView.ReportKeys[i]) || fb[FanoutBudgetView.ReportKeys[i]] == null) { ok = false; break; }
                lim[i] = I(fb, FanoutBudgetView.ReportKeys[i]);
            }
            if (ok)
            {
                now = FanoutBudgetView.Describe(lim, ja);
                pend = FanoutBudgetView.PendingText(lim, _fbVals, ja);
            }
        }
        _fbNow.Text = now ?? "";
        _fbNow.Foreground = Muted;
        _fbNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _fbPending.Text = pend ?? "";
        _fbPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _fbPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Split depth (1|2|3), beside the fan-out selector. Persists through SaveKey only; the runner
    // re-reads the key at every split (each_gate). What is IN EFFECT comes from status.json
    // "fanout_depth" (configured + effective), so the screen never claims a depth the coordinator
    // is not applying; the words and parsing live in FanoutDepthView (EffortPolicy.cs).
    UIElement FanoutDepthControl()
    {
        _fdLbl = new TextBlock(); _fdLbl.VerticalAlignment = VerticalAlignment.Center;
        _fdLbl.FontSize = 12;

        _fdBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_fdBox, "fanoutDepthBox");
        _fdBox.ToolTip = FanoutDepthView.Help(_lang == 0) + "\n" + FanoutDepthView.TakeEffectTip(_lang == 0);
        _fdBox.Cursor = Cursors.Hand; _fdBox.FontSize = 12;
        _fdBox.FontWeight = FontWeights.SemiBold; _fdBox.MinWidth = 64;
        _fdBox.Padding = new Thickness(8, 2, 4, 2);
        _fdBox.VerticalAlignment = VerticalAlignment.Center;
        var fdHelp = new Dictionary<string, string>();
        foreach (string m in FanoutDepthView.Modes) fdHelp[m] = FanoutDepthView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_fdBox, FanoutDepthView.Modes, fdHelp, _fdVal.ToString());
        _fdBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _fdBox.SelectionChanged += delegate
        {
            int sel;
            if (!int.TryParse(ComboVal(_fdBox), out sel)) return;
            sel = FanoutDepthView.Clamp(sel);
            if (sel == _fdVal) return;   // unchanged -> no write, no re-fire
            _fdVal = sel;
            SaveKey("fanout_max_depth", sel.ToString(System.Globalization.CultureInfo.InvariantCulture));
            PaintFanoutDepthInEffect(_lastRoot);
        };

        _fdNow = new TextBlock(); _fdNow.VerticalAlignment = VerticalAlignment.Center;
        _fdNow.FontSize = 11.5;
        _fdPending = new TextBlock(); _fdPending.VerticalAlignment = VerticalAlignment.Center;
        _fdPending.FontSize = 11.5; _fdPending.FontWeight = FontWeights.SemiBold;
        _fdPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_fdLbl, _fdBox, _fdNow, _fdPending);
        PaintFanoutDepth();
        return wrap;
    }
    void PaintFanoutDepth()
    {
        if (_fdLbl != null) { _fdLbl.Text = FanoutDepthView.Label(_lang == 0); _fdLbl.Foreground = Muted; }
        if (_fdBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_fdBox), _fdVal.ToString())) ComboSelectVal(_fdBox, _fdVal.ToString());
        _fdBox.Background = BtnBg; _fdBox.Foreground = Fg; _fdBox.BorderBrush = Border;
        StyleFlatCombo(_fdBox);
        PaintFanoutDepthInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "fanout_depth"). No report (old runner,
    // no run yet) -> nothing shown, never a guess from the selection.
    void PaintFanoutDepthInEffect(Dictionary<string, object> root)
    {
        if (_fdNow == null || _fdPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> fd = root != null ? Obj(root, "fanout_depth") : null;
        if (fd != null && fd.ContainsKey("configured") && fd["configured"] != null
            && fd.ContainsKey("effective") && fd["effective"] != null)
        {
            int conf = I(fd, "configured"), eff = I(fd, "effective");
            now = FanoutDepthView.Describe(conf, eff, S(fd, "reason"), ja);
            pend = FanoutDepthView.PendingText(conf, _fdVal, ja);
        }
        _fdNow.Text = now ?? "";
        _fdNow.Foreground = Muted;
        _fdNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _fdPending.Text = pend ?? "";
        _fdPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _fdPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Hierarchical merge (off|on), beside the split depth. Persists through SaveKey only; the
    // runner re-reads the key at every split (each_gate). On lets a split depth above 1 take
    // effect; what is IN EFFECT comes from status.json "fanout_depth".hierarchical_merge. The
    // words and parsing live in HierarchicalMergeView (EffortPolicy.cs).
    UIElement HierarchicalMergeControl()
    {
        _hmLbl = new TextBlock(); _hmLbl.VerticalAlignment = VerticalAlignment.Center;
        _hmLbl.FontSize = 12;

        _hmBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_hmBox, "hierarchicalMergeBox");
        _hmBox.ToolTip = HierarchicalMergeView.Help(_lang == 0) + "\n" + HierarchicalMergeView.TakeEffectTip(_lang == 0);
        _hmBox.Cursor = Cursors.Hand; _hmBox.FontSize = 12;
        _hmBox.FontWeight = FontWeights.SemiBold; _hmBox.MinWidth = 64;
        _hmBox.Padding = new Thickness(8, 2, 4, 2);
        _hmBox.VerticalAlignment = VerticalAlignment.Center;
        var hmHelp = new Dictionary<string, string>();
        foreach (string m in HierarchicalMergeView.Modes) hmHelp[m] = HierarchicalMergeView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_hmBox, HierarchicalMergeView.Modes, hmHelp, _hmVal);
        _hmBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _hmBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_hmBox);
            if (!HierarchicalMergeView.IsMode(sel) || sel == _hmVal) return;   // unchanged -> no write, no re-fire
            _hmVal = sel;
            SaveKey(HierarchicalMergeView.Key, _hmVal);
            PaintHierarchicalMergeInEffect(_lastRoot);
        };

        _hmNow = new TextBlock(); _hmNow.VerticalAlignment = VerticalAlignment.Center;
        _hmNow.FontSize = 11.5;
        _hmPending = new TextBlock(); _hmPending.VerticalAlignment = VerticalAlignment.Center;
        _hmPending.FontSize = 11.5; _hmPending.FontWeight = FontWeights.SemiBold;
        _hmPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_hmLbl, _hmBox, _hmNow, _hmPending);
        PaintHierarchicalMerge();
        return wrap;
    }
    void PaintHierarchicalMerge()
    {
        if (_hmLbl != null) { _hmLbl.Text = HierarchicalMergeView.Label(_lang == 0); _hmLbl.Foreground = Muted; }
        if (_hmBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_hmBox), _hmVal)) ComboSelectVal(_hmBox, _hmVal);
        _hmBox.ToolTip = HierarchicalMergeView.Help(_lang == 0) + "\n" + HierarchicalMergeView.TakeEffectTip(_lang == 0);
        _hmBox.Background = BtnBg; _hmBox.Foreground = Fg; _hmBox.BorderBrush = Border;
        StyleFlatCombo(_hmBox);
        PaintHierarchicalMergeInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "fanout_depth".hierarchical_merge). No
    // report (old runner, no run yet) -> nothing shown, never a guess from the selection.
    void PaintHierarchicalMergeInEffect(Dictionary<string, object> root)
    {
        if (_hmNow == null || _hmPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> fd = root != null ? Obj(root, "fanout_depth") : null;
        if (fd != null && fd.ContainsKey("hierarchical_merge") && fd["hierarchical_merge"] != null)
        {
            string rep = S(fd, "hierarchical_merge");
            now = HierarchicalMergeView.Describe(rep, ja);
            pend = HierarchicalMergeView.PendingText(rep, _hmVal, ja);
        }
        _hmNow.Text = now ?? "";
        _hmNow.Foreground = Muted;
        _hmNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _hmPending.Text = pend ?? "";
        _hmPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _hmPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Tool route (connector|connector-less). Persists through SaveKey only; relay/tool_bridge.py
    // re-reads the key whenever a fleet conversation is created (each_gate), so a change reaches the
    // next conversation and a running one keeps the route it started with. Words: ToolRouteView.
    UIElement ToolRouteControl()
    {
        _trLbl = new TextBlock(); _trLbl.VerticalAlignment = VerticalAlignment.Center;
        _trLbl.FontSize = 12;

        _trBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_trBox, "toolRouteBox");
        _trBox.ToolTip = ToolRouteView.Help(_lang == 0) + "\n" + ToolRouteView.TakeEffectTip(_lang == 0);
        _trBox.Cursor = Cursors.Hand; _trBox.FontSize = 12;
        _trBox.FontWeight = FontWeights.SemiBold; _trBox.MinWidth = 64;
        _trBox.Padding = new Thickness(8, 2, 4, 2);
        _trBox.VerticalAlignment = VerticalAlignment.Center;
        var trHelp = new Dictionary<string, string>();
        foreach (string m in ToolRouteView.Modes) trHelp[m] = ToolRouteView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_trBox, ToolRouteView.Modes, trHelp, _trVal);
        _trBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _trBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_trBox);
            if (!ToolRouteView.IsMode(sel) || sel == _trVal) return;   // unchanged -> no write, no re-fire
            _trVal = sel;
            SaveKey(ToolRouteView.Key, _trVal);
        };

        var wrap = SettingsComboBlock(_trLbl, _trBox, new TextBlock(), new TextBlock());
        PaintToolRoute();
        return wrap;
    }
    void PaintToolRoute()
    {
        if (_trLbl != null) { _trLbl.Text = ToolRouteView.Label(_lang == 0); _trLbl.Foreground = Muted; }
        if (_trBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_trBox), _trVal)) ComboSelectVal(_trBox, _trVal);
        _trBox.ToolTip = ToolRouteView.Help(_lang == 0) + "\n" + ToolRouteView.TakeEffectTip(_lang == 0);
        _trBox.Background = BtnBg; _trBox.Foreground = Fg; _trBox.BorderBrush = Border;
        StyleFlatCombo(_trBox);
    }
    // Consent-card auto-approval (on|off), under Tools. Persists through SaveKey only;
    // relay/consent_approve.py re-reads the key whenever a connector-less turn ends on a card
    // (each_gate). Words: ConsentApproveView.
    UIElement ConsentApproveControl()
    {
        _caLbl = new TextBlock(); _caLbl.VerticalAlignment = VerticalAlignment.Center;
        _caLbl.FontSize = 12;

        _caBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_caBox, "consentApproveBox");
        _caBox.ToolTip = ConsentApproveView.Help(_lang == 0) + "\n" + ConsentApproveView.TakeEffectTip(_lang == 0);
        _caBox.Cursor = Cursors.Hand; _caBox.FontSize = 12;
        _caBox.FontWeight = FontWeights.SemiBold; _caBox.MinWidth = 64;
        _caBox.Padding = new Thickness(8, 2, 4, 2);
        _caBox.VerticalAlignment = VerticalAlignment.Center;
        var caHelp = new Dictionary<string, string>();
        foreach (string m in ConsentApproveView.Modes) caHelp[m] = ConsentApproveView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_caBox, ConsentApproveView.Modes, caHelp, _caVal);
        _caBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _caBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_caBox);
            if (!ConsentApproveView.IsMode(sel) || sel == _caVal) return;   // unchanged -> no write, no re-fire
            _caVal = sel;
            SaveKey(ConsentApproveView.Key, _caVal);
        };

        var wrap = SettingsComboBlock(_caLbl, _caBox, new TextBlock(), new TextBlock());
        PaintConsentApprove();
        return wrap;
    }
    void PaintConsentApprove()
    {
        if (_caLbl != null) { _caLbl.Text = ConsentApproveView.Label(_lang == 0); _caLbl.Foreground = Muted; }
        if (_caBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_caBox), _caVal)) ComboSelectVal(_caBox, _caVal);
        _caBox.ToolTip = ConsentApproveView.Help(_lang == 0) + "\n" + ConsentApproveView.TakeEffectTip(_lang == 0);
        _caBox.Background = BtnBg; _caBox.Foreground = Fg; _caBox.BorderBrush = Border;
        StyleFlatCombo(_caBox);
    }
    // Resident tools in my own chats (off|on), in the Tools section under the tool route. Persists
    // through SaveKey only. Nothing acts on the value yet: relay/resident_guard.py reads it and
    // status.json reports it (each_gate). Words: ResidentToolsView.
    UIElement ResidentToolsControl()
    {
        _rtLbl = new TextBlock(); _rtLbl.VerticalAlignment = VerticalAlignment.Center;
        _rtLbl.FontSize = 12;

        _rtBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_rtBox, "residentToolsBox");
        _rtBox.ToolTip = ResidentToolsView.Help(_lang == 0) + "\n" + ResidentToolsView.TakeEffectTip(_lang == 0);
        _rtBox.Cursor = Cursors.Hand; _rtBox.FontSize = 12;
        _rtBox.FontWeight = FontWeights.SemiBold; _rtBox.MinWidth = 64;
        _rtBox.Padding = new Thickness(8, 2, 4, 2);
        _rtBox.VerticalAlignment = VerticalAlignment.Center;
        var rtHelp = new Dictionary<string, string>();
        foreach (string m in ResidentToolsView.Modes) rtHelp[m] = ResidentToolsView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_rtBox, ResidentToolsView.Modes, rtHelp, _rtVal);
        _rtBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _rtBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_rtBox);
            if (!ResidentToolsView.IsMode(sel) || sel == _rtVal) return;   // unchanged -> no write, no re-fire
            _rtVal = sel;
            SaveKey(ResidentToolsView.Key, _rtVal);
        };

        var wrap = SettingsComboBlock(_rtLbl, _rtBox, new TextBlock(), new TextBlock());
        PaintResidentTools();
        return wrap;
    }
    void PaintResidentTools()
    {
        if (_rtLbl != null) { _rtLbl.Text = ResidentToolsView.Label(_lang == 0); _rtLbl.Foreground = Muted; }
        if (_rtBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_rtBox), _rtVal)) ComboSelectVal(_rtBox, _rtVal);
        _rtBox.ToolTip = ResidentToolsView.Help(_lang == 0) + "\n" + ResidentToolsView.TakeEffectTip(_lang == 0);
        _rtBox.Background = BtnBg; _rtBox.Foreground = Fg; _rtBox.BorderBrush = Border;
        StyleFlatCombo(_rtBox);
    }
    // Read-only result cache (off|shadow), under Tools. Persists through SaveKey only;
    // relay/result_cache.py re-reads the key at every read-only tool call (each_gate).
    // Words: ResultCacheView.
    UIElement ResultCacheControl()
    {
        _rcLbl = new TextBlock(); _rcLbl.VerticalAlignment = VerticalAlignment.Center;
        _rcLbl.FontSize = 12;

        _rcBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_rcBox, "resultCacheBox");
        _rcBox.ToolTip = ResultCacheView.Help(_lang == 0) + "\n" + ResultCacheView.TakeEffectTip(_lang == 0);
        _rcBox.Cursor = Cursors.Hand; _rcBox.FontSize = 12;
        _rcBox.FontWeight = FontWeights.SemiBold; _rcBox.MinWidth = 64;
        _rcBox.Padding = new Thickness(8, 2, 4, 2);
        _rcBox.VerticalAlignment = VerticalAlignment.Center;
        var rcHelp = new Dictionary<string, string>();
        foreach (string m in ResultCacheView.Modes) rcHelp[m] = ResultCacheView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_rcBox, ResultCacheView.Modes, rcHelp, _rcVal);
        _rcBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _rcBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_rcBox);
            if (!ResultCacheView.IsMode(sel) || sel == _rcVal) return;   // unchanged -> no write, no re-fire
            _rcVal = sel;
            SaveKey(ResultCacheView.Key, _rcVal);
        };

        var wrap = SettingsComboBlock(_rcLbl, _rcBox, new TextBlock(), new TextBlock());
        PaintResultCache();
        return wrap;
    }
    void PaintResultCache()
    {
        if (_rcLbl != null) { _rcLbl.Text = ResultCacheView.Label(_lang == 0); _rcLbl.Foreground = Muted; }
        if (_rcBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_rcBox), _rcVal)) ComboSelectVal(_rcBox, _rcVal);
        _rcBox.ToolTip = ResultCacheView.Help(_lang == 0) + "\n" + ResultCacheView.TakeEffectTip(_lang == 0);
        _rcBox.Background = BtnBg; _rcBox.Foreground = Fg; _rcBox.BorderBrush = Border;
        StyleFlatCombo(_rcBox);
    }
    // Merge conversation (fresh|parent), in the Fan-out section. Persists through SaveKey only; the
    // runner re-reads the key at every split and every merge (each_gate). Parent runs the merge in
    // the splitting worker's own conversation; what is IN EFFECT, and the savings so far, come from
    // status.json "conversation_saving". The words and parsing live in MergeConversationView.
    UIElement MergeConversationControl()
    {
        _mcLbl = new TextBlock(); _mcLbl.VerticalAlignment = VerticalAlignment.Center;
        _mcLbl.FontSize = 12;

        _mcBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_mcBox, "mergeConversationBox");
        _mcBox.ToolTip = MergeConversationView.Help(_lang == 0) + "\n" + MergeConversationView.TakeEffectTip(_lang == 0);
        _mcBox.Cursor = Cursors.Hand; _mcBox.FontSize = 12;
        _mcBox.FontWeight = FontWeights.SemiBold; _mcBox.MinWidth = 64;
        _mcBox.Padding = new Thickness(8, 2, 4, 2);
        _mcBox.VerticalAlignment = VerticalAlignment.Center;
        var mcHelp = new Dictionary<string, string>();
        foreach (string m in MergeConversationView.Modes) mcHelp[m] = MergeConversationView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_mcBox, MergeConversationView.Modes, mcHelp, _mcVal);
        _mcBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _mcBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_mcBox);
            if (!MergeConversationView.IsMode(sel) || sel == _mcVal) return;   // unchanged -> no write, no re-fire
            _mcVal = sel;
            SaveKey(MergeConversationView.Key, _mcVal);
            PaintMergeConversationInEffect(_lastRoot);
        };

        _mcNow = new TextBlock(); _mcNow.VerticalAlignment = VerticalAlignment.Center;
        _mcNow.FontSize = 11.5; _mcNow.TextWrapping = TextWrapping.Wrap; _mcNow.MaxWidth = 300;
        _mcPending = new TextBlock(); _mcPending.VerticalAlignment = VerticalAlignment.Center;
        _mcPending.FontSize = 11.5; _mcPending.FontWeight = FontWeights.SemiBold;
        _mcPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_mcLbl, _mcBox, _mcNow, _mcPending);
        PaintMergeConversation();
        return wrap;
    }
    void PaintMergeConversation()
    {
        if (_mcLbl != null) { _mcLbl.Text = MergeConversationView.Label(_lang == 0); _mcLbl.Foreground = Muted; }
        if (_mcBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_mcBox), _mcVal)) ComboSelectVal(_mcBox, _mcVal);
        _mcBox.ToolTip = MergeConversationView.Help(_lang == 0) + "\n" + MergeConversationView.TakeEffectTip(_lang == 0);
        _mcBox.Background = BtnBg; _mcBox.Foreground = Fg; _mcBox.BorderBrush = Border;
        StyleFlatCombo(_mcBox);
        PaintMergeConversationInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies and has saved (status.json "conversation_saving"). No
    // report (old runner, no run yet) -> nothing shown, never a guess from the selection.
    void PaintMergeConversationInEffect(Dictionary<string, object> root)
    {
        if (_mcNow == null || _mcPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> cs = root != null ? Obj(root, "conversation_saving") : null;
        if (cs != null && cs.ContainsKey("merge_conversation") && cs["merge_conversation"] != null)
        {
            string rep = S(cs, "merge_conversation");
            int saved = cs.ContainsKey("aggregators_saved") && cs["aggregators_saved"] != null ? I(cs, "aggregators_saved") : 0;
            int unsent = cs.ContainsKey("unsent_created") && cs["unsent_created"] != null ? I(cs, "unsent_created") : 0;
            now = MergeConversationView.Describe(rep, saved, unsent, ja);
            pend = MergeConversationView.PendingText(rep, _mcVal, ja);
        }
        _mcNow.Text = now ?? "";
        _mcNow.Foreground = Muted;
        _mcNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _mcPending.Text = pend ?? "";
        _mcPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _mcPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Sibling write scope (off|shadow), beside the split depth. Persists through SaveKey only; the
    // coordinator re-reads the key at every sweep (each_gate). SHADOW ONLY: it records overlapping
    // sibling writes and blocks nothing, and there is no enforcing option. What is IN EFFECT comes
    // from status.json "fanout_write_scope"; the words and parsing live in WriteScopeView.
    UIElement WriteScopeControl()
    {
        _wsLbl = new TextBlock(); _wsLbl.VerticalAlignment = VerticalAlignment.Center;
        _wsLbl.FontSize = 12;

        _wsBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_wsBox, "writeScopeBox");
        _wsBox.ToolTip = WriteScopeView.Help(_lang == 0) + "\n" + WriteScopeView.TakeEffectTip(_lang == 0);
        _wsBox.Cursor = Cursors.Hand; _wsBox.FontSize = 12;
        _wsBox.FontWeight = FontWeights.SemiBold; _wsBox.MinWidth = 78;
        _wsBox.Padding = new Thickness(8, 2, 4, 2);
        _wsBox.VerticalAlignment = VerticalAlignment.Center;
        var wsHelp = new Dictionary<string, string>();
        foreach (string m in WriteScopeView.Modes) wsHelp[m] = WriteScopeView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_wsBox, WriteScopeView.Modes, wsHelp, _wsVal);
        _wsBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _wsBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_wsBox);
            if (!WriteScopeView.IsMode(sel) || sel == _wsVal) return;   // unchanged -> no write, no re-fire
            _wsVal = sel;
            SaveKey(WriteScopeView.Key, _wsVal);
            PaintWriteScopeInEffect(_lastRoot);
        };

        _wsNow = new TextBlock(); _wsNow.VerticalAlignment = VerticalAlignment.Center;
        _wsNow.FontSize = 11.5;
        _wsPending = new TextBlock(); _wsPending.VerticalAlignment = VerticalAlignment.Center;
        _wsPending.FontSize = 11.5; _wsPending.FontWeight = FontWeights.SemiBold;
        _wsPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_wsLbl, _wsBox, _wsNow, _wsPending);
        PaintWriteScope();
        return wrap;
    }
    void PaintWriteScope()
    {
        if (_wsLbl != null) { _wsLbl.Text = WriteScopeView.Label(_lang == 0); _wsLbl.Foreground = Muted; }
        if (_wsBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_wsBox), _wsVal)) ComboSelectVal(_wsBox, _wsVal);
        _wsBox.ToolTip = WriteScopeView.Help(_lang == 0) + "\n" + WriteScopeView.TakeEffectTip(_lang == 0);
        _wsBox.Background = BtnBg; _wsBox.Foreground = Fg; _wsBox.BorderBrush = Border;
        StyleFlatCombo(_wsBox);
        PaintWriteScopeInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "fanout_write_scope"). No report (old
    // runner, no run yet) -> nothing shown, never a guess from the selection.
    void PaintWriteScopeInEffect(Dictionary<string, object> root)
    {
        if (_wsNow == null || _wsPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> ws = root != null ? Obj(root, "fanout_write_scope") : null;
        if (ws != null && ws.ContainsKey("mode") && ws["mode"] != null)
        {
            string rep = S(ws, "mode");
            now = WriteScopeView.Describe(rep, ws.ContainsKey("overlaps_seen") && ws["overlaps_seen"] != null ? I(ws, "overlaps_seen") : 0, ja);
            pend = WriteScopeView.PendingText(rep, _wsVal, ja);
        }
        _wsNow.Text = now ?? "";
        _wsNow.Foreground = Muted;
        _wsNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _wsPending.Text = pend ?? "";
        _wsPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _wsPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Load-aware routing (off|shadow), beside the sibling write scope. Persists through SaveKey only;
    // the coordinator re-reads the key at every admission (each_gate). SHADOW ONLY: it records the
    // goal a router would have admitted and changes nothing, and there is no option that routes.
    // What is IN EFFECT comes from status.json "fanout_load_routing"; the words and parsing live in
    // LoadRoutingView.
    UIElement LoadRoutingControl()
    {
        _lrLbl = new TextBlock(); _lrLbl.VerticalAlignment = VerticalAlignment.Center;
        _lrLbl.FontSize = 12;

        _lrBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_lrBox, "loadRoutingBox");
        _lrBox.ToolTip = LoadRoutingView.Help(_lang == 0) + "\n" + LoadRoutingView.TakeEffectTip(_lang == 0);
        _lrBox.Cursor = Cursors.Hand; _lrBox.FontSize = 12;
        _lrBox.FontWeight = FontWeights.SemiBold; _lrBox.MinWidth = 78;
        _lrBox.Padding = new Thickness(8, 2, 4, 2);
        _lrBox.VerticalAlignment = VerticalAlignment.Center;
        var lrHelp = new Dictionary<string, string>();
        foreach (string m in LoadRoutingView.Modes) lrHelp[m] = LoadRoutingView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_lrBox, LoadRoutingView.Modes, lrHelp, _lrVal);
        _lrBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _lrBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_lrBox);
            if (!LoadRoutingView.IsMode(sel) || sel == _lrVal) return;   // unchanged -> no write, no re-fire
            _lrVal = sel;
            SaveKey(LoadRoutingView.Key, _lrVal);
            PaintLoadRoutingInEffect(_lastRoot);
        };

        _lrNow = new TextBlock(); _lrNow.VerticalAlignment = VerticalAlignment.Center;
        _lrNow.FontSize = 11.5;
        _lrPending = new TextBlock(); _lrPending.VerticalAlignment = VerticalAlignment.Center;
        _lrPending.FontSize = 11.5; _lrPending.FontWeight = FontWeights.SemiBold;
        _lrPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_lrLbl, _lrBox, _lrNow, _lrPending);
        PaintLoadRouting();
        return wrap;
    }
    void PaintLoadRouting()
    {
        if (_lrLbl != null) { _lrLbl.Text = LoadRoutingView.Label(_lang == 0); _lrLbl.Foreground = Muted; }
        if (_lrBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_lrBox), _lrVal)) ComboSelectVal(_lrBox, _lrVal);
        _lrBox.ToolTip = LoadRoutingView.Help(_lang == 0) + "\n" + LoadRoutingView.TakeEffectTip(_lang == 0);
        _lrBox.Background = BtnBg; _lrBox.Foreground = Fg; _lrBox.BorderBrush = Border;
        StyleFlatCombo(_lrBox);
        PaintLoadRoutingInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "fanout_load_routing"). No report (old
    // runner, no run yet) -> nothing shown, never a guess from the selection.
    void PaintLoadRoutingInEffect(Dictionary<string, object> root)
    {
        if (_lrNow == null || _lrPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> lr = root != null ? Obj(root, "fanout_load_routing") : null;
        if (lr != null && lr.ContainsKey("mode") && lr["mode"] != null)
        {
            string rep = S(lr, "mode");
            now = LoadRoutingView.Describe(rep,
                lr.ContainsKey("recorded") && lr["recorded"] != null ? I(lr, "recorded") : 0,
                lr.ContainsKey("differed") && lr["differed"] != null ? I(lr, "differed") : 0, ja);
            pend = LoadRoutingView.PendingText(rep, _lrVal, ja);
        }
        _lrNow.Text = now ?? "";
        _lrNow.Foreground = Muted;
        _lrNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _lrPending.Text = pend ?? "";
        _lrPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _lrPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Role and protocol version record (off|on), beside the campaign board. Persists through SaveKey
    // only; relay/org/versions.py reads the key once when a run starts (sweep_start). It appends
    // two small lines per run (definition hashes and the run id) and changes no prompt or admission,
    // so there is no "in effect" line: the words and parsing live in OrgVersionView.
    UIElement OrgVersionControl()
    {
        _ovLbl = new TextBlock(); _ovLbl.VerticalAlignment = VerticalAlignment.Center;
        _ovLbl.FontSize = 12;

        _ovBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_ovBox, "orgVersionBox");
        _ovBox.ToolTip = OrgVersionView.Help(_lang == 0) + "\n" + OrgVersionView.TakeEffectTip(_lang == 0);
        _ovBox.Cursor = Cursors.Hand; _ovBox.FontSize = 12;
        _ovBox.FontWeight = FontWeights.SemiBold; _ovBox.MinWidth = 78;
        _ovBox.Padding = new Thickness(8, 2, 4, 2);
        _ovBox.VerticalAlignment = VerticalAlignment.Center;
        var ovHelp = new Dictionary<string, string>();
        foreach (string m in OrgVersionView.Modes) ovHelp[m] = OrgVersionView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_ovBox, OrgVersionView.Modes, ovHelp, _ovVal);
        _ovBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _ovBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_ovBox);
            if (!OrgVersionView.IsMode(sel) || sel == _ovVal) return;   // unchanged -> no write, no re-fire
            _ovVal = sel;
            SaveKey(OrgVersionView.Key, _ovVal);
        };

        _ovNow = new TextBlock(); _ovNow.Visibility = Visibility.Collapsed;
        _ovPending = new TextBlock(); _ovPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_ovLbl, _ovBox, _ovNow, _ovPending);
        PaintOrgVersion();
        return wrap;
    }
    void PaintOrgVersion()
    {
        if (_ovLbl != null) { _ovLbl.Text = OrgVersionView.Label(_lang == 0); _ovLbl.Foreground = Muted; }
        if (_ovBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_ovBox), _ovVal)) ComboSelectVal(_ovBox, _ovVal);
        _ovBox.ToolTip = OrgVersionView.Help(_lang == 0) + "\n" + OrgVersionView.TakeEffectTip(_lang == 0);
        _ovBox.Background = BtnBg; _ovBox.Foreground = Fg; _ovBox.BorderBrush = Border;
        StyleFlatCombo(_ovBox);
    }
    // Org change proposals (off|on), beside the version record. Persists through SaveKey only;
    // the propose_org_change tool reads the key on each call. A proposal is a record and a decision
    // card and applies nothing, so there is no "in effect" line: the words and parsing live in
    // OrgProposalView.
    UIElement OrgProposalControl()
    {
        _opLbl = new TextBlock(); _opLbl.VerticalAlignment = VerticalAlignment.Center;
        _opLbl.FontSize = 12;

        _opBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_opBox, "orgProposalBox");
        _opBox.ToolTip = OrgProposalView.Help(_lang == 0) + "\n" + OrgProposalView.TakeEffectTip(_lang == 0);
        _opBox.Cursor = Cursors.Hand; _opBox.FontSize = 12;
        _opBox.FontWeight = FontWeights.SemiBold; _opBox.MinWidth = 78;
        _opBox.Padding = new Thickness(8, 2, 4, 2);
        _opBox.VerticalAlignment = VerticalAlignment.Center;
        var opHelp = new Dictionary<string, string>();
        foreach (string m in OrgProposalView.Modes) opHelp[m] = OrgProposalView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_opBox, OrgProposalView.Modes, opHelp, _opVal);
        _opBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _opBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_opBox);
            if (!OrgProposalView.IsMode(sel) || sel == _opVal) return;   // unchanged -> no write, no re-fire
            _opVal = sel;
            SaveKey(OrgProposalView.Key, _opVal);
        };

        _opNow = new TextBlock(); _opNow.Visibility = Visibility.Collapsed;
        _opPending = new TextBlock(); _opPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_opLbl, _opBox, _opNow, _opPending);
        PaintOrgProposal();
        return wrap;
    }
    void PaintOrgProposal()
    {
        if (_opLbl != null) { _opLbl.Text = OrgProposalView.Label(_lang == 0); _opLbl.Foreground = Muted; }
        if (_opBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_opBox), _opVal)) ComboSelectVal(_opBox, _opVal);
        _opBox.ToolTip = OrgProposalView.Help(_lang == 0) + "\n" + OrgProposalView.TakeEffectTip(_lang == 0);
        _opBox.Background = BtnBg; _opBox.Foreground = Fg; _opBox.BorderBrush = Border;
        StyleFlatCombo(_opBox);
    }
    // Org proposal shadow evaluation (off|shadow), beside the proposals. Persists through SaveKey
    // only; relay/org/shadow_eval.py reads the key once when a run starts (sweep_start). It records
    // which runs a proposal would have touched and applies nothing, so there is no "in effect" line:
    // the words and parsing live in OrgShadowView.
    UIElement OrgShadowControl()
    {
        _osLbl = new TextBlock(); _osLbl.VerticalAlignment = VerticalAlignment.Center;
        _osLbl.FontSize = 12;

        _osBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_osBox, "orgShadowBox");
        _osBox.ToolTip = OrgShadowView.Help(_lang == 0) + "\n" + OrgShadowView.TakeEffectTip(_lang == 0);
        _osBox.Cursor = Cursors.Hand; _osBox.FontSize = 12;
        _osBox.FontWeight = FontWeights.SemiBold; _osBox.MinWidth = 78;
        _osBox.Padding = new Thickness(8, 2, 4, 2);
        _osBox.VerticalAlignment = VerticalAlignment.Center;
        var osHelp = new Dictionary<string, string>();
        foreach (string m in OrgShadowView.Modes) osHelp[m] = OrgShadowView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_osBox, OrgShadowView.Modes, osHelp, _osVal);
        _osBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _osBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_osBox);
            if (!OrgShadowView.IsMode(sel) || sel == _osVal) return;   // unchanged -> no write, no re-fire
            _osVal = sel;
            SaveKey(OrgShadowView.Key, _osVal);
        };

        _osNow = new TextBlock(); _osNow.Visibility = Visibility.Collapsed;
        _osPending = new TextBlock(); _osPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_osLbl, _osBox, _osNow, _osPending);
        PaintOrgShadow();
        return wrap;
    }

    UIElement OrgAbControl()
    {
        _abLbl = new TextBlock(); _abLbl.VerticalAlignment = VerticalAlignment.Center;
        _abLbl.FontSize = 12;

        _abBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_abBox, "orgAbBox");
        _abBox.ToolTip = OrgAbView.Help(_lang == 0) + "\n" + OrgAbView.TakeEffectTip(_lang == 0);
        _abBox.Cursor = Cursors.Hand; _abBox.FontSize = 12;
        _abBox.FontWeight = FontWeights.SemiBold; _abBox.MinWidth = 78;
        _abBox.Padding = new Thickness(8, 2, 4, 2);
        _abBox.VerticalAlignment = VerticalAlignment.Center;
        var abHelp = new Dictionary<string, string>();
        foreach (string m in OrgAbView.Modes) abHelp[m] = OrgAbView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_abBox, OrgAbView.Modes, abHelp, _abVal);
        _abBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _abBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_abBox);
            if (!OrgAbView.IsMode(sel) || sel == _abVal) return;   // unchanged -> no write, no re-fire
            _abVal = sel;
            SaveKey(OrgAbView.Key, _abVal);
        };

        _abNow = new TextBlock(); _abNow.Visibility = Visibility.Collapsed;
        _abPending = new TextBlock(); _abPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_abLbl, _abBox, _abNow, _abPending);
        PaintOrgAb();
        return wrap;
    }
    void PaintOrgShadow()
    {
        if (_osLbl != null) { _osLbl.Text = OrgShadowView.Label(_lang == 0); _osLbl.Foreground = Muted; }
        if (_osBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_osBox), _osVal)) ComboSelectVal(_osBox, _osVal);
        _osBox.ToolTip = OrgShadowView.Help(_lang == 0) + "\n" + OrgShadowView.TakeEffectTip(_lang == 0);
        _osBox.Background = BtnBg; _osBox.Foreground = Fg; _osBox.BorderBrush = Border;
        StyleFlatCombo(_osBox);
    }

    void PaintOrgAb()
    {
        if (_abLbl != null) { _abLbl.Text = OrgAbView.Label(_lang == 0); _abLbl.Foreground = Muted; }
        if (_abBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_abBox), _abVal)) ComboSelectVal(_abBox, _abVal);
        _abBox.ToolTip = OrgAbView.Help(_lang == 0) + "\n" + OrgAbView.TakeEffectTip(_lang == 0);
        _abBox.Background = BtnBg; _abBox.Foreground = Fg; _abBox.BorderBrush = Border;
        StyleFlatCombo(_abBox);
    }
    // Campaign board (off|p0), beside load-aware routing. Persists through SaveKey only; the
    // coordinator reads the key once when a run starts (sweep_start), so a run in progress keeps
    // its mode. p1 (workers write) exists in the key's value set but is not offered here.
    // What is IN EFFECT comes from status.json "agent_board"; the words and parsing live in
    // AgentBoardView.
    UIElement AgentBoardControl()
    {
        _abLbl = new TextBlock(); _abLbl.VerticalAlignment = VerticalAlignment.Center;
        _abLbl.FontSize = 12;

        _abBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_abBox, "agentBoardBox");
        _abBox.ToolTip = AgentBoardView.Help(_lang == 0) + "\n" + AgentBoardView.TakeEffectTip(_lang == 0);
        _abBox.Cursor = Cursors.Hand; _abBox.FontSize = 12;
        _abBox.FontWeight = FontWeights.SemiBold; _abBox.MinWidth = 78;
        _abBox.Padding = new Thickness(8, 2, 4, 2);
        _abBox.VerticalAlignment = VerticalAlignment.Center;
        var abHelp = new Dictionary<string, string>();
        foreach (string m in AgentBoardView.Modes) abHelp[m] = AgentBoardView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_abBox, AgentBoardView.Modes, abHelp, _abVal);
        _abBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _abBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_abBox);
            if (!AgentBoardView.IsMode(sel) || sel == _abVal) return;   // unchanged -> no write, no re-fire
            _abVal = sel;
            SaveKey(AgentBoardView.Key, _abVal);
            PaintAgentBoardInEffect(_lastRoot);
        };

        _abNow = new TextBlock(); _abNow.VerticalAlignment = VerticalAlignment.Center;
        _abNow.FontSize = 11.5;
        _abPending = new TextBlock(); _abPending.VerticalAlignment = VerticalAlignment.Center;
        _abPending.FontSize = 11.5; _abPending.FontWeight = FontWeights.SemiBold;
        _abPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_abLbl, _abBox, _abNow, _abPending);
        PaintAgentBoard();
        return wrap;
    }
    void PaintAgentBoard()
    {
        if (_abLbl != null) { _abLbl.Text = AgentBoardView.Label(_lang == 0); _abLbl.Foreground = Muted; }
        if (_abBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_abBox), _abVal)) ComboSelectVal(_abBox, _abVal);
        _abBox.ToolTip = AgentBoardView.Help(_lang == 0) + "\n" + AgentBoardView.TakeEffectTip(_lang == 0);
        _abBox.Background = BtnBg; _abBox.Foreground = Fg; _abBox.BorderBrush = Border;
        StyleFlatCombo(_abBox);
        PaintAgentBoardInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "agent_board"). No report (old runner, no
    // run yet) -> nothing shown, never a guess from the selection.
    void PaintAgentBoardInEffect(Dictionary<string, object> root)
    {
        if (_abNow == null || _abPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> ab = root != null ? Obj(root, "agent_board") : null;
        if (ab != null && ab.ContainsKey("mode") && ab["mode"] != null)
        {
            string rep = S(ab, "mode");
            now = AgentBoardView.Describe(rep,
                ab.ContainsKey("posted") && ab["posted"] != null ? I(ab, "posted") : 0,
                ab.ContainsKey("refused") && ab["refused"] != null ? I(ab, "refused") : 0,
                ab.ContainsKey("write_errors") && ab["write_errors"] != null ? I(ab, "write_errors") : 0, ja);
            pend = AgentBoardView.PendingText(rep, _abVal, ja);
        }
        _abNow.Text = now ?? "";
        _abNow.Foreground = Muted;
        _abNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _abPending.Text = pend ?? "";
        _abPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _abPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Team formation (off|shadow), beside the load-aware routing. Persists through SaveKey only; the
    // coordinator re-reads the key at every split (each_gate). SHADOW ONLY: it records the team a
    // team-forming rule would have proposed and changes nothing, and there is no option that forms one.
    // What is IN EFFECT comes from status.json "team_mode"; the words and parsing live in TeamModeView.
    UIElement TeamModeControl()
    {
        _tmLbl = new TextBlock(); _tmLbl.VerticalAlignment = VerticalAlignment.Center;
        _tmLbl.FontSize = 12;

        _tmBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_tmBox, "teamModeBox");
        _tmBox.ToolTip = TeamModeView.Help(_lang == 0) + "\n" + TeamModeView.TakeEffectTip(_lang == 0);
        _tmBox.Cursor = Cursors.Hand; _tmBox.FontSize = 12;
        _tmBox.FontWeight = FontWeights.SemiBold; _tmBox.MinWidth = 78;
        _tmBox.Padding = new Thickness(8, 2, 4, 2);
        _tmBox.VerticalAlignment = VerticalAlignment.Center;
        var tmHelp = new Dictionary<string, string>();
        foreach (string m in TeamModeView.Modes) tmHelp[m] = TeamModeView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_tmBox, TeamModeView.Modes, tmHelp, _tmVal);
        _tmBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _tmBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_tmBox);
            if (!TeamModeView.IsMode(sel) || sel == _tmVal) return;   // unchanged -> no write, no re-fire
            _tmVal = sel;
            SaveKey(TeamModeView.Key, _tmVal);
            PaintTeamModeInEffect(_lastRoot);
        };

        _tmNow = new TextBlock(); _tmNow.VerticalAlignment = VerticalAlignment.Center;
        _tmNow.FontSize = 11.5;
        _tmPending = new TextBlock(); _tmPending.VerticalAlignment = VerticalAlignment.Center;
        _tmPending.FontSize = 11.5; _tmPending.FontWeight = FontWeights.SemiBold;
        _tmPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_tmLbl, _tmBox, _tmNow, _tmPending);
        PaintTeamMode();
        return wrap;
    }
    void PaintTeamMode()
    {
        if (_tmLbl != null) { _tmLbl.Text = TeamModeView.Label(_lang == 0); _tmLbl.Foreground = Muted; }
        if (_tmBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_tmBox), _tmVal)) ComboSelectVal(_tmBox, _tmVal);
        _tmBox.ToolTip = TeamModeView.Help(_lang == 0) + "\n" + TeamModeView.TakeEffectTip(_lang == 0);
        _tmBox.Background = BtnBg; _tmBox.Foreground = Fg; _tmBox.BorderBrush = Border;
        StyleFlatCombo(_tmBox);
        PaintTeamModeInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "team_mode"). No report (old runner, no run
    // yet) -> nothing shown, never a guess from the selection.
    void PaintTeamModeInEffect(Dictionary<string, object> root)
    {
        if (_tmNow == null || _tmPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> tm = root != null ? Obj(root, "team_mode") : null;
        if (tm != null && tm.ContainsKey("mode") && tm["mode"] != null)
        {
            string rep = S(tm, "mode");
            now = TeamModeView.Describe(rep,
                tm.ContainsKey("recorded") && tm["recorded"] != null ? I(tm, "recorded") : 0,
                tm.ContainsKey("differed") && tm["differed"] != null ? I(tm, "differed") : 0, ja);
            pend = TeamModeView.PendingText(rep, _tmVal, ja);
        }
        _tmNow.Text = now ?? "";
        _tmNow.Foreground = Muted;
        _tmNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _tmPending.Text = pend ?? "";
        _tmPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _tmPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    UIElement RelayFindingsControl()
    {
        _rfLbl = new TextBlock(); _rfLbl.VerticalAlignment = VerticalAlignment.Center;
        _rfLbl.FontSize = 12;

        _rfBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_rfBox, "relayFindingsBox");
        _rfBox.ToolTip = RelayFindingsView.Help(_lang == 0) + "\n" + RelayFindingsView.TakeEffectTip(_lang == 0);
        _rfBox.Cursor = Cursors.Hand; _rfBox.FontSize = 12;
        _rfBox.FontWeight = FontWeights.SemiBold; _rfBox.MinWidth = 78;
        _rfBox.Padding = new Thickness(8, 2, 4, 2);
        _rfBox.VerticalAlignment = VerticalAlignment.Center;
        var rfHelp = new Dictionary<string, string>();
        foreach (string m in RelayFindingsView.Modes) rfHelp[m] = RelayFindingsView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_rfBox, RelayFindingsView.Modes, rfHelp, _rfVal);
        _rfBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _rfBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_rfBox);
            if (!RelayFindingsView.IsMode(sel) || sel == _rfVal) return;   // unchanged -> no write, no re-fire
            _rfVal = sel;
            SaveKey(RelayFindingsView.Key, _rfVal);
            PaintRelayFindingsInEffect(_lastRoot);
        };

        _rfNow = new TextBlock(); _rfNow.VerticalAlignment = VerticalAlignment.Center;
        _rfNow.FontSize = 11.5;
        _rfPending = new TextBlock(); _rfPending.VerticalAlignment = VerticalAlignment.Center;
        _rfPending.FontSize = 11.5; _rfPending.FontWeight = FontWeights.SemiBold;
        _rfPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_rfLbl, _rfBox, _rfNow, _rfPending);
        PaintRelayFindings();
        return wrap;
    }
    void PaintRelayFindings()
    {
        if (_rfLbl != null) { _rfLbl.Text = RelayFindingsView.Label(_lang == 0); _rfLbl.Foreground = Muted; }
        if (_rfBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_rfBox), _rfVal)) ComboSelectVal(_rfBox, _rfVal);
        _rfBox.ToolTip = RelayFindingsView.Help(_lang == 0) + "\n" + RelayFindingsView.TakeEffectTip(_lang == 0);
        _rfBox.Background = BtnBg; _rfBox.Foreground = Fg; _rfBox.BorderBrush = Border;
        StyleFlatCombo(_rfBox);
        PaintRelayFindingsInEffect(_lastRoot);
    }
    // What the COORDINATOR says it applies (status.json "relay_findings"). No report (old runner, no
    // run yet) -> nothing shown, never a guess from the selection.
    void PaintRelayFindingsInEffect(Dictionary<string, object> root)
    {
        if (_rfNow == null || _rfPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> rf = root != null ? Obj(root, "relay_findings") : null;
        if (rf != null && rf.ContainsKey("mode") && rf["mode"] != null)
        {
            string rep = S(rf, "mode");
            now = RelayFindingsView.Describe(rep,
                rf.ContainsKey("recorded") && rf["recorded"] != null ? I(rf, "recorded") : 0,
                rf.ContainsKey("with_findings") && rf["with_findings"] != null ? I(rf, "with_findings") : 0, ja);
            pend = RelayFindingsView.PendingText(rep, _rfVal, ja);
        }
        _rfNow.Text = now ?? "";
        _rfNow.Foreground = Muted;
        _rfNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _rfPending.Text = pend ?? "";
        _rfPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _rfPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Auto-resume of an interrupted run (off|on), in its own Recovery section. Persists through
    // SaveKey only; the supervisor re-reads the key every cycle (each_gate). What is IN EFFECT comes
    // from status.json "auto_resume" {setting, last_decision, pending_snapshots}; the words and
    // parsing live in AutoResumeView (EffortPolicy.cs).
    UIElement AutoResumeControl()
    {
        _arLbl = new TextBlock(); _arLbl.VerticalAlignment = VerticalAlignment.Center;
        _arLbl.FontSize = 12;

        _arBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_arBox, "autoResumeBox");
        _arBox.ToolTip = AutoResumeView.Help(_lang == 0) + "\n" + AutoResumeView.TakeEffectTip(_lang == 0);
        _arBox.Cursor = Cursors.Hand; _arBox.FontSize = 12;
        _arBox.FontWeight = FontWeights.SemiBold; _arBox.MinWidth = 64;
        _arBox.Padding = new Thickness(8, 2, 4, 2);
        _arBox.VerticalAlignment = VerticalAlignment.Center;
        var arHelp = new Dictionary<string, string>();
        foreach (string m in AutoResumeView.Modes) arHelp[m] = AutoResumeView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_arBox, AutoResumeView.Modes, arHelp, _arVal);
        _arBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _arBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_arBox);
            if (!AutoResumeView.IsMode(sel) || sel == _arVal) return;   // unchanged -> no write, no re-fire
            _arVal = sel;
            SaveKey(AutoResumeView.Key, _arVal);
            PaintAutoResumeInEffect(_lastRoot);
        };

        _arNow = new TextBlock(); _arNow.VerticalAlignment = VerticalAlignment.Center;
        _arNow.FontSize = 11.5; _arNow.TextWrapping = TextWrapping.Wrap; _arNow.MaxWidth = 300;
        _arPending = new TextBlock(); _arPending.VerticalAlignment = VerticalAlignment.Center;
        _arPending.FontSize = 11.5; _arPending.FontWeight = FontWeights.SemiBold;
        _arPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_arLbl, _arBox, _arNow, _arPending);
        PaintAutoResume();
        return wrap;
    }
    void PaintAutoResume()
    {
        if (_arLbl != null) { _arLbl.Text = AutoResumeView.Label(_lang == 0); _arLbl.Foreground = Muted; }
        if (_arBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_arBox), _arVal)) ComboSelectVal(_arBox, _arVal);
        _arBox.ToolTip = AutoResumeView.Help(_lang == 0) + "\n" + AutoResumeView.TakeEffectTip(_lang == 0);
        _arBox.Background = BtnBg; _arBox.Foreground = Fg; _arBox.BorderBrush = Border;
        StyleFlatCombo(_arBox);
        PaintAutoResumeInEffect(_lastRoot);
    }
    // What the SUPERVISOR/COORDINATOR report (status.json "auto_resume"). No report (old runner, no
    // run yet) -> nothing shown, never a guess from the selection.
    void PaintAutoResumeInEffect(Dictionary<string, object> root)
    {
        if (_arNow == null || _arPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> ar = root != null ? Obj(root, "auto_resume") : null;
        if (ar != null && ar.ContainsKey("setting") && ar["setting"] != null)
        {
            string rep = S(ar, "setting");
            Dictionary<string, object> ld = Obj(ar, "last_decision");
            string dec = ld != null ? S(ld, "decision") : null;
            string rsn = ld != null ? S(ld, "reason") : null;
            int pc = ar.ContainsKey("pending_snapshots") && ar["pending_snapshots"] != null ? I(ar, "pending_snapshots") : 0;
            now = AutoResumeView.Describe(rep, dec, rsn, pc, ja);
            pend = AutoResumeView.PendingText(rep, _arVal, ja);
        }
        _arNow.Text = now ?? "";
        _arNow.Foreground = Muted;
        _arNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _arPending.Text = pend ?? "";
        _arPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _arPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Supervisor self-restart (off|on), in the Recovery section under the auto-resume control. The
    // supervisor is a PowerShell script loaded once, so it can run older code than the checkout;
    // it publishes that in .fleet/supervisor_state.json and this note words it (SupervisorCodeView).
    // Persists through SaveKey only; the supervisor reads the key only once it is stale (each_gate).
    UIElement SupervisorSelfRestartControl()
    {
        _srLbl = new TextBlock(); _srLbl.VerticalAlignment = VerticalAlignment.Center;
        _srLbl.FontSize = 12;

        _srBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_srBox, "supervisorCodeBox");
        _srBox.ToolTip = SupervisorCodeView.Help(_lang == 0) + "\n" + SupervisorCodeView.TakeEffectTip(_lang == 0);
        _srBox.Cursor = Cursors.Hand; _srBox.FontSize = 12;
        _srBox.FontWeight = FontWeights.SemiBold; _srBox.MinWidth = 64;
        _srBox.Padding = new Thickness(8, 2, 4, 2);
        _srBox.VerticalAlignment = VerticalAlignment.Center;
        var srHelp = new Dictionary<string, string>();
        foreach (string m in SupervisorCodeView.Modes) srHelp[m] = SupervisorCodeView.ModeLabel(m, _lang == 0);
        FillComboWithHelp(_srBox, SupervisorCodeView.Modes, srHelp, _srVal);
        _srBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _srBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_srBox);
            if (!SupervisorCodeView.IsMode(sel) || sel == _srVal) return;   // unchanged -> no write, no re-fire
            _srVal = sel;
            SaveKey(SupervisorCodeView.Key, _srVal);
        };

        _srNow = new TextBlock(); _srNow.VerticalAlignment = VerticalAlignment.Center;
        _srNow.FontSize = 11.5; _srNow.TextWrapping = TextWrapping.Wrap; _srNow.MaxWidth = 300;
        _srNow.FontWeight = FontWeights.SemiBold;
        _srNow.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_srLbl, _srBox, _srNow, new TextBlock());
        PaintSupervisorSelfRestart();
        return wrap;
    }
    void PaintSupervisorSelfRestart()
    {
        if (_srLbl != null) { _srLbl.Text = SupervisorCodeView.Label(_lang == 0); _srLbl.Foreground = Muted; }
        if (_srBox == null) return;
        if (!Equals(ComboVal(_srBox), _srVal)) ComboSelectVal(_srBox, _srVal);
        _srBox.ToolTip = SupervisorCodeView.Help(_lang == 0) + "\n" + SupervisorCodeView.TakeEffectTip(_lang == 0);
        _srBox.Background = BtnBg; _srBox.Foreground = Fg; _srBox.BorderBrush = Border;
        StyleFlatCombo(_srBox);
        _srReadAt = DateTime.MinValue;   // repaint the note in the new language / theme
        PaintSupervisorCodeNote();
    }
    // The plain message "supervisor is running older code: restart needed", from the supervisor's own
    // report. Nothing is shown when the file is missing, unreadable, names a dead process, or says
    // the code is current. Re-read at most every 5 s (this runs on every status refresh).
    void PaintSupervisorCodeNote()
    {
        if (_srNow == null) return;
        if ((DateTime.UtcNow - _srReadAt).TotalSeconds >= 5.0)
        {
            _srReadAt = DateTime.UtcNow;
            _srText = null;
            try
            {
                string path = Path.Combine(RepoRootForSettings(), ".fleet", "supervisor_state.json");
                if (File.Exists(path))
                {
                    var raw = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
                    bool alive = false;
                    if (raw != null && raw.ContainsKey("pid") && raw.ContainsKey("start_ts"))
                    {
                        // a PID alone can be reused after the supervisor dies: it must also have been born
                        // when the file says
                        int pid = Convert.ToInt32(raw["pid"], System.Globalization.CultureInfo.InvariantCulture);
                        double born = Convert.ToDouble(raw["start_ts"], System.Globalization.CultureInfo.InvariantCulture);
                        try
                        {
                            var p = System.Diagnostics.Process.GetProcessById(pid);
                            if (!p.HasExited)
                            {
                                double actual = new DateTimeOffset(p.StartTime.ToUniversalTime()).ToUnixTimeSeconds();
                                alive = born > 0.0 && Math.Abs(actual - born) <= 2.0;
                            }
                        }
                        catch (Exception) { alive = false; }
                    }
                    _srText = SupervisorCodeView.Describe(raw, alive, _lang == 0);
                }
            }
            catch (Exception) { _srText = null; }
        }
        _srNow.Text = _srText ?? "";
        _srNow.Foreground = Theme.Br(Theme.Warning(_dark));
        _srNow.Visibility = _srText != null ? Visibility.Visible : Visibility.Collapsed;
    }
    // Tool-call check interval (0|15|30|60 min), in the Recovery section. Persists through SaveKey
    // only; the bridge re-reads the key about every 5 minutes (each_gate). What is IN EFFECT comes
    // from the bridge's own account (.fleet/tool_probe_state.json, the same dict as its /status
    // "probe"); the words and parsing live in ToolProbeView (EffortPolicy.cs).
    UIElement ToolProbeControl()
    {
        _tpLbl = new TextBlock(); _tpLbl.VerticalAlignment = VerticalAlignment.Center;
        _tpLbl.FontSize = 12;

        _tpBox = new ComboBox();
        System.Windows.Automation.AutomationProperties.SetAutomationId(_tpBox, "toolProbeBox");
        _tpBox.ToolTip = ToolProbeView.Help(_lang == 0) + "\n" + ToolProbeView.TakeEffectTip(_lang == 0);
        _tpBox.Cursor = Cursors.Hand; _tpBox.FontSize = 12;
        _tpBox.FontWeight = FontWeights.SemiBold; _tpBox.MinWidth = 64;
        _tpBox.Padding = new Thickness(8, 2, 4, 2);
        _tpBox.VerticalAlignment = VerticalAlignment.Center;
        var tpHelp = new Dictionary<string, string>();
        foreach (string m in ToolProbeView.Choices) tpHelp[m] = ToolProbeView.ChoiceLabel(m, _lang == 0);
        FillComboWithHelp(_tpBox, ToolProbeView.Choices, tpHelp, _tpVal);
        _tpBox.DropDownOpened += delegate { CloseHeaderPopups("settings"); };
        _tpBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_tpBox);
            if (!ToolProbeView.IsChoice(sel) || sel == _tpVal) return;   // unchanged -> no write, no re-fire
            _tpVal = sel;
            SaveKey(ToolProbeView.Key, _tpVal);
            PaintToolProbeInEffect();
        };

        _tpNow = new TextBlock(); _tpNow.VerticalAlignment = VerticalAlignment.Center;
        _tpNow.FontSize = 11.5; _tpNow.TextWrapping = TextWrapping.Wrap; _tpNow.MaxWidth = 300;
        _tpPending = new TextBlock(); _tpPending.VerticalAlignment = VerticalAlignment.Center;
        _tpPending.FontSize = 11.5; _tpPending.FontWeight = FontWeights.SemiBold;
        _tpPending.Visibility = Visibility.Collapsed;

        var wrap = SettingsComboBlock(_tpLbl, _tpBox, _tpNow, _tpPending);
        PaintToolProbe();
        return wrap;
    }
    void PaintToolProbe()
    {
        if (_tpLbl != null) { _tpLbl.Text = ToolProbeView.Label(_lang == 0); _tpLbl.Foreground = Muted; }
        if (_tpBox == null) return;
        // assign only when different so SelectionChanged (which persists) does not re-fire
        if (!Equals(ComboVal(_tpBox), _tpVal)) ComboSelectVal(_tpBox, _tpVal);
        _tpBox.ToolTip = ToolProbeView.Help(_lang == 0) + "\n" + ToolProbeView.TakeEffectTip(_lang == 0);
        _tpBox.Background = BtnBg; _tpBox.Foreground = Fg; _tpBox.BorderBrush = Border;
        StyleFlatCombo(_tpBox);
        PaintToolProbeInEffect();
    }
    // The bridge's decision state, re-read only when the file changed. No report (old bridge, no
    // bridge yet) -> null, and the screen shows nothing rather than a guess from the selection.
    Dictionary<string, object> ReadToolProbeState()
    {
        try
        {
            string path = Path.Combine(RepoRoot(), ".fleet", "tool_probe_state.json");
            if (!File.Exists(path)) { _tpState = null; _tpStateMtimeTicks = -1; return null; }
            double ticks = (double)File.GetLastWriteTimeUtc(path).Ticks;
            if (ticks != _tpStateMtimeTicks)
            {
                _tpState = _js.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
                _tpStateMtimeTicks = ticks;
            }
            return _tpState;
        }
        catch (Exception) { return null; }   // mid-write or unreadable: show nothing this time
    }
    void PaintToolProbeInEffect()
    {
        if (_tpNow == null || _tpPending == null) return;
        bool ja = _lang == 0;
        string now = null, pend = null;
        Dictionary<string, object> st = ReadToolProbeState();
        if (st != null && st.ContainsKey("enabled") && st["enabled"] != null)
        {
            bool enabled = Convert.ToBoolean(st["enabled"]);
            double iv = Dbl(st, "interval_min");
            string src = S(st, "source");
            now = ToolProbeView.Describe(true, enabled, iv, src, Dbl(st, "last_sent"), S(st, "last_skipped_reason"),
                                         Dbl(st, "last_skipped_ts"), I(st, "skipped_since_start"), Dbl(st, "evidence_ts"),
                                         I(st, "backoff_failures"), NowUnix(), ja);
            pend = ToolProbeView.PendingText(true, enabled, iv, src, _tpVal, ja);
        }
        _tpNow.Text = now ?? "";
        _tpNow.Foreground = Muted;
        _tpNow.Visibility = now != null ? Visibility.Visible : Visibility.Collapsed;
        _tpPending.Text = pend ?? "";
        _tpPending.Foreground = Theme.Br(Theme.Warning(_dark));
        _tpPending.Visibility = pend != null ? Visibility.Visible : Visibility.Collapsed;
    }
    void PaintEffort()
    {
        if (_effortLbl != null) { _effortLbl.Text = T("effort"); _effortLbl.Foreground = Muted; }
        if (_effortBox == null) return;
        // keep the dropdown in sync with _effort (e.g. settings.txt changed externally) without
        // re-firing the persist handler -- assigning the same value is a no-op for SelectionChanged.
        if (!Equals(ComboVal(_effortBox), _effort)) ComboSelectVal(_effortBox, _effort);
        _effortBox.Background = BtnBg; _effortBox.Foreground = Fg; _effortBox.BorderBrush = Border;
        System.Windows.Automation.AutomationProperties.SetName(_effortBox, _lang == 0 ? "推論" : "Reasoning");
        StyleFlatCombo(_effortBox);   // re-template each paint so a theme toggle retints resting + popup
    }

    // WPY ComboBox bug fix: the stock Aero ControlTemplate ignores Background/Foreground and
    // paints a SYSTEM-themed toggle (a light/dark fill that does NOT track our slate theme -- in
    // LIGHT mode it shows as a dark box). We replace the whole template with a flat one whose
    // toggle border, the editable area, and the dropdown Popup/items are bound to OUR theme
    // brushes, so resting + open states match the cockpit in both dark and light. Built entirely
    // in code-behind (no XAML) via FrameworkElementFactory, and re-applied on every PaintEffort/
    // PaintApproval so toggling the theme retints it. Reusable for both dropdowns.
    void StyleFlatCombo(ComboBox cb)
    {
        if (cb == null) return;
        var tmpl = new ControlTemplate(typeof(ComboBox));

        // root grid: [content area *][toggle arrow auto], wrapped by a themed border
        var border = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        border.SetValue(System.Windows.Controls.Border.BackgroundProperty, BtnBg);
        border.SetValue(System.Windows.Controls.Border.BorderBrushProperty, Border);
        border.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new Thickness(1));
        border.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(Theme.RadChip));

        var grid = new FrameworkElementFactory(typeof(Grid));
        var c0 = new FrameworkElementFactory(typeof(ColumnDefinition));
        c0.SetValue(ColumnDefinition.WidthProperty, new GridLength(1, GridUnitType.Star));
        var c1 = new FrameworkElementFactory(typeof(ColumnDefinition));
        c1.SetValue(ColumnDefinition.WidthProperty, GridLength.Auto);
        grid.AppendChild(c0); grid.AppendChild(c1);

        // an invisible ToggleButton spanning both columns IS the click target that opens the popup.
        // Its template is just a transparent border so it contributes no system chrome of its own.
        var toggle = new FrameworkElementFactory(typeof(ToggleButton));
        toggle.SetValue(Grid.ColumnSpanProperty, 2);
        toggle.SetValue(ToggleButton.BackgroundProperty, Brushes.Transparent);
        toggle.SetValue(ToggleButton.BorderThicknessProperty, new Thickness(0));
        toggle.SetValue(ToggleButton.FocusableProperty, false);
        toggle.SetValue(ToggleButton.IsTabStopProperty, false);
        var togTmpl = new ControlTemplate(typeof(ToggleButton));
        var togBd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border));
        togBd.SetValue(System.Windows.Controls.Border.BackgroundProperty, Brushes.Transparent);
        togTmpl.VisualTree = togBd;
        toggle.SetValue(ToggleButton.TemplateProperty, togTmpl);
        var togBind = new System.Windows.Data.Binding("IsDropDownOpen");
        togBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        togBind.Mode = BindingMode.TwoWay;
        toggle.SetBinding(ToggleButton.IsCheckedProperty, togBind);

        // the selected-value text (column 0)
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(Grid.ColumnProperty, 0);
        var selBind = new System.Windows.Data.Binding("SelectionBoxItem");
        selBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        cp.SetBinding(ContentPresenter.ContentProperty, selBind);
        cp.SetValue(ContentPresenter.MarginProperty, new Thickness(8, 2, 2, 2));
        cp.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        cp.SetValue(System.Windows.Documents.TextElement.ForegroundProperty, Fg);
        cp.SetValue(UIElement.IsHitTestVisibleProperty, false);

        // a small themed arrow glyph (column 1)
        var arrow = new FrameworkElementFactory(typeof(System.Windows.Shapes.Path));
        arrow.SetValue(Grid.ColumnProperty, 1);
        arrow.SetValue(System.Windows.Shapes.Path.DataProperty, Geometry.Parse("M 0 0 L 8 0 L 4 5 Z"));
        arrow.SetValue(System.Windows.Shapes.Path.FillProperty, Muted);
        arrow.SetValue(System.Windows.Shapes.Path.MarginProperty, new Thickness(4, 0, 8, 0));
        arrow.SetValue(System.Windows.Shapes.Path.VerticalAlignmentProperty, VerticalAlignment.Center);
        arrow.SetValue(UIElement.IsHitTestVisibleProperty, false);

        // the dropdown popup: a themed border around the item host, so the OPEN list also matches.
        var popup = new FrameworkElementFactory(typeof(Popup), "PART_Popup");
        popup.SetValue(Popup.PlacementProperty, PlacementMode.Bottom);
        popup.SetValue(Popup.AllowsTransparencyProperty, true);
        popup.SetValue(Popup.FocusableProperty, false);
        var popBind = new System.Windows.Data.Binding("IsDropDownOpen");
        popBind.RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent);
        popup.SetBinding(Popup.IsOpenProperty, popBind);
        var popBd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border));
        popBd.SetValue(System.Windows.Controls.Border.BackgroundProperty, CardBg);
        popBd.SetValue(System.Windows.Controls.Border.BorderBrushProperty, Border);
        popBd.SetValue(System.Windows.Controls.Border.BorderThicknessProperty, new Thickness(1));
        popBd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, new CornerRadius(Theme.RadChip));
        popBd.SetValue(System.Windows.Controls.Border.MarginProperty, new Thickness(0, 2, 0, 0));
        popBd.SetValue(FrameworkElement.MinWidthProperty, 78.0);
        var sv = new FrameworkElementFactory(typeof(ScrollViewer));
        sv.SetValue(ScrollViewer.MaxHeightProperty, 220.0);
        var ip = new FrameworkElementFactory(typeof(ItemsPresenter));
        sv.AppendChild(ip); popBd.AppendChild(sv); popup.AppendChild(popBd);

        grid.AppendChild(toggle); grid.AppendChild(cp); grid.AppendChild(arrow); grid.AppendChild(popup);
        border.AppendChild(grid);
        tmpl.VisualTree = border;
        cb.Template = tmpl;

        // per-item style: themed background + Fg text + Accent-mix hover/selected, so the OPEN
        // list never falls back to the system (dark) item chrome.
        cb.ItemContainerStyle = FlatComboItemStyle();
        cb.ApplyTemplate();
    }

    Style FlatComboItemStyle()
    {
        var st = new Style(typeof(ComboBoxItem));
        st.Setters.Add(new Setter(Control.BackgroundProperty, BtnBg));
        st.Setters.Add(new Setter(Control.ForegroundProperty, Fg));
        st.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(8, 4, 8, 4)));
        st.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        var it = new ControlTemplate(typeof(ComboBoxItem));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.PaddingProperty, new TemplateBindingExtension(Control.PaddingProperty));
        var icp = new FrameworkElementFactory(typeof(ContentPresenter));
        bd.AppendChild(icp);
        it.VisualTree = bd;
        // hover + selected -> an accent-tinted fill (matches the cockpit's hover idiom), still readable.
        var hover = new Trigger { Property = ComboBoxItem.IsHighlightedProperty, Value = true };
        hover.Setters.Add(new Setter(Control.BackgroundProperty,
            // WAS #ea580c -- the accent from two revisions ago, frozen here because it was typed
            // instead of referenced. The palette moved twice and this hover did not follow.
            new SolidColorBrush(Mix(C(Theme.Accent(_dark)), CardColor(), 0.22)), "Bd"));
        hover.Setters.Add(new Setter(Control.ForegroundProperty, Fg));
        it.Triggers.Add(hover);
        st.Setters.Add(new Setter(Control.TemplateProperty, it));
        return st;
    }

    // Run-mode selector: run/plan/auto controls how a task starts.  It is intentionally labelled
    // separately from the Approval Center; the old "Approval" label made users expect this combo
    // to contain the actual pending decisions.
    static readonly string[] _approvalModes = { "run", "plan", "auto" };
    UIElement ApprovalControl()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center; wrap.Margin = new Thickness(0, 0, 12, 0);

        _approvalLbl = new TextBlock(); _approvalLbl.VerticalAlignment = VerticalAlignment.Center;
        _approvalLbl.FontSize = 12; _approvalLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_approvalLbl);

        _approvalBox = new ComboBox();
        _approvalBox.ToolTip = _lang == 0
            ? "実行方式: run=すぐ実行、plan=計画承認待ち、auto=通常フリートは計画承認待ち/フォルダ自律はGO-ASK-STOP判定"
            : "Run mode: run=run now, plan=wait for approval, auto=plain fleet waits for plan approval; folder autonomy uses GO/ASK/STOP";
        _approvalBox.Cursor = Cursors.Hand; _approvalBox.FontSize = 12;
        _approvalBox.FontWeight = FontWeights.SemiBold; _approvalBox.MinWidth = 74;
        _approvalBox.Padding = new Thickness(8, 2, 4, 2);
        _approvalBox.VerticalAlignment = VerticalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(_approvalBox, _lang == 0 ? "実行方式" : "Run mode");
        FillComboWithHelp(_approvalBox, _approvalModes, ApprovalHelp(), _approval);  // per-option hover help
        _approvalBox.DropDownOpened += delegate { CloseHeaderPopups("approval"); };
        _approvalBox.SelectionChanged += delegate
        {
            string sel = ComboVal(_approvalBox);
            if (string.IsNullOrEmpty(sel) || sel == _approval) return;
            _approval = sel;
            SaveKey("approval", _approval);
        };
        wrap.Children.Add(_approvalBox);

        PaintApproval();
        return wrap;
    }
    void PaintApproval()
    {
        if (_approvalLbl != null) { _approvalLbl.Text = T("run_mode"); _approvalLbl.Foreground = Muted; }
        if (_approvalBox == null) return;
        if (!Equals(ComboVal(_approvalBox), _approval)) ComboSelectVal(_approvalBox, _approval);
        _approvalBox.Background = BtnBg; _approvalBox.Foreground = Fg; _approvalBox.BorderBrush = Border;
        System.Windows.Automation.AutomationProperties.SetName(_approvalBox, _lang == 0 ? "実行方式" : "Run mode");
        StyleFlatCombo(_approvalBox);   // same flat-template fix so the open list matches the theme
    }

    // ── per-option hover help for the 推論 / 承認 dropdowns ──────────────────────────────────
    // Other cockpit buttons carry a ToolTip; the effort/approval dropdown OPTIONS did not. We now
    // add each option as a ComboBoxItem with its own ToolTip, so hovering an item in the open list
    // explains what that mode does. ComboVal/ComboSelectVal keep the string-based selection logic.
    Dictionary<string, string> EffortHelp()
    {
        return _lang == 0
            ? new Dictionary<string, string> {
                { "min", "最小: 速い・浅い。簡単なタスク向け（調査/反論を抑制）" },
                { "max", "最大: 深い調査と自己反論。難タスク向け（遅いが高品質）" },
                { "ultra", "ウルトラ: 最深。research＋self-test＋反論をフル動員（最重・最高品質）" },
                { "auto", "自動: タスク難度に応じて min〜ultra を自動選択（推奨）" } }
            : new Dictionary<string, string> {
                { "min", "Min: fast, shallow. Easy tasks (research/refute suppressed)." },
                { "max", "Max: deep research + self-refute. Hard tasks (slower, higher quality)." },
                { "ultra", "Ultra: deepest. Full research + self-test + refutation (heaviest, best)." },
                { "auto", "Auto: pick min..ultra by task difficulty (recommended)." } };
    }
    Dictionary<string, string> ApprovalHelp()
    {
        return _lang == 0
            ? new Dictionary<string, string> {
                { "run", "run: 承認を挟まず即実行。" },
                { "plan", "plan: 計画を提示して「承認待ち」で停止。カードに承認/修正を steer。" },
                { "auto", "auto: 通常フリートは計画承認待ち／フォルダ自律は GO-ASK-STOP ゲートで自走。" } }
            : new Dictionary<string, string> {
                { "run", "run: execute immediately, no approval step." },
                { "plan", "plan: present a plan and pause at approval; steer the card to approve/edit." },
                { "auto", "auto: plain fleet waits for plan approval; folder autonomy self-runs via GO-ASK-STOP." } };
    }
    void FillComboWithHelp(ComboBox cb, string[] opts, Dictionary<string, string> help, string current)
    {
        cb.Items.Clear();
        foreach (string m in opts)
        {
            var it = new ComboBoxItem(); it.Content = m;
            string h; if (help != null && help.TryGetValue(m, out h)) it.ToolTip = h;
            cb.Items.Add(it);
            if (m == current) cb.SelectedItem = it;
        }
    }
    static string ComboVal(ComboBox cb)
    {
        var it = cb.SelectedItem as ComboBoxItem;
        return it != null ? (it.Content as string) : (cb.SelectedItem as string);
    }
    static void ComboSelectVal(ComboBox cb, string val)
    {
        foreach (var o in cb.Items)
        {
            var it = o as ComboBoxItem;
            if (it != null && (it.Content as string) == val) { cb.SelectedItem = it; return; }
        }
    }
}
