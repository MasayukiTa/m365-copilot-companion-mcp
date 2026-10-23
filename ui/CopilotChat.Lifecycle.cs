// CopilotChat.Lifecycle.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Documents;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Web.Script.Serialization;

partial class ChatWindow
{
    // Re-render the open LOCAL conversation so per-message chrome (the copy-button tooltip) re-localizes
    // on a language toggle. Skip fleet/steer views: re-rendering those could re-fetch or drop steer mode.
    void RerenderActiveConversation()
    {
        if (_conv != null && _activeFleetUrl == null && _conv.Messages.Count > 0)
            OpenConversation(_conv);
    }

    // ── empty state (fresh window / after New chat) ──────────────────────────────
    // A quiet, centered block: muted one-line title, three suggestion chips (click ->
    // fill the composer + focus), and a "/ for commands" hint. Removed the instant the
    // first real message renders (RemoveEmptyState, called from AddUser/AddAssistantContainer),
    // re-shown by NewChat(). Built fresh each time so it always tracks the current language.
    FrameworkElement BuildEmptyState()
    {
        var stack = new StackPanel
        {
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            MaxWidth = 520, Margin = new Thickness(24, 80, 24, 24)
        };
        var title = new TextBlock
        {
            Text = T("empty_title"),
            FontSize = 15, TextAlignment = TextAlignment.Center, TextWrapping = TextWrapping.Wrap,
            HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 20)
        };
        SetRef(title, TextBlock.ForegroundProperty, "Muted");
        stack.Children.Add(title);

        var chips = new StackPanel { HorizontalAlignment = HorizontalAlignment.Center };
        chips.Children.Add(MakeSuggestChip(T("empty_s1")));
        chips.Children.Add(MakeSuggestChip(T("empty_s2")));
        chips.Children.Add(MakeSuggestChip(T("empty_s3")));
        stack.Children.Add(chips);

