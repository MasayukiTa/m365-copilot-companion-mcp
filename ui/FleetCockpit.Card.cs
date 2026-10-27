// FleetCockpit.Card.cs -- the worker card and its small parts: badges, pills, chevron, retry row, title.
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

    // Build a small outline button for the attention recovery row (§6: equal-weight neutral buttons).
    Button AttentionBtn(string label)
    {
        var b = new Button();
        b.Content = label;
        b.Cursor = Cursors.Hand;
        b.FontSize = 12;
        b.Padding = new Thickness(12, 3, 12, 3);
        b.Margin = new Thickness(0, 0, 6, 0);
        b.BorderThickness = new Thickness(1);
        b.Background = Brushes.Transparent;
        b.BorderBrush = Border;
        b.Foreground = Muted;
        b.Template = FlatButtonTemplate();
        return b;
    }

    // One canonical user-facing result string for the expanded result panel, clipboard, and
    // artifact detection. Keeping these entry points together prevents the menu from copying raw
    // runner chatter while the card displays the cleaned final answer.
    string WorkerResultText(Dictionary<string, object> w, bool terminal)
    {
        string displayResult = w != null ? S(w, "display_result") : "";
        if (!string.IsNullOrEmpty(displayResult)) return displayResult;

        string last = w != null ? S(w, "last") : "";
        if (!string.IsNullOrEmpty(last))
        {
            string cleaned = CleanAgentResultForUi(last);
            return !string.IsNullOrEmpty(cleaned)
                ? cleaned
                : (_lang == 0 ? "結果を受信しました" : "Result received");
        }

        string outcome = w != null ? S(w, "outcome") : "";
        return terminal ? OutcomeLabel(outcome) : (_lang == 0 ? "実行中…" : "Working…");
    }

    void CopyWorkerResult(string result)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(result)) throw new InvalidOperationException();
            Clipboard.SetText(result);
            ShowScaleToast(T("copy_result_done"));
        }
        catch (Exception) { ShowScaleToast(T("copy_result_fail")); }
    }

    // The parallel label/action lists a CardKebabBtn consumes. A null label is a separator; the
    // matching action slot is null too (CardKebabBtn pairs them by index).
    sealed class CardMenuModel
    {
        public readonly List<string> Labels = new List<string>();
        public readonly List<Action> Actions = new List<Action>();
        public void Add(string label, Action action) { Labels.Add(label); Actions.Add(action); }
        public void Separator() { Labels.Add(null); Actions.Add(null); }
    }

    // STATE -> MENU ITEMS, in ONE place. Every card (running / waiting / stopped / runtime-wait /
    // completed) gets its kebab items from here, so adding an action is one row in this method
    // rather than a new branch in Card(). Each item calls an action that ALREADY EXISTS elsewhere
    // in this class (OpenWorker, RevealPath, RetryGoal, RequestClose, ArchiveAndHide, ...), except
    // the priority toggle whose body lives in the separate priority_boost work (wired here as a
    // call site only -- see explain.md).
    //
    // Built lazily: this runs when the card is drawn, but the per-item delegates only fire on
    // click, and CardKebabBtn itself defers the ContextMenu's visual materialization to first open.
    CardMenuModel BuildCardMenu(Dictionary<string, object> w, string name, string goal, string conv,
                                string status, string reason, bool closed, bool terminal)
    {
        var m = new CardMenuModel();
        var wt = w;
        bool finishedCard = closed || terminal;

        string menuResult = WorkerResultText(wt, finishedCard);
        var artifactPaths = DetectExistingPaths(menuResult);
        bool controlGoal = IsLocalLoopControlGoal(goal);

        if (finishedCard)
        {
            // Completed/released card: reuse actions first, cleanup after a separator. This order
            // and these keys are pinned by test_fleet_cockpit_card_menu.py -- do not reshuffle.
            string copyText = menuResult;
            m.Add(T("copy_result"), delegate { CopyWorkerResult(copyText); });

            if (artifactPaths.Count > 0)
            {
                string firstArtifact = artifactPaths[0];
                m.Add(T("reveal_artifacts"), delegate {
                    ShowScaleToast(RevealPath(firstArtifact) ? T("reveal_artifacts_done") : T("reveal_artifacts_fail"));
                });
            }

            if (!controlGoal)
                m.Add(T("rerun_same"), delegate { RetryGoal(wt); ShowScaleToast(T("rerun_started")); });

            m.Separator();
            m.Add("→ " + T("to_history"), delegate { ArchiveAndHide(wt); });
            return m;
        }

        // Active card: running / waiting / stopped / runtime-wait (NOT closed, NOT terminal).
        // NOTE: the always-visible 開く/Open link to the left of the kebab already opens the
        // conversation, so the menu does NOT repeat it -- it carries the actions that link cannot.
        string nm = name;

        if (artifactPaths.Count > 0)
        {
            string firstArtifact = artifactPaths[0];
            m.Add(T("reveal_artifacts"), delegate {
                ShowScaleToast(RevealPath(firstArtifact) ? T("reveal_artifacts_done") : T("reveal_artifacts_fail"));
            });
        }

        // The fleet applies and persists the priority toggle.
        if (_priorityBoostEnabled && !string.IsNullOrEmpty(name) && !controlGoal)
        {
            bool on = IsBoosted(wt);
            string nmb = name;
            m.Add(on ? T("boost_clear") : T("boost_set"), delegate {
                RequestSetPriorityBoost(nmb, !on);
                ShowScaleToast(on ? T("boost_clear_done") : T("boost_set_done"));
            });
        }

        // Why it's waiting: real queue rank / workers-ahead / resource-wait are not emitted into
        // status.json yet, so this formats what IS known (status/reason/next_step), shown as-is.
        m.Add(T("why_waiting"), delegate { ShowWhyWaiting(wt); });

        // Stop: same behaviour the old always-visible 解放 button had -- close, then clear the card
        // only if the fleet is stopped (so a stopped-fleet card does not linger).
        m.Add(T("stop_task"), delegate {
            RequestClose(nm);
            if (_lastRoot != null
                && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                ArchiveAndHide(wt);
        });

        m.Separator();
        m.Add("→ " + T("to_history"), delegate { ArchiveAndHide(wt); });
        return m;
    }

    // Format what is known about why an active card is not progressing. Queue rank, workers-ahead
    // and resource-wait are not in status.json yet (no queue_position/work_dir keys exist), so we
    // present the agent-emitted status / reason / next_step as-is in a dialog. When the queue
    // fields arrive, prepend them here -- the menu wiring does not change.
    void ShowWhyWaiting(Dictionary<string, object> w)
    {
        string status = S(w, "status");
        string reason = S(w, "reason");
        string nextStep = S(w, "next_step");
        var sb = new StringBuilder();
        if (!string.IsNullOrEmpty(status))
            sb.Append(_lang == 0 ? "状態: " : "Status: ").Append(status).Append('\n');
        if (!string.IsNullOrEmpty(reason))
            sb.Append(_lang == 0 ? "理由: " : "Reason: ").Append(reason).Append('\n');
        if (!string.IsNullOrEmpty(nextStep))
            sb.Append(_lang == 0 ? "次の一手: " : "Next step: ").Append(nextStep).Append('\n');
        string body = sb.ToString().TrimEnd('\n');
        if (string.IsNullOrEmpty(body)) body = T("why_waiting_none");
        try { MessageBox.Show(this, body, T("why_waiting_title"),
                              MessageBoxButton.OK, MessageBoxImage.Information); }
        catch (Exception) { ShowScaleToast(body); }
    }

    Border Card(Dictionary<string, object> w)
    {
        string name = S(w, "name");
        string goal = S(w, "goal");
        string goalSummary = S(w, "goal_summary");
        string rawStatus = S(w, "status");
        string status = rawStatus == "ready" ? "waiting" : rawStatus;   // canonical (runner emits fine states)
        string reason = S(w, "reason");
        string last = S(w, "last");
        string conv = S(w, "conv_url");
        string convTitle = S(w, "conv_title");
        int turn = I(w, "turn");
        int reviews = I(w, "verify_attempts");
        bool verifiedOk = string.Equals(S(w, "verified"), "True", StringComparison.OrdinalIgnoreCase);
        bool closed = string.Equals(S(w, "closed"), "True", StringComparison.OrdinalIgnoreCase);
        bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                        || status == "error" || status == "cancelled";
        bool isOpen = _expanded.Contains(name);
        bool internalControl = true;
        if (!IsLocalLoopControlGoal(goal)) internalControl = false;
        // P0: INFRA_STUCK is an infra pause (Edge/sign-in/connector broke), NOT a task failure. It
        // gets the distinct ORANGE インフラ待ち treatment: a warning rail/pill, its actionable reason
        // shown as-is, and a 再投入 re-queue action — NOT the red stuck/error recovery surface.
        bool isInfra = !closed && !internalControl && IsInfraStuck(w);
        bool isLocalRuntimeWait = !closed
            && status == "waiting_runtime"
            && string.Equals(S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase)
            && string.Equals(S(w, "runtime_resume_allowed"), "True", StringComparison.OrdinalIgnoreCase);
        bool isAgentSetupWait = isLocalRuntimeWait && IsAgentSetupRuntimeWait(reason);
        bool isLocalLoop = string.Equals(
            S(w, "execution_profile"), "LOCAL_LOOP", StringComparison.OrdinalIgnoreCase);
        bool localSteerBlocked = isLocalLoop && (status == "waiting_user"
            || status == "waiting_external" || status == "needs_routing");
        // Attention lane: stuck/maxturns/error and NOT yet expanded -- gets recovery surface treatment.
        // INFRA_STUCK is carved out of the red attention lane (handled by its own infra branch).
        bool isAttention = !closed && IsOperatorAttention(w);

        // The chip carries the status. There used to be a 3px coloured rail down the left edge of
        // every row as well, and removing it cost nothing measurable: the chip sits at a nearly
        // constant x a few pixels in, so scanning the column for "which row needs me" reads the
        // chip, not the rail thirty pixels to its left. The rail restated what the chip already
        // said, in the one shape the operator has objected to for months.
        bool isDone = status == "done" || string.Equals(S(w, "outcome"), "DONE", StringComparison.OrdinalIgnoreCase);
        string chipKind = internalControl ? "neutral"
            : (isDone ? "success" : (isInfra ? "warning" : Theme.StatusKind(status)));
        // Fan-out PARENT: its real status is done/FANOUT on purpose, which reads as "finished"
        // while its children still run. The coordinator-side derived display_label (waiting etc.)
        // replaces the chip text only; status/outcome are untouched.
        string parentLabel = internalControl ? "" : S(w, "display_label");
        if (parentLabel.Length > 0)
        {
            string pds = S(w, "display_state");
            chipKind = pds == "merge_failed" ? "danger"
                     : (pds == "interrupted" ? "warning" : (pds == "done" ? "success" : "info"));
        }

        // Pass A2-1 TASK 1: demote the collapsed row to a LEDGER ROW.
        // - No rounded corners, no card background fill, no full border.
        // - Flat row on the app surface: bottom divider only (Theme.Border).
        // - Tighter vertical padding so rows are denser (~64-76px collapsed).
        var card = new Border();
        card.Tag = name;                // lets a chevron toggle find & replace just this card
        // Ledger row: only a bottom hairline divider (no rounded card border).
        card.BorderThickness = new Thickness(0, 0, 0, 1);
        card.BorderBrush = Border;
        // Transparent background (inherits the flat app surface).
        card.Background = Brushes.Transparent;
        // No margin between rows; the bottom border IS the separator.
        card.Margin = new Thickness(0, 0, 0, 0);

        var shell = new Grid();
        shell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // 12px on the left where the rail's 3px plus the old 10px inset used to sit, so the text
        // column does not move now that the rail is gone.
        var col = new StackPanel { Margin = new Thickness(12, 7, 12, 7) };
        Grid.SetColumn(col, 0); shell.Children.Add(col);

        // ── line 1: [chevron] [chip] [title* ........] [Open] [primary action | kebab⋮] ──
        // Fixed right-action width (~112px) so long titles never collide with actions.
        var top = new Grid();
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });  // left: chevron+chip+title
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(112, GridUnitType.Pixel) }); // right: actions

        // Right action area (fixed 112px): Open + primary action or kebab.
        var right = new StackPanel { Orientation = Orientation.Horizontal,
                                     HorizontalAlignment = HorizontalAlignment.Right,
                                     VerticalAlignment = VerticalAlignment.Center };
        {
            var openLink = new TextBlock();
            openLink.Text = _lang == 0 ? "開く" : "Open";
            openLink.Foreground = Muted; openLink.FontSize = 12;
            openLink.VerticalAlignment = VerticalAlignment.Center; openLink.Cursor = Cursors.Hand;
            openLink.Margin = new Thickness(0, 0, 8, 0);
            openLink.ToolTip = _lang == 0 ? "この会話をメインチャットで開く" : "Open this conversation in the chat";
            string onm = name; string ourl = conv;
            openLink.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; FlashOpen(card); OpenWorker(onm, ourl); };
            right.Children.Add(openLink);
        }
        // The kebab (More actions) now appears on EVERY card -- running, waiting, stopped,
        // runtime-wait, and completed -- in the same right-edge slot with the same look. Its item
        // list is built in ONE place (BuildCardMenu, the state->items table below) so a new action
        // is one row there, not a new branch here. The runtime-wait card additionally keeps its
        // explicit 再開/Resume button (a one-click recovery affordance), with the kebab beside it.
        if (isLocalRuntimeWait)
        {
            var runtimeResume = AttentionBtn(_lang == 0 ? "再開" : "Resume");
            runtimeResume.ToolTip = _lang == 0
                ? "実行環境を修復した後、同じ永続タスクを再開します"
                : "Resume the same durable task after repairing the runtime";
            Dictionary<string, object> runtimeWaitWorker = w;
            runtimeResume.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                ResumeLocalLoopRuntime(runtimeWaitWorker);
            };
            right.Children.Add(runtimeResume);
        }
        {
            var menu = BuildCardMenu(w, name, goal, conv, status, reason, closed, terminal);
            right.Children.Add(CardKebabBtn(menu.Labels.ToArray(), menu.Actions.ToArray(), w));
        }
        Grid.SetColumn(right, 1); top.Children.Add(right);

        // left cluster: chevron + status chip, then the title fills the rest (1 line, ellipsis)
        var left = new DockPanel { LastChildFill = true };
        var chev = ChevronToggle(name, isOpen); DockPanel.SetDock(chev, Dock.Left); left.Children.Add(chev);
        // INFRA_STUCK -> distinct ORANGE インフラ待ち pill; otherwise the normal status label.
        var chip = Pill(internalControl
            ? (_lang == 0 ? "内部制御" : "Internal control")
            : (isAgentSetupWait ? (_lang == 0 ? "エージェント設定待ち" : "Agent setup required")
                : (isInfra ? T("infra_wait") : (parentLabel.Length > 0 ? parentLabel : Theme.StatusLabel(status, _lang)))), chipKind);
        chip.Margin = new Thickness(2, 0, 5, 0);
        if (status == "interrupted" && !string.IsNullOrEmpty(reason)) chip.ToolTip = reason;
        DockPanel.SetDock(chip, Dock.Left); left.Children.Add(chip);
        // Top-priority marker: a small blue pill right after the status chip when the fleet has
        // applied priority_boost to this worker (read-only reflection of fleet state).
        if (_priorityBoostEnabled && IsBoosted(w))
        {
            var boostPill = Pill(T("boost_mark"), "info");
            boostPill.Margin = new Thickness(0, 0, 5, 0);
            boostPill.ToolTip = _lang == 0 ? "このタスクは最優先に指定されています" : "This task is marked top priority";
            DockPanel.SetDock(boostPill, Dock.Left); left.Children.Add(boostPill);
        }
        // AGENT BADGE (P0 feature 4): which agent this conversation is bound to. Green subtle badge
        // for the configured agent, WARNING-colored 既定Copilot badge for a plain /chat/ (default) url.
        var agentBadge = BuildAgentBadge(conv, convTitle);
        if (agentBadge != null) { DockPanel.SetDock(agentBadge, Dock.Left); left.Children.Add(agentBadge); }
        // EFFORT BADGE: only when the runner sent effort_level (policy not off); absent = none.
        var effBadge = BuildEffortBadge(w);
        if (effBadge != null) { DockPanel.SetDock(effBadge, Dock.Left); left.Children.Add(effBadge); }
        string headline = !string.IsNullOrEmpty(goalSummary)
            ? goalSummary : CardTitle(convTitle, goal);
        var ht = new TextBlock {
            Text = headline, Foreground = Fg, FontSize = 13.5, FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap
        };
        left.Children.Add(ht);
        Grid.SetColumn(left, 0); top.Children.Add(left);
        col.Children.Add(top);
        UIElement groupLine = BuildGroupLine(w);
        if (groupLine != null) col.Children.Add(groupLine);

        if (!isOpen)
        {
            // ── COLLAPSED ROW body (ledger row, not expanded drawer) ──────────────────────────
            if (isLocalRuntimeWait)
            {
                if (!string.IsNullOrEmpty(reason))
                {
                    var runtimeReason = new TextBlock
                    {
                        Text = (isAgentSetupWait ? (_lang == 0 ? "エージェント設定待ち: " : "Agent setup required: ")
                            : (_lang == 0 ? "実行環境待ち: " : "Runtime paused: ")) + OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(runtimeReason);
                }
            }
            else if (isInfra)
            {
                // P0 INFRA_STUCK collapsed row: the reason text is actionable (e.g. "sign-in
                // required" / "default-Copilot fallback"), so render it verbatim, then offer a
                // 再投入 action reusing the existing retry path. NOT a red error surface.
                if (!string.IsNullOrEmpty(reason))
                {
                    var ir = new TextBlock
                    {
                        Text = OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(ir);
                }
                var infraRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24, 6, 0, 2) };
                infraRow.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };
                // 再投入 (Re-queue): reuse RetryGoal (add_goal if live, else respawn) — the same
                // affordance stopped goals already use, per spec ("reuse the existing retry path").
                var requeueBtn = AttentionBtn(T("infra_retry"));
                requeueBtn.ToolTip = _lang == 0
                    ? "インフラ復旧後にこのゴールを再投入します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Re-queue this goal after the infra recovers (add_goal if live, else respawn fleet)";
                Dictionary<string, object> wInfra = w;
                requeueBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wInfra); };
                infraRow.Children.Add(requeueBtn);
                // Open-conversation shortcut so the user can inspect the parked lane.
                var infraOpen = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                string infraNm = name; string infraUrl = conv;
                infraOpen.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(infraNm, infraUrl); };
                infraRow.Children.Add(infraOpen);
                col.Children.Add(infraRow);
            }
            else if (isAttention)
            {
                // Pass A2-1 TASK 2: RECOVERY ROW for attention lanes (stuck/maxturns/error).
                // §6: "Not an error card — a recovery surface."

                // Line 2: human recovery sentence from the `reason` field [REAL].
                // Calm lead-in + verbatim reason text. Omit when reason is empty.
                if (!string.IsNullOrEmpty(reason))
                {
                    string leadIn = _lang == 0 ? "停止理由: " : "Stopped: ";
                    var rl2 = new TextBlock
                    {
                        Text = leadIn + OneLine(reason),
                        Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl2);
                }

                // Line 3: freshness — "最終更新 {N} 前" / "last update {N} ago" [COMPUTED].
                // Best proxy: transcript meta ts (start of this worker); fallback to status updated.
                {
                    string transcriptPath2 = S(w, "transcript");
                    double evidenceTs = ReadTranscriptStartTs(transcriptPath2);
                    // If transcript ts unavailable, fall back to the status.json top-level updated field.
                    if (evidenceTs <= 0 && _lastRoot != null)
                        evidenceTs = Dbl(_lastRoot, "updated");
                    string freshnessText;
                    if (evidenceTs > 0)
                    {
                        double age = NowUnix() - evidenceTs;
                        if (age < 0) age = 0;
                        freshnessText = _lang == 0
                            ? ("最終更新 " + Fmt(age) + " 前")
                            : ("last update " + Fmt(age) + " ago");
                    }
                    else
                    {
                        freshnessText = _lang == 0 ? "最終更新: 不明" : "last update: unknown";
                    }
                    var ml3 = new TextBlock
                    {
                        Text = freshnessText,
                        Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(ml3);
                }

                // Recovery actions (§6): [再開]/[Resume], [会話を開く]/[Open conversation], [停止]/[Stop].
                // Equal-weight outline buttons; NO accent color.
                var recov = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(24, 6, 0, 2)
                };
                recov.MouseLeftButtonUp += delegate (object s2, MouseButtonEventArgs e2) { e2.Handled = true; };

                // [再開]/[Resume]: fall back to RetryGoal (re-queues or re-spawns via existing path).
                var resumeBtn = AttentionBtn(_lang == 0 ? "再開" : "Resume");
                resumeBtn.ToolTip = _lang == 0
                    ? "このタスクを再開・再試行します（実行中なら add_goal、停止済なら fleet を再起動）"
                    : "Resume or retry this task (add_goal if fleet live, else respawn fleet)";
                Dictionary<string, object> wResume = w;
                resumeBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; RetryGoal(wResume); };
                recov.Children.Add(resumeBtn);

                // [会話を開く]/[Open conversation]: open the conversation/transcript in the main chat.
                var evidBtn = AttentionBtn(_lang == 0 ? "会話を開く" : "Open conversation");
                evidBtn.ToolTip = _lang == 0
                    ? "この会話を開いて最後のやり取りを確認する"
                    : "Open this conversation to review the last exchange";
                string evidNm = name; string evidUrl = conv;
                evidBtn.Click += delegate (object s2, RoutedEventArgs e2) { e2.Handled = true; FlashOpen(card); OpenWorker(evidNm, evidUrl); };
                recov.Children.Add(evidBtn);

                // [停止]/[Stop]: release/archive this lane via existing mechanisms.
                var stopBtn = AttentionBtn(_lang == 0 ? "停止" : "Stop");
                stopBtn.ToolTip = _lang == 0
                    ? "このレーンを停止して履歴へ移動する"
                    : "Stop this lane and move to history";
                string stopNm = name; Dictionary<string, object> wStop = w;
                stopBtn.Click += delegate (object s2, RoutedEventArgs e2)
                {
                    e2.Handled = true;
                    RequestClose(stopNm);
                    if (_lastRoot != null
                        && (!_lastRoot.ContainsKey("running") || Convert.ToBoolean(_lastRoot["running"]))
                        && (NowUnix() - Dbl(_lastRoot, "updated")) > 8)
                        ArchiveAndHide(wStop);
                    else
                        ArchiveAndHide(wStop);
                };
                recov.Children.Add(stopBtn);

                col.Children.Add(recov);
            }
            else
            {
                // ── Normal collapsed ledger row: line 2 + line 3 ────────────────────────────
                // Line 2: latest human-readable progress. Precedence: display_result > last > fallback.
                var execution = Obj(w, "execution");
                if (execution != null)
                {
                    string currentStep = S(execution, "current_step");
                    int currentIndex = I(execution, "current_step_index");
                    int totalSteps = I(execution, "total_steps");
                    // Open-ended LOCAL_LOOP has no turn_plan, so current_step is initially the
                    // authoritative goal itself. The headline already shows its compact task
                    // identity; repeating the full (often English, multi-kilobyte) goal here makes
                    // the collapsed card look like progress when it is only the original request.
                    bool currentStepRepeatsGoal = string.Equals(
                        (currentStep ?? "").Trim(), (goal ?? "").Trim(), StringComparison.Ordinal);
                    if (!string.IsNullOrEmpty(currentStep) && !currentStepRepeatsGoal)
                    {
                        string stepPrefix = totalSteps > 0
                            ? (currentIndex + "/" + totalSteps + "  ")
                            : (currentIndex + "  ");
                        var stepLine = new StackPanel { Orientation = Orientation.Horizontal,
                            Margin = new Thickness(24, 4, 0, 0) };
                        stepLine.Children.Add(MakeIcon("play_arrow", 13, Fg));
                        stepLine.Children.Add(new TextBlock
                        {
                            Text = stepPrefix + OneLine(currentStep),
                            Foreground = Fg, FontSize = 12.5, FontWeight = FontWeights.SemiBold,
                            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                            Margin = new Thickness(4, 0, 0, 0)
                        });
                        col.Children.Add(stepLine);
                    }
                }

                string collapsedDisplayResult = S(w, "display_result");
                string resultText;
                if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    resultText = collapsedDisplayResult;
                else if (!string.IsNullOrEmpty(last))
                    resultText = last;
                else if (terminal || closed)
                    resultText = "";
                else if (status == "pending")
                    resultText = (_lang == 0 ? "待機中…" : "Queued…");
                else
                    resultText = (_lang == 0 ? "実行中…" : "Working…");

                if (!string.IsNullOrEmpty(resultText))
                {
                    string displayLine;
                    if (!string.IsNullOrEmpty(collapsedDisplayResult))
                    {
                        // display_result is already clean; just take the first line for the collapsed view
                        int nl2 = collapsedDisplayResult.IndexOf('\n');
                        string fl2 = nl2 >= 0 ? collapsedDisplayResult.Substring(0, nl2) : collapsedDisplayResult;
                        displayLine = OneLine(!string.IsNullOrEmpty(fl2) ? fl2 : collapsedDisplayResult);
                    }
                    else if (!string.IsNullOrEmpty(last))
                    {
                        string cleanedResult = CleanAgentResultForUi(last);
                        string firstLine = "";
                        if (!string.IsNullOrEmpty(cleanedResult))
                        {
                            int nl = cleanedResult.IndexOf('\n');
                            firstLine = nl >= 0 ? cleanedResult.Substring(0, nl) : cleanedResult;
                        }
                        displayLine = !string.IsNullOrEmpty(firstLine)
                            ? OneLine(firstLine)
                            : (_lang == 0 ? "結果を受信しました" : "Result received");
                    }
                    else
                    {
                        displayLine = OneLine(resultText);
                    }
                    var rl = new TextBlock
                    {
                        Text = displayLine, Foreground = Muted, FontSize = 12.5,
                        TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 4, 0, 0)
                    };
                    col.Children.Add(rl);
                }

                // Requirement (6): one honest line on why this lane is not progressing — queue rank,
                // how many are ahead, and/or a resource wait. Built ONLY from fields the fleet puts
                // in the worker dict (no fabricated rank). Empty when the fleet supplies none, so
                // nothing is invented. Shown for non-terminal lanes only.
                if (!terminal && !closed)
                {
                    string whyLine = WhyNotProgressing(w);
                    if (!string.IsNullOrEmpty(whyLine))
                    {
                        var wl = new TextBlock
                        {
                            Text = whyLine,
                            Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                            TextTrimming = TextTrimming.CharacterEllipsis,
                            TextWrapping = TextWrapping.NoWrap,
                            Margin = new Thickness(24, 2, 0, 0)
                        };
                        wl.ToolTip = whyLine;
                        col.Children.Add(wl);
                    }
                }

                // Line 3: meta — [elapsed ·] {worker name | 完了時刻} · turn N · verified [COMPUTED].
                // On a COMPLETED (terminal) card the worker number (e.g. W67) is NOT shown: the
                // operator asked for the task's identity (its title, already on line 1) and WHEN it
                // finished, not which lane ran it. The completion time is the ts of the last
                // phase_event; if none exists we show neither number nor time (the other meta parts
                // still render). A live card keeps the worker name as before.
                var meta = new StringBuilder();
                string transcriptPath = S(w, "transcript");
                double startTs = ReadTranscriptStartTs(transcriptPath);
                if (startTs > 0)
                {
                    double cardElapsed = NowUnix() - startTs;
                    if (cardElapsed > 0)
                        meta.Append(Fmt(cardElapsed)).Append(" · ");
                }
                if (terminal)
                {
                    double doneTs = SubmittedTasks.CompletedUnix(w);
                    if (doneTs > 0)
                    {
                        string hhmm;
                        try
                        {
                            var dt = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                                .AddSeconds(doneTs).ToLocalTime();
                            hhmm = dt.ToString("HH:mm");
                        }
                        catch (Exception) { hhmm = ""; }
                        if (hhmm.Length > 0)
                            meta.Append(_lang == 0 ? ("完了 " + hhmm) : ("completed " + hhmm));
                    }
                }
                else
                {
                    meta.Append(name.ToUpper());
                }
                if (turn > 0) meta.Append(" · ").Append(T("turn")).Append(' ').Append(turn);
                // RUN DURATION (start..done), placed to the RIGHT of the turn count. Only a completed
                // card shows it, and only when BOTH the transcript start ts and the last phase_event ts
                // are known; otherwise nothing is appended (we never guess an elapsed from one endpoint).
                if (terminal)
                {
                    double runDoneTs = SubmittedTasks.CompletedUnix(w);
                    if (startTs > 0 && runDoneTs > 0)
                    {
                        string runDur = DurationHuman(runDoneTs - startTs, _lang == 0);
                        if (runDur.Length > 0)
                            meta.Append(" · ").Append(_lang == 0 ? "所要 " : "took ").Append(runDur);
                    }
                }
                if (reviews > 0) meta.Append(" · ").Append(_lang == 0 ? ("確認 " + reviews + " 回") : ("reviewed " + reviews));
                if (verifiedOk) meta.Append(" · ").Append(_lang == 0 ? "検証OK" : "verified");
                if (execution != null)
                {
                    int completed = I(execution, "completed_count");
                    int totalSteps = I(execution, "total_steps");
                    int artifactCount = ArrCount(execution, "artifacts");
                    meta.Append(" · ").Append(_lang == 0 ? "完了 " : "done ").Append(completed);
                    if (totalSteps > 0) meta.Append('/').Append(totalSteps);
                    if (artifactCount > 0)
                        meta.Append(" · ").Append(artifactCount).Append(_lang == 0 ? " 成果物" : " artifacts");
                    double progressTs = Dbl(execution, "last_progress_at");
                    if (progressTs > 0)
                    {
                        double progressAge = Math.Max(0, NowUnix() - progressTs);
                        meta.Append(" · ").Append(_lang == 0 ? "進捗 " : "progress ")
                            .Append(Fmt(progressAge)).Append(_lang == 0 ? " 前" : " ago");
                    }
                }
                // A completed card with no completion ts (no phase_events) drops its leading cell,
                // so the string can start with a stray " · ". Trim it so the line never leads with
                // a separator.
                string metaText = meta.ToString();
                if (metaText.StartsWith(" · ")) metaText = metaText.Substring(3);
                var ml = new TextBlock
                {
                    Text = metaText, Foreground = Theme.Br(Theme.Faint(_dark)), FontSize = 12,
                    Margin = new Thickness(24, 3, 0, 0)
                };
                col.Children.Add(ml);

                // TASK 3 (Bucket C): next_step + self_confidence [REAL — agent-emitted, shown as-is].
                string nextStep = S(w, "next_step");
                string selfConf = S(w, "self_confidence");

                // "→ 次: {next_step}" / "→ next: {next_step}"  — muted, 1 line ellipsis.
                if (!string.IsNullOrEmpty(nextStep))
                {
                    string nextPrefix = _lang == 0 ? "→ 次: " : "→ next: ";
                    var nextTb = new TextBlock
                    {
                        Text = nextPrefix + nextStep,
                        Foreground = Theme.Br(Theme.Muted(_dark)),
                        FontSize = 12,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                        TextWrapping = TextWrapping.NoWrap,
                        Margin = new Thickness(24, 2, 0, 0)
                    };
                    col.Children.Add(nextTb);
                }

                // Self-confidence chip — clearly labeled "自己申告" (self-reported, not verified).
                // Neutral/muted styling; do NOT use a trust-signal color. Subtle level tint only.
                if (!string.IsNullOrEmpty(selfConf))
                {
                    string confLabel = _lang == 0
                        ? ("自己申告: " + selfConf)
                        : ("self-reported: " + selfConf);
                    // Subtle background tint by level (very muted -- not a trust signal).
                    // The six hex values used to be written here. They were the only colours in
                    // the UI outside Theme.cs, which is the file that exists because the palette
                    // had already drifted once from being duplicated at call sites.
                    string tintKind = selfConf == "high" ? "success"
                                    : (selfConf == "medium" ? "warning" : "danger");
                    string tintHex = Theme.KindTint(tintKind, _dark);

                    var confChip = new Border();
                    confChip.CornerRadius = new CornerRadius(Theme.RadChip);
                    confChip.Background = Theme.Br(tintHex);
                    confChip.Padding = new Thickness(6, 1, 6, 1);
                    confChip.Margin = new Thickness(24, 3, 0, 0);
                    confChip.HorizontalAlignment = HorizontalAlignment.Left;
                    var confTb = new TextBlock();
                    confTb.Text = confLabel;
                    confTb.FontSize = 11;
                    confTb.Foreground = Theme.Br(Theme.Muted(_dark));
                    confChip.Child = confTb;
                    col.Children.Add(confChip);
                }
            }

            // Feature 1: always-visible steer affordance -- reachable WITHOUT expanding the card.
            // Same terminal gate the expanded drawer's SteerRow/RetryRow/ContinueRow switch uses
            // (below): a terminal card (done/stuck/maxturns/error/cancelled) gets nothing here.
            if (!terminal && !localSteerBlocked) col.Children.Add(CollapsedSteerRow(name));
        }
        else
        {
            // ── EXPANDED DRAWER ───────────────────────────────────────────────────────────────
            // Expanded detail is organized into tabs (spec): Overview (default) / Conversation /
            // Review / Logs -- so raw transcript, refuter notes and internal fields no longer all
            // dump onto the surface at once. Heavy content is built ONLY when expanded.
            col.Children.Add(BuildCardTabs(w, name, goal, last, reason, terminal));
            // Actions live BELOW the tabs (not inside one) so steer/retry are always reachable.
            if (!terminal && !localSteerBlocked) col.Children.Add(SteerRow(name));
            else if (IsRetryableWorker(w)) col.Children.Add(RetryRow(w));
            else col.Children.Add(ContinueRow(name, goal, S(w, "conv_url")));
        }

        card.Child = shell;
        // FIX B: while a Stop is in flight (optimistic, not yet confirmed by the sweep), dim every
        // non-terminal card so the click reads as "taking effect" immediately. terminal cards
        // (already done/stuck/etc.) are left alone -- Stop doesn't touch them.
        card.Opacity = (_stopping && !terminal) ? 0.45 : 1.0;
        // Always clickable: open this worker in the main chat BY NAME, so it works even when the
        // Copilot conv_url was never captured (the main chat renders the live status.json snapshot
        // for this worker). conv_url is passed too so /history can fill in the full transcript
        // when it is available.
        card.Cursor = Cursors.Hand;
        string wname = name; string url = conv;
        card.MouseLeftButtonUp += delegate { FlashOpen(card); OpenWorker(wname, url); };
        card.ToolTip = _lang == 0 ? "クリックでこの会話をメインに表示" : "Click to open this conversation in the chat";
        return card;
    }

    // Feature A: a read-only TextBox inside a clickable card would still raise the card's
    // MouseLeftButtonUp -> open-conversation, so dragging to select text would yank you into
    // the chat. Swallow the mouse-up at the TextBox (handledEventsToo) so selection works and
    // the card click never fires.
    void SwallowMouseUp(TextBox tb)
    {
        tb.AddHandler(UIElement.MouseLeftButtonUpEvent,
            new MouseButtonEventHandler(delegate(object s, MouseButtonEventArgs e) { e.Handled = true; }),
            true);
    }

    // Feature C: per-card "Retry" on a terminal non-DONE worker. Re-queues this exact goal
    // (with its acceptance checks + cwd) at priority via add_goal. Clicks must not bubble to
    // the card's open-conversation handler.
    UIElement RetryRow(Dictionary<string, object> w)
    {
        var dp = new DockPanel();
        dp.Margin = new Thickness(0, 12, 0, 0);
        dp.MouseLeftButtonUp += delegate(object s, MouseButtonEventArgs e) { e.Handled = true; };
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted;
        note.VerticalAlignment = VerticalAlignment.Center;
        note.TextWrapping = TextWrapping.Wrap;
        dp.Children.Add(note);
        var btn = new Button();
        btn.Content = T("retry");
        btn.Background = AccentFill; btn.Foreground = AccentFg; btn.BorderThickness = new Thickness(0);
        btn.Padding = new Thickness(12, 4, 12, 4); btn.Cursor = Cursors.Hand; btn.FontSize = 12;
        btn.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(btn, Dock.Right);
        Dictionary<string, object> wkr = w;
        btn.Click += delegate
        {
            RetryGoal(wkr);
            note.Text = RunIsLive() ? "" : T("retry_note");
        };
        dp.Children.Add(btn);
        return dp;
    }

    // Build one add_goal entry { text, checks, cwd, priority } for a worker dict. checks is
    // passed through as-is from the worker (whatever the fleet stored -- a list).
    Dictionary<string, object> RetryEntry(Dictionary<string, object> w)
    {
        var item = new Dictionary<string, object>();
        item["text"] = S(w, "goal");
        object checks = null;
        if (w.ContainsKey("checks")) checks = w["checks"];
        item["checks"] = checks;
        item["cwd"] = S(w, "cwd");
        item["priority"] = true;
        item["retry"] = true;   // the runner refuses a retry whose goal is already live
        return item;
    }

    // Re-run the worker's goal. TWO honest paths, picked by whether a run is LIVE:
    //  * LIVE  -> send an add_goal command of its own (one file per command); the running fleet
    //            consumes it on its next sweep and re-runs it WITH its acceptance gate (checks+cwd).
    //  * FINISHED/stale -> nothing alive would ever drain add_goal, so instead SPAWN a fresh fleet
    //            for this goal text (SpawnFleet). The relaunched run picks it up and re-runs it.
    // The auto-retry scanner only calls this while live, so its add_goal path is unchanged.
    void RetryGoal(Dictionary<string, object> w)
    {
        string goal = S(w, "goal");
        if (string.IsNullOrEmpty(goal)) return;
        if (IsLocalLoopControlGoal(goal))
        {
            if (_startNote != null) _startNote.Text = _lang == 0
                ? "これはLOCAL_LOOPの内部制御記録です。通常Fleetとして再実行せず、耐久タスクのカードから再開してください。"
                : "This is a LOCAL_LOOP control record. Do not rerun it as Fleet work; resume the durable task card instead.";
            return;
        }
        if (RunIsLive())
        {
            SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
            var adds = new List<object>();
            adds.Add(RetryEntry(w));
            SendCommand(Cmd1("add_goal", adds));
            NoteSubmitted(new List<string> { S(w, "goal") }, submitBaseline);
            return;
        }
        try { SpawnFleet(new List<string> { goal }, "retry_input.txt"); _lastSig = ""; } catch (Exception) { }
    }

    // Feature C bulk: re-run EVERY currently-shown terminal non-DONE worker (respecting the active
    // filter). LIVE -> ONE add_goal command carrying every retried entry; FINISHED/stale -> ONE
    // relaunched fleet carrying
    // all the retried goal texts (mirrors RetryGoal's live/finished split).
    // THE RETRY BUDGET IS SHARED WITH AutoRetryScan, DELIBERATELY. This button had no cap of
    // any kind, so _autoRetryMax bounded only the automatic path. One submitted goal reached
    // 891 workers, 575 of them stuck: each press re-queued every still-stuck goal again, and
    // nothing on screen said the press had done anything -- so it was pressed again.
    //
    // Counting against the same per-goal counter stops the amplification. RETURNING the
    // skipped count is what stops the re-pressing, and matters just as much: a skip nobody
    // can see is indistinguishable from a button that did not work, which is the behaviour
    // that produced the pile in the first place.
    int RetryAllShown(List<Dictionary<string, object>> shown, out int skippedAtCap,
                      Action<bool, string> done = null)
    {
        skippedAtCap = 0;
        bool live = RunIsLive();
        var adds = new List<object>();
        var goalTexts = new List<string>();
        int n = 0;
        foreach (Dictionary<string, object> w in shown)
        {
            if (!IsTerminalWorker(w)) continue;
            // Same closed set as AutoRetryScan and as relay/outcomes.py. "Retry all" used to
            // mean "everything that is not DONE", which swept up fan-out parents and threw
            // away the merged answers they carried.
            if (!IsRetryableWorker(w)) continue;
            // Already re-queued once (by us or by the runner): never a second copy of this worker.
            if (RetryAlreadyCovered(null, w)) continue;
            string g = S(w, "goal");
            // Same counter, same key (goal text), same ceiling as AutoRetryScan, so the auto
            // and manual paths cannot each spend a full allowance on the same goal.
            if (!string.IsNullOrEmpty(g))
            {
                int used = 0;
                if (_autoRetryCount.ContainsKey(g)) used = _autoRetryCount[g];
                if (used >= _autoRetryMax) { skippedAtCap++; continue; }
                _autoRetryCount[g] = used + 1;   // count BEFORE queueing, as AutoRetryScan does
            }
            _autoRetriedWorkers.Add(RetryWorkerKey(w));
            adds.Add(RetryEntry(w));
            if (!string.IsNullOrEmpty(g)) goalTexts.Add(g);
            n++;
        }
        if (n == 0) return 0;
        // The baseline is sampled HERE, on the calling (UI) thread, before the handoff: it reads
        // UI-owned state (_history) and must predate the command/process the delivery creates.
        SubmissionBaseline submitBaseline = CaptureSubmissionBaseline();
        Action deliver = delegate
        {
            if (live)
            {
                SendCommand(Cmd1("add_goal", adds));
                NoteSubmitted(goalTexts, submitBaseline);
            }
            else if (goalTexts.Count > 0)
            {
                SpawnFleet(goalTexts, "retry_input.txt", false, false, null, submitBaseline);
            }
        };
        if (done == null)
        {
            // Synchronous form (kept for callers that need the result inline).
            try { deliver(); if (!live) _lastSig = ""; } catch (Exception) { }
            return n;
        }
        // Everything below is blocking work: command-file IO with a 2 s rename retry, the goals
        // file write, Process.Start of the runner, and one index over every worker + history row.
        // On the UI thread that froze the window with no feedback for as long as it took (about
        // 85 s on a loaded machine with a few hundred rows). It runs on the thread pool; the
        // outcome comes back through `done` on the UI thread.
        System.Threading.ThreadPool.QueueUserWorkItem(delegate
        {
            string err = null;
            try { deliver(); }
            catch (Exception ex) { err = ex.Message; }
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (err == null && !live) _lastSig = "";
                done(err == null, err);
            }));
        });
        return n;
    }

    UIElement MakeReleaseContent()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.Children.Add(MakeIcon("close", 13, Fg));
        var t = new TextBlock(); t.Text = "  " + T("release");
        t.FontSize = 12; t.Foreground = Fg; t.VerticalAlignment = VerticalAlignment.Center;
        sp.Children.Add(t);
        return sp;
    }

    // Status chip (spec): a small OUTLINED pill -- colored border + colored text on a transparent
    // fill, never a saturated block. The kind is one of neutral/info/success/warning/danger.
    // P0 AGENT BADGE (feature 4): show which agent a worker's conversation is bound to, read
    // from its conv_url in status.json.
    //   • conv_url carries "/agent/<id>" AND <id> matches the configured agent -> subtle Muted
    //     badge with the agent's short name (12-char title, else "agent").
    //   • conv_url carries "/agent/<otherid>" -> subtle badge (still an agent, just not ours).
    //   • conv_url is a plain "/chat/" url with NO "/agent/" segment -> WARNING 既定Copilot badge
    //     (a default-Copilot conversation has no MCP connector — the incident case).
    //   • no conv_url yet (worker just started) -> null (no badge; don't accuse prematurely).
    Border BuildAgentBadge(string convUrl, string convTitle)
    {
        if (string.IsNullOrEmpty(convUrl)) return null;
        string lo = convUrl.ToLowerInvariant();
        bool hasAgentSeg = lo.Contains("/agent/");
        if (hasAgentSeg)
        {
            // Agent-bound conversation. Prefer a short name from the title; fall back to "agent".
            string shortName = "agent";
            if (!string.IsNullOrEmpty(convTitle))
            {
                string tt = OneLine(convTitle);
                shortName = tt.Length > 12 ? tt.Substring(0, 12) : tt;
            }
            bool mine = !string.IsNullOrEmpty(_agentMarkerId)
                        && lo.Contains(_agentMarkerId.ToLowerInvariant());
            // Subtle (Muted) badge either way; the configured-agent case is the expected/quiet state.
            return AgentBadge(shortName, mine ? "neutral" : "neutral");
        }
        // A conversation URL that is a plain /chat/ (no /agent/ segment) — default Copilot.
        if (lo.Contains("/chat/") || lo.Contains("/conversation/"))
            return AgentBadge(T("badge_default_copilot"), "warning");
        return null;
    }

    // Small outlined badge. kind: "neutral" (subtle) | "warning" (default-Copilot alert).
    Border AgentBadge(string text, string kind)
    {
        Brush color = kind == "warning" ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.Muted(_dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        b.CornerRadius = new CornerRadius(Theme.RadChip);
        b.Padding = new Thickness(5, 0, 5, 0);
        b.Margin = new Thickness(0, 0, 5, 0);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 10.5; t.FontWeight = FontWeights.SemiBold;
        t.VerticalAlignment = VerticalAlignment.Center;
        b.Child = t;
        b.ToolTip = kind == "warning"
            ? (_lang == 0 ? "既定Copilotの会話（MCPコネクタ無し）。エージェントに接続し直してください。"
                          : "Default-Copilot conversation (no MCP connector). Reconnect to the agent.")
            : (_lang == 0 ? "この会話はエージェントに接続されています" : "This conversation is bound to the agent");
        return b;
    }

    // Per-worker effort pill from the additive status.json fields (EffortPolicyView words it).
    Border BuildEffortBadge(Dictionary<string, object> w)
    {
        bool ja = _lang == 0;
        Dictionary<string, object> last = Obj(w, "effort_last_switch");
        string level = S(w, "effort_level"), src = S(w, "effort_source");
        string text = EffortPolicyView.BadgeText(level, src, last, ja);
        if (text == null) return null;
        var b = Pill(text, "neutral");
        b.Margin = new Thickness(0, 0, 5, 0);
        b.ToolTip = EffortPolicyView.BadgeTip(level, src, last, ja);
        return b;
    }

    // Requirement (6): build a single honest line explaining why a lane is not progressing. Uses
    // ONLY fields the fleet writes into the worker dict — never fabricates a rank. Returns "" when
    // the fleet supplies none of them, so an operator never sees an invented queue position.
    //   - queue_rank / queue_ahead : the lane's position and how many lanes are ahead of it
    //   - resource_wait           : a named resource the lane is blocked on (tab slot, token, lock)
    //   - execution.waiting_reason : the fleet's own human sentence for why it is parked
    // When the lane is boosted but still waiting, we prefix 最優先 so the operator sees the boost
    // is applied yet something else (a resource, not rank) is holding it — that is the "why it's
    // still slow despite boosting" answer the owner asked for.
    string WhyNotProgressing(Dictionary<string, object> w)
    {
        if (w == null) return "";
        bool ja = _lang == 0;
        var bits = new List<string>();

        // Queue position + lanes ahead (only if the fleet provided numbers).
        string rankTxt = S(w, "queue_rank");
        string aheadTxt = S(w, "queue_ahead");
        int rank, ahead;
        bool hasRank = int.TryParse(rankTxt, out rank) && rank > 0;
        bool hasAhead = int.TryParse(aheadTxt, out ahead) && ahead >= 0;
        if (hasRank && hasAhead)
            bits.Add(ja ? ("待ち " + rank + " 番目・前に " + ahead + " 件")
                        : ("queue #" + rank + ", " + ahead + " ahead"));
        else if (hasRank)
            bits.Add(ja ? ("待ち " + rank + " 番目") : ("queue #" + rank));
        else if (hasAhead)
            bits.Add(ja ? ("前に " + ahead + " 件") : (ahead + " ahead"));

        // Resource wait (named blocker), if present.
        string res = S(w, "resource_wait");
        if (!string.IsNullOrEmpty(res))
            bits.Add((ja ? "資源待ち: " : "waiting on: ") + OneLine(res));

        // Fleet's own waiting sentence, if present and nothing more specific was added.
        if (bits.Count == 0)
        {
            var ex = Obj(w, "execution");
            string wr = ex != null ? S(ex, "waiting_reason") : "";
            if (!string.IsNullOrEmpty(wr))
                bits.Add(OneLine(wr));
        }

        if (bits.Count == 0) return "";
        string line = (ja ? "進まない理由: " : "Why waiting: ") + string.Join(" · ", bits.ToArray());
        if (IsBoosted(w)) line = T("boost_mark") + " · " + line;
        return line;
    }

    Border Pill(string text, string kind)
    {
        var color = Theme.Br(Theme.KindColor(kind, _dark));
        var b = new Border();
        b.Background = Brushes.Transparent;
        b.BorderBrush = color; b.BorderThickness = new Thickness(1);
        // Tight rounded-rect tag (not a CornerRadius=999 stadium oval). The oval shape left
        // empty space inside the frame on each side of a short label like "完了", which read as a
        // floating "枠"/gap between the chip and the goal text. A small radius hugs the label.
        b.CornerRadius = new CornerRadius(Theme.RadChip);
        b.Padding = new Thickness(6, 1, 6, 1);
        b.VerticalAlignment = VerticalAlignment.Center;
        var t = new TextBlock();
        t.Text = text; t.Foreground = color;
        t.FontSize = 11.5; t.FontWeight = FontWeights.SemiBold;
        b.Child = t;
        return b;
    }

    // Feature 2: Pill() itself carries no external spacing (callers position it inside a DockPanel/
    // StackPanel that already spaces siblings). A WrapPanel does not, so give each header chip a
    // small right/bottom margin -- small helper instead of repeating the Thickness at every call site.
    Border ChipMargin(Border pill)
    {
        pill.Margin = new Thickness(0, 0, 6, 4);
        return pill;
    }

    // Collapse newlines/tabs/runs of whitespace into single spaces -- for the one-line card result.
    static string OneLine(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length);
        bool lastSpace = false;
        foreach (char c in s)
        {
            char ch = (c == '\n' || c == '\r' || c == '\t') ? ' ' : c;
            if (ch == ' ') { if (lastSpace) continue; lastSpace = true; }
            else lastSpace = false;
            sb.Append(ch);
        }
        return sb.ToString().Trim();
    }

    // Task 3: clean agent result text for display only (never touch logs/transcript).
    // Normalizes line endings, trims lines, drops empty and preamble-only lines,
    // removes adjacent duplicate lines, and trims a trailing lone "DONE" if redundant.
    static readonly string[] _resultPreambleTokens = {
        "desktopfile操作", "browser操作", "computeruse", "Copilot", "エージェント"
    };
    static string CleanAgentResultForUi(string raw)
    {
        if (string.IsNullOrEmpty(raw)) return "";
        // Normalize CRLF/CR to LF, split, trim each line, drop empty.
        string normalized = raw.Replace("\r\n", "\n").Replace("\r", "\n");
        string[] parts = normalized.Split('\n');
        var lines = new List<string>();
        foreach (string p in parts)
        {
            string t = p.Trim();
            if (t.Length == 0) continue;
            // drop lines that are only a known preamble token (case-insensitive exact match)
            bool isPreamble = false;
            foreach (string tok in _resultPreambleTokens)
            {
                if (string.Equals(t, tok, StringComparison.OrdinalIgnoreCase))
                { isPreamble = true; break; }
            }
            if (isPreamble) continue;
            lines.Add(t);
        }
        // Remove repeated identical adjacent lines.
        var deduped = new List<string>();
        for (int i = 0; i < lines.Count; i++)
        {
            if (deduped.Count == 0 || lines[i] != deduped[deduped.Count - 1])
                deduped.Add(lines[i]);
        }
        // If the final line is exactly "DONE" and previous line already ends with "DONE", drop it.
        if (deduped.Count >= 2
            && deduped[deduped.Count - 1] == "DONE"
            && deduped[deduped.Count - 2].EndsWith("DONE"))
        {
            deduped.RemoveAt(deduped.Count - 1);
        }
        if (deduped.Count == 0) return "";
        var sb2 = new StringBuilder();
        for (int i = 0; i < deduped.Count; i++)
        {
            if (i > 0) sb2.Append('\n');
            sb2.Append(deduped[i]);
        }
        return sb2.ToString();
    }

    UIElement Dots()
    {
        var sp = new StackPanel(); sp.Orientation = Orientation.Horizontal;
        sp.VerticalAlignment = VerticalAlignment.Center; sp.Margin = new Thickness(8, 0, 0, 0);
        for (int i = 0; i < 3; i++)
        {
            var dot = new System.Windows.Shapes.Ellipse();
            dot.Width = 6; dot.Height = 6; dot.Fill = Muted; dot.Margin = new Thickness(2, 0, 0, 0);
            var anim = new DoubleAnimation(0.25, 1.0, new Duration(TimeSpan.FromMilliseconds(600)));
            anim.AutoReverse = true; anim.RepeatBehavior = RepeatBehavior.Forever;
            anim.BeginTime = TimeSpan.FromMilliseconds(i * 150);
            dot.BeginAnimation(UIElement.OpacityProperty, anim);
            sp.Children.Add(dot);
        }
        return sp;
    }

    // Disclosure chevron: '>' collapsed, 'v' expanded -- drawn as vector geometry to match the
    // Material-Symbols-as-paths aesthetic (the glyph subset has no chevron). Clicking toggles
    // this worker's detail and re-renders immediately. e.Handled stops the click from bubbling
    // to the card's open-conversation handler.
    UIElement ChevronToggle(string name, bool expanded)
    {
        var hit = new Border();
        hit.Background = Brushes.Transparent;            // whole padded area is the hit target
        hit.Padding = new Thickness(8, 8, 6, 8);
        hit.MinWidth = 24; hit.MinHeight = 28;           // ~24x28 px target -- tighter caret->chip, still easy to click
        hit.Cursor = Cursors.Hand;
        hit.VerticalAlignment = VerticalAlignment.Center;
        hit.HorizontalAlignment = HorizontalAlignment.Center;
        hit.ToolTip = _lang == 0
            ? (expanded ? "詳細を閉じる" : "詳細を開く（最新の進捗・割り込み）")
            : (expanded ? "Collapse details" : "Expand (latest progress + steer)");

        var path = new System.Windows.Shapes.Path();
        path.Data = Geometry.Parse("M 0,0 L 4,4 L 0,8");   // a '>' caret
        path.Stroke = Muted; path.StrokeThickness = 1.6;
        path.StrokeStartLineCap = PenLineCap.Round;
        path.StrokeEndLineCap = PenLineCap.Round;
        path.StrokeLineJoin = PenLineJoin.Round;
        path.Width = 7; path.Height = 11; path.Stretch = Stretch.None;
        path.VerticalAlignment = VerticalAlignment.Center;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        if (expanded)
        {
            path.RenderTransformOrigin = new Point(0.5, 0.5);
            path.RenderTransform = new RotateTransform(90);   // '>' -> 'v'
        }
        hit.Child = path;

        string nm = name;
        // Fire on button-DOWN so the chevron beats the ListBoxItem's selection handling, which
        // captures the mouse on DOWN and would otherwise swallow our UP. PreviewMouseLeftButtonDown
        // runs in the tunnel before the item sees it; e.Handled stops the open-conversation handler.
        hit.PreviewMouseLeftButtonDown += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
            ToggleExpand(nm);
        };
        // Also swallow the button-UP: the toggle drives on DOWN, but the card opens the conversation
        // on MouseLeftButtonUp -- a DOWN-only Handled does NOT stop the separate UP event, so the UP
        // would bubble to card.MouseLeftButtonUp -> OpenWorker and wrongly open main. Consume it here
        // (same pattern as openLink / SwallowMouseUp elsewhere in this file).
        hit.PreviewMouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
        {
            e.Handled = true;
        };
        return hit;
    }

    // Flip one worker's detail and refresh the virtualizing list. With virtualization the old
    // "find the card in _cards.Children" trick no longer applies (only ~10-20 cards are realized),
    // so we rebuild the lightweight row models and reassign -- cheap, because only visible rows
    // realize. Scroll offset is preserved via ListScroller(), and _lastSig is re-synced to the
    // new expanded state so the next 700ms tick doesn't immediately trigger a redundant rebuild.
    void ToggleExpand(string name)
    {
        if (_expanded.Contains(name)) _expanded.Remove(name);
        else _expanded.Add(name);
        if (_lastRoot == null) { _lastSig = ""; OnTick(null, null); return; }

        ScrollViewer sc = ListScroller();
        double off = (sc != null) ? sc.VerticalOffset : 0.0;
        SetRows(BuildRows(_lastRoot));
        if (off > 0.0)
        {
            double target = off;
            // Restore the (pixel-based) offset AFTER the VirtualizingStackPanel has re-measured the
            // full scroll extent. At Loaded priority the extent isn't final yet, so ScrollTo clamps
            // to ~0 -> the big upward jump when expanding a low card. Run at Background (after
            // layout), force a layout pass, then re-apply clamped to the scrollable range so the
            // toggled card stays put and its detail appears in place.
            Dispatcher.BeginInvoke(new Action(delegate
            {
                ScrollViewer s2 = ListScroller();
                if (s2 == null) return;
                if (_list != null) _list.UpdateLayout();
                double max = s2.ScrollableHeight;
                double t = (target < 0.0) ? 0.0 : (target > max ? max : target);
                s2.ScrollToVerticalOffset(t);
            }), System.Windows.Threading.DispatcherPriority.Background);
        }
        _lastSig = Sig(_lastRoot) + "|s" + _submittedSig;   // the same shape OnTick compares
    }

    // Markers that open scaffolding WE compose and append to an operator's own instruction --
    // fanout range headers, fan-in aggregation notes, subtask separators, continuation notes.
    // Mirrors tools/skill_lessons.py's _COMPOSED_FROM exactly (same list, same "cut at the
    // earliest one" rule) so a card headline and a generated Skill proposal agree on where an
    // operator's own words end. MEASURED without this: 69 stored goals render, verbatim, as one
    // of these markers and nothing else -- e.g. a card headlined only "【前回タスクの続き】" or
    // "【この会話が担当する範囲 — 全体の 1/9】". Both are literally true and completely useless:
    // every unrelated fanout run's first card says the same thing.
    static readonly string[] _ComposedFrom = new string[] {
        "【この会話が担当する範囲", "【分割実行の結果をまとめてください】",
        "--- サブタスク ", "【前回タスクの続き】"
    };

    // The fixed per-turn protocol preamble (relay/copilot_autopilot_relay.py's PROTOCOL) that is
    // prepended ahead of the operator's own goal for every turn sent to a worker. Anchored on its
    // literal opening and literal closing sentence -- both constants -- rather than the whole
    // block, because the text between them (OUTPUT_DISCIPLINE) is loaded from a file and can
    // change length without notice. A goal that doesn't start with this preamble is untouched.
    const string _PreambleStart = "【最重要】使えるツールは";
    const string _PreambleEndMark = "まず最初のステップを実行。";

    // The part of `goal` a person actually typed: composed scaffolding, then the fixed protocol
    // preamble, stripped. Mirrors tools/skill_lessons.py's operator_instruction() -- conservative
    // in the same direction: a goal carrying neither is returned unchanged, never emptied.
    string OperatorInstruction(string goal)
    {
        string text = goal ?? "";
        int cut = text.Length;
        foreach (string mark in _ComposedFrom)
        {
            int i = text.IndexOf(mark, StringComparison.Ordinal);
            if (i >= 0 && i < cut) cut = i;
        }
        string outp = text.Substring(0, cut).Trim();
        if (outp.Length == 0) outp = text.Trim();
        if (outp.StartsWith(_PreambleStart, StringComparison.Ordinal))
        {
            int endIdx = outp.IndexOf(_PreambleEndMark, StringComparison.Ordinal);
            if (endIdx >= 0)
            {
                int rest = endIdx + _PreambleEndMark.Length;
                if (rest < outp.Length && outp[rest] == '\n') rest++;
                if (rest + 5 <= outp.Length && outp.Substring(rest, 5) == "Goal:") rest += 5;
                while (rest < outp.Length && (outp[rest] == ' ' || outp[rest] == '\n')) rest++;
                if (rest < outp.Length) outp = outp.Substring(rest).Trim();
            }
        }
        return outp;
    }

    // A concise card/conversation title, in order of preference: the Copilot-generated conv_title
    // when present and not generic; else the issue heading derived from the goal (the first real
    // line after a "== ... issue ==" marker -- SWE-bench-shaped goals only); else the first
    // non-boilerplate line of the OPERATOR'S OWN instruction (composed scaffolding + the fixed
    // protocol preamble stripped via OperatorInstruction, above); else the raw goal. Trimmed so
    // long goal text never wrecks readability.
    string CardTitle(string convTitle, string goal)
    {
        // A generic Copilot auto-title ("会話" / "Chat" / "新しいチャット") carries no information,
        // so prefer the goal-derived heading -- otherwise every SWE worker card just reads "会話"
        // and w0..w7 are indistinguishable at a glance.
        string ct = (convTitle ?? "").Trim();
        bool genericCt = ct.Length <= 4 || ct == "会話" || ct == "新しいチャット"
                         || ct.Equals("Chat", StringComparison.OrdinalIgnoreCase)
                         || ct.Equals("New chat", StringComparison.OrdinalIgnoreCase);
        if (!string.IsNullOrEmpty(ct) && !genericCt) return Trunc(ct, 90);
        if (string.IsNullOrEmpty(goal)) return string.IsNullOrEmpty(ct) ? "" : Trunc(ct, 90);
        string[] lines = goal.Replace("\r", "").Split('\n');
        for (int i = 0; i < lines.Length; i++)
        {
            string ln = lines[i].Trim();
            if (ln.StartsWith("==") && ln.IndexOf("issue", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                for (int j = i + 1; j < lines.Length; j++)
                {
                    string h = lines[j].Trim();
                    if (h.Length > 0) return Trunc(h, 90);
                }
            }
        }
        // From here on, work off the operator's own instruction rather than the raw goal, so a
        // fanout/continuation goal's headline names the job instead of the scaffolding we glued
        // onto it.
        string opGoal = OperatorInstruction(goal);
        if (string.IsNullOrEmpty(opGoal)) opGoal = goal;
        string[] opLines = opGoal.Replace("\r", "").Split('\n');
        foreach (string l in opLines)
        {
            string t = l.Trim();
            if (t.Length > 0 && !t.StartsWith("==") && !t.StartsWith("あなたは")
                && !t.StartsWith("対象") && !t.StartsWith("この"))
                return Trunc(t, 90);
        }
        foreach (string l in opLines) { string t = l.Trim(); if (t.Length > 0) return Trunc(t, 90); }
        return "";
    }
}
