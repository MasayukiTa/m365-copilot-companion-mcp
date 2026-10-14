// FleetCockpit.Spine.cs -- the left spine panel: focus worker, timeline, spine content.
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

    // A2-2: Refresh the Evidence Spine panel on each tick. Keyed off a signature so the
    // heavy DOM rebuild (and col-width change) only happens when the run state changes.
    // Called from OnTick after RenderCards (so _toolbarAll is already up to date).
    void RefreshSpine(Dictionary<string, object> root, bool idle)
    {
        if (_spinePanel == null || _spineCol == null) return;

        // NOT-RUNNING: the left "実行タイムライン" follows an EXPANDED past/history task, so inspecting
        // a finished task shows ITS timeline on the left (works after a run ends too, not only when
        // fully idle). A RUNNING fleet always owns the spine (the live run is never hidden), so this
        // past-task focus is suppressed while running. (user choice: 走行中は現在優先)
        bool spineRunning = root != null && (!root.ContainsKey("running") || Convert.ToBoolean(root["running"]));
        if (!spineRunning && root != null && _history != null && _history.Count > 0)
        {
            // WHICH TASK THE TIMELINE IS ABOUT. Expanded wins; with nothing expanded it follows
            // the row at the TOP of the list, which is the one the eye is on. Before this, an
            // un-expanded list left the panel on the live-run spine, so every finished task
            // appeared to share the newest run's clock -- the operator saw one timeline and
            // reasonably read it as belonging to whatever they were looking at.
            Dictionary<string, object> focusEntry = null;
            if (_expanded != null && _expanded.Count > 0)
            {
                for (int hi = _history.Count - 1; hi >= 0; hi--)
                {
                    var he = _history[hi] as Dictionary<string, object>;
                    if (he == null) continue;
                    string hk = S(he, "key");
                    if (!string.IsNullOrEmpty(hk) && _expanded.Contains(hk)) { focusEntry = he; break; }
                }
            }
            if (focusEntry == null)
            {
                // The newest entry is the one rendered first. Same ordering the list uses, so
                // the panel and the top row cannot disagree about which task they mean.
                for (int hi = _history.Count - 1; hi >= 0; hi--)
                {
                    var he = _history[hi] as Dictionary<string, object>;
                    if (he != null) { focusEntry = he; break; }
                }
            }
            if (focusEntry != null)
            {
                string fkey = S(focusEntry, "key");
                string fsig = "PAST|" + fkey + "|" + (_dark ? "D" : "L") + _lang;
                if (fsig == _spineSig) return;
                _spineSig = fsig;
                _spineCol.Width = new GridLength(220);
                _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
                _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
                var pastList = new List<Dictionary<string, object>>();
                pastList.Add(focusEntry);
                bool jaP = _lang == 0;
                var wrap = new StackPanel();
                wrap.Margin = new Thickness(12, 12, 8, 0);
                var pastTag = new TextBlock();
                pastTag.Text = jaP ? "過去のタスク" : "Past task";
                // NOT Accent. Theme.cs reserves that colour for a primary action -- "primary
                // action ONLY" is written beside its definition -- and this is a static eyebrow
                // above a read-only summary. Painting a label in the click-me colour spends the
                // one colour the eye is trained to look for on something that cannot be clicked,
                // and it is the only warm thing on that panel, so it takes the attention first.
                // The emphasis it needs is weight, which it already has.
                pastTag.Foreground = Theme.Br(Theme.Muted(_dark));
                pastTag.FontSize = 10; pastTag.FontWeight = FontWeights.SemiBold;
                pastTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(pastTag);
                var titleTag = new TextBlock();
                string focusSummary = S(focusEntry, "goal_summary");
                titleTag.Text = !string.IsNullOrEmpty(focusSummary)
                    ? focusSummary : CardTitle(S(focusEntry, "conv_title"), S(focusEntry, "goal"));
                titleTag.Foreground = Theme.Br(Theme.Muted(_dark)); titleTag.FontSize = 10;
                titleTag.TextTrimming = TextTrimming.CharacterEllipsis;
                titleTag.Margin = new Thickness(0, 0, 0, 2);
                wrap.Children.Add(titleTag);
                wrap.Children.Add(BuildSpineContent(root, "ended", true, pastList));
                // A past task is finished, so it opens at the top: nothing new will arrive.
                _spinePanel.Child = SpineScroll(wrap, false);
                return;
            }
        }

        // Ensure _toolbarAll reflects current root (it may be empty on first tick before RenderCards).
        // If _toolbarAll is empty but root has workers, use root's workers directly for the check.
        bool hasWorkers = false;
        if (!idle)
        {
            if (_toolbarAll != null && _toolbarAll.Count > 0)
                hasWorkers = true;
            else if (root != null)
            {
                object wo2x;
                if (root.TryGetValue("workers", out wo2x) && wo2x is object[])
                    hasWorkers = ((object[])wo2x).Length > 0;
            }
        }

        // Build a lightweight signature: started + overall phase + worker count + theme/lang.
        string overallPhase = "idle";
        bool runEnded = false;
        // Collect workers from _toolbarAll or directly from root as fallback.
        var spineWorkers = new List<Dictionary<string, object>>();
        if (_toolbarAll != null && _toolbarAll.Count > 0)
            spineWorkers = _toolbarAll;
        else if (root != null)
        {
            object wox2;
            if (root.TryGetValue("workers", out wox2) && wox2 is object[])
                foreach (object ow2 in (object[])wox2)
                {
                    var ww2 = ow2 as Dictionary<string, object>;
                    if (ww2 != null) spineWorkers.Add(ww2);
                }
        }
        if (root != null && hasWorkers)
        {
            bool running = !root.ContainsKey("running") || Convert.ToBoolean(root["running"]);
            runEnded = !running;
            bool anyAttn = false;
            bool allDone = true;
            foreach (Dictionary<string, object> tw in spineWorkers)
            {
                string st = S(tw, "status");
                if (!IsTerminalWorker(tw)) { allDone = false; }
                if (st == "stuck" || st == "maxturns" || st == "error") anyAttn = true;
                if (st == "verifying") overallPhase = "verifying";
                else if ((st == "researching" || st == "refuting" || st == "waiting" || st == "ready")
                         && overallPhase == "idle") overallPhase = "running";
            }
            if (anyAttn) overallPhase = "attn";
            if (runEnded || allDone) overallPhase = "ended";
        }
        string started = root != null ? S(root, "started") : "";
        // Re-key on the selected task's OPERATOR-FACING CONTENT. The old spine was a timeline,
        // so phase-event count was enough. Content details must repaint when progress/current/next/
        // waiting/artifacts change even if status and phase-event count do not.
        string primaryDetailSig = "";
        if (spineWorkers != null && spineWorkers.Count > 0)
        {
            Dictionary<string, object> primaryW = SpineFocusWorker(spineWorkers);
            if (primaryW != null) primaryDetailSig = SpineDetailSignature(primaryW);
        }
        string spineSig = (hasWorkers ? "1" : "0") + "|" + started + "|" + overallPhase
                          + "|" + (_toolbarAll != null ? _toolbarAll.Count : 0)
                          + "|" + (_dark ? "D" : "L") + _lang
                          + "|" + primaryDetailSig;
        if (spineSig == _spineSig) return;
        _spineSig = spineSig;

        if (!hasWorkers)
        {
            _spineCol.Width = new GridLength(0);
            _spinePanel.Child = null;
            return;
        }

        _spineCol.Width = new GridLength(220);
        _spinePanel.BorderBrush = Theme.Br(Theme.Border(_dark));
        _spinePanel.Background = Theme.Br(Theme.Bg(_dark));
        _spinePanel.Child = SpineScroll(
            BuildSpineContent(root, overallPhase, runEnded, spineWorkers), !runEnded);
    }

    // THE TIMELINE FOLLOWED workers[0] AND NOTHING ELSE. Open a running worker while the first
    // one has finished and the left panel keeps showing the finished one's phases -- reported
    // as "the timeline for the thing I am looking at is not shown", and that is exactly what it
    // did: five goals, w0 done at 17:50, and expanding w1 (still running) left the spine ending
    // at 完了 17:50.
    //
    // Expanding a card is how the operator says which one they are inspecting; the not-running
    // branch already honours it for past tasks. This makes the live branch honour it too, and
    // falls back to the first worker when nothing is expanded.
    Dictionary<string, object> SpineFocusWorker(List<Dictionary<string, object>> workers)
    {
        if (workers == null || workers.Count == 0) return null;
        if (_expanded != null && _expanded.Count > 0)
            foreach (var w in workers)
            {
                if (w == null) continue;
                string nm = S(w, "name");
                if (!string.IsNullOrEmpty(nm) && _expanded.Contains(nm)) return w;
            }
        return workers[0];
    }

    string SpineDetailSignature(Dictionary<string, object> w)
    {
        if (w == null) return "";
        var parts = new List<string>();
        parts.Add(S(w, "name"));
        parts.Add(S(w, "status"));
        parts.Add(I(w, "turn").ToString());
        parts.Add(I(w, "max_turns").ToString());
        parts.Add(S(w, "goal_summary"));
        parts.Add(S(w, "reason"));
        parts.Add(S(w, "outcome"));
        parts.Add(I(w, "verify_attempts").ToString());
        object phaseRaw;
        if (w.TryGetValue("phase_events", out phaseRaw) && phaseRaw is object[])
        {
            object[] phaseArr = (object[])phaseRaw;
            parts.Add(phaseArr.Length.ToString());
            if (phaseArr.Length > 0)
            {
                var lastPhase = phaseArr[phaseArr.Length - 1] as Dictionary<string, object>;
                if (lastPhase != null)
                {
                    parts.Add(S(lastPhase, "event"));
                    parts.Add(S(lastPhase, "label"));
                    parts.Add(S(lastPhase, "ts"));
                }
            }
        }
        var execution = Obj(w, "execution");
        if (execution != null)
        {
            parts.Add(S(execution, "state"));
            parts.Add(S(execution, "current_step"));
            parts.Add(S(execution, "last_progress"));
            parts.Add(S(execution, "next_step"));
            parts.Add(S(execution, "waiting_reason"));
            parts.Add(I(execution, "completed_count").ToString());
            parts.Add(I(execution, "total_steps").ToString());
            object artsRaw;
            if (execution.TryGetValue("artifacts", out artsRaw) && artsRaw is object[])
            {
                foreach (object obj in (object[])artsRaw)
                {
                    var artifact = obj as Dictionary<string, object>;
                    if (artifact == null) continue;
                    parts.Add(S(artifact, "path"));
                    parts.Add(S(artifact, "name"));
                    parts.Add(S(artifact, "uri"));
                }
            }
        }
        return string.Join("|", parts.ToArray());
    }

    // Left task-inspection spine. Content details answer "what is it doing now?"; the timeline
    // directly below answers "how did it get here?". Both follow the same selected worker, and
    // the expanded card keeps the richer evidence view rather than being the only timeline.
    // GIVE THE SPINE A VIEWPORT. The column is a fixed 220px lane whose content has NO upper
    // bound: the timeline is one entry per phase transition, and the Border it sat in simply
    // CLIPPED everything past the fold. The entries were rendered and unreachable, with no
    // scrollbar to say so -- the failure looked like "the timeline stops at 12:17" rather than
    // like a missing viewport.
    //
    // WHY IT SURVIVED THIS LONG: benchmark runs are short and uniform, so the timeline never
    // outgrew the lane. General use does, and it is general use that this is for. A fixed-size
    // container sized against the shortest input is a bug waiting for a real workload.
    //
    // LIVE RUNS RIDE THE BOTTOM. While a run is going the newest entry is the one worth seeing,
    // and this panel is rebuilt whenever the phase signature changes -- so a top-anchored
    // rebuild would hide the very event that triggered it. Keyed off ExtentHeightChange, which
    // is non-zero only when CONTENT grows, so it never fights a user who has scrolled up.
    // A finished run opens at the top, where its story starts.
    ScrollViewer SpineScroll(UIElement content, bool follow)
    {
        var sv = new ScrollViewer();
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        sv.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        sv.Focusable = false;
        sv.IsTabStop = false;
        sv.Content = content;
        if (follow)
            sv.ScrollChanged += delegate(object s, ScrollChangedEventArgs e)
            {
                if (e.ExtentHeightChange != 0) sv.ScrollToEnd();
            };
        return sv;
    }

    void AddSpineTimeline(StackPanel outer, Dictionary<string, object> w)
    {
        if (outer == null || w == null) return;
        bool ja = _lang == 0;

        var divider = new Border();
        divider.Height = 1;
        divider.Background = Theme.Br(Theme.Border(_dark));
        divider.Margin = new Thickness(0, 12, 0, 8);
        outer.Children.Add(divider);

        var title = new TextBlock();
        title.Text = ja ? "実行タイムライン" : "Execution timeline";
        title.Foreground = Theme.Br(Theme.Muted(_dark));
        title.FontSize = 10.5;
        title.FontWeight = FontWeights.SemiBold;
        title.Margin = new Thickness(0, 0, 0, 2);
        outer.Children.Add(title);

        bool real = false;
        object peRaw;
        if (w.TryGetValue("phase_events", out peRaw) && peRaw is object[])
            real = ((object[])peRaw).Length > 0;
        outer.Children.Add(new TextBlock {
            Text = real ? (ja ? "(フェーズ遷移)" : "(phase transitions)")
                        : (ja ? "(ターン記録から推定)" : "(estimated from turns)"),
            Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 9.5,
            Margin = new Thickness(0, 0, 0, 7) });

        // The phase name alone is not enough operationally. Surface what this selected worker
        // is doing NOW, but only from fields the runner actually published -- no invented step.
        string timelineNow = "";
        var timelineExec = Obj(w, "execution");
        if (timelineExec != null)
        {
            string current = S(timelineExec, "current_step");
            string progress = S(timelineExec, "last_progress");
            if (!string.IsNullOrEmpty(current)) timelineNow = current;
            if (!string.IsNullOrEmpty(progress) && progress != current)
                timelineNow = string.IsNullOrEmpty(timelineNow) ? progress : (timelineNow + " · " + progress);
        }
        if (string.IsNullOrEmpty(timelineNow)) timelineNow = S(w, "reason");
        timelineNow = (timelineNow ?? "").Replace("\r", " ").Replace("\n", " ").Trim();
        if (timelineNow.Length > 180) timelineNow = timelineNow.Substring(0, 179).TrimEnd() + "…";
        if (!string.IsNullOrEmpty(timelineNow))
            outer.Children.Add(new TextBlock {
                Text = (ja ? "現在: " : "Now: ") + timelineNow,
                Foreground = Muted, FontSize = 10.5, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 8) });

        string status = S(w, "status");
        string outcome = S(w, "outcome");
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                     || status == "error" || status == "cancelled";
        int reviews = I(w, "verify_attempts");
        var events = BuildTimelineEvents(S(w, "transcript"), outcome, terminal, reviews, w);
        for (int i = 0; i < events.Count; i++)
        {
            bool last = i == events.Count - 1;
            string label = events[i].Item1;
            string color = events[i].Item2;

            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var rail = new DockPanel();
            rail.HorizontalAlignment = HorizontalAlignment.Center;
            var head = new Border { Width = 1.5, Height = 7,
                Background = i > 0 ? Theme.Br(Theme.Border(_dark)) : Brushes.Transparent,
                HorizontalAlignment = HorizontalAlignment.Center };
            DockPanel.SetDock(head, Dock.Top);
            rail.Children.Add(head);
            var dot = new System.Windows.Shapes.Ellipse { Width = 8, Height = 8,
                Fill = Theme.Br(color), HorizontalAlignment = HorizontalAlignment.Center };
            DockPanel.SetDock(dot, Dock.Top);
            rail.Children.Add(dot);
            rail.Children.Add(new Border { Width = 1.5,
                Background = last ? Brushes.Transparent : Theme.Br(Theme.Border(_dark)),
                HorizontalAlignment = HorizontalAlignment.Center });
            Grid.SetColumn(rail, 0);
            row.Children.Add(rail);

            var tb = new TextBlock { Text = label, Foreground = Theme.Br(color), FontSize = 11.0,
                FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(4, i == 0 ? 1 : 0, 0, 5) };
            Grid.SetColumn(tb, 1);
            row.Children.Add(tb);
            outer.Children.Add(row);
        }
    }

    // Derives events honestly from available data: run started ts, transcript first-turn ts,
    // current overall phase (polled), run ended state. No fabricated phase_events.
    UIElement BuildSpineContent(Dictionary<string, object> root, string overallPhase, bool runEnded,
                                List<Dictionary<string, object>> workers)
    {
        bool ja = _lang == 0;
        var outer = new StackPanel();
        outer.Margin = new Thickness(12, 12, 8, 12);

        var sectionLbl = new TextBlock();
        sectionLbl.Text = ja ? "内容詳細" : "Content details";
        sectionLbl.Foreground = Theme.Br(Theme.Muted(_dark));
        sectionLbl.FontSize = 10.5;
        sectionLbl.FontWeight = FontWeights.SemiBold;
        sectionLbl.Margin = new Thickness(0, 0, 0, 7);
        outer.Children.Add(sectionLbl);

        Dictionary<string, object> primaryWorker = SpineFocusWorker(workers);
        if (primaryWorker == null)
        {
            outer.Children.Add(new TextBlock {
                Text = ja ? "表示できるタスク情報がありません" : "No task details available",
                Foreground = Theme.Br(Theme.Muted(_dark)), FontSize = 12.0,
                TextWrapping = TextWrapping.Wrap });
            return outer;
        }

        string fullGoal = S(primaryWorker, "goal");
        string goalSummary = S(primaryWorker, "goal_summary");
        if (string.IsNullOrEmpty(goalSummary))
            goalSummary = CardTitle(S(primaryWorker, "conv_title"), fullGoal);
        if (!string.IsNullOrEmpty(goalSummary))
            outer.Children.Add(new TextBlock {
                Text = goalSummary, Foreground = Fg, FontSize = 13.0,
                FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 7) });

        string status = S(primaryWorker, "status");
        int turn = I(primaryWorker, "turn");
        int maxTurns = I(primaryWorker, "max_turns");
        string statusText = StatusLabel(status);
        if (string.IsNullOrEmpty(statusText)) statusText = overallPhase;
        if (turn > 0)
            statusText += maxTurns > 0 ? ("  ·  Turn " + turn + "/" + maxTurns) : ("  ·  Turn " + turn);
        if (!string.IsNullOrEmpty(statusText))
            outer.Children.Add(new TextBlock {
                Text = statusText, Foreground = Theme.Br(Theme.Secondary(_dark)), FontSize = 11.5,
                TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 7) });

        var execution = Obj(primaryWorker, "execution");
        string waiting = "";
        if (execution != null)
        {
            string state = S(execution, "state");
            string current = S(execution, "current_step");
            string progress = S(execution, "last_progress");
            string next = S(execution, "next_step");
            waiting = S(execution, "waiting_reason");

            if (!string.IsNullOrEmpty(current))
            {
                outer.Children.Add(new TextBlock { Text = ja ? "現在" : "Current",
                    Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                    FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 2, 0, 1) });
                string currentText = current;
                if (!string.IsNullOrEmpty(state) && state != current) currentText = state + "  ·  " + current;
                outer.Children.Add(new TextBlock { Text = currentText, Foreground = Fg, FontSize = 12.0,
                    TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 5) });
            }
            else if (!string.IsNullOrEmpty(state))
            {
                outer.Children.Add(new TextBlock { Text = (ja ? "状態: " : "State: ") + state,
                    Foreground = Fg, FontSize = 12.0, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 2, 0, 5) });
            }

            if (!string.IsNullOrEmpty(progress))
                outer.Children.Add(new TextBlock { Text = (ja ? "進捗: " : "Progress: ") + progress,
                    Foreground = Muted, FontSize = 11.5, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 1, 0, 4) });
            if (!string.IsNullOrEmpty(next))
                outer.Children.Add(new TextBlock { Text = (ja ? "次: " : "Next: ") + next,
                    Foreground = Muted, FontSize = 11.5, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 1, 0, 4) });

            object artsRaw;
            if (execution.TryGetValue("artifacts", out artsRaw) && artsRaw is object[])
            {
                object[] arts = (object[])artsRaw;
                if (arts.Length > 0)
                {
                    outer.Children.Add(new TextBlock { Text = ja ? "成果物" : "Artifacts",
                        Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                        FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 5, 0, 1) });
                    int shown = 0;
                    foreach (object obj in arts)
                    {
                        var artifact = obj as Dictionary<string, object>;
                        if (artifact == null) continue;
                        string label = S(artifact, "path");
                        if (string.IsNullOrEmpty(label)) label = S(artifact, "name");
                        if (string.IsNullOrEmpty(label)) label = S(artifact, "uri");
                        if (string.IsNullOrEmpty(label)) continue;
                        outer.Children.Add(new TextBlock { Text = "• " + label,
                            Foreground = Muted, FontSize = 11.0, TextWrapping = TextWrapping.Wrap,
                            Margin = new Thickness(5, 1, 0, 1) });
                        shown++;
                        if (shown >= 5) break;
                    }
                    if (arts.Length > shown)
                        outer.Children.Add(new TextBlock { Text = "+" + (arts.Length - shown),
                            Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                            Margin = new Thickness(5, 1, 0, 2) });
                }
            }
        }

        string reason = !string.IsNullOrEmpty(waiting) ? waiting : S(primaryWorker, "reason");
        if (!string.IsNullOrEmpty(reason))
        {
            outer.Children.Add(new TextBlock { Text = ja ? "待機・補足" : "Waiting / note",
                Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 10.5,
                FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 6, 0, 1) });
            outer.Children.Add(new TextBlock { Text = reason, Foreground = Muted, FontSize = 11.5,
                TextWrapping = TextWrapping.Wrap });
        }

        AddSpineTimeline(outer, primaryWorker);
        return outer;
    }
}
