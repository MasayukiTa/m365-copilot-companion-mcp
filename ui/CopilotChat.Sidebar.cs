// CopilotChat.Sidebar.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
    // ── sidebar state persistence (pinned / archived / collapsed) ────────────────
    // Schema: {"pinned":["id",...], "archived":["id",...], "forced_today":["id",...],
    //          "collapsed":{"pinned":false,"today":false,"fleet":false,"archived":true}}
    void LoadSidebarState()
    {
        try
        {
            if (string.IsNullOrEmpty(_sidebarStatePath) || !File.Exists(_sidebarStatePath)) return;
            string txt = File.ReadAllText(_sidebarStatePath, Encoding.UTF8);
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return;

            if (d.ContainsKey("pinned") && d["pinned"] is object[])
                foreach (object o in (object[])d["pinned"])
                    if (o != null) _pinned.Add(o.ToString());

            if (d.ContainsKey("archived") && d["archived"] is object[])
                foreach (object o in (object[])d["archived"])
                    if (o != null) _archived.Add(o.ToString());

            if (d.ContainsKey("forced_today") && d["forced_today"] is object[])
                foreach (object o in (object[])d["forced_today"])
                    if (o != null) _forcedToday.Add(o.ToString());

            if (d.ContainsKey("collapsed") && d["collapsed"] is Dictionary<string, object>)
            {
                var col = (Dictionary<string, object>)d["collapsed"];
                bool v;
                foreach (string key in new string[] { "pinned", "today", "fleet", "archived" })
                {
                    if (col.ContainsKey(key) && col[key] != null && bool.TryParse(col[key].ToString(), out v))
                        _sectionCollapsed[key] = v;
                }
            }
        }
        catch { }
    }

    void SaveSidebarState()
    {
        try
        {
            if (string.IsNullOrEmpty(_sidebarStatePath)) return;
            Directory.CreateDirectory(Path.GetDirectoryName(_sidebarStatePath));
            var pinnedArr = new List<object>(_pinned.Count);
            foreach (string s in _pinned) pinnedArr.Add(s);
            var archivedArr = new List<object>(_archived.Count);
            foreach (string s in _archived) archivedArr.Add(s);
            var forcedArr = new List<object>(_forcedToday.Count);
            foreach (string s in _forcedToday) forcedArr.Add(s);
            var col = new Dictionary<string, object>();
            foreach (var kv in _sectionCollapsed) col[kv.Key] = kv.Value;
            var state = new Dictionary<string, object>();
            state["pinned"]      = pinnedArr.ToArray();
            state["archived"]    = archivedArr.ToArray();
            state["forced_today"] = forcedArr.ToArray();
            state["collapsed"]   = col;
            File.WriteAllText(_sidebarStatePath, _cjs.Serialize(state), new UTF8Encoding(false));
        }
        catch { }
    }

    // Auto-archive heuristic: collapses the big historical pile (old eval/bench/SWE threads,
    // anything older than 3 days) into the collapsed Archived section. Applied ONLY after
    // _pinned / active-conv / _archived / _forcedToday checks in RefreshConvList -- those
    // always take precedence, so the open conv and manually-kept convs are never hidden.
    // Undated convs (Ts==0) are NOT force-archived here: a fleet registry entry whose ts
    // wasn't written yet would wrongly vanish. They fall through the classification ladder
    // (undated fleet -> Fleet runs; undated non-fleet -> the final else -> Archived). Returns
    // true to archive.
    bool IsAutoArchive(Conversation cc)
    {
        string title = (cc.Title ?? "").ToLowerInvariant();
        string[] keywords = new string[]
        {
            "eval", "bench", "swe", "matplotlib", "sphinx", "django",
            "instance_", "pass@", "対象リポジトリ", "git チェックアウト",
            "あなたは実在の", "solve", "grade"
        };
        foreach (string kw in keywords)
            if (title.IndexOf(kw, StringComparison.Ordinal) >= 0) return true;

        if (cc.Ts > 0)
        {
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var dt = epoch.AddSeconds(cc.Ts).ToLocalTime();
            if ((DateTime.Now - dt).TotalDays > 3) return true;   // older than 3 days -> archive
        }
        return false;
    }

    // ── sidebar list with rename / delete ───────────────────────────────────────
    void RefreshConvList()
    {
        // Sort the sidebar by RECENCY (newest last-activity first). Stable on equal Ts.
        var idx = new Dictionary<Conversation, int>();
        for (int i = 0; i < _all.Count; i++) idx[_all[i]] = i;
        _all.Sort(delegate (Conversation a, Conversation b)
        {
            int c = b.Ts.CompareTo(a.Ts);
            return c != 0 ? c : idx[a].CompareTo(idx[b]);
        });

        // 4-section partition with explicit precedence (per conv, top wins):
        //   a. _pinned                       -> Pinned
        //   b. ACTIVE conv (open one)        -> Recent       (never hide the open conversation)
        //   c. _archived (manual)            -> Archived     (manual archive always wins)
        //   d. _forcedToday (manual unarchive) -> Fleet runs if fleet else Recent (force visible)
        //   e. IsAutoArchive                 -> Archived     (applies to fleet too -> old runs collapse)
        //   f. Source == "fleet"             -> Fleet runs   (only recent fleet runs reach here)
        //   g. Ts>0 AND within last 3 local days -> Recent   (aligned with the >3-day auto-archive cutoff)
        //   h. else                          -> Archived
        var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        var recentStart = DateTime.Today.AddDays(-3);   // "Recent" = last 3 days; older -> Archived

        var pinnedList   = new List<Conversation>();
        var fleetList    = new List<Conversation>();
        var todayList    = new List<Conversation>();   // "Recent" section
        var archivedList = new List<Conversation>();

        foreach (var c in _all)
        {
            if (_pinned.Contains(c.Id))
            {
                pinnedList.Add(c);
            }
            // NOTE: the open conversation is NOT force-classified here -- it is classified by the
            // normal rules below (so it CAN be pinned/archived), and RenderSection always renders
            // the active conv even when its section is collapsed, so it is never hidden.
            else if (_archived.Contains(c.Id))
            {
                archivedList.Add(c);
            }
            else if (_forcedToday.Contains(c.Id))
            {
                if (c.Source == "fleet") fleetList.Add(c); else todayList.Add(c);
            }
            else if (IsAutoArchive(c))
            {
                archivedList.Add(c);
            }
            else if (c.Source == "fleet")
            {
                fleetList.Add(c);
            }
            else if (c.Ts > 0 && epoch.AddSeconds(c.Ts).ToLocalTime() >= recentStart)
            {
                todayList.Add(c);
            }
            else
            {
                archivedList.Add(c);
            }
        }

        _convList.Children.Clear();
        if (_historyNotice.Length > 0) _convList.Children.Add(MakeHistoryNotice(_historyNotice));

        // Render sections in order: Pinned, Recent, Fleet, Archived.
        // Header always shows when the section is non-empty (so a collapsed section can be expanded).
        // When a section is COLLAPSED, its rows are hidden EXCEPT the active conversation's row, which
        // always renders beneath the header -- otherwise the user couldn't click back to the open conv.
        RenderSection(pinnedList,   "sec_pinned",   "pinned",   false, false, true);
        RenderSection(todayList,    "sec_today",    "today",    false, false, false);
        RenderSection(fleetList,    "sec_fleet",    "fleet",    true,  false, false);
        RenderSection(archivedList, "sec_archived", "archived", false, true,  false);
        if (TranscriptsPending() > 0) _convList.Children.Add(MakeOlderRow(TranscriptsPending()));

        RefreshHeadTitle();   // keep the header title in sync with the active conversation (Wave 2)
    }

    // ITEM 3c: per-section "show all" override for the session. When a section holds >8 items we
    // render the first 8 + a muted "+N more" row; clicking it flips the section here (for the session)
    // so the full list renders. COLLAPSING the section (chevron) resets the override — MakeSectionHeader
    // clears the entry so re-expanding starts capped again.

    // How many rows a sidebar section shows before "+N more". Chosen in the cockpit settings popup
    // (settings.txt sidebar_section_cap, default 8; 0 = no cap) and re-read when that file changes.

    int SectionCap { get { return _sectionCap <= 0 ? int.MaxValue : _sectionCap; } }

    // Emits one section: header (if non-empty) + its rows. When collapsed, rows are skipped
    // EXCEPT the active conversation (cc.Id == _conv.Id), which always renders so the open
    // conversation is never unreachable behind a collapsed header.
    void RenderSection(List<Conversation> list, string labelKey, string collapseKey, bool isFleet, bool archived, bool isPinned)
    {
        if (list.Count == 0) return;
        _convList.Children.Add(MakeSectionHeader(T(labelKey), collapseKey, list.Count));
        bool collapsed = _sectionCollapsed.ContainsKey(collapseKey) && _sectionCollapsed[collapseKey];
        if (!collapsed)
        {
            // ITEM 3c: cap long sections (>8) to the first 8 + a "+N more" row that expands the full
            // list for the session. The active conversation is always rendered even if it falls past
            // the cap, so it is never hidden.
            bool expanded = _sectionExpanded.Contains(collapseKey);
            if (list.Count > SectionCap && !expanded)
            {
                int shown = 0;
                foreach (var c in list)
                {
                    if (shown < SectionCap) { AddConvRow(c, isFleet, archived, isPinned); shown++; }
                    else if (c.Id == _conv.Id) AddConvRow(c, isFleet, archived, isPinned);   // active conv always reachable
                }
                _convList.Children.Add(MakeShowMoreRow(collapseKey, list.Count - SectionCap));
            }
            else
            {
                foreach (var c in list) AddConvRow(c, isFleet, archived, isPinned);
            }
        }
        else
        {
            foreach (var c in list)
                if (c.Id == _conv.Id) AddConvRow(c, isFleet, archived, isPinned);   // keep the open conv reachable
        }
    }

    // One line at the top of the sidebar when part of the history could not be read.
    UIElement MakeHistoryNotice(string text)
    {
        var tb = new TextBlock
        {
            Text = text, FontSize = 11.5, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(12, 6, 8, 6)
        };
        SetRef(tb, TextBlock.ForegroundProperty, "Warning");
        return tb;
    }

    // "Older..." row under the last section: loads the next page (one day) of the transcripts
    // that are on disk but not yet listed. See LoadTranscriptPage.
    UIElement MakeOlderRow(int pending)
    {
        var tb = new TextBlock
        {
            Text = T("older_more").Replace("{0}", pending.ToString()),
            FontSize = 11.5, VerticalAlignment = VerticalAlignment.Center
        };
        SetRef(tb, TextBlock.ForegroundProperty, "Accent");
        var btn = new Button
        {
            Content = tb, HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(16, 6, 6, 8), Margin = new Thickness(0, 4, 0, 4),
            BorderThickness = new Thickness(0), Background = Brushes.Transparent, Cursor = Cursors.Hand
        };
        btn.Click += delegate { LoadOlderPage(); };
        return btn;
    }

    // Muted "+N more" row (ITEM 3c). Clicking expands the section for the session (RefreshConvList).
    UIElement MakeShowMoreRow(string collapseKey, int hidden)
    {
        var tb = new TextBlock
        {
            Text = T("show_more").Replace("{0}", hidden.ToString()),
            FontSize = 11.5, VerticalAlignment = VerticalAlignment.Center
        };
        SetRef(tb, TextBlock.ForegroundProperty, "Faint");
        var btn = new Button
        {
            Content = tb, HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(16, 5, 6, 6), Margin = new Thickness(0, 2, 0, 2),
            BorderThickness = new Thickness(0), Background = Brushes.Transparent, Cursor = Cursors.Hand
        };
        string key = collapseKey;
        btn.Click += delegate { _sectionExpanded.Add(key); RefreshConvList(); };
        return btn;
    }

    // Builds a clickable collapsible section-header row: chevron + label + count.
    // Clicking toggles _sectionCollapsed[key], saves state, and re-renders the list.
    UIElement MakeSectionHeader(string label, string key, int count)
    {
        bool collapsed = _sectionCollapsed.ContainsKey(key) && _sectionCollapsed[key];
        // The subset's own chevrons. This was U+25B8 / U+25BE in whatever font had them, next to
        // a column of drawn icons it could never match in weight or baseline.
        var chevronBlock = (FrameworkElement)MakeIcon(collapsed ? "chevron_right" : "expand_more", 10);
        chevronBlock.VerticalAlignment = VerticalAlignment.Center;
        chevronBlock.Margin = new Thickness(0, 0, 5, 0);

        string displayLabel = _lang == 0 ? label : label.ToUpperInvariant();
        var labelBlock = new TextBlock
        {
            Text = displayLabel, FontSize = _lang == 0 ? 11 : 10, FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center
        };
        SetRef(labelBlock, TextBlock.ForegroundProperty, "Faint");

        var countBlock = new TextBlock
        {
            Text = "(" + count + ")", FontSize = 10,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(5, 0, 0, 0)
        };
        SetRef(countBlock, TextBlock.ForegroundProperty, "Faint");

        var row = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center
        };
        row.Children.Add(chevronBlock);
        row.Children.Add(labelBlock);
        row.Children.Add(countBlock);

        var btn = new Button
        {
            Content = row,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(6, 5, 6, 5),
            Margin = new Thickness(0, 8, 0, 2),
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            Cursor = Cursors.Hand
        };
        // capture key for the lambda (C# 5 closure)
        string capturedKey = key;
        btn.Click += delegate
        {
            bool cur = _sectionCollapsed.ContainsKey(capturedKey) && _sectionCollapsed[capturedKey];
            _sectionCollapsed[capturedKey] = !cur;
            if (!cur) _sectionExpanded.Remove(capturedKey);   // collapsing resets the "show all" override (ITEM 3c)
            SaveSidebarState();
            RefreshConvList();
        };
        return btn;
    }

    // Builds and appends one conversation row (or an inline rename editor) into _convList.
    // isFleet:  rows in the Fleet section (quieter nav, no accent).
    // archived: rows in Archived section render in "Faint" (de-emphasized).
    // isPinned: rows in Pinned section get a subtle leading pin glyph.
    // Muted-but-visible resting opacity for the rename/delete row actions (Fix 3) -- discoverable
    // at rest, full strength on hover. Was 0 (fully invisible until hover).
    const double RowActionRestOpacity = 0.35;

    void AddConvRow(Conversation cc, bool isFleet, bool archived = false, bool isPinned = false)
    {
        if (_renamingId == cc.Id)
        {
            var ed = new TextBox { Text = cc.Title, Margin = new Thickness(0, 2, 0, 2), Padding = new Thickness(7, 5, 7, 5), BorderThickness = new Thickness(1) };
            SetRef(ed, BackgroundProperty, "Panel"); SetRef(ed, ForegroundProperty, "Fg"); SetRef(ed, Control.BorderBrushProperty, "Accent");
            ed.Loaded += delegate { ed.Focus(); ed.SelectAll(); };
            ed.KeyDown += delegate (object s, KeyEventArgs e)
            {
                if (e.Key == Key.Enter) { cc.Title = ed.Text.Trim().Length > 0 ? ed.Text.Trim() : cc.Title; _renamingId = null; SaveConversation(cc); RefreshConvList(); }
                else if (e.Key == Key.Escape) { _renamingId = null; RefreshConvList(); }
            };
            ed.LostFocus += delegate { if (_renamingId == cc.Id) { cc.Title = ed.Text.Trim().Length > 0 ? ed.Text.Trim() : cc.Title; _renamingId = null; SaveConversation(cc); RefreshConvList(); } };
            _convList.Children.Add(ed);
            return;
        }

        bool isActive = cc.Id == _conv.Id;
        // row = background border: the ACTIVE row reads as a quietly darker card (Selected token) filling
        // the full rounded rect at the same corner radius as every row — NO colored left rail, NO colored
        // border (the orange rail was rejected as bad design). Non-selected rows are transparent.
        var rowBorder = new Border { CornerRadius = new CornerRadius(Theme.RadSmall), Margin = new Thickness(0, 1, 0, 1) };
        if (isActive)
        {
            SetRef(rowBorder, BackgroundProperty, "Selected");
        }
        else
        {
            rowBorder.Background = Brushes.Transparent;
        }

        var rowGrid = new Grid { MinHeight = 38 };
        rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });   // rename link (hover)
        rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });   // trash (hover)

        // ITEM 3a: DISPLAY-trim long saved titles (first line, max 40) without rewriting stored data.
        var titleText = cc.Untitled() ? T("newchat") : TrimTitle(cc.Title, 40);

        // Inner content: optional pin glyph + title label.
        var contentRow = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        if (isPinned)
        {
            var pinMark = new TextBlock { Text = "\uE718", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 5, 0) };
            SetRef(pinMark, TextBlock.ForegroundProperty, "Muted");
            contentRow.Children.Add(pinMark);
        }
        var lbl = new TextBlock
        {
            Text = titleText, TextWrapping = TextWrapping.NoWrap,
            TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 13,
            FontWeight = isActive ? FontWeights.SemiBold : FontWeights.Normal,
            VerticalAlignment = VerticalAlignment.Center
        };
        contentRow.Children.Add(lbl);
        var b = new Button
        {
            Content = contentRow,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            // No left rail anymore -> uniform left padding so active/inactive titles align identically.
            Padding = new Thickness(8, 0, 8, 0), BorderThickness = new Thickness(0),
            Cursor = Cursors.Hand, Background = Brushes.Transparent, ToolTip = titleText
        };
        // Active row: full Fg; archived non-active: Faint (de-emphasized); others: Muted.
        string fgKey = isActive ? "Fg" : (archived ? "Faint" : "Muted");
        SetRef(b, ForegroundProperty, fgKey);
        b.Click += delegate { OpenConversation(cc); };
        // right-click context menu: Pin/Unpin, Archive/Unarchive, Rename
        bool curPinned   = _pinned.Contains(cc.Id);
        bool curArchived = _archived.Contains(cc.Id) || (!_forcedToday.Contains(cc.Id) && IsAutoArchive(cc));
        var menu = new ContextMenu();
        var miPin = new MenuItem { Header = curPinned ? T("unpin") : T("pin") };
        miPin.Click += delegate { if (_pinned.Contains(cc.Id)) _pinned.Remove(cc.Id); else _pinned.Add(cc.Id); SaveSidebarState(); RefreshConvList(); };
        menu.Items.Add(miPin);
        var miArchive = new MenuItem { Header = curArchived ? T("unarchive") : T("archive") };
        miArchive.Click += delegate
        {
            if (_archived.Contains(cc.Id) || (!_forcedToday.Contains(cc.Id) && IsAutoArchive(cc)))
            { _archived.Remove(cc.Id); _forcedToday.Add(cc.Id); }
            else
            { _archived.Add(cc.Id); _forcedToday.Remove(cc.Id); if (_sectionCollapsed.ContainsKey("archived")) _sectionCollapsed["archived"] = false; }
            SaveSidebarState(); RefreshConvList();
        };
        menu.Items.Add(miArchive);
        var miR = new MenuItem { Header = T("rename") };
        miR.Click += delegate { _renamingId = cc.Id; RefreshConvList(); };
        menu.Items.Add(miR);
        b.ContextMenu = menu;
        Grid.SetColumn(b, 0); rowGrid.Children.Add(b);

        // ITEM 3d: hover "Rename" text link (no pencil glyph exists in the 8-glyph subset; a small
        // Muted 11px link is the discoverable affordance). Triggers the SAME rename flow as the
        // context-menu Rename. Hidden by default, revealed with the trash on row hover.
        var renameLink = new Button
        {
            Content = T("rename_link"), FontSize = 11,
            Height = 38, Padding = new Thickness(6, 0, 4, 0),
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            Cursor = Cursors.Hand, ToolTip = T("rename"), Opacity = RowActionRestOpacity, IsHitTestVisible = true,
            VerticalContentAlignment = VerticalAlignment.Center
        };
        SetRef(renameLink, ForegroundProperty, "Muted");
        renameLink.Click += delegate { _renamingId = cc.Id; RefreshConvList(); };
        renameLink.Visibility = (cc.Messages.Count > 0 || !string.IsNullOrEmpty(cc.Title)) ? Visibility.Visible : Visibility.Collapsed;
        Grid.SetColumn(renameLink, 1); rowGrid.Children.Add(renameLink);

        // trash icon (Segoe MDL2 Assets) -- hidden by default, revealed on row hover.
        var trash = new Button
        {
            Content = "\uE74D", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 13,
            Width = 32, Height = 38, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            Cursor = Cursors.Hand, ToolTip = T("delete"), Opacity = RowActionRestOpacity, IsHitTestVisible = true
        };
        SetRef(trash, ForegroundProperty, "Muted");
        // Fast path: a plain click deletes the LOCAL record only (mode 1 -- per ShowDeleteBanner's
        // own m1s label this is "the safest" choice; it never touches the Copilot-side conversation)
        // with the existing inline toast, instead of forcing the 3-mode banner every time. Shift+click
        // (the modifier this file already uses for the Enter-vs-newline distinction, ~line 401) still
        // opens the full banner for the less-common modes 2/3.
        trash.Click += delegate
        {
            if ((Keyboard.Modifiers & ModifierKeys.Shift) != 0) ShowDeleteBanner(cc);
            else { ExecuteDelete(cc, 1); ToastUndo(); }
        };
        bool actionable = cc.Messages.Count > 0 || !string.IsNullOrEmpty(cc.ConvUrl)
                          || !string.IsNullOrEmpty(cc.Title);
        trash.Visibility = actionable ? Visibility.Visible : Visibility.Collapsed;
        // Trash right-click: same pin/archive/rename actions as title button.
        var trMenu = new ContextMenu();
        var trPin = new MenuItem { Header = curPinned ? T("unpin") : T("pin") };
        trPin.Click += delegate { if (_pinned.Contains(cc.Id)) _pinned.Remove(cc.Id); else _pinned.Add(cc.Id); SaveSidebarState(); RefreshConvList(); };
        trMenu.Items.Add(trPin);
        var trArchive = new MenuItem { Header = curArchived ? T("unarchive") : T("archive") };
        trArchive.Click += delegate
        {
            if (_archived.Contains(cc.Id) || (!_forcedToday.Contains(cc.Id) && IsAutoArchive(cc)))
            { _archived.Remove(cc.Id); _forcedToday.Add(cc.Id); }
            else
            { _archived.Add(cc.Id); _forcedToday.Remove(cc.Id); if (_sectionCollapsed.ContainsKey("archived")) _sectionCollapsed["archived"] = false; }
            SaveSidebarState(); RefreshConvList();
        };
        trMenu.Items.Add(trArchive);
        var trRename = new MenuItem { Header = T("rename") };
        trRename.Click += delegate { _renamingId = cc.Id; RefreshConvList(); };
        trMenu.Items.Add(trRename);
        trash.ContextMenu = trMenu;
        Grid.SetColumn(trash, 2); rowGrid.Children.Add(trash);
        // Reveal the trash icon AND the rename link on row hover; hide both when the pointer leaves.
        var trashRef = trash;
        var renameRef = renameLink;
        rowBorder.MouseEnter += delegate
        {
            if (trashRef.Visibility == Visibility.Visible) { trashRef.Opacity = 1; trashRef.IsHitTestVisible = true; }
            if (renameRef.Visibility == Visibility.Visible) { renameRef.Opacity = 1; renameRef.IsHitTestVisible = true; }
        };
        rowBorder.MouseLeave += delegate
        {
            trashRef.Opacity = RowActionRestOpacity; trashRef.IsHitTestVisible = true;
            renameRef.Opacity = RowActionRestOpacity; renameRef.IsHitTestVisible = true;
        };
        rowBorder.Child = rowGrid;
        _convList.Children.Add(rowBorder);
    }

    // ── sidebar collapse / expand (Codex / Claude-desktop style) ──────────────────
    void ToggleSidebar()
    {
        _sidebarCollapsed = !_sidebarCollapsed;
        ApplySidebarState();
        SaveSettings();
    }

    void ApplySidebarState()
    {
        if (_rootGrid == null || _sideBorderRef == null) return;
        if (_sidebarCollapsed)
        {
            _rootGrid.ColumnDefinitions[0].Width = new GridLength(0);
            _sideBorderRef.Visibility = Visibility.Collapsed;
        }
        else
        {
            _rootGrid.ColumnDefinitions[0].Width = new GridLength(260);
            _sideBorderRef.Visibility = Visibility.Visible;
        }
        // Update the toggle button tooltip to reflect current state.
        if (_sideToggleBtn != null)
            _sideToggleBtn.ToolTip = (_lang == 0 ? "サイドバーを切り替える (Ctrl+B)" : "Toggle sidebar (Ctrl+B)");
    }
}
