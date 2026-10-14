// FleetCockpit.History.cs -- history and clear: history header/rows, LoadHistory/SaveHistory, ClearHistory, archive.
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

    // The history SECTION HEADER (Clear button + caption). Factored out of the old AppendHistory
    // so the virtualizing converter can build it for a Kind==2 row. The history ROWS are built
    // separately by HistoryRow() per Kind==3 row.
    UIElement HistoryHeader()
    {
        var head = new DockPanel();
        head.Margin = new Thickness(8, 16, 8, 4);
        var clear = new Button();
        clear.Content = T("clear_history") + " (" + _history.Count + ")";
        clear.Cursor = Cursors.Hand; clear.BorderThickness = new Thickness(1);
        clear.Background = BtnBg; clear.Foreground = Fg; clear.BorderBrush = Border;
        clear.Padding = new Thickness(12, 2, 12, 2); clear.FontSize = 12;
        clear.Click += delegate { ClearHistory(); };
        DockPanel.SetDock(clear, Dock.Right);
        head.Children.Add(clear);

        // P2 (b): live search box. Docked right of the caption; typing filters the rows below with a
        // ~300ms debounce (so it doesn't rebuild on every keystroke). Built ONCE and preserved across
        // renders (the header row's signature is static), so focus + caret survive a re-filter.
        if (_histSearchBox == null)
        {
            _histSearchBox = new TextBox();
            System.Windows.Automation.AutomationProperties.SetAutomationId(_histSearchBox, "histSearch");
            _histSearchBox.Text = _histQuery ?? "";
            _histSearchBox.Width = 160; _histSearchBox.FontSize = 12;
            _histSearchBox.Padding = new Thickness(6, 2, 6, 2);
            _histSearchBox.VerticalContentAlignment = VerticalAlignment.Center;
            _histSearchBox.BorderThickness = new Thickness(1);
            _histSearchBox.ToolTip = T("hist_search");
            _histSearchBox.TextChanged += delegate { OnHistSearchChanged(); PaintHistHint(); };
            _histSearchBox.GotKeyboardFocus += delegate { PaintHistHint(); };
            _histSearchBox.LostKeyboardFocus += delegate { PaintHistHint(); };
        }
        _histSearchBox.Background = BtnBg; _histSearchBox.Foreground = Fg; _histSearchBox.BorderBrush = Border;
        // The box is cached so focus and caret survive a re-filter, but the wrapper around it is
        // not: this method runs again whenever the list re-realizes the header row (virtualization
        // recycling, DataContext change), and handing an element that still has a logical parent
        // to a second Border throws InvalidOperationException. That escapes through a binding
        // converter, where nothing catches it, and WPF aborts the process -- the cockpit was
        // dying mid-run. Disconnect from the previous wrapper first.
        // BOTH KINDS OF PARENT. The box used to sit directly in a Border, so a Decorator cast
        // found it; it now sits in a Grid alongside its placeholder, and a Decorator cast returns
        // null for a Panel -- which would put the crash this guard exists to prevent straight
        // back. Detaching from whichever it is keeps the guard honest as the tree changes.
        var prevDec = LogicalTreeHelper.GetParent(_histSearchBox) as Decorator;
        if (prevDec != null) prevDec.Child = null;
        var prevPanel = LogicalTreeHelper.GetParent(_histSearchBox) as Panel;
        if (prevPanel != null) prevPanel.Children.Remove(_histSearchBox);
        // A TEXTBOX WITH NOTHING IN IT IS NOT A CONTROL, IT IS A RECTANGLE. This one had a
        // tooltip and nothing else -- no label, no placeholder, no icon -- so on the screen it
        // read as a box that had failed to draw. WPF has no placeholder, so the standard shape
        // is a hint drawn behind the text and hidden the moment there is any.
        if (_histHint == null)
        {
            _histHint = new TextBlock();
            _histHint.FontSize = 12;
            _histHint.IsHitTestVisible = false;
            _histHint.VerticalAlignment = VerticalAlignment.Center;
            _histHint.Margin = new Thickness(8, 0, 0, 0);
        }
        _histHint.Text = _lang == 0 ? "絞り込み" : "Filter";
        _histHint.Foreground = Theme.Br(Theme.Faint(_dark));
        var hintHost = LogicalTreeHelper.GetParent(_histHint) as Panel;
        if (hintHost != null) hintHost.Children.Remove(_histHint);

        var searchStack = new Grid();
        searchStack.Children.Add(_histSearchBox);
        searchStack.Children.Add(_histHint);
        var searchWrap = new Border { Child = searchStack, Margin = new Thickness(8, 0, 8, 0) };
        DockPanel.SetDock(searchWrap, Dock.Right);
        head.Children.Add(searchWrap);
        PaintHistHint();

        // A HEADING AND ITS HELP TEXT, NOT ONE SENTENCE IN ONE WEIGHT. What this section IS and
        // how it BEHAVES were set in the same 12.5px muted grey, so the eye had to read the whole
        // line to find the one word that names the section.
        var ht = new TextBlock();
        ht.VerticalAlignment = VerticalAlignment.Center;
        var htName = new System.Windows.Documents.Run(_lang == 0 ? "履歴" : "History");
        htName.FontWeight = FontWeights.SemiBold;
        htName.Foreground = Fg;
        ht.Inlines.Add(htName);
        var htNote = new System.Windows.Documents.Run(_lang == 0
            ? "  クリアするまで蓄積、クリックで会話を表示"
            : "  stacks until cleared, click to open");
        htNote.Foreground = Theme.Br(Theme.Faint(_dark));
        ht.Inlines.Add(htNote);
        ht.FontSize = 12.5;
        head.Children.Add(ht);
        return head;
    }

    // Show the hint only while the box is genuinely empty. Not tied to focus alone: a focused
    // empty box still needs to say what it filters, and a box with text in it must not have a
    // second string underneath.
    void PaintHistHint()
    {
        if (_histHint == null) return;
        bool empty = _histSearchBox == null || string.IsNullOrEmpty(_histSearchBox.Text);
        _histHint.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
    }

    // Debounced history-search handler: stash the query, then (re)arm a ~300ms one-shot timer that
    // triggers a single re-render. Prevents a rebuild-per-keystroke thrash while staying live.
    void OnHistSearchChanged()
    {
        if (_histSearchBox == null) return;
        _histQuery = _histSearchBox.Text ?? "";
        if (_histSearchTimer == null)
        {
            _histSearchTimer = new DispatcherTimer();
            _histSearchTimer.Interval = TimeSpan.FromMilliseconds(300);
            _histSearchTimer.Tick += delegate { _histSearchTimer.Stop(); ForceRender(); };
        }
        _histSearchTimer.Stop();
        _histSearchTimer.Start();
    }

    // Divider that separates the live (active/queued) cards above from this run's TERMINAL workers
    // below -- so the top of the list is only what's still running.
    UIElement CompletedDivider()
    {
        int n = 0;
        if (_toolbarShown != null)
            foreach (Dictionary<string, object> w in _toolbarShown)
                if (IsTerminalWorker(w)) n++;
        var head = new DockPanel();
        head.Margin = new Thickness(8, 16, 8, 4);
        var all = new Button();
        all.Content = T("all_to_history");
        all.Cursor = Cursors.Hand; all.BorderThickness = new Thickness(1);
        all.Background = BtnBg; all.Foreground = Fg; all.BorderBrush = Border;
        all.Padding = new Thickness(12, 2, 12, 2); all.FontSize = 12;
        all.ToolTip = _lang == 0 ? "完了をすべて履歴へ移動" : "Move all completed to history";
        all.Click += delegate { ArchiveAllTerminal(); };
        DockPanel.SetDock(all, Dock.Right);
        head.Children.Add(all);
        var t = new TextBlock();
        t.Text = (_lang == 0 ? "完了 — " : "Completed — ") + n + (_lang == 0 ? " 件（このラン）" : " (this run)");
        t.Foreground = Muted; t.FontSize = 12.5; t.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(t);
        return head;
    }

    Border HistoryRow(Dictionary<string, object> e)
    {
        string status = S(e, "status");
        bool internalControl = IsLocalLoopControlGoal(S(e, "goal"));
        string ck = ColorKey(status);
        string conv = S(e, "conv_url");
        // Default COLLAPSED, exactly like a live card: a terminal worker that scrolls down into
        // History must NOT spring open. The disclosure key is the archive `key` (started#name),
        // which never collides with a live worker name (those carry no '#'), so it can ride the
        // same _expanded set / ToggleExpand path as live cards.
        string hkey = S(e, "key");
        bool isOpen = !string.IsNullOrEmpty(hkey) && _expanded.Contains(hkey);

        var row = new Border();
        row.BorderThickness = new Thickness(1);
        row.BorderBrush = Border; row.Background = CardBg;
        row.CornerRadius = new CornerRadius(Theme.RadCard);
        row.Padding = new Thickness(16, 8, 16, 8); row.Margin = new Thickness(8, 3, 8, 3);

        var col = new StackPanel();
        var dp = new DockPanel();
        // chevron disclosure (only when there is a key to toggle)
        if (!string.IsNullOrEmpty(hkey))
        {
            var chev = ChevronToggle(hkey, isOpen);
            DockPanel.SetDock(chev, Dock.Left);
            dp.Children.Add(chev);
        }
        string hcanon = status == "ready" ? "waiting" : status;
        var pill = internalControl
            ? Pill(_lang == 0 ? "内部制御" : "Internal control", "neutral")
            : Pill(Theme.StatusLabel(hcanon, _lang), Theme.StatusKind(hcanon));
        pill.Margin = new Thickness(0, 0, 5, 0);
        DockPanel.SetDock(pill, Dock.Left);
        dp.Children.Add(pill);
        var turns = new TextBlock();
        turns.Text = T("turn") + " " + I(e, "turn");
        turns.Foreground = Muted; turns.FontSize = 11.5;
        turns.VerticalAlignment = VerticalAlignment.Center;
        turns.HorizontalAlignment = HorizontalAlignment.Right;
        DockPanel.SetDock(turns, Dock.Right);
        dp.Children.Add(turns);
        // Collapsed: the concise headline (conv_title if captured, else derived) -- single line,
        // ellipsis-trimmed -- so History matches the live collapsed card instead of dumping the
        // full goal text. The long goal only appears when this row is explicitly expanded.
        var head = new TextBlock();
        string histSummary = S(e, "goal_summary");
        head.Text = !string.IsNullOrEmpty(histSummary)
            ? histSummary : CardTitle(S(e, "conv_title"), S(e, "goal"));
        head.Foreground = Fg; head.FontSize = 13;
        head.VerticalAlignment = VerticalAlignment.Center;
        head.TextTrimming = TextTrimming.CharacterEllipsis;
        dp.Children.Add(head);
        col.Children.Add(dp);

        if (isOpen)
        {
            // expanded: full goal, wrapped, as muted selectable text (mirrors the live card)
            var g = new TextBox();
            g.Text = S(e, "goal");
            g.Foreground = Muted; g.FontSize = 12.5;
            g.IsReadOnly = true; g.BorderThickness = new Thickness(0);
            g.Background = Brushes.Transparent; g.Padding = new Thickness(0);
            g.IsTabStop = false; g.TextWrapping = TextWrapping.Wrap;
            g.Margin = new Thickness(0, 6, 0, 2);
            SwallowMouseUp(g);
            col.Children.Add(g);
            if (!string.IsNullOrEmpty(S(e, "submitter")))
                col.Children.Add(new TextBlock { Text = (_lang == 0 ? "提出者: " : "Submitted by: ") + S(e, "submitter"),
                                                 Foreground = Muted, FontSize = 11.5, Margin = new Thickness(0, 2, 0, 0) });
            if (!string.IsNullOrEmpty(S(e, "transcript"))) col.Children.Add(ReviewerLedgerPanel(S(e, "transcript")));

            // "続ける" (Continue): send a FOLLOW-UP instruction to this finished task, carrying its
            // prior context. Launches a FRESH fleet run whose goal PREPENDS the prior goal + a note
            // to re-read the on-disk outputs (no reopenable URL for this agent). Capture e's values
            // into locals BEFORE the delegate for closure-safety (same pattern as the row handler).
            string contPrior = S(e, "goal");
            string contConv = S(e, "conv_url");
            var contBtn = new Button();
            contBtn.Content = _lang == 0 ? "続ける" : "Continue";
            contBtn.Cursor = Cursors.Hand;
            contBtn.FontSize = 12;
            contBtn.Padding = new Thickness(12, 3, 12, 3);
            contBtn.Margin = new Thickness(0, 6, 0, 0);
            contBtn.HorizontalAlignment = HorizontalAlignment.Right;
            contBtn.BorderThickness = new Thickness(1);
            contBtn.Background = BtnBg;
            contBtn.BorderBrush = Border;
            contBtn.Foreground = Fg;
            contBtn.Template = FlatButtonTemplate();
            contBtn.Click += delegate
            {
                string fu = PromptFollowup();
                if (string.IsNullOrEmpty(fu)) return;
                string goalText = ContinueText(contPrior, fu, contConv);
                // The continuation goal is MULTI-LINE; SpawnFleet's GoalsToJsonl now escapes a
                // plain multi-line goal string safely on its own, so no manual serialization is
                // needed for the common case. Only pre-serialize here when there's an EXTRA key
                // beyond "text" to carry (resume_conv) -- GoalsToJsonl detects an already-JSON
                // goal string and passes it through as-is instead of double-wrapping it.
                var glist = new List<string>();
                if (!string.IsNullOrEmpty(contConv))
                {
                    var gd = new Dictionary<string, object>();
                    gd["text"] = goalText;
                    gd["resume_conv"] = contConv;
                    glist.Add(_js.Serialize(gd));
                }
                else
                {
                    glist.Add(goalText);
                }
                SpawnFleet(glist, "continue_input.txt");
                if (_startNote != null)
                {
                    _startNote.Text = _lang == 0
                        ? "前回タスクの続きを開始しました（成果物を読み直してから追加指示を実行します）。"
                        : "Started a continuation of the prior task (it re-reads the saved outputs, then runs the follow-up).";
                }
            };
            if (!internalControl) col.Children.Add(contBtn);
        }

        row.Child = col;
        // A history row is openable when we have ANY way to reach its body: a real conv_url, the
        // disk transcript path, or the worker name (the chat resolves the .jsonl by name). conv_url
        // is empty in the final snapshot / for the new agent, so keying ONLY on it left completed
        // conversations un-clickable -- the body was on disk the whole time. (friction #20)
        string htx = S(e, "transcript"), hnm = S(e, "name");
        if (!string.IsNullOrEmpty(conv) || !string.IsNullOrEmpty(htx) || !string.IsNullOrEmpty(hnm))
        {
            row.Cursor = Cursors.Hand;
            string url = conv, tx = htx, nm = hnm;
            row.MouseLeftButtonUp += delegate { FlashOpen(row); OpenHistory(nm, url, tx); };
        }
        return row;
    }

    // ── persistent history: finished/released tasks stack until cleared ───────────
    // "COULD NOT READ IT" AND "THERE IS NOTHING TO READ" ARE NOT THE SAME ANSWER, and this pair
    // of methods treated them as one. LoadHistory emptied _history first and swallowed every
    // failure, so a load that failed was indistinguishable from a first run -- and the next
    // SaveHistory then wrote the empty list over the file it had just failed to read.
    //
    // MEASURED 2026-09-08. The cockpit started at 18:21:10 with an empty _history. No worker
    // reached a terminal state for the next 24 minutes, so nothing saved and the file still held
    // its 44 rows at 18:44. At 18:45 a run archived its first worker, SaveHistory fired, and 44
    // rows became 1. Nothing reported anything. Set against 218 runs' worth of transcripts on
    // disk, the 44 rows that survived were themselves the remains of earlier rounds of this --
    // the oldest was from 03:40 the same morning.
    //
    // The trigger for that particular failed load is not established, and this does not pretend
    // to fix it. What it fixes is the consequence: a load that fails now refuses to let the
    // save destroy the evidence, and the unreadable file is kept under a dated name rather than
    // overwritten, so the next reader still has something to look at.
    bool _historyLoaded;          // false = the last LoadHistory could not read the file

    void LoadHistory()
    {
        _history = new List<object>();
        _archivedKeys = new System.Collections.Generic.HashSet<string>();
        _historyLoaded = false;
        try
        {
            // Absent is a legitimately empty history -- a first run. That is a successful load.
            if (!File.Exists(_historyPath)) { _historyLoaded = true; return; }
            var arr = _js.DeserializeObject(File.ReadAllText(_historyPath, Encoding.UTF8)) as object[];
            // Present but not an array is CORRUPT, not empty: do not claim it loaded.
            if (arr == null) return;
            foreach (object o in arr)
            {
                _history.Add(o);
                var d = o as Dictionary<string, object>;
                if (d != null && d.ContainsKey("key")) _archivedKeys.Add(S(d, "key"));
            }
            _historyLoaded = true;
        }
        catch (Exception) { }
        if (!_historyLoaded) PreserveUnreadableHistory();
    }

    // Keep what could not be read. Renaming rather than copying means the next save writes a new
    // file instead of appending to a damaged one, and the original bytes are still on disk for
    // whoever asks what went wrong.
    void PreserveUnreadableHistory()
    {
        try
        {
            if (!File.Exists(_historyPath)) return;
            string kept = _historyPath + ".unreadable-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
            if (!File.Exists(kept)) File.Move(_historyPath, kept);
        }
        catch (Exception) { }
    }

    void SaveHistory()
    {
        // A SAVE AFTER A FAILED LOAD IS A DELETION. _history holds only what this session
        // archived, so writing it over a file whose contents were never read replaces every
        // earlier row with the handful from this run. Refusing costs the current session's rows
        // until the next clean start; writing costs every row ever recorded.
        if (!_historyLoaded) return;
        try { File.WriteAllText(_historyPath, _js.Serialize(_history), new UTF8Encoding(false)); }
        catch (Exception) { }
    }
    // The STABLE per-run+worker key (run identity + worker name). `started` is absent from
    // some per-worker dicts, so it's read from the root snapshot. Matches the archive scheme.
    static string WorkerKey(string started, Dictionary<string, object> w)
    { return (started ?? "") + "#" + S(w, "name"); }

    void LoadHidden()
    {
        _hiddenKeys = new System.Collections.Generic.HashSet<string>();
        try
        {
            if (!File.Exists(_hiddenPath)) return;
            var arr = _js.DeserializeObject(File.ReadAllText(_hiddenPath, Encoding.UTF8)) as object[];
            if (arr == null) return;
            foreach (object o in arr)
                if (o != null) _hiddenKeys.Add(o.ToString());
        }
        catch (Exception) { }
    }

    void SaveHidden()
    {
        try
        {
            var list = new List<object>();
            foreach (string k in _hiddenKeys) list.Add(k);
            File.WriteAllText(_hiddenPath, _js.Serialize(list), new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    void ClearHistory()
    {
        // RECORD THE DECISION BEFORE TOUCHING THE DATA. Renaming the file aside protects the
        // BYTES; it does not protect the CHOICE, and those are not the same thing. .fleet/
        // socket_route.jsonl is append-only and still holds every conversation this button
        // discards, so a rebuild from it reconstructs exactly what was cleared -- which is what
        // happened on 2026-09-13: 3,469 rows the operator had deleted came back, perfectly, and
        // the tool that did it had no way to know it was undoing an instruction rather than
        // repairing a loss. This line is the way to know. tools/rebuild_history.py reads it as a
        // watermark and withholds everything at or before it.
        //
        // FIRST, not last: a clear interrupted between the rename and the log would leave the
        // history gone and the reason unrecorded, which is precisely the state this prevents.
        int clearedRows = _history.Count;
        try
        {
            File.AppendAllText(_clearedLogPath,
                _js.Serialize(new Dictionary<string, object> {
                    { "ts", NowUnix() },
                    { "iso", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") },
                    { "rows", clearedRows },
                    { "by", "cockpit/clear_history" },
                }) + "\n", new UTF8Encoding(false));
        }
        catch (Exception) { }
        _history.Clear(); _archivedKeys.Clear();
        // RENAMED, NOT DELETED -- the same rule PreserveUnreadableHistory already applies to
        // this exact file. The button still clears the history; the bytes are still on disk for
        // whoever asks what went wrong. .fleet/history.json went missing once and the only code
        // that could have removed it was this line, with no confirmation in front of it and
        // nothing left behind to read. An absent history is indistinguishable from a first run
        // by design (see LoadHistory), so nothing would have reported it either.
        try
        {
            if (File.Exists(_historyPath))
            {
                string kept = _historyPath + ".cleared-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
                if (File.Exists(kept)) File.Delete(_historyPath);
                else File.Move(_historyPath, kept);
            }
        }
        catch (Exception) { }
        try
        {
            var st = ReadStatus();
            bool runningFlag = st != null && st.ContainsKey("running") && Convert.ToBoolean(st["running"])
                               && !(st.ContainsKey("idle") && Convert.ToBoolean(st["idle"]));
            // A LIVE run updates status.json ~once/second. If "running" is true but the
            // file is STALE (the run crashed / was killed without writing a final snapshot,
            // e.g. a send failure), it is not actually live -- so allow clearing it.
            // Otherwise a dead run's card sticks forever and Clear appears to do nothing.
            double updated = 0;
            try { if (st != null && st.ContainsKey("updated")) updated = Convert.ToDouble(st["updated"]); }
            catch (Exception) { }
            bool stale = updated > 0 && (NowUnix() - updated) > 15;
            bool live = runningFlag && !stale;

            if (!live)
            {
                // Run is finished/dead: a clean reset -- blank the snapshot AND forget hides.
                File.WriteAllText(_statusPath,
                    "{\"total\":0,\"done_count\":0,\"running\":false,\"idle\":true,\"workers\":[]}",
                    new UTF8Encoding(false));
                _hiddenKeys.Clear();
                SaveHidden();
            }
            else
            {
                // Run is LIVE: the runner rewrites status.json each second, so we can't blank it
                // (it would just reappear, and would clobber running workers). Instead HIDE the
                // TERMINAL cards persistently -- the runner regenerating them won't un-hide them.
                // Running (non-terminal) workers are NEVER hidden: a live task vanishing is a hazard.
                object wo; string started = S(st, "started");
                if (st != null && st.TryGetValue("workers", out wo) && wo is object[])
                {
                    bool any = false;
                    foreach (object o in (object[])wo)
                    {
                        var w = o as Dictionary<string, object>;
                        if (w == null || !IsTerminalWorker(w)) continue;
                        if (_hiddenKeys.Add(WorkerKey(started, w))) any = true;
                    }
                    if (any) SaveHidden();
                }
            }
        }
        catch (Exception) { }
        ForceRender();
    }
    // The finished run's last archive pass. See the call site for why it exists and why it
    // may run only once. ArchiveTerminal is already idempotent through _archivedKeys; the
    // once-per-run key is what keeps a CLEARED history clear, because clearing empties
    // _archivedKeys and a per-tick pass would refill it from the frozen snapshot.
    void ArchiveRunTailOnce(Dictionary<string, object> root)
    {
        if (root == null) return;
        string started = S(root, "started");
        if (string.IsNullOrEmpty(started)) return;
        if (_tailArchivedRunStarted == started) return;
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        if (((object[])wo).Length == 0) return;
        _tailArchivedRunStarted = started;
        ArchiveTerminal(root);
    }

    void ArchiveTerminal(Dictionary<string, object> root)
    {
        object wo;
        if (!root.TryGetValue("workers", out wo) || !(wo is object[])) return;
        string started = S(root, "started");
        bool added = false;
        foreach (object o in (object[])wo)
        {
            var w = (Dictionary<string, object>)o;
            string status = S(w, "status");
            // MUST MATCH relay/relay_fleet.py's TERMINAL. `content_refused` was missing from
            // both terminal tests in this file while Python has set it since it was added, so
            // a worker that ended that way was never archived -- and worse, IsTerminalWorker
            // returning false for it stops MaybeAutoArchive for the WHOLE run, so one refused
            // goal could cost every other card in that run its history entry.
            bool terminal = status == "done" || status == "stuck" || status == "maxturns"
                            || status == "error" || status == "cancelled"
                            || status == "content_refused";
            if (!terminal) continue;
            string conv = S(w, "conv_url");
            // STABLE key per run+worker (conv_url is absent in the final snapshot, so
            // keying on it would double-archive the same task).
            string key = started + "#" + S(w, "name");
            if (_archivedKeys.Contains(key)) continue;
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = status;
            e["goal_summary"] = S(w, "goal_summary");
            e["conv_title"] = S(w, "conv_title");
            e["outcome"] = S(w, "outcome"); e["conv_url"] = conv;
            // Carry the disk transcript path + worker name so a HISTORY row can still show the
            // full body: conv_url is empty in the final snapshot (and for the new agent), so
            // without these a completed conversation strands on "本文はまだ取得できません" even
            // though the jsonl transcript exists on disk.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["submitter"] = S(w, "submitter");
            // CARRY THE VERDICT, NOT JUST THE STATUS. relay_fleet.py warns beside its own
            // panel ledger that anything reaching an analysis only through history.json is
            // "hostage to a second program choosing to carry the field" -- this is that
            // program, and it was not carrying this one.
            //
            // The worker keeps a three-state answer: null = never checked, false = the
            // acceptance check ran and failed, true = it passed. `status` cannot express
            // that: "done" covers both "proved" and "never asked". Dropping it here is why
            // the self-improvement dashboard reports verify_rate 0.0 -- it reads this file,
            // and the field has never been in it. Measured: 44 archived rows, none carrying
            // `verified`, while the live snapshot for the same worker carries it.
            //
            // run_id names the run so a row joins to its transcripts and to the other
            // ledgers; `key` is "<epoch>#<worker>", unique per row and joining to nothing.
            // BOTH ARCHIVE SITES GET THIS. Fixing one and leaving the other is the exact
            // shape of the misses this file already records.
            e["verified"] = w.ContainsKey("verified") ? w["verified"] : null;
            e["verify_attempts"] = I(w, "verify_attempts");
            e["run_id"] = S(w, "run_id");
            // THE ADMISSION-TIME ID (codex-plan item 1, 2026-09-09). Not the same thing as
            // run_id just above -- run_id names the SWEEP this worker ran in, jid names the
            // ADMITTED GOAL, unique per submission. BOTH ARCHIVE SITES GET THIS, same rule
            // as run_id's own comment: fixing one and leaving the other repeats the miss.
            e["jid"] = S(w, "jid");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            CarryTimeline(e, w, started);
            _history.Add(e);
            added = true;
        }
        if (added) SaveHistory();
    }

    // THE EXECUTION TIMELINE'S ONLY REAL SOURCE, and it was being dropped on the floor.
    //
    // The timeline panel has two modes: real phase transitions from `phase_events`, and a
    // two-point estimate derived from conversation turns, labelled "(会話ターンから推定)". The
    // real mode has never once fired for a past task -- measured on the live archive: 150 rows,
    // ZERO with phase_events. The fleet records them per worker, and both archive builders here
    // copied goal/status/outcome/transcript/turn and simply did not copy this one, so every
    // history row fell back to the estimate and showed 投入 and 終了 and nothing between.
    //
    // ONE HELPER, CALLED FROM BOTH. The two builders had already drifted into the identical
    // omission; adding the line twice would set them up to drift again the next time a field
    // is added.
    void CarryTimeline(Dictionary<string, object> e, Dictionary<string, object> w, string started)
    {
        object pe;
        if (w.TryGetValue("phase_events", out pe) && pe != null) e["phase_events"] = pe;
        // The run's start, so the timeline has an anchor even when a worker's own first event
        // is missing. Kept separate from "ts", which is when the row was ARCHIVED -- those are
        // different moments and conflating them is what made an end time wrong before.
        if (!string.IsNullOrEmpty(started)) e["run_started"] = started;
        object st;
        if (w.TryGetValue("started_ts", out st) && st != null) e["started_ts"] = st;
        object en;
        if (w.TryGetValue("ended_ts", out en) && en != null) e["ended_ts"] = en;
    }

    // Client-side archive of ONE worker into the persisted history + hide its card. Unlike the
    // per-worker 解放 (which writes a command file and needs a LIVE fleet to consume it), this works
    // with the fleet stopped -- so a finished or stale card can be moved to history any time.
    void _archiveOne(Dictionary<string, object> w)
    {
        if (w == null) return;
        string started = _lastRoot != null ? S(_lastRoot, "started") : "";
        string key = started + "#" + S(w, "name");
        if (!_archivedKeys.Contains(key))
        {
            _archivedKeys.Add(key);
            var e = new Dictionary<string, object>();
            e["key"] = key; e["goal"] = S(w, "goal"); e["status"] = S(w, "status");
            e["goal_summary"] = S(w, "goal_summary");
            e["conv_title"] = S(w, "conv_title"); e["outcome"] = S(w, "outcome");
            e["conv_url"] = S(w, "conv_url");
            // see _archiveTerminal: carry transcript path + name so the history row can show the
            // full disk transcript even when conv_url is empty.
            e["transcript"] = S(w, "transcript"); e["name"] = S(w, "name");
            e["submitter"] = S(w, "submitter");
            // Same three fields as the terminal-archive path above, and for the same
            // reason -- a row archived by hand must not be poorer than one archived
            // automatically, or the history depends on which route retired the worker.
            e["verified"] = w.ContainsKey("verified") ? w["verified"] : null;
            e["verify_attempts"] = I(w, "verify_attempts");
            e["run_id"] = S(w, "run_id");
            // THE ADMISSION-TIME ID (codex-plan item 1, 2026-09-09). Not the same thing as
            // run_id just above -- run_id names the SWEEP this worker ran in, jid names the
            // ADMITTED GOAL, unique per submission. BOTH ARCHIVE SITES GET THIS, same rule
            // as run_id's own comment: fixing one and leaving the other repeats the miss.
            e["jid"] = S(w, "jid");
            e["turn"] = I(w, "turn"); e["seq"] = _history.Count;
            e["ts"] = NowUnix();   // P2: archived-at timestamp -> date-group subheaders in History
            CarryTimeline(e, w, started);
            _history.Add(e);
        }
        _hiddenKeys.Add(WorkerKey(started, w));
    }

    void ArchiveAndHide(Dictionary<string, object> w) { _archiveOne(w); SaveHistory(); SaveHidden(); ForceRender(); }

    // Bulk: move every TERMINAL worker currently shown into history (the 完了-divider button).
    void ArchiveAllTerminal()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            if (IsTerminalWorker(w)) _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }

    // Bulk-clear EVERY shown worker -- used by Stop-all when the run has gone STALE (no live fleet to
    // consume a stop command). Unlike ArchiveAllTerminal this clears non-terminal cards too, because a
    // stale run's "running" workers are frozen leftovers, not actually executing. Makes Stop-all do
    // something visible even with the driver dead, instead of silently writing an unread command file.
    void ArchiveAllStale()
    {
        if (_toolbarShown == null) return;
        foreach (Dictionary<string, object> w in new List<Dictionary<string, object>>(_toolbarShown))
            _archiveOne(w);
        SaveHistory(); SaveHidden(); ForceRender();
    }
}
