// FleetCockpit.Conversation.cs -- the conversation view on a card: mini thread, steer and continue rows.
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

    //: How many recent entries the card lists. Three is the starting value, not a measurement --
    //: it is what fits beside the state and the controls without the card becoming the reading
    //: surface again. The rest are reachable by count and a link, never by scrolling here.
    const int MINI_THREAD_ENTRIES = 3;

    //: How much of an entry is shown so a reader can decide whether to open it. A line, not a
    //: paragraph: the question this answers is "is this worth reading", and a paragraph answers
    //: "what does it say", which is the other screen's job.
    const int MINI_THREAD_SUMMARY_CHARS = 110;

    // WHAT THE CARD SHOWS OF A CONVERSATION: enough to choose what to read, and no reading
    // surface of its own.
    //
    // THE DEFECT THIS REPLACES. Each turn was a TextBox capped at MaxHeight 90 with its own
    // scrollbar, inside a ScrollViewer capped at 240 with another, inside the card list's
    // scroller. Three scroll regions stacked in the same direction: to reach the second turn you
    // scrolled the first one to its end, then the panel, and the card moved under you while you
    // did it. Reported as 「開いてしまえば一番下までスクロールしないと次のが見られない」 --
    // read-shaped, not readable. An external review put the principle exactly: 「問題はスクロールの
    // 存在ではなく、同じ操作方向に異なる本文領域が重なっていること」.
    //
    // Raising the caps does not fix it and lowering them is the same thing smaller: a monitoring
    // card cannot host hundreds of lines without becoming the thing it was meant to summarise,
    // and one long-running worker would push every other card off the screen. So the card lists
    // WHAT HAPPENED and the full text opens where reading belongs -- the chat window, positioned
    // on the entry, which the review recommended over an in-card expansion (the card jumps),
    // a modal (it covers the monitor) and an unbounded body (one lane eats the view).
    //
    // Null when the transcript is empty or missing, as before.
    UIElement MiniThread(string transcriptPath, string workerName, string convUrl)
    {
        var turns = ReadLastTurns(transcriptPath, MINI_THREAD_ENTRIES);
        if (turns.Count == 0) return null;
        bool ja = _lang == 0;
        var panel = new StackPanel { Margin = new Thickness(0, 6, 0, 6) };

        int total = CountTurns(transcriptPath);
        // The listed entries are the LAST few, so the first of them sits at this offset in the
        // whole transcript. Clicking one has to name the entry the reader chose, not the n-th
        // of the three shown.
        int firstIndex = Math.Max(0, total - turns.Count);
        int shown = 0;
        foreach (var t in turns)
        {
            bool user = t.Item1 == "U";
            string body = (t.Item2 ?? "").Replace("\r", "");
            int lines = body.Length == 0 ? 0 : body.Split('\n').Length;
            string summary = FirstMeaningfulLine(body, MINI_THREAD_SUMMARY_CHARS);

            var row = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
            var head = new TextBlock
            {
                Text = user ? (ja ? "指示 / あなた" : "Instruction / You") : (ja ? "エージェント" : "Agent"),
                FontSize = 11,
                FontWeight = FontWeights.SemiBold,
                Foreground = user ? Accent : Muted,
                Margin = new Thickness(0, 0, 0, 2),
            };
            row.Children.Add(head);

            // ONE LINE, AND IT DOES NOT SCROLL. TextTrimming rather than a height cap: a clipped
            // line is honest about being clipped, a scrollbox invites reading and then refuses.
            var line = new TextBlock
            {
                Text = summary,
                FontSize = 12,
                Foreground = Fg,
                TextTrimming = TextTrimming.CharacterEllipsis,
                TextWrapping = TextWrapping.NoWrap,
            };
            row.Children.Add(line);

            // THE SIZE IS THE REASON TO OPEN IT, so it is stated rather than implied by a
            // scrollbar the reader has to discover.
            if (lines > 1)
            {
                var size = new TextBlock
                {
                    Text = ja ? ("本文 " + lines + " 行") : (lines + " lines"),
                    FontSize = 11,
                    Foreground = Muted,
                    Margin = new Thickness(0, 2, 0, 0),
                };
                row.Children.Add(size);
            }

            // THE ENTRY IS THE CHOOSER. Clicking it opens the conversation in the chat window
            // AT THIS ENTRY -- the card decides what to read, the chat window is where reading
            // happens. Opening at the end would hand the reader the job of finding again what
            // they had just picked.
            int idx = firstIndex + shown;
            string wn = workerName, cu = convUrl;
            var hit = new Border
            {
                Child = row,
                Background = Brushes.Transparent,
                Cursor = Cursors.Hand,
                Padding = new Thickness(0, 4, 0, 4),
            };
            hit.ToolTip = ja ? "この発言をメインチャットで開く" : "Open this entry in the chat window";
            hit.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e)
            {
                e.Handled = true;
                OpenWorker(wn, cu, idx);
            };
            panel.Children.Add(hit);
            shown++;
        }

        if (total > turns.Count)
        {
            var more = new TextBlock
            {
                Text = ja ? ("ほか " + (total - turns.Count) + " 件") : ((total - turns.Count) + " more"),
                FontSize = 11,
                Foreground = Muted,
                Margin = new Thickness(0, 0, 0, 0),
            };
            panel.Children.Add(more);
        }
        return panel;
    }

    // The first line worth showing: skips blanks and a lone markdown heading marker, and trims to
    // `max`. Not a summary -- a label. Producing a real summary is the agent's job, not the
    // card's, and a card that paraphrases would be inventing.
    static string FirstMeaningfulLine(string body, int max)
    {
        if (string.IsNullOrEmpty(body)) return "";
        foreach (string raw in body.Split('\n'))
        {
            string ln = raw.Trim();
            if (ln.Length == 0) continue;
            int h = 0; while (h < ln.Length && ln[h] == '#') h++;
            if (h > 0 && h < ln.Length && ln[h] == ' ') ln = ln.Substring(h + 1).Trim();
            ln = ln.Replace("**", "").Replace("`", "");
            if (ln.Length == 0) continue;
            return ln.Length > max ? ln.Substring(0, max) + "…" : ln;
        }
        return "";
    }

    // How many turns the transcript holds, so "ほか N 件" is a fact rather than a guess. Counts
    // the same lines ReadLastTurns keeps (meta and guid markers skipped); 0 on any failure,
    // which renders as no line at all rather than as a wrong number.
    int CountTurns(string transcriptPath)
    {
        try
        {
            if (string.IsNullOrEmpty(transcriptPath) || !File.Exists(transcriptPath)) return 0;
            int n = 0;
            foreach (string ln in File.ReadLines(transcriptPath))
            {
                if (ln.Length == 0) continue;
                if (ln.IndexOf("\"role\"", StringComparison.Ordinal) < 0) continue;
                n++;
            }
            return n;
        }
        catch (Exception) { return 0; }
    }

    // Last `n` (role, text) turns from a jsonl transcript (skips meta / guid marker lines).
    List<Tuple<string, string>> ReadLastTurns(string path, int n)
    {
        var all = new List<Tuple<string, string>>();
        try
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path)) return all;
            string[] lines;
            using (var fsr = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (var ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> o;
                try { o = _js.DeserializeObject(ln) as Dictionary<string, object>; } catch { continue; }
                if (o == null || !o.ContainsKey("role")) continue;
                string role = o["role"] != null ? o["role"].ToString() : "assistant";
                string text = (o.ContainsKey("text") && o["text"] != null) ? o["text"].ToString() : "";
                all.Add(Tuple.Create(role.StartsWith("user") ? "U" : "A", text));
            }
        }
        catch { }
        if (all.Count > n) all = all.GetRange(all.Count - n, n);
        return all;
    }

    // Feature 1: slim always-visible steer affordance on a COLLAPSED non-terminal card -- mid-run
    // steering used to require expanding the card first. One line: a borderless underline TextBox
    // + a small "↳" send glyph. Reuses TrySteerSend (see above) for the actual command write.
    //
    // Keystroke-safety (the reason this is more than a two-line TextBox): a LIVE worker's `last`/
    // `turn`/etc. stream every ~700ms sweep, which changes this card's RowSig and makes Card(w)
    // rebuild the WHOLE card -- including this box -- independent of whether the user is mid-type.
    // Three things make that survivable:
    //   1. Text is seeded from _steerDraft[name] on every build, and TextChanged writes back to
    //      it -- so a torn-down-and-rebuilt box never starts empty.
    //   2. _steerBoxRef[name] is updated to the newest TextBox instance on every build, and
    //      RenderCards' post-SetRows Dispatcher pass (Loaded priority, mirroring the existing
    //      scroll-restore pass right above it) re-Focus()es + re-seats the caret on whichever
    //      instance is current, so a rebuild mid-type does not drop focus either.
    //   3. GotFocus/LostFocus maintain _steerFocusWorker so that pass knows there IS a focus to
    //      restore, and to WHICH worker. LostFocus defers its "did focus really leave" check to
    //      ContextIdle -- strictly after the Loaded-priority restore -- so a teardown-triggered
    //      LostFocus on the OLD box (which fires before the new box is focused) can't race ahead
    //      and clear the tracked worker before the restore runs.
    UIElement CollapsedSteerRow(string name)
    {
        var outer = new DockPanel();
        outer.Margin = new Thickness(24, 5, 0, 0);
        // must not bubble to the card's whole-row open-conversation click handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var send = new Button();
        // Icon-only so the row stays one line. Was U+21B3, which is a typographic arrow and
        // not a control: nothing set its weight or its size but the fallback font.
        send.Content = MakeIcon("subdirectory_arrow_left", 14, Muted);
        send.ToolTip = _lang == 0 ? "送信" : "Send";
        send.Background = Brushes.Transparent; send.Foreground = Muted;
        send.BorderThickness = new Thickness(0); send.Cursor = Cursors.Hand; send.FontSize = 13;
        send.Padding = new Thickness(6, 0, 2, 0);
        DockPanel.SetDock(send, Dock.Right);
        outer.Children.Add(send);

        string nm = name;
        // input + placeholder watermark overlay (same idea as SteerRow's, compacted into one line)
        var inputGrid = new Grid();
        var tb = new TextBox();
        SwallowMouseUp(tb);   // selecting/typing must not trigger the card's open-conversation click
        tb.FontSize = 12; tb.Padding = new Thickness(4, 2, 4, 2);
        tb.BorderThickness = new Thickness(0, 0, 0, 1); tb.BorderBrush = Border;
        tb.Background = Brushes.Transparent; tb.Foreground = Fg; tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "このタスクへ追加指示（長時間タスクでは次の安全なターン境界で反映）"
                                : "Add an operator instruction (durable tasks apply it at the next safe turn boundary)";
        string draft;
        tb.Text = _steerDraft.TryGetValue(nm, out draft) ? draft : "";
        _steerBoxRef[nm] = tb;   // newest realized instance for this worker (used by the focus-restore pass)

        var placeholder = new TextBlock();
        placeholder.Text = T("steer_collapsed_placeholder");
        placeholder.Foreground = Muted; placeholder.FontSize = 12;
        placeholder.Padding = new Thickness(4, 2, 4, 2);
        placeholder.IsHitTestVisible = false;
        placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);

        Func<bool> trySend = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) ShowScaleToast(failReason);
                return false;
            }
            tb.Text = "";
            _steerDraft.Remove(nm);
            ShowScaleToast(T("steer_collapsed_ack"));
            return true;
        };
        send.Click += delegate { trySend(); };
        // PreviewKeyDown (tunnel), NOT KeyDown (bubble) -- matches _goalInput's own Enter
        // handling (line ~4358). MEASURED BUG: the operator typed a steer into a running
        // job's box and pressing Enter minimised the whole cockpit window instead of (or as
        // well as) sending it. This box used a bubbling KeyDown, so this control's own
        // Handled=true only stops OTHER BUBBLE handlers -- it can never run early enough to
        // stop a tunnel-phase handler above it, and it cannot stop a class handler anywhere
        // in the ancestor chain that listens with handledEventsToo=true (WPF's IsDefault-button
        // mechanism is exactly that shape). Handling it in Preview, at the box itself, marks
        // Handled during the tunnel pass -- before the bubble pass (and anything hooked to it)
        // ever runs -- which is the earliest point this control can act and the same guarantee
        // the composer already relies on.
        tb.PreviewKeyDown += delegate (object s2, KeyEventArgs e2)
        {
            if (e2.Key == Key.Return) { trySend(); e2.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            _steerDraft[nm] = tb.Text;
            placeholder.Visibility = string.IsNullOrEmpty(tb.Text) ? Visibility.Visible : Visibility.Collapsed;
        };
        tb.GotFocus += delegate { _steerFocusWorker = nm; };
        tb.LostFocus += delegate
        {
            // Defer the "did focus really move away" check to ContextIdle -- strictly lower
            // priority than the Loaded-priority restore pass in RenderCards, so if this LostFocus
            // fired only because the container was rebuilt out from under the user (not because
            // they clicked elsewhere), the restore pass has already re-focused the NEW box by the
            // time this runs, and stillOnSteer below comes back true (no-op).
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_steerFocusWorker != nm) return;
                TextBox cur = Keyboard.FocusedElement as TextBox;
                TextBox tracked;
                bool stillOnSteer = cur != null && _steerBoxRef.TryGetValue(nm, out tracked) && cur == tracked;
                if (!stillOnSteer) _steerFocusWorker = null;
            }), System.Windows.Threading.DispatcherPriority.ContextIdle);
        };
        outer.Children.Add(inputGrid);   // last child + LastChildFill -> fills remaining width
        return outer;
    }

    // ② steering: inject a mid-task instruction into this worker's conversation.
    // Task 6: restyled as a small composer -- rounded surfaceSubtle border, placeholder watermark,
    // neutral send button, post-send wording updated.
    UIElement SteerRow(string name)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 12, 0, 0);
        // clicks inside this row must not bubble to the card's open-conversation handler
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        // mini-composer wrapper border (matches bottom composer look, smaller)
        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(Theme.RadCard);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(12, 8, 12, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "送信" : "Send";
        // Task 6: neutral by default (not accent)
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        // honest feedback when there is no live fleet to consume the steer
        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        // input + placeholder watermark overlay
        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "このタスクへ追加指示（長時間タスクでは次の安全なターン境界で反映）"
                                : "Add an operator instruction (durable tasks apply it at the next safe turn boundary)";
        string savedDraft;
        tb.Text = _steerDraft.TryGetValue(name, out savedDraft) ? savedDraft : "";
        _steerBoxRef[name] = tb;
        // placeholder watermark text (hides when text is present)
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "このタスクに追加指示..." : "Add instruction to this task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string nm = name;
        // returns true iff the steer was actually sent; keeps the text + shows a note otherwise.
        // Reuses TrySteerSend (the ONE steer-send code path, shared with CollapsedSteerRow).
        Func<bool> trySteer = delegate
        {
            string failReason;
            if (!TrySteerSend(nm, tb.Text, out failReason))
            {
                if (failReason != null) note.Text = failReason;
                return false;
            }
            tb.Text = "";
            _steerDraft.Remove(nm);
            // Task 6: updated post-send wording
            note.Text = _lang == 0 ? "次のターンに送信しました" : "Queued for the next turn";
            return true;
        };
        send.Click += delegate { trySteer(); };
        // PreviewKeyDown, not KeyDown -- see the matching note on CollapsedSteerRow's box
        // above (same control family, same fix, same reason).
        tb.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { trySteer(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            _steerDraft[nm] = tb.Text;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }

    // "続ける" (Continue) inline composer for a FINISHED (done) card -- the SAME mini-composer UI as
    // SteerRow (割り込み). A done worker is terminal and can't take a steer, so instead of injecting
    // into a live worker this launches a FRESH continuation: BuildContinueGoal prepends the prior
    // goal + "re-read your on-disk outputs" so the agent recovers context, then runs the follow-up.
    // Written as ONE JSONL line by SpawnFleet's GoalsToJsonl (the prefix is multi-line; a plain
    // multi-line string would otherwise be split one bogus goal per source line).
    // No RunIsLive gate -- a continuation starts its own run.
    UIElement ContinueRow(string name, string goal, string conv)
    {
        var outer = new StackPanel();
        outer.Margin = new Thickness(0, 12, 0, 0);
        outer.MouseLeftButtonUp += delegate (object s, MouseButtonEventArgs e) { e.Handled = true; };

        var composerBorder = new Border();
        composerBorder.CornerRadius = new CornerRadius(Theme.RadCard);
        composerBorder.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        composerBorder.BorderBrush = Border;
        composerBorder.BorderThickness = new Thickness(1);
        composerBorder.Padding = new Thickness(12, 8, 12, 8);

        var dp = new DockPanel();

        var send = new Button();
        send.Content = _lang == 0 ? "続ける" : "Continue";
        send.Background = Brushes.Transparent; send.Foreground = Fg;
        send.BorderThickness = new Thickness(1); send.BorderBrush = Border;
        send.Padding = new Thickness(12, 4, 12, 4); send.Cursor = Cursors.Hand; send.FontSize = 12;
        send.FontWeight = FontWeights.SemiBold;
        DockPanel.SetDock(send, Dock.Right);
        dp.Children.Add(send);

        var note = new TextBlock();
        note.FontSize = 11.5; note.Foreground = Muted; note.TextWrapping = TextWrapping.Wrap;
        note.VerticalAlignment = VerticalAlignment.Center; note.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(note, Dock.Right);
        dp.Children.Add(note);

        var inputGrid = new Grid();
        var tb = new TextBox();
        tb.FontSize = 12.5; tb.Padding = new Thickness(4, 3, 4, 3);
        tb.BorderThickness = new Thickness(0); tb.Background = Brushes.Transparent; tb.Foreground = Fg;
        tb.CaretBrush = Fg;
        tb.ToolTip = _lang == 0 ? "この完了タスクに追加指示（前回の成果物を読み直してから実行）"
                                : "Follow-up to this finished task (re-reads its saved outputs first)";
        var placeholder = new TextBlock();
        placeholder.Text = _lang == 0 ? "完了タスクに続けて指示..." : "Continue this finished task...";
        placeholder.Foreground = Muted; placeholder.FontSize = 12.5;
        placeholder.Padding = new Thickness(4, 3, 4, 3);
        placeholder.VerticalAlignment = VerticalAlignment.Center;
        placeholder.IsHitTestVisible = false;
        inputGrid.Children.Add(placeholder);
        inputGrid.Children.Add(tb);
        dp.Children.Add(inputGrid);

        composerBorder.Child = dp;
        outer.Children.Add(composerBorder);

        string g = goal, c = conv;
        Func<bool> tryContinue = delegate
        {
            string t = (tb.Text ?? "").Trim();
            if (t.Length == 0) return false;
            string goalText = ContinueText(g, t, c);
            // SpawnFleet's GoalsToJsonl escapes a plain multi-line goal string safely on its
            // own; only pre-serialize here to carry the EXTRA resume_conv key (GoalsToJsonl
            // detects an already-JSON goal string and passes it through as-is).
            var glist = new List<string>();
            if (!string.IsNullOrEmpty(c))
            {
                var gd = new Dictionary<string, object>();
                gd["text"] = goalText;
                gd["resume_conv"] = c;
                glist.Add(_js.Serialize(gd));
            }
            else
            {
                glist.Add(goalText);
            }
            SpawnFleet(glist, "continue_input.txt");
            tb.Text = "";
            note.Text = _lang == 0 ? "続きを開始しました" : "Continuation started";
            return true;
        };
        send.Click += delegate { tryContinue(); };
        // PreviewKeyDown, not KeyDown -- see the matching note on CollapsedSteerRow's box.
        tb.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Return) { tryContinue(); e.Handled = true; }
        };
        tb.TextChanged += delegate
        {
            bool hasText = tb.Text != null && tb.Text.Length > 0;
            placeholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
            if (note.Text.Length > 0 && hasText) note.Text = "";
        };
        return outer;
    }
}
