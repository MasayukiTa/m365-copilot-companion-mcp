// FleetCockpit.CardToolbar.cs -- the pinned card toolbar: directive band, counts, filter segments, auto-retry/archive buttons.
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

    // TASK 3: Directive band — shown only in the live/active branch, inserted above card list.
    // Reads goal from the first/primary worker (passed as `firstWorker`).
    // [REAL goal], [COMPUTED meta] — spec §5a. No fabrication; multi-goal case is honest.
    UIElement DirectiveBand(Dictionary<string, object> firstWorker)
    {
        bool ja = _lang == 0;

        // Gather goal texts from the ON-BOARD workers (History-cleared lanes excluded, set in
        // BuildRows) to determine single vs multi-goal.
        var goalTexts = new List<string>();
        var goalDisplays = new List<string>();
        var dbSrc = _directiveBandWorkers != null && _directiveBandWorkers.Count > 0 ? _directiveBandWorkers : _toolbarAll;
        foreach (Dictionary<string, object> tw in dbSrc)
        {
            string fullGoal = S(tw, "goal");
            if (string.IsNullOrEmpty(fullGoal) || goalTexts.Contains(fullGoal)) continue;
            goalTexts.Add(fullGoal);
            string displayGoal = S(tw, "goal_summary");
            goalDisplays.Add(!string.IsNullOrEmpty(displayGoal) ? displayGoal : CardTitle("", fullGoal));
        }

        // Section label: "DIRECTIVE" / "指示" when a single goal; "Goals (N)" / "ゴール (N)" for multiple.
        bool multiGoal = goalTexts.Count > 1;
        string sectionLabel = multiGoal
            ? (ja ? ("ゴール (" + goalTexts.Count + ")") : ("Goals (" + goalTexts.Count + ")"))
            : (ja ? "指示" : "DIRECTIVE");

        // EVERY GOAL, ONE PER LINE. This used to render goalTexts[0] and summarise the rest as
        // "(他 N lane)" -- so with two goals in flight the band showed one of them and a count,
        // and WHICH one it showed depended on the order the worker list happened to arrive in.
        // Every refresh could pick a different first element, so the panel flickered between
        // goals as lanes progressed, and the operator saw the view "move" on its own.
        //
        // The list was already complete here; only the rendering threw it away. Concurrent
        // lanes are concurrent: they belong side by side, not behind a number.
        string goalDisplay = string.Join("\n", goalDisplays.ToArray());

        // Meta line: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
        string metaLine = _directiveBandMeta;

        var outer = new Border();
        outer.Margin = new Thickness(16, 0, 16, 0);
        outer.Padding = new Thickness(0, 8, 0, 0);
        outer.BorderThickness = new Thickness(0, 0, 0, 1);
        outer.BorderBrush = Theme.Br(Theme.Border(_dark));

        var col = new StackPanel();

        // Section label
        var lbl = new TextBlock();
        lbl.Text = sectionLabel;
        lbl.Foreground = Theme.Br(Theme.Muted(_dark));
        lbl.FontSize = Theme.FsMeta;
        lbl.FontWeight = FontWeights.SemiBold;
        lbl.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(lbl);

        // Separator line
        var sep = new Border();
        sep.Height = 1;
        sep.Background = Theme.Br(Theme.Border(_dark));
        sep.Margin = new Thickness(0, 0, 0, 6);
        col.Children.Add(sep);

        // Goal text — show even if empty (no fallback fabrication; empty just shows nothing)
        if (!string.IsNullOrEmpty(goalDisplay))
        {
            var goalTb = new TextBlock();
            goalTb.Text = goalDisplay;
            goalTb.Foreground = Theme.Br(Theme.Text(_dark));
            goalTb.FontSize = Theme.FsBody;
            goalTb.TextWrapping = TextWrapping.Wrap;
            goalTb.Margin = new Thickness(0, 0, 0, 4);
            col.Children.Add(goalTb);
        }

        // Meta line (started · elapsed · lanes)
        if (!string.IsNullOrEmpty(metaLine))
        {
            var metaTb = new TextBlock();
            metaTb.Text = metaLine;
            metaTb.Foreground = Theme.Br(Theme.Muted(_dark));
            metaTb.FontSize = Theme.FsMeta;
            metaTb.Margin = new Thickness(0, 0, 0, 8);
            col.Children.Add(metaTb);
        }

        outer.Child = col;
        return outer;
    }


    // Single source of truth for the toolbar's per-category counts. Returns an int[] so both the
    // toolbar render (BuildCardToolbar) and the row signature (RowSig) use IDENTICAL arithmetic --
    // otherwise a status-only change (pending->done) that keeps cntAll constant would not flip the
    // signature and the toolbar would render stale counts until the next forced re-render.
    // Index map: [0]=cntAll [1]=cntActive [2]=cntNeeds [3]=cntDone [4]=doneN [5]=maxN [6]=badN
    //            [7]=hiddenTerminal (terminal cards moved to History, excluded above -- folding this
    //            into the signature makes "send to history" also re-render the toolbar).
    int[] ToolbarCounts(List<Dictionary<string, object>> all)
    {
        int cntAll = 0, cntActive = 0, cntNeeds = 0, cntDone = 0;
        int doneN = 0, maxN = 0, badN = 0, hiddenTerminal = 0, intN = 0;
        string startedRootTb = _lastRoot != null ? S(_lastRoot, "started") : "";
        if (all != null)
        {
            foreach (Dictionary<string, object> w in all)
            {
                if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRootTb, w)))
                {
                    hiddenTerminal++;
                    continue;   // moved to History -- not on the board, don't count
                }
                cntAll++;
                string oc = S(w, "outcome");
                string st = S(w, "status");
                if (oc == "DONE") { doneN++; cntDone++; }
                else if (oc == "MAXTURNS") maxN++;
                // A CONTRADICTED claim counts with the failures, never with the completions.
                // Leaving it out of both tallies would be worse than either: the totals would
                // stop adding up to the number of workers, and a card would vanish from the
                // counts while still sitting on screen.
                else if (oc == "STUCK" || oc == "ERROR" || oc == "CANCELLED"
                         || oc == "EVIDENCE_CONTRADICTED") badN++;
                if (st == "awaiting") cntNeeds++;
                if (IsInterruptedWorker(w)) intN++;   // own counter: resumable, not a failure of the goal
                if (!IsTerminalWorker(w) && !IsInterruptedWorker(w) && st != "pending") cntActive++;
            }
        }
        return new int[] { cntAll, cntActive, cntNeeds, cntDone, doneN, maxN, badN, hiddenTerminal, intN };
    }

    UIElement BuildCardToolbar(List<Dictionary<string, object>> all,
                               List<Dictionary<string, object>> shown)
    {
        // Per-tab counts from the ON-BOARD worker list = the full list MINUS the terminal cards
        // the user moved to History (_hiddenKeys). We don't use `shown` because that is also
        // narrowed by the active filter; we want totals-per-category independent of the filter,
        // but a card moved to History must not be counted (else "all 1 / done 1" persists after
        // everything was sent to history). Counts come from the shared ToolbarCounts() helper so
        // the render and RowSig can never diverge.
        int[] tc = ToolbarCounts(all);
        int cntAll = tc[0], cntActive = tc[1], cntNeeds = tc[2], cntDone = tc[3];
        int doneN = tc[4], maxN = tc[5], badN = tc[6], cntIntr = tc[8];

        var bar = new Border();
        bar.BorderThickness = new Thickness(1); bar.BorderBrush = Border;
        bar.Background = CardBg; bar.CornerRadius = new CornerRadius(Theme.RadPopover);
        bar.Padding = new Thickness(12, 8, 12, 8); bar.Margin = new Thickness(8, 2, 8, 8);
        // clicks inside the toolbar must not bubble (it isn't a card, but stay safe)
        bar.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };

        var dp = new DockPanel();

        // right cluster: bulk MANUAL retry button (one-shot, respects the active filter)
        var rightCl = new StackPanel(); rightCl.Orientation = Orientation.Horizontal;
        rightCl.VerticalAlignment = VerticalAlignment.Center;

        // Only surface the bulk-retry button when there is at least one retry target in
        // `shown`. Mirrors RetryAllShown's selection, which is now the same closed set Python
        // uses -- see AutoRetryScan for what "anything but DONE" cost the fan-out.
        int retryTargets = 0;
        foreach (Dictionary<string, object> rw in shown)
        {
            if (!IsTerminalWorker(rw)) continue;
            if (!IsRetryableWorker(rw)) continue;
            retryTargets++;
        }
        if (retryTargets > 0)
        {
            var retryAll = new Button();
            retryAll.Content = T("retry_all");
            retryAll.Background = Brushes.Transparent; retryAll.Foreground = Fg;
            retryAll.BorderThickness = new Thickness(1); retryAll.BorderBrush = Theme.Br(Theme.BorderStrong(_dark));
            retryAll.Padding = new Thickness(12, 4, 12, 4); retryAll.Cursor = Cursors.Hand;
            retryAll.FontSize = 12; retryAll.FontWeight = FontWeights.SemiBold;
            retryAll.Margin = new Thickness(12, 0, 0, 0);
            retryAll.VerticalAlignment = VerticalAlignment.Center;
            List<Dictionary<string, object>> shownCap = shown;
            retryAll.Click += delegate
            {
                // Immediate, visible acknowledgement: the button is disabled and relabelled before
                // anything else runs, so a second press cannot queue the same goals twice and the
                // press never looks like a no-op. The delivery itself runs off the UI thread
                // (RetryAllShown with a completion callback); the outcome replaces the label.
                if (!retryAll.IsEnabled) return;
                string idleLabel = T("retry_all");
                retryAll.IsEnabled = false;
                retryAll.Content = _lang == 0 ? "再開中…" : "Resuming…";
                if (_toolbarNote != null) _toolbarNote.Text = _lang == 0 ? "再開を依頼しています…" : "Requesting resume…";
                bool wasLive = RunIsLive();
                int skipped;
                var queuedBox = new int[1];   // the callback runs later, after queued is known
                int queued = RetryAllShown(shownCap, out skipped, delegate (bool ok, string err)
                {
                    retryAll.Content = idleLabel;
                    retryAll.IsEnabled = true;
                    if (_toolbarNote == null) return;
                    if (!ok)
                    {
                        _toolbarNote.Text = (_lang == 0 ? "再開に失敗: " : "Resume failed: ") + err;
                        return;
                    }
                    _toolbarNote.Text = (_lang == 0 ? "再開を依頼しました（" + queuedBox[0] + " 件）"
                                                    : "Resume requested")
                                        + (wasLive ? "" : " — " + T("retry_note"));
                });
                queuedBox[0] = queued;
                if (queued == 0)
                {
                    // Nothing to deliver, so no callback will come: say so and release the button.
                    retryAll.Content = idleLabel;
                    retryAll.IsEnabled = true;
                    if (_toolbarNote != null)
                        _toolbarNote.Text = skipped > 0
                            ? skipped + "/" + skipped + " " + T("retry_capped")
                            : (_lang == 0 ? "再開できる停止タスクはありません" : "Nothing to resume");
                    return;
                }
                if (skipped > 0 && _toolbarNote != null)
                {
                    // Say how many of the shown targets were refused and why. Without this
                    // the press looks like a no-op and gets repeated.
                    _toolbarNote.Text = skipped + "/" + (queued + skipped) + " " + T("retry_capped");
                }
            };
            rightCl.Children.Add(retryAll);
        }

        DockPanel.SetDock(rightCl, Dock.Right);
        dp.Children.Add(rightCl);

        // left: 4 spec tabs as a single segmented control container
        var left = new StackPanel(); left.Orientation = Orientation.Horizontal;
        left.VerticalAlignment = VerticalAlignment.Center;

        // Segmented container: one rounded Border holding all four tabs side-by-side.
        var seg = new Border();
        seg.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        seg.BorderBrush = Theme.Br(Theme.Border(_dark));
        seg.BorderThickness = new Thickness(1);
        seg.CornerRadius = new CornerRadius(Theme.RadSmall);
        seg.VerticalAlignment = VerticalAlignment.Center;
        var segRow = new StackPanel(); segRow.Orientation = Orientation.Horizontal;

        // Build all four tabs; insert thin dividers between them.
        string allLabel = T("flt_all") + " " + cntAll;
        string activeLabel = T("flt_active") + " " + cntActive;
        string needsLabel = T("flt_needs") + " " + cntNeeds;
        string doneLabel = T("flt_done") + " " + cntDone;

        segRow.Children.Add(SegFilterButton(allLabel, 0, false, 0, true, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(activeLabel, 1, false, 0, false, false));
        segRow.Children.Add(SegDivider());
        segRow.Children.Add(SegFilterButton(needsLabel, 2, true, cntNeeds, false, false));
        segRow.Children.Add(SegDivider());
        bool showIntr = cntIntr > 0 || _cardFilter == 4;
        segRow.Children.Add(SegFilterButton(doneLabel, 3, false, 0, false, !showIntr));
        if (showIntr)
        {
            segRow.Children.Add(SegDivider());
            segRow.Children.Add(SegFilterButton(T("flt_intr") + " " + cntIntr, 4, false, 0, false, true));
        }

        seg.Child = segRow;
        left.Children.Add(seg);

        // Task 5: only show the summary when there are failures; hide when nothing notable.
        int failTotal = badN + maxN;
        if (failTotal > 0)
        {
            var summary = new TextBlock();
            summary.Text = (_lang == 0 ? ("失敗 " + failTotal) : ("Failed " + failTotal));
            summary.Foreground = Theme.Br(Theme.Danger(_dark)); summary.FontSize = 12;
            summary.FontWeight = FontWeights.SemiBold;
            summary.VerticalAlignment = VerticalAlignment.Center;
            summary.Margin = new Thickness(16, 0, 0, 0);
            left.Children.Add(summary);
        }

        _toolbarNote = new TextBlock();
        _toolbarNote.Foreground = Muted; _toolbarNote.FontSize = 11.5;
        _toolbarNote.VerticalAlignment = VerticalAlignment.Center;
        _toolbarNote.Margin = new Thickness(16, 0, 0, 0);
        left.Children.Add(_toolbarNote);

        dp.Children.Add(left);
        bar.Child = dp;
        return bar;
    }
    TextBlock _toolbarNote;
    Button _autoRetryBtn;
    TextBlock _autoRetryCapVal;
    Button _reconnectChatBtn;   // settings-panel "チャット再接続"/"Reconnect chat" -- always available,
                                 // regardless of the Tool dot's state (see RunBridgeReconnectManual).

    // ON => calm success-tinted chip (AccentSoft bg with Success border + text); OFF => neutral muted.
    // Accent fill is reserved exclusively for the primary Start action; this is a status toggle chip.
    void PaintAutoRetryBtn()
    {
        if (_autoRetryBtn == null) return;
        _autoRetryBtn.Content = T("autoretry") + ": " + (_autoRetry ? "ON" : "OFF");
        if (_autoRetry)
        {
            // Subtle success treatment: light chip background, success-colored border, dark/fg text
            // In dark mode: AccentSoft bg is very dark (#3A2416) -- use SurfaceSubtle instead for
            // better readability as a success chip; pair with Success border and Success-tinted text.
            _autoRetryBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoRetryBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoRetryBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoRetryBtn.Background = BtnBg; _autoRetryBtn.Foreground = Muted; _autoRetryBtn.BorderBrush = Border;
        }
    }

    // P2 (c): auto-archive toggle chip -- same calm success/neutral treatment as PaintAutoRetryBtn.
    void PaintAutoArchiveBtn()
    {
        if (_autoArchiveBtn == null) return;
        _autoArchiveBtn.Content = T("autoarchive") + ": " + (_autoArchive ? "ON" : "OFF");
        if (_autoArchive)
        {
            _autoArchiveBtn.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _autoArchiveBtn.Foreground = Theme.Br(Theme.Success(_dark));
            _autoArchiveBtn.BorderBrush = Theme.Br(Theme.Success(_dark));
        }
        else
        {
            _autoArchiveBtn.Background = BtnBg; _autoArchiveBtn.Foreground = Muted; _autoArchiveBtn.BorderBrush = Border;
        }
    }

    void SetAutoRetryMax(int v)
    {
        _autoRetryMax = Math.Max(1, Math.Min(3, v));   // hard safety bound: 1..3, never unbounded
        SaveKey("autoretry_max", _autoRetryMax.ToString());
        if (_autoRetryCapVal != null) _autoRetryCapVal.Text = _autoRetryMax.ToString();
    }

    // Thin vertical divider between segmented filter tabs.
    UIElement SegDivider()
    {
        var d = new Border { Width = 1, Background = Theme.Br(Theme.Border(_dark)),
                             VerticalAlignment = VerticalAlignment.Stretch,
                             Margin = new Thickness(0, 4, 0, 4) };
        return d;
    }

    // One tab button inside the segmented filter container. Active tab gets a Surface-colored
    // rounded background; inactive tabs are transparent. `isNeeds` + amber treatment preserved.
    // `isFirst`/`isLast` clip corner radius so the active fill doesn't overflow the container border.
    Button SegFilterButton(string label, int val, bool isNeeds, int needsCount, bool isFirst, bool isLast)
    {
        var b = new Button();
        b.Content = label; b.Cursor = Cursors.Hand; b.FontSize = 12;
        b.Padding = new Thickness(12, 5, 12, 5);
        b.BorderThickness = new Thickness(0);   // no individual border -- container holds the border
        bool active = _cardFilter == val;
        CornerRadius cr = new CornerRadius(
            isFirst ? 5 : 0, isLast ? 5 : 0, isLast ? 5 : 0, isFirst ? 5 : 0);
        if (active)
        {
            if (isNeeds && needsCount > 0)
            {
                // Warning amber: active "Needs input" with items
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Theme.Br(Theme.Warning(_dark));
                b.FontWeight = FontWeights.SemiBold;
            }
            else
            {
                // Normal active: Surface fill, Fg text, semibold
                b.Background = Theme.Br(Theme.Surface(_dark));
                b.Foreground = Fg;
                b.FontWeight = FontWeights.SemiBold;
            }
        }
        else
        {
            b.Background = Brushes.Transparent;
            b.Foreground = Muted;
            b.FontWeight = FontWeights.Normal;
        }
        // Use flat template so Background is respected (default Aero ignores it)
        var tmpl = new ControlTemplate(typeof(Button));
        var bd = new FrameworkElementFactory(typeof(System.Windows.Controls.Border), "Bd");
        bd.SetValue(System.Windows.Controls.Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
        bd.SetValue(System.Windows.Controls.Border.CornerRadiusProperty, cr);
        var cp2 = new FrameworkElementFactory(typeof(ContentPresenter));
        cp2.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        cp2.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
        cp2.SetValue(ContentPresenter.MarginProperty, new Thickness(0));
        bd.AppendChild(cp2);
        tmpl.VisualTree = bd;
        b.Template = tmpl;
        int v = val;
        b.Click += delegate
        {
            if (_cardFilter == v) return;
            _cardFilter = v;
            _lastSig = ""; OnTick(null, null);
        };
        return b;
    }


    // "⋮" kebab button for card secondary actions. A null label inserts a visual separator.
    // Uses a WPF ContextMenu
    // (not a manual Popup) so it survives the VirtualizingStackPanel recycling its host row
    // without causing layout/focus loops. A ContextMenu is hosted in its own popup root and
    // is NOT tied to the visual tree of the virtualized element; it handles detach gracefully.
    UIElement CardKebabBtn(string[] labels, Action[] actions, Dictionary<string, object> w)
    {
        var btn = new Button();
        // Draw three vertical dots as geometry
        var dotsPanel = new StackPanel { Orientation = Orientation.Vertical,
                                         VerticalAlignment = VerticalAlignment.Center,
                                         HorizontalAlignment = HorizontalAlignment.Center };
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse { Width = 3, Height = 3, Fill = Muted };
            if (i > 0) dot.Margin = new Thickness(0, 3, 0, 0);
            dotsPanel.Children.Add(dot);
        }
        btn.Content = dotsPanel;
        btn.Width = 28; btn.Height = 28; btn.Cursor = Cursors.Hand;
        btn.BorderThickness = new Thickness(0); btn.Background = Brushes.Transparent;
        btn.Padding = new Thickness(0);
        btn.ToolTip = _lang == 0 ? "その他の操作" : "More actions";
        btn.Template = FlatButtonTemplate();
        // Screen-reader name: include the worker name so each card's kebab is distinguishable.
        string kebabWorker = S(w, "name");
        string kebabName;
        if (!string.IsNullOrEmpty(kebabWorker))
            kebabName = _lang == 0 ? (kebabWorker + " その他の操作") : (kebabWorker + " more actions");
        else
            kebabName = _lang == 0 ? "その他の操作" : "More actions";
        System.Windows.Automation.AutomationProperties.SetName(btn, kebabName);

        // Build a ContextMenu. Unlike a manual Popup, a ContextMenu lives in its own HwndSource
        // and does not hold a reference that breaks when the virtualizing container is recycled.
        var cm = new ContextMenu();
        cm.Background = Theme.Br(Theme.Surface(_dark));
        cm.BorderBrush = Theme.Br(Theme.Border(_dark));
        cm.BorderThickness = new Thickness(1);
        cm.Padding = new Thickness(2);
        // The kebab sits at the far-right edge of a card. Shift the popup left so the complete
        // menu remains inside the Fleet window instead of growing off its right edge.
        cm.MinWidth = 240;
        cm.HorizontalOffset = -250;

        // Lazy menu build: with ~330 cards on screen, materializing every card's MenuItems up
        // front is wasted work (most menus are never opened). Defer the item construction to the
        // menu's first Opened, and guard with a built flag so re-opening does not rebuild.
        string[] lbls = labels;
        Action[] acts = actions;
        bool menuBuilt = false;
        cm.Opened += delegate (object s3, RoutedEventArgs e3)
        {
            if (menuBuilt) return;
            menuBuilt = true;
            for (int i = 0; i < lbls.Length; i++)
            {
                int idx = i;
                if (lbls[idx] == null)
                {
                    cm.Items.Add(new Separator { Margin = new Thickness(4, 3, 4, 3) });
                    continue;
                }
                var mi = new MenuItem();
                mi.Header = lbls[idx];
                mi.Background = Brushes.Transparent;
                mi.Foreground = Theme.Br(Theme.Text(_dark));
                mi.BorderThickness = new Thickness(0);
                mi.Padding = new Thickness(12, 6, 12, 6);
                mi.FontSize = 12.5;
                Action a = acts[idx];
                mi.Click += delegate (object s2, RoutedEventArgs e2) { a(); };
                cm.Items.Add(mi);
            }
        };
        btn.ContextMenu = cm;

        btn.Click += delegate (object s, RoutedEventArgs e)
        {
            e.Handled = true;
            if (btn.ContextMenu != null)
            {
                btn.ContextMenu.PlacementTarget = btn;
                btn.ContextMenu.Placement = PlacementMode.Bottom;
                btn.ContextMenu.IsOpen = true;
            }
        };
        // ButtonBase already handles its mouse-up. Do not mark PreviewMouseLeftButtonUp handled:
        // doing so prevents ButtonBase from raising Click, leaving this menu apparently dead.
        return btn;
    }
}
