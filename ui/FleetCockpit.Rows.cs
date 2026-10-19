// FleetCockpit.Rows.cs -- the row list: item template, BuildRows, history rows, Row types, empty state, resume affordance.
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

    // ItemContainerStyle: invisible chrome. The ControlTemplate is just a ContentPresenter, so
    // there is NO selection highlight, NO mouse-over, NO focus rectangle -- it behaves like a
    // plain scrolling row, not a selectable list item.
    Style BuildItemContainerStyle()
    {
        var st = new Style(typeof(ListBoxItem));
        st.Setters.Add(new Setter(Control.BackgroundProperty, Brushes.Transparent));
        st.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(0)));
        st.Setters.Add(new Setter(FrameworkElement.MarginProperty, new Thickness(0)));
        st.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty, HorizontalAlignment.Stretch));
        st.Setters.Add(new Setter(UIElement.FocusableProperty, false));
        var tmpl = new ControlTemplate(typeof(ListBoxItem));
        var cp = new FrameworkElementFactory(typeof(ContentPresenter));
        cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Stretch);
        tmpl.VisualTree = cp;
        st.Setters.Add(new Setter(Control.TemplateProperty, tmpl));
        return st;
    }

    // ItemTemplate: a ContentControl whose Content is the item itself, run through RowConverter,
    // which calls the EXISTING builders by Row.Kind and returns the realized UIElement. Because
    // containers recycle, Convert runs for visible items only.
    DataTemplate BuildRowTemplate()
    {
        var dt = new DataTemplate();
        var cc = new FrameworkElementFactory(typeof(ContentControl));
        var b = new System.Windows.Data.Binding();
        b.Converter = new RowConverter(this);
        cc.SetBinding(ContentControl.ContentProperty, b);
        cc.SetValue(UIElement.FocusableProperty, false);
        dt.VisualTree = cc;
        return dt;
    }

    void RenderCards(Dictionary<string, object> root)
    {
        _lastRoot = root;               // cache for single-card toggles
        PaintEffortPolicyInEffect(root);   // what the runner reports is in effect (effort policy)
        PaintFanoutInEffect(root);         // what the coordinator was started with (fan-out)
        PaintFanoutBudgetInEffect(root);   // the per-tree limits the coordinator applies
        PaintFanoutDepthInEffect(root);    // the split depth the coordinator applies
        PaintHierarchicalMergeInEffect(root);   // the hierarchical-merge state the coordinator applies
        PaintMergeConversationInEffect(root);   // the merge-conversation state and the savings so far
        PaintWriteScopeInEffect(root);     // the sibling write-scope mode the coordinator applies
        PaintLoadRoutingInEffect(root);    // the load-aware routing mode the coordinator applies
        PaintAutoResumeInEffect(root);     // the auto-resume setting and the gate's last decision
        PaintSupervisorCodeNote();         // "supervisor is running older code" (supervisor_state.json)
        PaintToolProbeInEffect();          // why the tool-call check did or did not send a message
        // Preserve scroll position across the rebuild. Without this, every worker update
        // (status/turn change) reset the list and snapped the view back to the TOP -- which is
        // exactly why scrolling "didn't work" while tasks were live: the user scrolled down, a
        // tick fired, and they were yanked up again. Capture now, restore after the new content
        // is laid out (read the ListBox's internal ScrollViewer, which #ListScroller finds).
        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;

        SetRows(BuildRows(root));
        RefreshPinnedToolbar();     // rebuild the pinned filter bar (only when its signature changed)

        if (off > 0.0)
        {
            double target = off;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 != null) s2.ScrollToVerticalOffset(target);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }

        // Feature 1 keystroke-safety: mirrors the scroll-restore pass just above, same Loaded
        // priority (i.e. runs after this sweep's layout has realized whatever containers SetRows
        // just replaced). If the user was mid-type in some worker's steer box, its card may have
        // just been rebuilt (RowSig changed for reasons unrelated to steering -- a LIVE worker's
        // `last`/`turn` streams every sweep) into a BRAND NEW TextBox instance. The new instance
        // already has the right text (CollapsedSteerRow/SteerRow seed it from _steerDraft on every
        // build) -- what it does NOT have yet is focus, so re-apply it here and seat the caret at
        // the end. Runs unconditionally (not gated on scroll offset) since a steer rebuild can
        // happen with zero scroll.
        string focusWorker = _steerFocusWorker;
        if (!string.IsNullOrEmpty(focusWorker))
        {
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != focusWorker) return;   // focus moved on before this ran
                TextBox tb2;
                if (_steerBoxRef.TryGetValue(focusWorker, out tb2) && tb2 != null)
                {
                    try { tb2.Focus(); tb2.CaretIndex = tb2.Text != null ? tb2.Text.Length : 0; }
                    catch (Exception) { }
                }
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }
    }

    // Rebuild the PINNED filter bar into its fixed host above the list. Called after BuildRows (so
    // _toolbarAll/_toolbarShown are current). Uses the same signature the toolbar row used (case 0
    // in RowSig) so the heavy Border is only re-created when a count/filter/theme actually changes —
    // otherwise we leave the existing element in place (no flicker, no needless layout). The bar is
    // hidden in the empty state (no workers AND no history), matching the old behavior where the
    // toolbar row was simply not emitted.
    void RefreshPinnedToolbar()
    {
        if (_pinnedToolbarHost == null) return;
        bool empty = (_toolbarAll == null || _toolbarAll.Count == 0) && (_history == null || _history.Count == 0);
        if (empty)
        {
            _pinnedToolbarHost.Visibility = Visibility.Collapsed;
            _pinnedToolbarHost.Child = null;
            _pinnedToolbarSig = "";
            return;
        }
        // Signature identical to RowSig(case 0) so we rebuild on exactly the same triggers.
        int[] tc = ToolbarCounts(_toolbarAll);
        string g = (_dark ? "D" : "L") + _lang.ToString();
        string sig = "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                     + "|all" + tc[0] + ":act" + tc[1] + ":need" + tc[2] + ":done" + tc[3]
                     + ":max" + tc[5] + ":bad" + tc[6] + ":hid" + tc[7] + ":int" + tc[8]
                     + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
        _pinnedToolbarHost.Visibility = Visibility.Visible;
        if (sig == _pinnedToolbarSig && _pinnedToolbarHost.Child != null) return;   // unchanged -> keep as-is
        _pinnedToolbarSig = sig;
        _pinnedToolbarHost.Child = BuildCardToolbar(_toolbarAll, _toolbarShown);
    }

    // Build the lightweight row-model list for the virtualizing ListBox: a toolbar row, one card
    // row per filtered/sorted worker, then (if history non-empty) a history-header row + one
    // history row per entry. The heavy UIElements are NOT built here -- the ItemTemplate's
    // converter builds them lazily for visible rows only.
    List<object> BuildRows(Dictionary<string, object> root)
    {
        // gather workers in natural order
        var workers = new List<Dictionary<string, object>>();
        object wo;
        if (root.TryGetValue("workers", out wo) && wo is object[])
            foreach (object o in (object[])wo)
                workers.Add((Dictionary<string, object>)o);

        // Feature B: apply the view filter. Outcome is null/"" while running, so only terminal
        // workers ever have outcome=="DONE"; filter 1 hides DONE (keeps failures + running),
        // filter 2 shows only DONE.
        // Persistent hide: skip TERMINAL cards the user cleared during a live run (their key is
        // in _hiddenKeys). Non-terminal workers are never hidden, and a fresh run gets new keys
        // (started changes), so this only suppresses exactly the cards the user dismissed.
        string startedRoot = S(root, "started");
        var shown = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> w in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(w) && _hiddenKeys.Contains(WorkerKey(startedRoot, w)))
                continue;
            string oc = S(w, "outcome");
            string st = S(w, "status");
            // Tab 1 = Active: non-terminal AND not pending (actively working statuses)
            if (_cardFilter == 1 && (IsTerminalWorker(w) || IsInterruptedWorker(w) || st == "pending")) continue;
            // Tab 2 = Needs input: awaiting only
            if (_cardFilter == 2 && st != "awaiting") continue;
            // Tab 3 = Done: outcome == DONE
            if (_cardFilter == 3 && oc != "DONE") continue;
            // Tab 4 = Interrupted (coordinator died, resumable); the tab only exists while one is on the board
            if (_cardFilter == 4 && !IsInterruptedWorker(w)) continue;
            shown.Add(w);
        }
        // THE LIST'S ORDER, decided in one place (SubmittedTasks.Compose, executed by
        // ui/test_a_submitted_task_is_on_top_at_once.py): the "submitted, not picked up yet"
        // group first, then fresh pending workers (the newest tasks), active, older pending,
        // terminal. The submitted group is shown on the All and Active tabs -- a task that was
        // just sent is new work, not an approval request or a finished result.
        List<SubmittedView> subs = (_cardFilter == 0 || _cardFilter == 1)
            ? ReadQueuedJobs() : new List<SubmittedView>();
        List<SubmittedDisplayItem> order = SubmittedTasks.Compose(subs, shown, NowUnix(),
                                                                  IsTerminalForOrder);
        shown = new List<Dictionary<string, object>>();
        foreach (SubmittedDisplayItem it in order)
            if (it.Worker != null) shown.Add(it.Worker);

        // stash for the converter (it rebuilds the toolbar row from these when a container recycles)
        _toolbarAll = workers;
        _toolbarShown = shown;

        var rows = new List<object>();
        // Empty state (spec): no run yet and no history -> a calm centered suggestion block instead
        // of a blank workspace (the big top textarea is already gone -- it's the bottom composer now).
        // Anything submitted still goes ABOVE it.
        if (workers.Count == 0 && _history.Count == 0)
        {
            AppendSubmittedRows(rows, order);
            rows.Add(MkRow(5, null, null));
            return rows;
        }
        // The submitted group: the first rows of the list, above the directive band.
        AppendSubmittedRows(rows, order);
        // NOTE: the toolbar (すべて/実行中/承認待ち/完了 filter) is NO LONGER a scrolling row. It is
        // pinned in _pinnedToolbarHost above the list (see RefreshPinnedToolbar, called from
        // RenderCards). Row kind 0 is retained in the converter for safety but never emitted here.

        // TASK 3: Directive band — show only while there is at least one ON-BOARD worker (i.e. not
        // every card has been sent to History). Once the whole run is cleared to History the band
        // disappears (user request 2026-06-28). On-board = workers minus _hiddenKeys (History);
        // NOT filtered by the active tab, so the band is independent of which tab is selected.
        var onBoard = new List<Dictionary<string, object>>();
        foreach (Dictionary<string, object> ow in workers)
        {
            if (_hiddenKeys.Count > 0 && IsTerminalWorker(ow) && _hiddenKeys.Contains(WorkerKey(startedRoot, ow)))
                continue;   // in History -> not on the board
            onBoard.Add(ow);
        }
        // Compute _directiveBandMeta here so the Sig captures the live elapsed/lane counts.
        if (onBoard.Count > 0)
        {
            // Compute meta: "started HH:MM · {elapsed} · {active}/{total} lanes active" [COMPUTED]
            double dbStarted = Dbl(root, "started");
            double dbNow = NowUnix();
            bool dbRunning = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            double dbElapsed = dbRunning
                ? (dbStarted > 0 ? dbNow - dbStarted : 0)
                : (root.ContainsKey("elapsed_s") ? Dbl(root, "elapsed_s")
                   : (Dbl(root, "updated") > 0 && dbStarted > 0 ? Dbl(root, "updated") - dbStarted : 0));
            int dbActive = 0;
            foreach (Dictionary<string, object> dw in onBoard)
                if (!IsTerminalWorker(dw) && !IsInterruptedWorker(dw) && S(dw, "status") != "pending") dbActive++;
            bool dbJa = _lang == 0;
            var dbMeta = new StringBuilder();
            if (dbStarted > 0)
            {
                string startHM = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                    .AddSeconds(dbStarted).ToLocalTime().ToString("HH:mm");
                dbMeta.Append(dbJa ? "開始 " : "started ").Append(startHM);
                dbMeta.Append(" · ").Append(Fmt(dbElapsed));
            }
            if (dbElapsed > 0 || dbStarted == 0)
            {
                if (dbMeta.Length > 0) dbMeta.Append(" · ");
                if (!dbRunning && dbActive == 0)
                {
                    // Run is done: show completed count instead of active/total lane status
                    int dbDone = 0;
                    foreach (Dictionary<string, object> dw2 in onBoard)
                        if (S(dw2, "outcome") == "DONE") dbDone++;
                    dbMeta.Append(dbDone);
                    dbMeta.Append(dbJa ? "件完了" : " done");
                }
                else
                {
                    dbMeta.Append(dbActive).Append("/").Append(onBoard.Count);
                    dbMeta.Append(dbJa ? " lane 稼働中" : " lanes active");
                }
            }
            _directiveBandMeta = dbMeta.ToString();

            // DirectiveBand aggregates goals from the on-board set (so lanes moved to History also
            // drop out of the "Goals (N)" count), not the full _toolbarAll.
            _directiveBandWorkers = onBoard;
            rows.Add(MkRow(6, onBoard[0], null));      // directive band: first on-board worker's goal [REAL]
        }
        // Tab 0 (All): partition active/queued vs terminal with a divider.
        // Tabs 1-3 are explicit single-criterion views -- no re-partition needed.
        if (_cardFilter == 0)
        {
            var active = new List<Dictionary<string, object>>();
            var done = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> w in shown)
                (IsTerminalWorker(w) ? done : active).Add(w);
            foreach (Dictionary<string, object> w in active)
                rows.Add(MkRow(1, w, null));
            if (done.Count > 0)
            {
                rows.Add(MkRow(4, null, null));       // "完了 (this run)" divider
                foreach (Dictionary<string, object> w in done)
                    rows.Add(MkRow(1, w, null));
            }
        }
        else
        {
            foreach (Dictionary<string, object> w in shown)
                rows.Add(MkRow(1, w, null));          // one card per worker
        }
        // ③ Dedup: a terminal worker is archived into history the moment it finishes, but its card
        // also stays ON-BOARD (in `完了`) until the user moves it to History. During that window the
        // same started#name shows in BOTH places. Compute the on-board key set and skip history rows
        // that match it -- so a finished task appears once (on board) and only drops into History
        // once it actually leaves the board (すべて履歴へ / →履歴).
        var onBoardKeys = new System.Collections.Generic.HashSet<string>();
        foreach (Dictionary<string, object> bw in onBoard)
            onBoardKeys.Add(WorkerKey(startedRoot, bw));
        AppendHistoryRows(rows, onBoardKeys);
        return rows;
    }

    // Append a history-header row + one history row per entry (newest first) to the row model.
    void AppendHistoryRows(List<object> rows, System.Collections.Generic.HashSet<string> onBoardKeys)
    {
        if (_history.Count == 0) return;
        // ③ Build the visible (deduped) entry list first: skip any history entry whose key
        // (started#name) is still ON-BOARD, so the same finished task is not shown twice.
        var visible = new List<Dictionary<string, object>>();
        for (int i = _history.Count - 1; i >= 0; i--)
        {
            var e = _history[i] as Dictionary<string, object>;
            if (e == null) continue;
            string ek = S(e, "key");
            if (onBoardKeys != null && !string.IsNullOrEmpty(ek) && onBoardKeys.Contains(ek))
                continue;   // still on the board -> don't duplicate it in History
            visible.Add(e);
        }
        if (visible.Count == 0) return;               // everything is still on-board -> no History section yet
        rows.Add(MkRow(2, null, null));               // history header (carries the search box)

        // P2 (b) SEARCH: case-insensitive substring filter over title + result/goal text.
        string q = (_histQuery ?? "").Trim();
        List<Dictionary<string, object>> filtered = visible;
        if (q.Length > 0)
        {
            string ql = q.ToLowerInvariant();
            filtered = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> e in visible)
            {
                string hay = ((string.IsNullOrEmpty(S(e, "goal_summary"))
                    ? CardTitle(S(e, "conv_title"), S(e, "goal")) : S(e, "goal_summary")) + " "
                              + S(e, "goal") + " " + S(e, "display_result") + " "
                              + S(e, "last") + " " + S(e, "outcome")).ToLowerInvariant();
                if (hay.IndexOf(ql, StringComparison.Ordinal) >= 0) filtered.Add(e);
            }
        }
        if (filtered.Count == 0) return;              // header still shown so the search box stays reachable

        // P2 (a) DATE GROUPS: emit a subheader (今日 / 昨日 / YYYY-MM-DD / その他) whenever the day
        // bucket changes. `visible` is already newest-first, so groups come out most-recent-first.
        string lastGroup = null;
        foreach (Dictionary<string, object> e in filtered)
        {
            string grp = HistoryGroupLabel(e);
            if (grp != lastGroup)
            {
                var gd = new Dictionary<string, object>();
                gd["label"] = grp;
                rows.Add(MkRow(7, null, gd));         // date-group subheader
                lastGroup = grp;
            }
            rows.Add(MkRow(3, null, e));             // one history row per visible entry
        }
    }

    // The date-group bucket label for a history entry, derived from its `ts` (archived-at unix
    // seconds). Entries archived before `ts` existed (old history) have no timestamp -> その他 /
    // Earlier. Today / Yesterday are relative to local midnight; older days show YYYY-MM-DD.
    string HistoryGroupLabel(Dictionary<string, object> e)
    {
        double ts = Dbl(e, "ts");
        if (ts <= 0) return T("hist_earlier");
        DateTime day;
        try { day = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(ts).ToLocalTime().Date; }
        catch (Exception) { return T("hist_earlier"); }
        DateTime today = DateTime.Now.Date;
        if (day == today) return T("hist_today");
        if (day == today.AddDays(-1)) return T("hist_yesterday");
        return day.ToString("yyyy-MM-dd");
    }

    // The date-group subheader UIElement (Kind==7). Small muted caption, matching the History chrome.
    UIElement HistoryGroupHeader(string label)
    {
        return new TextBlock {
            Text = label, Foreground = Muted, FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(12, 12, 8, 2) };
    }

    // Build one row model and freeze its render signature (so SetRows can diff old-vs-new rows
    // with a plain string compare). Replaces the old `new Row(...)` call sites.
    Row MkRow(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        var r = new Row(kind, w, hist);
        r.Sig = RowSig(kind, w, hist);
        return r;
    }

    // Per-row render signature: captures every bit of state the row's builder reads, evaluated at
    // build time. Equal Sig => identical UIElement => SetRows can leave that container untouched.
    string RowSig(int kind, Dictionary<string, object> w, Dictionary<string, object> hist)
    {
        string g = (_dark ? "D" : "L") + _lang.ToString();   // theme/lang re-chrome every row
        switch (kind)
        {
            case 0:  // toolbar: reflects per-category counts (same helper the toolbar renders from,
                     // so a status-only reclassification like pending->done flips the signature)
                int[] tc0 = ToolbarCounts(_toolbarAll);
                return "T|" + g + "|" + _toolbarShown.Count + "/" + _toolbarAll.Count
                       + "|all" + tc0[0] + ":act" + tc0[1] + ":need" + tc0[2] + ":done" + tc0[3]
                       + ":max" + tc0[5] + ":bad" + tc0[6] + ":hid" + tc0[7] + ":int" + tc0[8]
                       + "|ar" + (_autoRetry ? 1 : 0) + ":" + _autoRetryMax + "|f" + _cardFilter;
            case 2: return "HH|" + g;                          // history header (static chrome; search box preserved across renders)
            case 7:                                            // date-group subheader: keyed on its label
                return "HG|" + g + "|" + (hist != null ? S(hist, "label") : "");
            case 8:                                            // resume affordance: keyed on N
                return "RA|" + g + "|" + (hist != null ? I(hist, "n") : 0)
                       + "|" + (hist != null ? S(hist, "signature") : "");
            case 9:                                            // submitted, not picked up yet: key + label + dismiss
            {
                var sv = hist != null && hist.ContainsKey("view") ? hist["view"] as SubmittedView : null;
                if (sv == null) return "SB|" + g;
                return "SB|" + g + "|" + StableShortHash(sv.Key) + "|" + SubmittedTasks.Label(sv, _lang == 0)
                       + "|" + (sv.Dismissable ? "x" : "-");
            }
            case 4: return "DV|" + g;                          // "完了 (this run)" divider
            case 5: return "ES|" + g + "|" + ReadQueuedJobs().Count;   // empty state: its headline counts the submitted group
            case 6:                                            // directive band: keyed on first-worker goal + started
                return "DB|" + g + "|" + (w != null ? S(w, "goal") + "|" + S(w, "name") : "")
                       + "|tc" + (_toolbarAll.Count) + "|" + _directiveBandMeta;
            case 3:                                            // history row: stable per entry, but expand state changes the render
            {
                string hk3 = hist != null ? S(hist, "key") : "";
                bool hOpen = !string.IsNullOrEmpty(hk3) && _expanded.Contains(hk3);
                return "h|" + g + "|" + (hist != null ? RuntimeHelpers.GetHashCode(hist) : 0)
                       + "|" + (hOpen ? "E" : "C");
            }
            default:                                           // kind 1: worker card
                string nm = S(w, "name");
                var sb = new StringBuilder("c|");
                sb.Append(g).Append('|').Append(nm)
                  .Append(S(w, "status")).Append(S(w, "turn")).Append(S(w, "outcome"))
                  .Append(S(w, "closed"));   // closed flips the left rail to neutral -> must re-render
                // The collapsed card now shows a result line + meta (turn/reviews/verified), so its
                // signature must track `last`/reviews/verified too -- otherwise the at-a-glance line
                // would freeze while the worker streams. Hash (not length) catches same-length content changes.
                string lastVal = S(w, "last");
                string drVal = S(w, "display_result");
                sb.Append(_expanded.Contains(nm) ? "#E" : "#C")
                  .Append(StableShortHash(lastVal)).Append(':').Append(StableShortHash(drVal))
                  .Append(':').Append(S(w, "verify_attempts")).Append(S(w, "verified"));
                // P0: conv_url drives the agent badge (agent-bound vs 既定Copilot); reason drives the
                // INFRA_STUCK row text — track both so those cards re-render when they change.
                sb.Append(':').Append(StableShortHash(S(w, "conv_url")))
                  .Append(':').Append(StableShortHash(S(w, "reason")))
                  // P1: the card headline prefers conv_title (Copilot auto-title) -> re-render when it arrives
                  .Append(':').Append(StableShortHash(S(w, "conv_title")));
                // TASK 3 (Bucket C): track next_step + self_confidence so the collapsed row re-renders.
                sb.Append('|').Append(S(w, "next_step").Length).Append(':').Append(S(w, "self_confidence"));
                // effort badge fields: re-render when the level/source/last switch change
                sb.Append("|ef:").Append(S(w, "effort_level")).Append(S(w, "effort_source"))
                  .Append(StableShortHash(S(Obj(w, "effort_last_switch") ?? new Dictionary<string, object>(), "reason")))
                  .Append(S(Obj(w, "effort_last_switch") ?? new Dictionary<string, object>(), "turn"));
                var ex = Obj(w, "execution");
                if (ex != null)
                    sb.Append("|exec:").Append(S(ex, "state"))
                      .Append(':').Append(S(ex, "current_step_index"))
                      .Append(':').Append(S(ex, "total_steps"))
                      .Append(':').Append(S(ex, "completed_count"))
                      .Append(':').Append(ArrCount(ex, "artifacts"))
                      .Append(':').Append(StableShortHash(S(ex, "current_step")))
                      .Append(':').Append(StableShortHash(S(ex, "last_progress")))
                      .Append(':').Append(S(ex, "last_progress_at"));
                // FIX B: _stopping dims non-terminal cards -- track it so a Stop click (or its
                // resolution) re-templates this card instead of reusing the old realized element.
                sb.Append('|').Append(_stopping ? "1" : "0");
                // Feature 1: fold whether THIS card's collapsed steer box currently has a non-empty
                // draft and/or keyboard focus into the signature. This does not, by itself, protect
                // the draft/focus (that's _steerDraft + _steerBoxRef + the RenderCards restore pass,
                // which work regardless of Sig) -- it exists so that starting/ending a steer draft
                // or focus on this specific card is itself a tracked, deterministic state transition
                // rather than invisible to the diff, matching how every other read field here is
                // captured. Booleans only (not the draft text itself) -- empty-to-non-empty and
                // unfocused-to-focused are each a single transition, so this does NOT force a
                // rebuild on every keystroke the way hashing the live draft text would.
                string draftSt;
                bool hasDraft = _steerDraft.TryGetValue(nm, out draftSt) && !string.IsNullOrEmpty(draftSt);
                bool hasFocus = _steerFocusWorker == nm;
                sb.Append('|').Append(hasDraft ? "d1" : "d0").Append(hasFocus ? "f1" : "f0");
                sb.Append("|grp:").Append(StableShortHash(GroupLineText(w) + GroupExtraSig(w) + "|" + S(w, "display_label")));
                return sb.ToString();
        }
    }

    // Reconcile the persistent _rows collection toward `rows` IN PLACE. Item-by-item Replace plus
    // tail Add/Remove -- never Clear() and never a fresh ItemsSource -- so the VirtualizingStackPanel
    // never sees a collection Reset and the pixel scroll offset is preserved. Only rows whose Sig
    // changed are swapped, so unchanged cards keep their realized containers (no flicker, and a
    // chevron toggle re-templates exactly the one card that changed).
    void SetRows(List<object> rows)
    {
        if (_list == null) return;
        int n = rows.Count, m = _rows.Count;
        int common = n < m ? n : m;
        for (int i = 0; i < common; i++)
        {
            var nw = rows[i] as Row;
            var od = _rows[i] as Row;
            if (od == null || nw == null || od.Sig != nw.Sig)
                _rows[i] = rows[i];        // targeted Replace -> only this container re-templates
        }
        if (n > m) { for (int i = m; i < n; i++) _rows.Add(rows[i]); }
        else if (m > n) { for (int i = m - 1; i >= n; i--) _rows.RemoveAt(i); }
    }

    // Walk the visual tree from _list to find its internal ScrollViewer (present once the
    // template is applied). Used to capture/restore scroll offset across a re-render. Returns
    // null before the first layout pass.
    ScrollViewer ListScroller()
    {
        if (_list == null) return null;
        return FindScrollViewer(_list);
    }
    static ScrollViewer FindScrollViewer(DependencyObject root)
    {
        if (root == null) return null;
        int n = VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            DependencyObject ch = VisualTreeHelper.GetChild(root, i);
            ScrollViewer sv = ch as ScrollViewer;
            if (sv != null) return sv;
            ScrollViewer deep = FindScrollViewer(ch);
            if (deep != null) return deep;
        }
        return null;
    }

    // Tiny per-row model. Kind: 0=toolbar, 1=card, 2=history-header, 3=history-row, 4=completed-divider,
    // 5=empty state, 6=directive band, 7=date-group header, 8=resume affordance,
    // 9=submitted-not-picked-up-yet (Hist["view"] is its SubmittedView).
    class Row
    {
        public int Kind;
        public Dictionary<string, object> Worker;
        public Dictionary<string, object> Hist;
        // Render signature, computed at build time (MkRow). Two rows with equal Sig produce an
        // identical UIElement, so SetRows skips re-templating them -- and a later compare of an
        // old row vs a freshly-built one is just a string compare (the state is frozen in here).
        public string Sig;
        public Row(int kind, Dictionary<string, object> worker, Dictionary<string, object> hist)
        { Kind = kind; Worker = worker; Hist = hist; }
    }

    // Converts a Row model into its realized UIElement by dispatching on Row.Kind to the EXISTING
    // builders. Holds a back-reference to the window (C# 5: no lambdas-as-closures over fields in
    // a converter, so pass `this`). Called for visible rows only because containers recycle.
    class RowConverter : IValueConverter
    {
        readonly CockpitWindow _w;
        public RowConverter(CockpitWindow w) { _w = w; }
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            var r = value as Row;
            if (r == null) return null;
            if (r.Kind == 0) return _w.BuildCardToolbar(_w._toolbarAll, _w._toolbarShown);
            if (r.Kind == 1) return _w.Card(r.Worker);
            if (r.Kind == 2) return _w.HistoryHeader();
            if (r.Kind == 3) return _w.HistoryRow(r.Hist);
            if (r.Kind == 4) return _w.CompletedDivider();
            if (r.Kind == 5) return _w.EmptyState();
            if (r.Kind == 6) return _w.DirectiveBand(r.Worker);
            if (r.Kind == 7) return _w.HistoryGroupHeader(r.Hist != null ? S(r.Hist, "label") : "");
            if (r.Kind == 9) return _w.SubmittedRow(
                r.Hist != null && r.Hist.ContainsKey("view") ? r.Hist["view"] as SubmittedView : null);
            if (r.Kind == 8) return _w.ResumeAffordance(
                r.Hist != null ? I(r.Hist, "n") : 0,
                r.Hist != null ? S(r.Hist, "signature") : "");
            return null;
        }
        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        { return Binding.DoNothing; }
    }

    // Stable sort by severity rank (C# 5: List.Sort is NOT stable, so do a manual stable sort
    // by bucketing in rank order while preserving original index order within each rank).
    static List<Dictionary<string, object>> StableBySeverity(List<Dictionary<string, object>> src)
    {
        var outp = new List<Dictionary<string, object>>();
        for (int rank = 0; rank <= 3; rank++)
            foreach (Dictionary<string, object> w in src)
                if (SeverityRank(w) == rank) outp.Add(w);
        return outp;
    }

    // Default-view order: what is HAPPENING now floats to the top, finished work sinks to the
    // bottom. Stable within each bucket so a worker keeps its place (and its W-name identity)
    // and cards do not jump around as unrelated workers tick. status.json lists workers in launch
    // order, so without this the earliest-launched (now-completed) workers stay pinned at the top.
    //
    // The order itself now lives in SubmittedTasks.Compose, where a test executes it: submitted
    // (not picked up yet) -> fresh pending worker (the newest task; FRESH_PENDING_S says what
    // "fresh" is) -> active -> older pending -> terminal. A pending worker used to sort below
    // every active card however new it was, so the task just submitted was never the one on top.
    // This is the terminal test that order uses; "freed" sinks with the finished ones, as before.
    static bool IsTerminalForOrder(Dictionary<string, object> w)
    {
        return IsTerminalWorker(w) || S(w, "status") == "freed";
    }

    void AppendSubmittedRows(List<object> rows)
    {
        AppendSubmittedRows(rows, SubmittedTasks.Compose(ReadQueuedJobs(), null, NowUnix(),
                                                         IsTerminalForOrder));
    }

    // One row per submitted entry, in the order Compose gave them.
    void AppendSubmittedRows(List<object> rows, List<SubmittedDisplayItem> order)
    {
        if (order == null) return;
        foreach (SubmittedDisplayItem it in order)
        {
            if (it.Submitted == null) continue;
            var d = new Dictionary<string, object>();
            d["view"] = it.Submitted;
            rows.Add(MkRow(9, null, d));
        }
    }

    // A "submitted, not picked up yet" row: the label (state + age + where it is), then the goal
    // on one line, trimmed like a card title. Drawn as a quiet bordered row, not a card: there is
    // no worker behind it yet, so there is nothing to expand, steer or retry. The only action is
    // the person's own dismiss, offered once SubmittedTasks says the entry may be dismissed (no
    // file behind it, past the unconfirmed threshold) -- never on a fresh entry that the fleet
    // may still pick up, and never on a timer.
    UIElement SubmittedRow(SubmittedView v)
    {
        bool ja = _lang == 0;
        var row = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(8, 8, 8, 4) };
        var dp = new DockPanel { LastChildFill = true };
        if (v != null && v.Dismissable)
        {
            var close = IconButton("close", 14, ja ? "この投入済みタスクの表示を消す" : "Dismiss this submitted task");
            close.Width = 28; close.Height = 28; close.Margin = new Thickness(8, 0, 0, 0);
            close.Padding = new Thickness(0); close.BorderThickness = new Thickness(0);
            close.Background = Brushes.Transparent; close.Foreground = Muted;
            close.Template = FlatButtonTemplate();
            close.ToolTip = ja ? "消す（フリートの作業は取り消しません）"
                               : "Dismiss (does not cancel anything in the fleet)";
            string key = v.Key;
            close.Click += delegate (object sender, RoutedEventArgs e)
            {
                e.Handled = true;
                if (_submitted.Dismiss(key, NowUnix())) ForceRender();
            };
            DockPanel.SetDock(close, Dock.Right); dp.Children.Add(close);
        }
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock {
            Text = SubmittedTasks.Label(v, ja),
            // Full-strength text once an unconfirmed entry is stale: that is the one worth a look.
            Foreground = (v != null && v.Unconfirmed && v.Stale) ? Fg : Muted,
            FontSize = 11.5, FontWeight = FontWeights.SemiBold,
            TextTrimming = TextTrimming.CharacterEllipsis });
        stack.Children.Add(new TextBlock {
            Text = OneLine(v != null ? v.Goal : "", 140),
            Foreground = Fg, FontSize = 13, Margin = new Thickness(0, 2, 0, 0),
            TextTrimming = TextTrimming.CharacterEllipsis,
            ToolTip = v != null ? v.Goal : "" });
        dp.Children.Add(stack);
        row.Child = dp;
        return row;
    }

    // Feature B/C toolbar: filter selector + outcome summary + bulk-retry. Rebuilt each
    // RenderCards as the first row of _cards (no worker Tag, so ToggleExpand skips it).
    // Empty state (spec): a centered narrow block with quiet suggestion chips, shown when there is
    // no run and no history. Suggestions just pre-fill the bottom composer -- they don't launch.
    UIElement EmptyState()
    {
        // A SUBMITTED JOB IS NOT "NO TASKS". status.json describes workers, and a job that has
        // been queued has none until a coordinator starts -- so this said "no fleet tasks"
        // while the queue held work, and an operator could not tell a submission that landed
        // from one that went nowhere. The list is the merged "submitted" group, the same one
        // the rows above this block are drawn from.
        var queued = ReadQueuedJobs();
        // Closer to the rows when there are submitted rows above it to read with.
        var outer = new Border { Margin = new Thickness(0, queued.Count > 0 ? 24 : 80, 0, 0) };
        var block = new StackPanel { MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Center };

        if (queued.Count > 0)
        {
            block.Children.Add(new TextBlock {
                Text = _lang == 0
                    ? string.Format("投入済み {0} 件 — まだどのワーカーも受け取っていません", queued.Count)
                    : string.Format("{0} submitted — no worker has picked them up yet", queued.Count),
                Foreground = Fg, FontSize = 15, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) });
            block.Children.Add(new TextBlock {
                Text = _lang == 0
                    ? "キューには入っています。コーディネータが起動すると進捗がここに出ます。"
                    : "They are on the queue. Progress appears here once a coordinator starts.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 0, 0, 12) });
            // The entries themselves are NOT listed again here: they are the rows directly
            // above this block (AppendSubmittedRows), drawn from this same list, so the count
            // in the headline and the rows on screen cannot disagree.
        }
        else
        {
            block.Children.Add(new TextBlock {
                Text = _lang == 0 ? "タスクはまだありません" : "No fleet tasks",
                Foreground = Fg, FontSize = 15, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) });
            block.Children.Add(new TextBlock {
                Text = _lang == 0 ? "複数のタスクを並行で走らせ、ここで進捗を確認します。"
                                  : "Run several tasks in parallel, then monitor progress here.",
                Foreground = Muted, FontSize = 12.5, TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 0, 0, 16) });
        }

        string[] suggestions = _lang == 0
            ? new string[] { "失敗テストを修正", "UIの問題をレビュー", "READMEを更新", "フォルダのタスクを実行" }
            : new string[] { "Fix failing tests", "Review UI issues", "Update README", "Run folder task" };
        var wrap = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Center };
        foreach (string s in suggestions)
        {
            string text = s;
            var chip = new Button {
                Content = text, Cursor = Cursors.Hand, FontSize = 12,
                Background = Brushes.Transparent, Foreground = Fg,
                BorderBrush = Border, BorderThickness = new Thickness(1),
                Padding = new Thickness(12, 5, 12, 5), Margin = new Thickness(4, 4, 4, 4)
            };
            chip.Click += delegate
            {
                if (_goalInput != null)
                {
                    _goalInput.Text = text;
                    _goalInput.CaretIndex = text.Length;
                    _goalInput.Focus();
                }
            };
            wrap.Children.Add(chip);
        }
        block.Children.Add(wrap);
        outer.Child = block;
        return outer;
    }

    // P2 RESUME: inline banner shown (idle only, N>0) offering to relaunch the fleet with --resume,
    // which re-queues the unfinished goals from the last run's durable ledger. Hidden while a run is
    // live or when N==0 (the row is simply not emitted in those cases).
    UIElement ResumeAffordance(int n, string signature)
    {
        var row = new Border {
            BorderThickness = new Thickness(1), BorderBrush = Border, Background = BtnBg,
            CornerRadius = new CornerRadius(Theme.RadCard),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(8, 8, 8, 4) };
        var dp = new DockPanel { LastChildFill = true };

        // This banner is advisory, not a blocking error. Let the user dismiss this exact stale
        // resume state permanently; a new interrupted run has a different signature and returns.
        var close = IconButton("close", 14, _lang == 0 ? "未完了ランの通知を閉じる" : "Dismiss unfinished-run notice");
        close.Width = 28; close.Height = 28; close.Margin = new Thickness(8, 0, 0, 0);
        close.Padding = new Thickness(0); close.BorderThickness = new Thickness(0);
        close.Background = Brushes.Transparent; close.Foreground = Muted;
        close.Template = FlatButtonTemplate();
        close.ToolTip = _lang == 0 ? "この未完了ランを非表示" : "Hide this unfinished run";
        string capturedSignature = signature;
        close.Click += delegate (object sender, RoutedEventArgs e)
        { e.Handled = true; DismissResumeState(capturedSignature); };
        DockPanel.SetDock(close, Dock.Right); dp.Children.Add(close);

        var btn = new Button {
            Content = _lang == 0 ? "再開" : "Resume", Cursor = Cursors.Hand, FontSize = 12,
            FontWeight = FontWeights.SemiBold, Padding = new Thickness(12, 4, 12, 4),
            BorderThickness = new Thickness(1) };
        btn.Background = BtnBg; btn.Foreground = Fg; btn.BorderBrush = Border;
        btn.Template = FlatButtonTemplate();
        btn.Click += delegate
        {
            // Acknowledge at once and keep Process.Start (blocking on a loaded machine) off the
            // UI thread; the result replaces the label/note when it is known.
            if (!btn.IsEnabled) return;
            string idle = btn.Content as string;
            btn.IsEnabled = false;
            btn.Content = _lang == 0 ? "再開中…" : "Resuming…";
            if (_startNote != null) _startNote.Text = _lang == 0 ? "前回のランを再開しています…" : "Resuming the last run…";
            System.Threading.ThreadPool.QueueUserWorkItem(delegate
            {
                string err = null;
                try { SpawnFleetResume(); }
                catch (Exception ex) { err = ex.Message; }
                Dispatcher.BeginInvoke(new Action(delegate
                {
                    btn.Content = idle; btn.IsEnabled = true;
                    if (err == null) _lastSig = "";
                    if (_startNote != null)
                        _startNote.Text = err == null
                            ? (_lang == 0 ? "前回のランを再開しました。" : "Resumed the last run.")
                            : (_lang == 0 ? "再開に失敗: " : "Resume failed: ") + err;
                }));
            });
        };
        DockPanel.SetDock(btn, Dock.Right);
        dp.Children.Add(btn);

        var msg = new TextBlock {
            Text = _lang == 0 ? ("前回のランに未完了が " + n + " 件")
                              : ("Last run left " + n + " unfinished"),
            Foreground = Fg, FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis };
        dp.Children.Add(msg);

        row.Child = dp;
        return row;
    }
}
