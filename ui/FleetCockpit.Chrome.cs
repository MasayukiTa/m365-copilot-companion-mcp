// FleetCockpit.Chrome.cs -- BuildChrome and the window chrome paint/relabel/rebuild path.
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

    void BuildChrome()
    {
        var root = new DockPanel();
        _headBar = new Border();
        _headBar.Padding = new Thickness(24, 20, 16, 8);
        DockPanel.SetDock(_headBar, Dock.Top);

        // Header = a 2-column Grid, NOT a DockPanel. The old DockPanel let the (un-clipped)
        // title StackPanel render its long subtitle PAST its arrange rect, sliding under the
        // right-docked control band whose opaque themed backgrounds then painted over the text
        // -> the subtitle "disappeared" exactly where it reached the RAM controls. A Grid gives
        // the title column a HARD bounded width (star) next to the auto-width controls, so the
        // two never overlap; the subtitle is then trimmed with an ellipsis instead of hidden.
        // Header = a 2-col x 2-row Grid. Row 0: title (col 0) + RAM/lang/theme controls (col 1).
        // Row 1: the SUBTITLE, spanning BOTH columns (full width, also under the controls). The
        // subtitle carries the long live line (elapsed + ETA + goals + concurrency); keeping it in
        // col 0 only meant the RAM-controls column ate its right end and the ETA got clipped ("8...").
        // Full-width row + wrapping = nothing is ever hidden.
        var headRow = new Grid();
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // title + controls
        headRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // subtitle (full width)

        // right controls: live status/actions, language, theme (row 0, col 1).
        // Vertically CENTERED so the control cluster shares one horizontal center line with the
        // left-edge health strip (redesign: single clean header band, dots flush-left / controls
        // flush-right, all centered on one line — no big in-content title above).
        var ctrls = new StackPanel();
        ctrls.Orientation = Orientation.Horizontal;
        ctrls.VerticalAlignment = VerticalAlignment.Center;
        ctrls.HorizontalAlignment = HorizontalAlignment.Right;

        // Read-only live tab pressure. Configuration belongs in Settings; while idle this chip is
        // hidden so the header does not duplicate the Start/ceiling/autoscale controls.
        _workerChip = new TextBlock();
        _workerChip.FontSize = 12; _workerChip.VerticalAlignment = VerticalAlignment.Center;
        _workerChip.Margin = new Thickness(0, 0, 12, 0);
        _workerChip.Padding = new Thickness(12, 3, 12, 3);
        UpdateWorkerChip(0, 0, false);   // initial paint
        _workerChipBorder = new Border();
        _workerChipBorder.Child = _workerChip;
        _workerChipBorder.BorderThickness = new Thickness(1);
        _workerChipBorder.CornerRadius = new CornerRadius(Theme.RadSmall);
        _workerChipBorder.Padding = new Thickness(0);
        _workerChipBorder.VerticalAlignment = VerticalAlignment.Center;
        _workerChipBorder.Margin = new Thickness(0, 0, 12, 0);
        _workerChipBorder.Visibility = Visibility.Collapsed;
        PaintWorkerChipBorder(_workerChipBorder);
        ctrls.Children.Add(_workerChipBorder);

        // HEADER CONTROL SET IS PINNED by relay/test_cockpit_header_controls.py. Do NOT add a
        // control here: new settings belong in the gear popup (BuildSettingsPanel), in the
        // "Fan-out" / "Effort" sections or a new section of their own.
        ctrls.Children.Add(EffortControl());
        ctrls.Children.Add(ApprovalControl());
        ctrls.Children.Add(ApprovalCenterControl());
        ctrls.Children.Add(FleetControls());
        // gear -> settings popup consolidating the scattered start/上限/retry/disk-floor controls.
        ctrls.Children.Add(SettingsControl());
        // Language and theme toggles: 1-click direct icon buttons in the header (frequently used).
        _langBtn = IconButton("translate", 18, _lang == 0 ? "言語切替" : "Toggle language");
        _langBtn.ToolTip = _lang == 0 ? "English / 日本語 切替" : "Toggle language";
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveKey("lang", _lang.ToString()); RebuildChrome(); };
        ctrls.Children.Add(_langBtn);
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18, _dark ? "Switch to light mode" : "Switch to dark mode");
        _themeBtn.ToolTip = _dark ? "Switch to light mode" : "Switch to dark mode";
        _themeBtn.Click += delegate { _dark = !_dark; SaveKey("dark", _dark ? "1" : "0"); ApplyTheme(); };
        ctrls.Children.Add(_themeBtn);
        // Rare items (open main chat, self-improve) stay in the overflow. Keep fields non-null (PaintChrome references them).
        _mainBtn = IconButton("chat", 18, _lang == 0 ? "メインチャットを開く" : "Open main chat");
        _mainBtn.Click += delegate { OpenMain(); };
        _siBtn = IconButton("account_tree", 18, _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement");
        _siBtn.Click += delegate { new SelfImproveDashboardWindow().Show(); };
        ctrls.Children.Add(OverflowControl());
        Grid.SetColumn(ctrls, 1); Grid.SetRow(ctrls, 0);
        headRow.Children.Add(ctrls);

        // Redesign: the big in-content title was REMOVED (it duplicated the OS window title
        // "Fleet Cockpit" and truncated to "Fl…", wasting the top-left). _header is kept as a
        // live, non-null TextBlock so the existing PaintChrome/OnTick references (which set its
        // .Text/.Foreground) never NRE — it is simply NOT added to the visual tree.
        _header = new TextBlock(); _header.FontSize = 22; _header.FontWeight = FontWeights.SemiBold;
        _header.VerticalAlignment = VerticalAlignment.Center;
        _header.TextTrimming = TextTrimming.CharacterEllipsis; _header.TextWrapping = TextWrapping.NoWrap;
        _header.Visibility = Visibility.Collapsed;   // defensive: never rendered even if re-parented

        // P0 HEALTH STRIP now occupies col 0 (where the title was): five infra dots + inline Fix
        // pill, left-aligned and vertically centered so the dots サーバ→エージェント sit on the same
        // center line as the right-side control cluster. One clean horizontal band.
        var healthHost = BuildHealthStrip();
        Grid.SetColumn((UIElement)healthHost, 0); Grid.SetRow((UIElement)healthHost, 0);
        headRow.Children.Add((UIElement)healthHost);

        // subtitle -- its OWN row spanning BOTH columns, so the long elapsed+ETA line uses the full
        // width and is never clipped by the controls column. Wrap (not ellipsis) so it's never hidden.
        // Feature 2: _sub itself is no longer shown -- UpdateHeader still builds its sentence (kept
        // as a plain-text fallback / ToolTip source) but the VISIBLE row 1 content is now _subChips,
        // a WrapPanel of discrete Pill() chips occupying the identical grid cell.
        _sub = new TextBlock(); _sub.FontSize = 13; _sub.Margin = new Thickness(0, 4, 16, 0);
        _sub.TextWrapping = TextWrapping.Wrap;
        Grid.SetColumn(_sub, 0); Grid.SetColumnSpan(_sub, 2); Grid.SetRow(_sub, 1);
        _sub.Visibility = Visibility.Collapsed;
        headRow.Children.Add(_sub);

        _subChips = new WrapPanel();
        _subChips.Margin = new Thickness(0, 4, 16, 0);
        Grid.SetColumn(_subChips, 0); Grid.SetColumnSpan(_subChips, 2); Grid.SetRow(_subChips, 1);
        headRow.Children.Add(_subChips);

        _headBar.Child = headRow;
        root.Children.Add(_headBar);
        root.Children.Add(BuildMtBanner());
        // BuildGateBanner() is NOT added here -- it is docked inside the run column (col1) so it
        // cannot overhang the timeline spine. See the comment at that call site.
        root.Children.Add(BuildCapBanner());
        root.Children.Add(BuildRejectionBanner());
        // The composer docks to the BOTTOM (spec: agent-workspace feel, not a form). It must be
        // added before _list so the list — the LastChildFill element — fills the space above it.
        root.Children.Add(BuildInputBar());

        // Virtualizing card list. A ListBox brings its own ScrollViewer + VirtualizingStackPanel,
        // so with 100-164 cards only the ~10-20 visible rows are realized -> scrolling and
        // window maximize/restore stay fast. The container chrome is templated away so it reads
        // as a plain scrolling list (no selection highlight / mouse-over / focus rect).
        _list = new ListBox();
        _list.BorderThickness = new Thickness(0);
        _list.Background = Brushes.Transparent;
        _list.Padding = new Thickness(16, 6, 16, 20);  // bottom clears the composer DropShadow (BlurRadius 14 + ShadowDepth 2 ≈ 12px) so the last row is not overlapped by the shadow. 20, not 18: the spacing scale steps in fours above the optical range, and 18 was the only value off it.
        ScrollViewer.SetVerticalScrollBarVisibility(_list, ScrollBarVisibility.Auto);
        ScrollViewer.SetHorizontalScrollBarVisibility(_list, ScrollBarVisibility.Disabled);
        // Pixel scroll (not item scroll) so the list can SIZE TO CONTENT inside an Auto row and
        // therefore bottom-anchor: a short list hugs the composer (no dead gap), a long list is
        // capped to the viewport (MaxHeight) and scrolls. Item-virtualization is traded away here;
        // the fleet ledger row count is modest and rows are light.
        ScrollViewer.SetCanContentScroll(_list, false);
        // VIRTUALIZATION OFF, WHICH IS WHAT THE LINE ABOVE ALREADY ASSUMED.
        //
        // The comment above says item-virtualization is traded away, and then the next line
        // turned it on. The two together are the bug the history list had: pixel scrolling over
        // a VIRTUALIZING panel means WPF has to ESTIMATE the extent, because the rows it has not
        // realized have no measured height. These rows are cards of very different heights, so
        // the estimate is short -- and a short extent is a scrollbar that stops before the end.
        // The last entries could not be reached at all; reported from the running cockpit as
        // "the history will not scroll all the way down".
        //
        // With it off every row is measured, so the extent is exact and the end is reachable.
        // That is the trade the comment above describes and the row count justifies: the ledger
        // is tens of rows, not thousands, and each is a light template.
        VirtualizingPanel.SetIsVirtualizing(_list, false);
        _list.Focusable = false;
        _list.IsTabStop = false;
        KeyboardNavigation.SetDirectionalNavigation(_list, KeyboardNavigationMode.None);
        _list.ItemContainerStyle = BuildItemContainerStyle();
        _list.ItemTemplate = BuildRowTemplate();
        _list.ItemsSource = _rows;     // bound ONCE; SetRows mutates _rows in place (no Reset)

        // A2-2: 2-column Grid: col0 = Evidence Spine (fixed ~220px, collapses to 0 when idle),
        // col1 = _list (star, unchanged). The spine does NOT scroll with the list.
        var lanesGrid = new Grid();
        _spineCol = new ColumnDefinition();
        _spineCol.Width = new GridLength(0);       // starts hidden (idle); RefreshSpine will open it
        lanesGrid.ColumnDefinitions.Add(_spineCol);
        lanesGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        _spinePanel = new Border();
        _spinePanel.BorderThickness = new Thickness(0, 0, 1, 0);
        _spinePanel.Padding = new Thickness(0);
        Grid.SetColumn(_spinePanel, 0);
        lanesGrid.Children.Add(_spinePanel);

        // Bottom-anchor wrapper (chat-style): a '*' spacer row absorbs the slack ABOVE the rows,
        // so when the content is shorter than the viewport the rows sit just above the composer
        // instead of leaving a dead void between the last row and the input. The list row is Auto
        // (sizes to content) but the list's MaxHeight is bound to the wrapper height, so a long
        // list is capped to the viewport and scrolls normally.
        var listWrap = new Grid();
        listWrap.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        listWrap.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var maxBind = new System.Windows.Data.Binding("ActualHeight");
        maxBind.Source = listWrap;
        _list.SetBinding(FrameworkElement.MaxHeightProperty, maxBind);
        Grid.SetRow(_list, 1);
        listWrap.Children.Add(_list);

        // Col1 is now a DockPanel: the pinned filter bar sits at the TOP (fixed), the bottom-anchored
        // listWrap fills the rest. Because the toolbar host is OUTSIDE the ListBox's ScrollViewer, it
        // never scrolls and never moves as cards/history grow — it is anchored to the top of the run
        // area directly under the header. The left padding matches the list's inner padding (18) so
        // the bar and the cards under it share the same left edge.
        var col1 = new DockPanel { LastChildFill = true };
        // The approval banner belongs to the RUN column, not to the whole window. Docked on the
        // root it spanned the full width and overhung the timeline spine once that column opened,
        // so the banner's box crossed the spine's frame instead of lining up with the cards it
        // refers to. Docking it here keeps its left edge on the same 18px gutter as the toolbar
        // and the cards below it.
        col1.Children.Add(BuildGateBanner());
        _pinnedToolbarHost = new Border();
        _pinnedToolbarHost.Padding = new Thickness(16, 6, 16, 0);
        _pinnedToolbarHost.Visibility = Visibility.Collapsed;   // shown once there is a run/history to filter
        DockPanel.SetDock(_pinnedToolbarHost, Dock.Top);
        col1.Children.Add(_pinnedToolbarHost);
        col1.Children.Add(listWrap);   // LastChildFill -> fills below the pinned bar
        Grid.SetColumn(col1, 1);
        lanesGrid.Children.Add(col1);

        root.Children.Add(lanesGrid);

        // UI SCALE: anchor the ScaleTransform as the LayoutTransform on THIS root DockPanel — the
        // single element that contains ALL chrome (header + pinned toolbar host + health strip +
        // card list + composer). LayoutTransform (not RenderTransform) so children re-measure and
        // reflow at the zoomed size instead of being bitmap-stretched. RebuildChrome() calls
        // BuildChrome() afresh, so a NEW root gets the transform re-applied here every rebuild.
        _rootScale = new ScaleTransform(_uiScale, _uiScale);
        root.LayoutTransform = _rootScale;
        // Overlay toast lives OUTSIDE the scaled root (added to a Grid wrapper) so its size is
        // constant regardless of zoom; it self-fades on each scale change.
        var appRoot = new Grid();
        appRoot.Children.Add(root);
        appRoot.Children.Add(BuildScaleToast());
        Content = appRoot;
        PaintChrome();
        StartHealthPoll();
    }

    // Close all header popups/dropdowns except the one named by `except`.
    // except: "settings" | "overflow" | "effort" | "approval" | null (close all)
    void CloseHeaderPopups(string except)
    {
        if (except != "settings" && _settingsPopup != null) _settingsPopup.IsOpen = false;
        if (except != "overflow" && _overflowPopup != null) _overflowPopup.IsOpen = false;
        if (except != "effort" && _effortBox != null) _effortBox.IsDropDownOpen = false;
        if (except != "approval" && _approvalBox != null) _approvalBox.IsDropDownOpen = false;
        // The /help popup is a separate HWND that used to linger when a header popup opened. Fold it
        // into the same one-at-a-time close path (plus the slash-command palette) so opening
        // settings/overflow/effort/approval also dismisses help, and vice versa.
        if (except != "help" && _helpPopup != null) _helpPopup.IsOpen = false;
        if (except != "slash" && _gcmdPopup != null) _gcmdPopup.IsOpen = false;
    }

    // Read-only runtime status. Hide it while idle/finished; Settings owns configuration.
    void UpdateWorkerChip(int openTabs, int liveCap, bool isLive)
    {
        if (_workerChip == null) return;
        if (_workerChipBorder != null)
            _workerChipBorder.Visibility = isLive ? Visibility.Visible : Visibility.Collapsed;
        if (!isLive) return;
        int open = Math.Max(0, openTabs);
        int cap = Math.Max(1, liveCap > 0 ? liveCap : _maxtabs);
        string label = _lang == 0 ? ("タブ " + open + "/" + cap) : ("Tabs " + open + "/" + cap);
        _workerChip.Text = label;
        _workerChip.Foreground = Theme.Br(Theme.Muted(_dark));
        string hint = T("tabs_chip_hint");
        _workerChip.ToolTip = hint;
        if (_workerChipBorder != null) _workerChipBorder.ToolTip = hint;
    }

    void PaintWorkerChipBorder(Border b)
    {
        if (b == null) return;
        b.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        b.BorderBrush = Theme.Br(Theme.Border(_dark));
    }

    void PaintChrome()
    {
        Background = Bg;
        _headBar.Background = Bg;
        _header.Foreground = Fg;
        _sub.Foreground = Muted;
        if (_list != null) _list.Background = Bg;
        // satellite icon removed per UX feedback -- nothing to paint at top-left of the title row.
        // restyle the header buttons for the theme
        foreach (Button b in new Button[] { _mainBtn, _siBtn, _themeBtn, _langBtn, _gearBtn, _maxMinus, _maxPlus, _autoMinus, _autoPlus })
            if (b != null) { b.Background = BtnBg; b.Foreground = Fg; b.BorderBrush = Border; }
        if (_overflowBtn != null) { _overflowBtn.Background = BtnBg; _overflowBtn.Foreground = Fg; _overflowBtn.BorderBrush = Border; }
        if (_gearBtn != null) _gearBtn.Content = MakeIcon("settings", 18, Fg);
        if (_themeBtn != null) _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18, Fg);
        if (_langBtn != null) _langBtn.Content = MakeIcon("translate", 18, Fg);
        if (_maxValue != null) _maxValue.Foreground = Fg;
        if (_autoLbl != null) _autoLbl.Foreground = Muted;
        if (_autoValue != null) _autoValue.Foreground = Fg;
        if (_workerChip != null) UpdateWorkerChip(0, 0, false);
        PaintWorkerChipBorder(_workerChipBorder);
        PaintHealthChrome();
        ApplyHealthToUi();     // re-tint the dots for the new theme
        _agentMarkerId = ExtractAgentMarker();
        PaintAutoToggle();
        UpdateAutoEnabled();
        PaintEffort();
        PaintEffortPolicy();
        PaintFanout();
        PaintApproval();
        PaintApprovalCenterButton(PendingGates(ReadStatus()).Count);
        PaintPause();
        PaintStop();
        if (_stopBtn != null) { _stopBtn.Background = BtnBg; _stopBtn.Foreground = Fg; _stopBtn.BorderBrush = Border; }
        if (_inBar != null) _inBar.Background = Bg;
        // Floating composer: SurfaceSubtle bg (quieter than BtnBg); faint border for low-contrast frame.
        if (_composerBox != null)
        {
            _composerBox.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            _composerBox.BorderBrush = Theme.Br(Theme.Border(_dark));
            _composerBox.Opacity = 1.0;
        }
        if (_goalInput != null)
        {
            _goalInput.Background = Brushes.Transparent; _goalInput.Foreground = Fg;
            _goalInput.BorderBrush = Brushes.Transparent; _goalInput.CaretBrush = Fg;
        }
        if (_composerWatermark != null) _composerWatermark.Foreground = Muted;
        if (_composerHint != null) _composerHint.Foreground = Muted;
        if (_startBtn != null) { _startBtn.Background = AccentFill; _startBtn.Foreground = AccentFg; }
        if (_folderBtn != null) { _folderBtn.Background = Brushes.Transparent; _folderBtn.Foreground = Fg; _folderBtn.BorderBrush = Theme.Br(Theme.BorderStrong(_dark)); }
        if (_startNote != null) _startNote.Foreground = Muted;
        // Banners: a quiet surface card with a 3px warning LEFT rail (spec), not an orange fill.
        // The action is a secondary warning button (outline), not a primary orange block.
        var warn = Theme.Br(Theme.Warning(_dark));
        if (_mtBanner != null)
        {
            _mtBanner.Background = CardBg;
            _mtBanner.BorderThickness = new Thickness(1);
            _mtBanner.BorderBrush = warn;
            if (_mtBannerLbl != null) _mtBannerLbl.Foreground = Fg;
        }
        if (_mtApplyNow != null) { _mtApplyNow.Background = Brushes.Transparent; _mtApplyNow.Foreground = warn; _mtApplyNow.BorderBrush = warn; _mtApplyNow.BorderThickness = new Thickness(1); }
        if (_mtLater != null) { _mtLater.Background = Brushes.Transparent; _mtLater.Foreground = Muted; _mtLater.BorderBrush = Border; }
        if (_gateBanner != null)
        {
            _gateBanner.Background = CardBg;
            // Uniform, not a thicker left edge: a one-sided stripe reads as sticky-note
        // decoration. The banner's border colour already carries the urgency.
        _gateBanner.BorderThickness = new Thickness(1);
            _gateBanner.BorderBrush = warn;
            // Force a full rebuild on theme change so buttons repaint correctly.
            _gateSig = "";
        }
        if (_capBanner != null)
        {
            _capBanner.Background = CardBg;
            _capBanner.BorderThickness = new Thickness(1);
            _capBanner.BorderBrush = warn;
            if (_capBannerLbl != null) _capBannerLbl.Foreground = Fg;
        }
        if (_capForceBtn != null) { _capForceBtn.Background = Brushes.Transparent; _capForceBtn.Foreground = warn; _capForceBtn.BorderBrush = warn; }
        if (_capRestoreBtn != null) { _capRestoreBtn.Background = Brushes.Transparent; _capRestoreBtn.Foreground = Fg; _capRestoreBtn.BorderBrush = Border; }
        if (_rejBanner != null)
        {
            _rejBanner.Background = CardBg;
            _rejBanner.BorderThickness = new Thickness(1);
            _rejBanner.BorderBrush = warn;
            if (_rejBannerLbl != null) _rejBannerLbl.Foreground = Fg;
        }
        Relabel();
    }

    void Relabel()
    {
        if (_maxValue != null) _maxValue.Text = _maxtabs.ToString();
        if (_autoLbl != null) _autoLbl.Text = T("max_tabs2");
        if (_autoValue != null) _autoValue.Text = _autoMax.ToString();
        PaintAutoToggle();
        PaintEffort();
        PaintEffortPolicy();
        PaintFanout();
        PaintApproval();
        PaintApprovalCenterButton(PendingGates(ReadStatus()).Count);
        PaintPause();
        PaintStop();
        // _stopBtn / _pauseBtn now render drawn icons (PaintPause/PaintStop), not text labels.
        if (_startBtn != null) _startBtn.Content = T("start");
        if (_folderBtn != null) _folderBtn.Content = T("folder");
        if (_goalInput != null) _goalInput.ToolTip = T("goalhint");
        // _startNote is now transient feedback only; the persistent hint lives in the composer footer.
        if (_mtApplyNow != null) _mtApplyNow.Content = _lang == 0 ? "今すぐ反映" : "Apply now";
        if (_mtLater != null) _mtLater.Content = _lang == 0 ? "次回起動から" : "Next run";
    }

    // Language toggle must re-evaluate EVERY localized string, not just the handful Relabel() touches.
    // Strings set once at construction (the goal-box hint watermark, tooltips, the settings-panel
    // labels, filter/retry buttons, ...) otherwise keep their build-time language -> the user sees a
    // half-translated UI after toggling. Rebuilding the whole chrome re-runs every T()/_lang branch.
    // _rows is bound once and survives, so cards persist; we preserve the typed goal text + scroll.
    void RebuildChrome()
    {
        string goalText = _goalInput != null ? _goalInput.Text : null;
        bool prevRunActive = _composerRunActive;
        BuildChrome();
        if (goalText != null && _goalInput != null) _goalInput.Text = goalText;
        // A2-2: reset spine+composer state after a full chrome rebuild so PaintComposerMode
        // re-applies the current mode (BuildChrome created fresh controls with idle defaults).
        _spineSig = "";
        _composerRunActive = !prevRunActive; // force the guard in PaintComposerMode to fire
        PaintChrome();
        ForceRender();
    }

    void ApplyTheme()
    {
        ApplyThemeBrushes();
        PaintChrome();
        ForceRender();
    }
    void ForceRender()
    {
        _lastSig = "";
        OnTick(null, null);
    }
}