        var slash = new TextBlock
        {
            Text = T("empty_slash"),
            FontSize = 12, TextAlignment = TextAlignment.Center,
            HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 16, 0, 0)
        };
        SetRef(slash, TextBlock.ForegroundProperty, "Faint");
        stack.Children.Add(slash);
        return stack;
    }

    // One suggestion chip: SurfaceSubtle bg + 1px Border, small radius, translucent Hover overlay
    // on mouseover; clicking drops its text into the composer and focuses it.
    Border MakeSuggestChip(string text)
    {
        var tb = new TextBlock { Text = text, FontSize = 12.5, TextWrapping = TextWrapping.Wrap };
        SetRef(tb, TextBlock.ForegroundProperty, "Fg");
        var chip = new Border
        {
            Child = tb, CornerRadius = new CornerRadius(Theme.RadSmall), BorderThickness = new Thickness(1),
            Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(0, 4, 0, 4),
            Cursor = Cursors.Hand, HorizontalAlignment = HorizontalAlignment.Stretch
        };
        SetRef(chip, BackgroundProperty, "PanelAlt");   // SurfaceSubtle
        SetRef(chip, Border.BorderBrushProperty, "Border");
        chip.MouseEnter += delegate { SetRef(chip, BackgroundProperty, "Hover"); };
        chip.MouseLeave += delegate { SetRef(chip, BackgroundProperty, "PanelAlt"); };
        string t = text;
        chip.MouseLeftButtonUp += delegate { _input.Text = t; _input.CaretIndex = _input.Text.Length; _input.Focus(); };
        return chip;
    }

    // Render the empty state IFF there are no message children (safe to call redundantly).
    void ShowEmptyState()
    {
        if (_messages == null) return;
        if (_messages.Children.Count > 0) return;   // real content present -> never overlay it
        _emptyState = BuildEmptyState();
        _messages.Children.Add(_emptyState);
    }

    // Drop the empty state the moment real content arrives (single-append hook).
    void RemoveEmptyState()
    {
        if (_emptyState == null) return;
        _messages.Children.Remove(_emptyState);
        _emptyState = null;
    }

    void NewChat()
    {
        var newConv = new Conversation();
        new Thread((ThreadStart)delegate
        {
            try { BridgeCall("/new"); _pageConv = newConv; } catch { }
        }) { IsBackground = true }.Start();
        _conv = newConv;
        _conv.Ts = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        _all.Insert(0, _conv);
        _messages.Children.Clear();
        _emptyState = null;             // cleared with the children above; rebuild fresh below
        ShowEmptyState();               // fresh chat -> show the empty state again
        _activeFleetUrl = null; ShowRunState(null); RefreshSteerVisual();
        StopFollowingTranscript();      // a brand-new chat has nothing to live-follow
        RefreshConvList();
        _input.Focus();
    }

    void OpenConversation(Conversation c)
    {
        StopFollowingTranscript();      // leaving whatever was open -- its watcher must not linger
        _conv = c;
        // Persist which conversation is open NOW. SaveSettings only ran on theme / language /
        // zoom / sidebar changes, so nothing recorded the active conversation and the restore
        // on next launch had nothing to restore.
        SaveSettings();
        _messages.Children.Clear();
        // FIRST: the disk transcript. Reading the .jsonl is instant and works regardless of which
        // agent the bridge is currently on -- this is what actually makes a past FLEET chat openable.
        // The old path re-scraped live via the bridge, which times out for any conversation whose
        // agent the bridge isn't connected to (every registered conv was from a previous agent), so
        // NOT ONE past chat was retrievable. conv_url scrape is now only a last resort.
        if (c.Messages.Count == 0)
        {
            string tp = !string.IsNullOrEmpty(c.Transcript) ? c.Transcript
                        : (!string.IsNullOrEmpty(c.Name) && FleetConvIdentity.MayResolveByWorkerName(c.ConvUrl)
                            ? NewestTranscriptForWorker(c.Name) : "");
            if (!string.IsNullOrEmpty(tp))
            {
                c.Transcripts = FleetConvIdentity.MergeTranscriptLineage(c.Transcripts, null, tp);
                c.Transcript = FleetConvIdentity.LatestTranscript(c.Transcripts, tp);
                // The registry keeps only the head of an old row's goal (relay/fleet_retention.py
                // compaction); the whole text is the transcript's first line, and a follow-up is
                // addressed by it (ChatSend.DecideFleetSend), so restore it as the row is opened.
                c.Goal = FleetConvIdentity.MergeForward(c.Goal, TranscriptMetaGoal(c.Transcript));
                var tm = ReadTranscriptLineage(c.Transcripts);
                foreach (var mm in tm) c.Messages.Add(mm);
            }
        }
        // a registry/fleet conversation we haven't loaded yet -> pull it via /history
        if (c.Messages.Count == 0 && !string.IsNullOrEmpty(c.ConvUrl))
        {
            var note = new TextBlock { Text = T("loadingconv"), FontSize = 12.5, Margin = new Thickness(2, 2, 2, 12) };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            RefreshConvList();
            string url = c.ConvUrl;
            new Thread((ThreadStart)delegate { OpenFromFleet(url, c.Title ?? ""); }) { IsBackground = true }.Start();
            return;
        }
        // No local messages AND no conv_url -> there's nothing to fetch (a fleet conversation whose
        // URL was never captured). Show a clear, actionable note instead of a blank pane: the full
        // transcript IS reachable from the cockpit card (which resolves it by worker/transcript).
        if (c.Messages.Count == 0)
        {
            var note = new TextBlock
            {
                Text = _lang == 0
                    ? "この会話の本文はここからは取得できません。\n並列タスクの全文は、コックピットの該当カード（開く）から開くと表示されます。"
                    : "This conversation's body isn't fetchable here.\nOpen it from its cockpit card (Open) to see the full transcript.",
                FontSize = 12.5, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(2, 2, 2, 12)
            };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            RefreshConvList();
            return;
        }
        _activeFleetUrl = null; ShowRunState(null); RefreshSteerVisual();   // a normal local conversation is NOT steer mode
        foreach (var m in c.Messages) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
        // Re-arm live-follow for a re-opened fleet conversation whose messages were already
        // cached in 'c' -- without this, switching away and back would need a poll tick to
        // resume showing new turns, which is the exact defect this feature exists to remove.
        // A no-op (and stays stopped) for a plain local chat or a worker no longer live-tracked.
        MaybeFollowConversation(c, c.Transcript);
        RefreshConvList();
        if (!string.IsNullOrEmpty(c.ConvUrl))
            new Thread((ThreadStart)delegate
            {
                try { BridgeCall("/switch?url=" + Uri.EscapeDataString(c.ConvUrl)); _pageConv = c; } catch { }
            }) { IsBackground = true }.Start();
    }

    // A plain click on the trash used to delete the file immediately, with no confirmation
    // and nothing to undo -- one stray click on a small icon in a list of hundreds and the
    // record was gone. A confirmation dialog would be worse here (it is in the way of exactly
    // the bulk tidy-up the list invites), so the delete is deferred instead: the row leaves at
    // once and the file is removed only once the undo window closes.

    Conversation _pendingUndo;                 // removed from the list, file not yet deleted

    DispatcherTimer _undoTimer;

    void CommitPendingDelete()
    {
        var c = _pendingUndo;
        _pendingUndo = null; _pendingUndoIndex = -1;
        if (_undoTimer != null) { _undoTimer.Stop(); _undoTimer = null; }
        if (c == null) return;
        try { var p = Path_(c.Id); if (File.Exists(p)) File.Delete(p); } catch { }

        // A CONVERSATION LIVES IN FOUR PLACES AND THIS CLEARED ONE. Deleting the app's own
        // file left the row in the bridge's session store and the row in the fleet's
        // conversation list, so the cockpit kept listing it and opening it there brought the
        // whole conversation back into this app. Reported as: deleted from the main view, still
        // in the fleet, resurrected on click.
        //
        // Done here rather than in DeleteLocal so Undo still works: nothing is forgotten until
        // the undo window closes. Fire-and-forget on a background thread -- a registry that
        // cannot be reached must not hold up the UI, and the delete has already happened.
        var url = c.ConvUrl ?? "";
        var cid = c.Id ?? "";
        if (!string.IsNullOrEmpty(url) || !string.IsNullOrEmpty(cid))
            new Thread((ThreadStart)delegate
            {
                try
                {
                    BridgeCall("/forget?url=" + Uri.EscapeDataString(url)
                            + "&sid=" + Uri.EscapeDataString(cid));
                }
                catch { }
            }) { IsBackground = true }.Start();
    }

    void UndoPendingDelete()
    {
        var c = _pendingUndo;
        if (c == null) return;
        _pendingUndo = null;
        if (_undoTimer != null) { _undoTimer.Stop(); _undoTimer = null; }
        int at = _pendingUndoIndex;
        if (at < 0 || at > _all.Count) at = _all.Count;
        _all.Insert(at, c);
        _pendingUndoIndex = -1;
        OpenConversation(c);
        RefreshConvList();
        Toast(_lang == 0 ? "削除を取り消しました。" : "Deletion undone.");
    }

    void DeleteLocal(Conversation c)
    {
        // Any earlier pending delete is now settled -- only one can be undone at a time.
        CommitPendingDelete();
        // Clean up sidebar state for the deleted conversation.
        bool _sc = _pinned.Remove(c.Id) | _archived.Remove(c.Id) | _forcedToday.Remove(c.Id);
        if (_sc) SaveSidebarState();
        if (_renamingId == c.Id) _renamingId = null;   // clear any in-flight rename of the deleted conv
        // Remember where it sat so the next conversation opened is the NEIGHBOUR, not
        // whatever happens to be first on disk. Deleting several in a row otherwise threw
        // the user back to the top of the list every time.
        int was = _all.IndexOf(c);
        _all.Remove(c);
        // Hold the record for UndoWindow before touching the file.
        _pendingUndo = c; _pendingUndoIndex = was;
        _undoTimer = new DispatcherTimer { Interval = UndoWindow };
        _undoTimer.Tick += delegate { CommitPendingDelete(); };
        _undoTimer.Start();
        if (_conv.Id == c.Id)
        {
            if (_all.Count > 0)
            {
                int next = was;
                if (next >= _all.Count) next = _all.Count - 1;
                if (next < 0) next = 0;
                OpenConversation(_all[next]);
            }
            else { StopFollowingTranscript(); _conv = new Conversation(); _all.Add(_conv); _messages.Children.Clear(); }
        }
        RefreshConvList();
    }

    void HideBanner() { _banner.Visibility = Visibility.Collapsed; _signinBannerShown = false; }

    void ShowDeleteBanner(Conversation c)
    {
        _signinBannerShown = false;   // this is the delete banner, not the sign-in banner
        _bannerBody.Children.Clear();
        var raw = c.Untitled() ? T("newchat") : c.Title;
        var title = raw.Length > 24 ? raw.Substring(0, 24) + "…" : raw;
        var head = new TextBlock { Text = (_lang == 0 ? "「" + title + "」" : "\"" + title + "\"") + T("del_head"), FontWeight = FontWeights.SemiBold, FontSize = 13.5, Margin = new Thickness(0, 0, 0, 12), TextWrapping = TextWrapping.Wrap };
        SetRef(head, TextBlock.ForegroundProperty, "Fg");
        _bannerBody.Children.Add(head);
        _bannerBody.Children.Add(ModeButton(c, 1, T("m1t"), T("m1s")));
        _bannerBody.Children.Add(ModeButton(c, 2, T("m2t"), T("m2s")));
        _bannerBody.Children.Add(ModeButton(c, 3, T("m3t"), T("m3s")));
        var foot = new DockPanel { Margin = new Thickness(0, 12, 0, 0) };
        var note = new TextBlock { Text = T("del_note"), FontSize = 11.5, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap };
        SetRef(note, TextBlock.ForegroundProperty, "Muted");
        var cancel = new Button { Content = T("cancel"), BorderThickness = new Thickness(1), Cursor = Cursors.Hand, FontSize = 12.5, Padding = new Thickness(16, 5, 16, 6), FontWeight = FontWeights.SemiBold };
        SetRef(cancel, BackgroundProperty, "PanelAlt"); SetRef(cancel, ForegroundProperty, "Fg"); SetRef(cancel, Control.BorderBrushProperty, "Border");
        cancel.Click += delegate { HideBanner(); };
        DockPanel.SetDock(cancel, Dock.Right);
        foot.Children.Add(cancel); foot.Children.Add(note);
        _bannerBody.Children.Add(foot);
        _banner.Visibility = Visibility.Visible;
    }

    Button ModeButton(Conversation c, int mode, string title, string sub)
    {
        var sp = new StackPanel();
        var t = new TextBlock { Text = mode + ". " + title, FontWeight = FontWeights.SemiBold, FontSize = 13, TextWrapping = TextWrapping.Wrap };
        SetRef(t, TextBlock.ForegroundProperty, "Fg");
        var s = new TextBlock { Text = sub, FontSize = 11.5, Margin = new Thickness(0, 2, 0, 0), TextWrapping = TextWrapping.Wrap };
        SetRef(s, TextBlock.ForegroundProperty, "Muted");
        sp.Children.Add(t); sp.Children.Add(s);
        var b = new Button { Content = sp, HorizontalContentAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 3, 0, 3), Padding = new Thickness(12, 8, 12, 8), BorderThickness = new Thickness(1), Cursor = Cursors.Hand };
        SetRef(b, BackgroundProperty, "PanelAlt");
        SetRef(b, Control.BorderBrushProperty, _deleteMode == mode ? "Accent" : "Border");
        var cc = c;
        b.Click += delegate { _deleteMode = mode; SaveSettings(); HideBanner(); ExecuteDelete(cc, mode); };
        return b;
    }

    void ExecuteDelete(Conversation c, int mode)
    {
        var url = c.ConvUrl;
        var title = c.Title ?? "";   // the /chat history rows carry no id -> match by title
        DeleteLocal(c);
        if (mode == 1) { Toast(T("t_local")); return; }
        if (mode == 2)
        {
            if (!string.IsNullOrEmpty(url)) new Thread((ThreadStart)delegate { try { BridgeCall("/switch?url=" + Uri.EscapeDataString(url)); } catch { } }) { IsBackground = true }.Start();
            Toast(T("t_open"));
            return;
        }
        // mode 3: best-effort auto, fall back to "open in Copilot"
        if (string.IsNullOrEmpty(url)) { Toast(T("t_nourl")); return; }
        new Thread((ThreadStart)delegate
        {
            bool ok = false;
            try { var j = BridgeCall("/delete?url=" + Uri.EscapeDataString(url) + "&title=" + Uri.EscapeDataString(title)); ok = j != null && j.Contains("\"ok\": true"); } catch { }
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (ok) Toast(T("t_auto_ok"));
                else
                {
                    Toast(T("t_auto_fail"));
                    new Thread((ThreadStart)delegate { try { BridgeCall("/switch?url=" + Uri.EscapeDataString(url)); } catch { } }) { IsBackground = true }.Start();
                }
            }));
        }) { IsBackground = true }.Start();
    }

    // The plain toast states what happened; this one lets it be taken back. Shown after a
    // single-click delete, whose whole point is that it did not stop to ask first.
    void ToastUndo()
    {
        if (_pendingUndo == null) return;
        var row = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 12, 0, 4)
        };
        var tb = new TextBlock
        {
            Text = (_lang == 0 ? "一覧から削除しました（Copilot 側は残しています）。"
                               : "Removed from the list (kept on the Copilot side)."),
            FontSize = 12, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap
        };
        SetRef(tb, TextBlock.ForegroundProperty, "Muted");
        row.Children.Add(tb);
        var undo = new Button
        {
            Content = (_lang == 0 ? "元に戻す" : "Undo"),
            FontSize = 12, Cursor = Cursors.Hand, Margin = new Thickness(12, 0, 0, 0),
            Padding = new Thickness(8, 1, 8, 1), BorderThickness = new Thickness(1),
            Background = Brushes.Transparent
        };
        SetRef(undo, ForegroundProperty, "Fg");
        undo.Click += delegate { UndoPendingDelete(); row.Visibility = Visibility.Collapsed; };
        row.Children.Add(undo);
        _messages.Children.Add(row);
        StickToEnd();
    }

    void Toast(string text)
    {
        var tb = new TextBlock { Text = text, FontSize = 12, TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 12, 0, 4), TextWrapping = TextWrapping.Wrap };
        SetRef(tb, TextBlock.ForegroundProperty, "Muted");
        _messages.Children.Add(tb);
        StickToEnd();
    }
}
