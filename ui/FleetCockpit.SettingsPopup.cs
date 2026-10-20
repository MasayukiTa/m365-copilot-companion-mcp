// FleetCockpit.SettingsPopup.cs -- the gear settings popup: panel, advanced submenu, floors and retention steppers.
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

    System.Windows.Controls.Primitives.Popup _settingsPopup;
    Button _gearBtn;
    Button _maxMinus, _maxPlus;
    ComboBox _effortBox;
    ComboBox _approvalBox;
    TextBlock _effortLbl;
    ComboBox _effortPolicyBox;
    TextBlock _effortPolicyLbl, _effortPolicyNow, _effortPolicyWarn;
    ComboBox _fanoutBox;
    TextBlock _fanoutLbl, _fanoutNow, _fanoutPending;
    TextBlock _approvalLbl;
    Button _pauseBtn, _stopBtn;
    System.Windows.Shapes.Path _pauseIcon, _stopIcon;   // drawn geometry (no font glyph needed)
    Button _autoToggle;
    Button _autoMinus, _autoPlus;
    TextBlock _autoLbl, _autoValue;

    // 詳細設定 / Advanced (settings panel) -- アクセス範囲 (folder_access.json) +
    // 接続クライアント (.unlock_state.json via tools.security's grant_ip/revoke_ip). Rebuilt
    // fresh every time the gear popup opens, same as the rest of BuildSettingsPanel.
    Button _accessFullBtn, _accessRestrictedBtn;
    StackPanel _folderRowsPanel;
    StackPanel _clientRowsPanel;

    // Autoscale toggle for the Settings panel. Autoscale, start tabs and ceiling are one concept,
    // so they live together instead of splitting one setting between the header and gear menu.
    UIElement AutoscaleControls()
    {
        var group = new StackPanel(); group.Orientation = Orientation.Horizontal;
        group.VerticalAlignment = VerticalAlignment.Center; group.Margin = new Thickness(0, 2, 0, 4);
        group.ToolTip = _lang == 0
            ? "空きRAMに応じて並列タブ数を開始値から上限まで自動調整します。"
            : "Automatically adjust parallel tabs from the start value up to the ceiling based on free RAM.";

        // a. autoscale ON/OFF toggle (simple themed button whose label flips)
        _autoToggle = new Button();
        _autoToggle.ToolTip = _lang == 0 ? "RAM空きに応じて並列タブ数を自動調整 (ON/OFF)" : "Auto-adjust parallel tabs to free RAM (ON/OFF)";
        _autoToggle.Cursor = Cursors.Hand; _autoToggle.BorderThickness = new Thickness(1);
        _autoToggle.Padding = new Thickness(12, 3, 12, 3); _autoToggle.FontSize = 12;
        _autoToggle.FontWeight = FontWeights.SemiBold;
        _autoToggle.Margin = new Thickness(0);
        _autoToggle.VerticalAlignment = VerticalAlignment.Center;
        _autoToggle.Click += delegate
        {
            _autoscale = !_autoscale;
            SaveKey("autoscale", _autoscale ? "1" : "0");
            PaintAutoToggle();
            UpdateAutoEnabled();
            // live-apply: reflect the new mode into a running fleet immediately.
            if (_autoscale) RequestSetAutoscaleIfLive();
            else RequestSetMaxtabsIfLive();
        };
        group.Children.Add(_autoToggle);

        // Paint it NOW, not only from the Click handler. The settings panel rebuilds this
        // control every time it opens, and PaintAutoToggle is what sets the button's label and
        // colours -- so until the user clicked it the toggle rendered as an empty pill with no
        // text, giving no clue whether RAM auto-adjust was on or off. Clicking to find out also
        // flipped the setting, which is the opposite of what someone checking the state wants.
        PaintAutoToggle();
        UpdateAutoEnabled();

        return group;
    }

    // Ceiling (上限) stepper -- mirrors MaxTabsStepper's −/+ pattern. Lives in the settings panel.
    // Reuses the _autoLbl/_autoMinus/_autoValue/_autoPlus fields so PaintChrome/UpdateAutoEnabled/
    // SetAutoMax keep driving it unchanged.
    UIElement CeilingStepper()
    {
        var wrap = new StackPanel(); wrap.Orientation = Orientation.Horizontal;
        wrap.VerticalAlignment = VerticalAlignment.Center;
        _autoLbl = new TextBlock(); _autoLbl.VerticalAlignment = VerticalAlignment.Center;
        _autoLbl.FontSize = 12; _autoLbl.Margin = new Thickness(0, 0, 8, 0);
        wrap.Children.Add(_autoLbl);
        _autoMinus = MiniButton("−");
        _autoMinus.Click += delegate { SetAutoMax(_autoMax - 1); };
        wrap.Children.Add(_autoMinus);
        _autoValue = new TextBlock(); _autoValue.VerticalAlignment = VerticalAlignment.Center;
        _autoValue.FontSize = 13; _autoValue.FontWeight = FontWeights.SemiBold;
        _autoValue.Margin = new Thickness(8, 0, 8, 0); _autoValue.MinWidth = 14;
        _autoValue.TextAlignment = TextAlignment.Center;
        wrap.Children.Add(_autoValue);
        _autoPlus = MiniButton("+");
        _autoPlus.Click += delegate { SetAutoMax(_autoMax + 1); };
        wrap.Children.Add(_autoPlus);
        return wrap;
    }

    // ── settings panel (gear popup) ──────────────────────────────────────────────
    // A gear button that opens a Popup consolidating the formerly-scattered toolbar controls:
    // start tabs, ceiling (上限) tabs, auto-retry on/off, retry cap, and the NEW user-editable
    // disk floor (GB). Built fresh on every open so theme/lang are always current. Uses the same
    // flat control templates (MiniButton/FlatButtonTemplate) the rest of the cockpit uses.
    UIElement SettingsControl()
    {
        _gearBtn = IconButton("settings", 18, _lang == 0 ? "設定" : "Settings");
        _gearBtn.ToolTip = _lang == 0 ? "設定（タブ数・再試行・容量床）" : "Settings (tabs / retry / disk floor)";
        System.Windows.Automation.AutomationProperties.SetAutomationId(_gearBtn, "settingsButton");
        _settingsPopup = new System.Windows.Controls.Primitives.Popup();
        _settingsPopup.PlacementTarget = _gearBtn;
        _settingsPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
        _settingsPopup.StaysOpen = false;          // click-away closes it
        _settingsPopup.AllowsTransparency = true;
        // Right-align: offset so the panel's right edge lines up with the gear button's right edge
        // (grows leftward, avoids right-window-edge clipping). MinWidth=280, button~30px -> -(280-30).
        _settingsPopup.HorizontalOffset = -250;
        _gearBtn.Click += delegate
        {
            if (_settingsPopup.IsOpen) { _settingsPopup.IsOpen = false; return; }
            CloseHeaderPopups("settings");
            _settingsPopup.Child = BuildSettingsPanel();   // rebuild so theme/lang/values are fresh
            _settingsPopup.IsOpen = true;
        };
        return _gearBtn;
    }

    // Overflow "⋯" button: opens a compact popup listing the four low-frequency header actions.
    // Built like _settingsPopup: StaysOpen=false, rebuilt fresh on each open.
    UIElement OverflowControl()
    {
        _overflowBtn = new Button();
        // Built by hand rather than through IconButton, so it did not inherit the height token or
        // the rounded template and stayed square while the five beside it were fixed. Hand-built
        // twins are how one member of a set escapes every change made to the set.
        _overflowBtn.Width = 36; _overflowBtn.Height = HeaderCtrlH; _overflowBtn.Cursor = Cursors.Hand;
        _overflowBtn.BorderThickness = new Thickness(1); _overflowBtn.Margin = new Thickness(4, 0, 0, 0);
        _overflowBtn.Template = FlatButtonTemplate();
        _overflowBtn.ToolTip = _lang == 0 ? "その他のメニュー" : "More options";
        System.Windows.Automation.AutomationProperties.SetName(_overflowBtn, _lang == 0 ? "その他のメニュー" : "More options");
        // Draw three dots as text (no glyph needed; plain text at this size is crisp)
        _overflowBtn.Content = new TextBlock { Text = "⋯", FontSize = 14,
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        _overflowPopup = new System.Windows.Controls.Primitives.Popup();
        _overflowPopup.PlacementTarget = _overflowBtn;
        _overflowPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
        _overflowPopup.StaysOpen = false;
        _overflowPopup.AllowsTransparency = true;
        // Right-align: offset so the panel's right edge lines up with the overflow button's right edge
        // (grows leftward, avoids right-window-edge clipping). MinWidth=200, button~36px -> -(200-36).
        _overflowPopup.HorizontalOffset = -164;
        _overflowBtn.Click += delegate
        {
            if (_overflowPopup.IsOpen) { _overflowPopup.IsOpen = false; return; }
            CloseHeaderPopups("overflow");
            _overflowPopup.Child = BuildOverflowPanel();
            _overflowPopup.IsOpen = true;
        };
        return _overflowBtn;
    }

    UIElement BuildOverflowPanel()
    {
        // Only rare items live here: main chat and self-improve.
        // Lang/theme are now 1-click icon buttons directly in the header.
        var card = new Border();
        card.Background = Theme.Br(Theme.Surface(_dark));
        card.BorderBrush = Theme.Br(Theme.Border(_dark));
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadCard);
        card.Padding = new Thickness(4, 4, 4, 4);
        card.Margin = new Thickness(0, 4, 8, 4);
        card.MinWidth = 200;
        // Light, tasteful shadow (no heavy DropShadow; a thin border carries the panel)
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 8, ShadowDepth = 1, Opacity = 0.18, Color = C("#000000") };

        var col = new StackPanel();

        // Item 1: Open main chat -- icon row (settings-list style), not a flush text button.
        col.Children.Add(OverflowItem("chat",
            _lang == 0 ? "メインチャットを開く" : "Open main chat",
            delegate { _overflowPopup.IsOpen = false; OpenMain(); }));

        // Item 2: Self-improvement dashboard
        col.Children.Add(OverflowItem("account_tree",
            _lang == 0 ? "自己改善ダッシュボード" : "Self-improvement",
            delegate { _overflowPopup.IsOpen = false; new SelfImproveDashboardWindow().Show(); }));

        card.Child = col;
        return card;
    }

    // A menu row in the overflow popup: [icon]  label, full-width hover highlight -- so the two
    // entries read as distinct, tappable rows (settings-list style) instead of flush text that
    // needs a divider to tell apart.
    Button OverflowItem(string glyph, string label, Action action)
    {
        var b = new Button();
        b.Cursor = Cursors.Hand;
        b.Background = Brushes.Transparent;
        b.BorderThickness = new Thickness(0);
        b.Padding = new Thickness(12, 8, 16, 8);
        b.HorizontalContentAlignment = HorizontalAlignment.Left;
        b.Template = FlatButtonTemplate(true);   // left-align so the two rows' icons line up

        var row = new StackPanel();
        row.Orientation = Orientation.Horizontal;
        var icHost = new ContentControl();
        icHost.Content = MakeIcon(glyph, 16, Theme.Br(Theme.Muted(_dark)));
        icHost.Width = 22; icHost.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(icHost);
        var tb = new TextBlock();
        tb.Text = label; tb.Foreground = Fg; tb.FontSize = 13;
        tb.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(tb);
        b.Content = row;

        // FlatButtonTemplate has no IsMouseOver trigger, so drive the row highlight manually.
        Brush hov = Theme.Br(Theme.SurfaceSubtle(_dark));
        b.MouseEnter += delegate { b.Background = hov; };
        b.MouseLeave += delegate { b.Background = Brushes.Transparent; };

        Action a = action;
        b.Click += delegate { a(); };
        return b;
    }

    // One labeled −/+ stepper row for the settings panel. label on the left, [− value +] on the right.
    //: WHEN A CONTROL TAKES EFFECT. Mirrors tools/settings_keys.py, which is the declaration;
    //: test_the_panel_states_the_same_timing_the_declaration_does pins the two together.
    //:
    //: This is a copy on purpose. ui/rebuild_ui.ps1 enumerates its .cs files by hand, so a
    //: shared file added and forgotten breaks a button silently -- a trap this repository has
    //: already sprung. Copies are allowed; disagreeing copies are not.
    static string SettingsTiming(string key)
    {
        switch (key)
        {
            case "disk_floor_gb":
            case "ram_floor_mb":
            case "maxtabs":
                return "live";
            case "tool_probe_idle_min":
            case "merge_conversation":
            case "tool_transport":
            case "fanout_hierarchical_merge":
            case "fleet_auto_resume":
            case "rate_ceiling_rpm":
            case "supervisor_self_restart":
            case "job_approval_mode":
            case "fanout_write_scope":
            case "fanout_load_routing":
            case "team_mode":
            case "fanout_max_depth":
            case "fanout_max_total":
            case "fanout_max_active":
            case "fanout_max_turns":
            case "fanout_max_wall_min":
            case "effort_policy":
                return "each_gate";
            case "session_retention_days":
            case "session_max_mb":
                return "bridge_start";
            case "agent_board_mode":
            case "org_version_record":
            case "autoscale":
            case "autoscale_max":
            case "autoscale_per_tab_mb":
            case "effort":
            case "autoretry":
            case "autoretry_max":
            case "fanout":
            case "fleet_log_days":
            case "fleet_store_days":
            case "fleet_scratch_days":
            case "fleet_compress_hours":
                return "sweep_start";
            default:
                return "ui_only";
        }
    }

    //: The operator-facing words. Short enough to sit beside a label without wrapping, and
    //: never "applied" -- the panel writes a file; whether the fleet has read it is a fact the
    //: panel does not have, and showing the first as the second is the 2026-09-16 misreport.
    string TimingBadge(string key)
    {
        bool ja = _lang == 0;
        switch (SettingsTiming(key))
        {
            case "live":         return ja ? "実行中に反映" : "affects a running fleet";
            case "each_gate":    return ja ? "次の判定から"   : "from the next decision";
            case "sweep_start":  return ja ? "次の実行から"   : "from the next run";
            case "bridge_start": return ja ? "bridge再起動後" : "after a bridge restart";
            default:             return "";
        }
    }

    UIElement SettingsStepperRow(string label, TextBlock valueBlock, Button minus, Button plus)
    {
        return SettingsStepperRow(label, valueBlock, minus, plus, null);
    }

    UIElement SettingsStepperRow(string label, TextBlock valueBlock, Button minus, Button plus,
                                 string settingsKey)
    {
        var row = new DockPanel(); row.Margin = new Thickness(0, 5, 0, 5); row.LastChildFill = false;
        var lbl = new TextBlock(); lbl.Text = label; lbl.Foreground = Fg; lbl.FontSize = 12.5;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(lbl, Dock.Left); row.Children.Add(lbl);
        // WHEN IT TAKES EFFECT, beside the label rather than in a tooltip. Three of these
        // controls change a fleet that is already running and the rest do not, and nothing on
        // the screen distinguished them -- so "I changed it and nothing happened" was an
        // ordinary experience with no way to tell it from a defect.
        if (!string.IsNullOrEmpty(settingsKey))
        {
            string badge = TimingBadge(settingsKey);
            if (!string.IsNullOrEmpty(badge))
            {
                var when = new TextBlock(); when.Text = badge; when.Foreground = Muted;
                when.FontSize = 10.5; when.Margin = new Thickness(8, 0, 0, 0);
                when.VerticalAlignment = VerticalAlignment.Center;
                DockPanel.SetDock(when, Dock.Left); row.Children.Add(when);
            }
        }
        var stp = new StackPanel(); stp.Orientation = Orientation.Horizontal;
        stp.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(stp, Dock.Right);
        stp.Children.Add(minus);
        valueBlock.Foreground = Fg; valueBlock.FontSize = 13; valueBlock.FontWeight = FontWeights.SemiBold;
        valueBlock.Margin = new Thickness(12, 0, 12, 0); valueBlock.MinWidth = 28;
        valueBlock.TextAlignment = TextAlignment.Center; valueBlock.VerticalAlignment = VerticalAlignment.Center;
        stp.Children.Add(valueBlock);
        stp.Children.Add(plus);
        row.Children.Add(stp);
        return row;
    }

    TextBlock _diskFloorVal;
    // The re-unlock control's fields lived here. Removed with the control itself: the harness
    // asks every sweep whether a logged refusal went unclaimed and delivers the unlock on its
    // own (relay/fleet_runner.py:sweep_unclaimed_refusals). The receipt it produces still lands
    // in status.json and is printed by scripts/status.py, where a record belongs -- a panel
    // note that has to be read while it is on screen is not a record.
    TextBlock SectionHeader(string text)
    {
        var t = new TextBlock(); t.Text = text; t.Foreground = Muted; t.FontSize = 11;
        t.FontWeight = FontWeights.SemiBold; t.Margin = new Thickness(0, 12, 0, 2);
        return t;
    }

    System.Windows.Controls.Primitives.Popup _advancedPopup;

    // The row that opens 詳細設定 to the side. Styled as a menu item -- label left, chevron
    // right -- so it reads as "there is more behind this", not as another setting.
    UIElement AdvancedSubmenuRow()
    {
        var btn = new Button();
        btn.Cursor = Cursors.Hand;
        btn.HorizontalContentAlignment = HorizontalAlignment.Stretch;
        btn.Padding = new Thickness(8, 6, 8, 6);
        btn.Margin = new Thickness(0, 2, 0, 2);
        btn.Background = BtnBg; btn.Foreground = Fg; btn.BorderBrush = Border;
        btn.BorderThickness = new Thickness(1);
        btn.Template = FlatButtonTemplate();

        var row = new DockPanel(); row.LastChildFill = true;
        var chev = new TextBlock();
        chev.Text = "›";                       // ›
        chev.Foreground = Muted; chev.FontSize = 14;
        chev.VerticalAlignment = VerticalAlignment.Center;
        DockPanel.SetDock(chev, Dock.Right); row.Children.Add(chev);
        var lbl = new TextBlock();
        lbl.Text = L("アクセス範囲・接続クライアント", "Access scope & connected clients");
        lbl.Foreground = Fg; lbl.FontSize = 12.5;
        lbl.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(lbl);
        btn.Content = row;

        // Automation name: the row's own label describes its CONTENTS, so without this the
        // submenu is unfindable by name in tooling and by a screen reader looking for 詳細設定.
        System.Windows.Automation.AutomationProperties.SetName(btn, L("詳細設定", "Advanced"));

        btn.Click += delegate { ToggleAdvancedPopup(btn); };
        return btn;
    }

    void ToggleAdvancedPopup(UIElement anchor)
    {
        if (_advancedPopup != null && _advancedPopup.IsOpen)
        {
            _advancedPopup.IsOpen = false;
            return;
        }
        _advancedPopup = new System.Windows.Controls.Primitives.Popup();
        _advancedPopup.PlacementTarget = anchor;
        // Left, not Right: the settings panel is right-aligned to the gear and grows leftward,
        // so its right edge is already at the window edge -- a submenu placed to the right
        // opens off-screen or on top of its own parent.
        _advancedPopup.Placement = System.Windows.Controls.Primitives.PlacementMode.Left;
        _advancedPopup.VerticalOffset = -8;
        _advancedPopup.AllowsTransparency = true;
        _advancedPopup.StaysOpen = false;

        var card = new System.Windows.Controls.Border();
        card.Background = CardBg; card.BorderBrush = Border; card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadPopover); card.Padding = new Thickness(16, 12, 16, 16);
        card.Margin = new Thickness(8, 6, 8, 6); card.MinWidth = 340; card.MaxWidth = 420;
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 16, ShadowDepth = 2, Opacity = 0.28, Color = C("#000000") };

        var col = new StackPanel(); col.Orientation = Orientation.Vertical;
        var title = new TextBlock(); title.Text = L("詳細設定", "Advanced"); title.Foreground = Fg;
        title.FontSize = 14; title.FontWeight = FontWeights.SemiBold;
        title.Margin = new Thickness(0, 0, 0, 6);
        col.Children.Add(title);
        // The lists inside can be long (one row per allowed folder, one per connected client),
        // and a popup does not scroll on its own -- without this it grows past the screen and
        // the buttons at the bottom become unreachable.
        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        scroll.MaxHeight = 520;
        scroll.Content = BuildAdvancedSettingsSection();
        col.Children.Add(scroll);
        card.Child = col;
        _advancedPopup.Child = card;

        // The parent settings popup closes on any click outside its own visual tree, and this
        // child is a separate HWND -- so opening it would immediately dismiss the parent and
        // take the child with it. Hold the parent open for as long as the child is up.
        if (_settingsPopup != null)
        {
            _settingsPopup.StaysOpen = true;
            _advancedPopup.Closed += delegate
            {
                if (_settingsPopup != null) _settingsPopup.StaysOpen = false;
            };
        }
        _advancedPopup.IsOpen = true;
    }

    void SetRetDays(int v)
    {
        _retDays = Math.Max(0, Math.Min(3650, v));
        SaveKey("session_retention_days", _retDays.ToString());
        if (_retDaysValue != null) _retDaysValue.Text = _retDays == 0 ? T("ret_keep") : _retDays.ToString();
    }

    void SetRetMb(int v)
    {
        // Steps of 100 MB below 1 GB and 500 above it: the numbers that matter here are
        // "a few hundred" and "a couple of gigabytes", and single-MB precision would mean
        // holding the button down for a minute to reach either.
        _retMb = Math.Max(0, Math.Min(100000, v));
        SaveKey("session_max_mb", _retMb.ToString());
        if (_retMbValue != null) _retMbValue.Text = _retMb == 0 ? T("ret_keep") : _retMb.ToString();
    }

    int RetMbStep() { return _retMb >= 1000 ? 500 : 100; }

    //: FIXED, because a step computed from the current value changes under the button as you
    //: press it, and the two directions disagree at every boundary: from 100 minus gave 90
    //: (step 10) while from 110 minus gave 85 (step 25), so + followed by - did not return you
    //: to where you started. The earlier version was coarse near the top so the button need not
    //: be held down -- a fair concern -- but an increment you cannot predict is worse than a few
    //: more presses, and 5 still reaches both of the settings that matter (a bit under the
    //: published 100, and low because we are being refused) quickly.
    const int RATE_STEP = 5;

    void SetRateCeiling(int v)
    {
        // 0 means no ceiling and it is REACHABLE: stepping down past the last notch lands on it
        // rather than clamping above it, so turning this off never requires editing a file.
        _rateCeiling = Math.Max(0, Math.Min(10000, v));
        SaveKey("rate_ceiling_rpm", _rateCeiling.ToString());
        if (_rateCeilingValue != null)
            _rateCeilingValue.Text = _rateCeiling == 0 ? T("ret_keep") : _rateCeiling.ToString();
    }

    void SetFleetLogDays(int v)
    {
        _fleetLogDays = Math.Max(1, Math.Min(3650, v));
        SaveKey("fleet_log_days", _fleetLogDays.ToString());
        if (_fleetLogDaysValue != null) _fleetLogDaysValue.Text = _fleetLogDays.ToString();
    }

    void SetSidebarCap(int v)
    {
        _sidebarCap = Math.Max(0, Math.Min(500, v));
        SaveKey("sidebar_section_cap", _sidebarCap.ToString());
        if (_sidebarCapValue != null) _sidebarCapValue.Text = _sidebarCap == 0 ? T("sidebar_cap_all") : _sidebarCap.ToString();
    }

    void SetFleetStoreDays(int v)
    {
        _fleetStoreDays = Math.Max(1, Math.Min(3650, v));
        SaveKey("fleet_store_days", _fleetStoreDays.ToString());
        if (_fleetStoreDaysValue != null) _fleetStoreDaysValue.Text = _fleetStoreDays.ToString();
    }

    void SetFleetScratchDays(int v)
    {
        _fleetScratchDays = Math.Max(1, Math.Min(3650, v));
        SaveKey("fleet_scratch_days", _fleetScratchDays.ToString());
        if (_fleetScratchDaysValue != null) _fleetScratchDaysValue.Text = _fleetScratchDays.ToString();
    }

    void SetFleetCompressHours(int v)
    {
        // A FLOOR OF ONE HOUR, not zero. Compressing a run's files the moment it finishes
        // would fight whatever is still reading them, and "0" reads as "off" to an operator
        // while relay/fleet_retention.py would take it literally.
        _fleetCompressHours = Math.Max(1, Math.Min(720, v));
        SaveKey("fleet_compress_hours", _fleetCompressHours.ToString());
        if (_fleetCompressHoursValue != null)
            _fleetCompressHoursValue.Text = _fleetCompressHours.ToString();
    }

    UIElement BuildSettingsPanel()
    {
        var card = new Border();
        card.Background = CardBg; card.BorderBrush = Border; card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadPopover); card.Padding = new Thickness(16, 12, 16, 16);
        card.Margin = new Thickness(0, 6, 8, 6); card.MinWidth = 280;
        // AND A CEILING, because MinWidth alone lets the card take the whole host. Every row
        // here docks its label left and its stepper right, which is right at a panel width and
        // absurd at half a window: the control ends up a hand's width from the label it
        // belongs to. 332 = the 300 the notes in this panel already cap themselves at, plus
        // the 16+16 padding -- so the widest thing inside decides the width, as it should,
        // and nothing inside is wider than a readable line.
        card.MaxWidth = 332;
        // soft shadow so the floating panel reads as elevated
        card.Effect = new System.Windows.Media.Effects.DropShadowEffect
        { BlurRadius = 16, ShadowDepth = 2, Opacity = 0.28, Color = C("#000000") };

        var col = new StackPanel(); col.Orientation = Orientation.Vertical;

        var title = new TextBlock(); title.Text = T("settings"); title.Foreground = Fg;
        title.FontSize = 14; title.FontWeight = FontWeights.SemiBold; title.Margin = new Thickness(0, 0, 0, 4);
        col.Children.Add(title);

        // ── Parallel tabs: start + ceiling (上限) steppers ──
        col.Children.Add(SectionHeader(T("set_tabs_section")));
        col.Children.Add(AutoscaleControls());
        var startMinus = MiniButton("−"); startMinus.Click += delegate { SetMaxTabs(_maxtabs - 1); };
        var startPlus = MiniButton("+"); startPlus.Click += delegate { SetMaxTabs(_maxtabs + 1); };
        _maxMinus = startMinus; _maxPlus = startPlus;   // keep refs so PaintChrome re-themes them (mirrors ceiling)
        _maxValue = new TextBlock(); _maxValue.Text = _maxtabs.ToString();
        col.Children.Add(SettingsStepperRow(T("def_tabs"), _maxValue, startMinus, startPlus, "maxtabs"));
        // ceiling stepper -- reuses _autoLbl/_autoMinus/_autoValue/_autoPlus via CeilingStepper fields
        var ceilMinus = MiniButton("−"); ceilMinus.Click += delegate { SetAutoMax(_autoMax - 1); };
        var ceilPlus = MiniButton("+"); ceilPlus.Click += delegate { SetAutoMax(_autoMax + 1); };
        _autoValue = new TextBlock(); _autoValue.Text = _autoMax.ToString();
        _autoMinus = ceilMinus; _autoPlus = ceilPlus;   // keep refs so UpdateAutoEnabled can grey them
        col.Children.Add(SettingsStepperRow(T("max_tabs2"), _autoValue, ceilMinus, ceilPlus, "autoscale_max"));

        // ── Conversation retention: days (default 90, since 2026-09-24) + size cap (default: no cap) ──
        col.Children.Add(SectionHeader(T("set_retention_section")));
        var retDMinus = MiniButton("−"); retDMinus.Click += delegate { SetRetDays(_retDays - (_retDays > 30 ? 30 : 7)); };
        var retDPlus = MiniButton("+"); retDPlus.Click += delegate { SetRetDays(_retDays + (_retDays >= 30 ? 30 : 7)); };
        _retDaysValue = new TextBlock(); _retDaysValue.Text = _retDays == 0 ? T("ret_keep") : _retDays.ToString();
        col.Children.Add(SettingsStepperRow(T("ret_days"), _retDaysValue, retDMinus, retDPlus, "session_retention_days"));

        var retMMinus = MiniButton("−"); retMMinus.Click += delegate { SetRetMb(_retMb - RetMbStep()); };
        var retMPlus = MiniButton("+"); retMPlus.Click += delegate { SetRetMb(_retMb + RetMbStep()); };
        _retMbValue = new TextBlock(); _retMbValue.Text = _retMb == 0 ? T("ret_keep") : _retMb.ToString();
        col.Children.Add(SettingsStepperRow(T("ret_mb"), _retMbValue, retMMinus, retMPlus, "session_max_mb"));

        // -- Rate ceiling: the line admission holds at, and the strip's denominator --
        col.Children.Add(SectionHeader(T("set_rate_section")));
        var rcMinus = MiniButton("−"); rcMinus.Click += delegate { SetRateCeiling(_rateCeiling - RATE_STEP); };
        var rcPlus = MiniButton("+"); rcPlus.Click += delegate { SetRateCeiling(_rateCeiling + RATE_STEP); };
        _rateCeilingValue = new TextBlock();
        _rateCeilingValue.Text = _rateCeiling == 0 ? T("ret_keep") : _rateCeiling.ToString();
        col.Children.Add(SettingsStepperRow(T("rate_rpm"), _rateCeilingValue, rcMinus, rcPlus, "rate_ceiling_rpm"));
        var rcNote = new TextBlock();
        rcNote.FontSize = 11; rcNote.TextWrapping = TextWrapping.Wrap;
        rcNote.Margin = new Thickness(0, 2, 0, 2); rcNote.MaxWidth = 300;
        rcNote.Text = T("rate_note");
        col.Children.Add(rcNote);

        // -- Working directory: how long finished runs' logs are kept --
        col.Children.Add(SectionHeader(T("set_fleetret_section")));
        var flMinus = MiniButton("−"); flMinus.Click += delegate { SetFleetLogDays(_fleetLogDays - 7); };
        var flPlus = MiniButton("+"); flPlus.Click += delegate { SetFleetLogDays(_fleetLogDays + 7); };
        _fleetLogDaysValue = new TextBlock(); _fleetLogDaysValue.Text = _fleetLogDays.ToString();
        col.Children.Add(SettingsStepperRow(T("fleet_log_days"), _fleetLogDaysValue, flMinus, flPlus, "fleet_log_days"));

        var fsMinus = MiniButton("−"); fsMinus.Click += delegate { SetFleetStoreDays(_fleetStoreDays - 7); };
        var fsPlus = MiniButton("+"); fsPlus.Click += delegate { SetFleetStoreDays(_fleetStoreDays + 7); };
        _fleetStoreDaysValue = new TextBlock(); _fleetStoreDaysValue.Text = _fleetStoreDays.ToString();
        col.Children.Add(SettingsStepperRow(T("fleet_store_days"), _fleetStoreDaysValue, fsMinus, fsPlus, "fleet_store_days"));

        // -- Chat sidebar: how many conversations each section lists before "+N more" (the Fleet
        // runs section hid tonight's runs behind the old fixed 8). Lives HERE, in the popup, never
        // in the header; the chat window re-reads settings.txt and re-renders.
        col.Children.Add(SectionHeader(T("set_sidebar_section")));
        var sbMinus = MiniButton("\u2212"); sbMinus.Click += delegate { SetSidebarCap(_sidebarCap - 4); };
        var sbPlus = MiniButton("+"); sbPlus.Click += delegate { SetSidebarCap(_sidebarCap + 4); };
        _sidebarCapValue = new TextBlock(); _sidebarCapValue.Text = _sidebarCap == 0 ? T("sidebar_cap_all") : _sidebarCap.ToString();
        col.Children.Add(SettingsStepperRow(T("sidebar_section_cap"), _sidebarCapValue, sbMinus, sbPlus, "sidebar_section_cap"));

        // TWO KEYS THE PANEL READ BUT NEVER WROTE. relay/fleet_retention.py has asked for
        // fleet_scratch_days and fleet_compress_hours on every run since it was written; no
        // control here ever set them, so the only way to choose was to edit settings.txt by
        // hand -- which is the one thing an operator is told not to do. A setting reachable
        // only by breaking the rule about settings is a setting nobody has.
        var fscMinus = MiniButton("−"); fscMinus.Click += delegate { SetFleetScratchDays(_fleetScratchDays - 7); };
        var fscPlus = MiniButton("+"); fscPlus.Click += delegate { SetFleetScratchDays(_fleetScratchDays + 7); };
        _fleetScratchDaysValue = new TextBlock(); _fleetScratchDaysValue.Text = _fleetScratchDays.ToString();
        col.Children.Add(SettingsStepperRow(T("fleet_scratch_days"), _fleetScratchDaysValue,
                                            fscMinus, fscPlus, "fleet_scratch_days"));

        var fchMinus = MiniButton("−"); fchMinus.Click += delegate { SetFleetCompressHours(_fleetCompressHours - 1); };
        var fchPlus = MiniButton("+"); fchPlus.Click += delegate { SetFleetCompressHours(_fleetCompressHours + 1); };
        _fleetCompressHoursValue = new TextBlock(); _fleetCompressHoursValue.Text = _fleetCompressHours.ToString();
        col.Children.Add(SettingsStepperRow(T("fleet_compress_hours"), _fleetCompressHoursValue,
                                            fchMinus, fchPlus, "fleet_compress_hours"));

        var flNote = new TextBlock();
        flNote.FontSize = 11; flNote.TextWrapping = TextWrapping.Wrap;
        flNote.Margin = new Thickness(0, 2, 0, 2); flNote.MaxWidth = 300;
        flNote.Text = T("fleet_ret_note");
        col.Children.Add(flNote);

        // ── Auto-retry: on/off toggle + cap ──
        col.Children.Add(SectionHeader(T("set_retry_section")));
        _autoRetryBtn = new Button();
        _autoRetryBtn.BorderThickness = new Thickness(1); _autoRetryBtn.Cursor = Cursors.Hand;
        _autoRetryBtn.Padding = new Thickness(12, 4, 12, 4); _autoRetryBtn.FontSize = 12;
        _autoRetryBtn.FontWeight = FontWeights.SemiBold; _autoRetryBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _autoRetryBtn.Margin = new Thickness(0, 2, 0, 4);
        _autoRetryBtn.Template = FlatButtonTemplate();
        _autoRetryBtn.ToolTip = _lang == 0
            ? "停止したゴールを自動で再投入（上限まで・既定OFF）。無限ループは起きません。"
            : "Auto re-queue stopped goals (up to the cap; default OFF). Never loops forever.";
        _autoRetryBtn.Click += delegate { _autoRetry = !_autoRetry; SaveKey("autoretry", _autoRetry ? "1" : "0"); PaintAutoRetryBtn(); };
        PaintAutoRetryBtn();
        col.Children.Add(_autoRetryBtn);
        var capMinus = MiniButton("−"); capMinus.Click += delegate { SetAutoRetryMax(_autoRetryMax - 1); };
        var capPlus = MiniButton("+"); capPlus.Click += delegate { SetAutoRetryMax(_autoRetryMax + 1); };
        _autoRetryCapVal = new TextBlock(); _autoRetryCapVal.Text = _autoRetryMax.ToString();
        col.Children.Add(SettingsStepperRow(T("cap"), _autoRetryCapVal, capMinus, capPlus, "autoretry_max"));

        // ── P2 (c): Auto-archive on finish (default OFF) ──
        col.Children.Add(SectionHeader(T("set_archive_section")));
        _autoArchiveBtn = new Button();
        _autoArchiveBtn.BorderThickness = new Thickness(1); _autoArchiveBtn.Cursor = Cursors.Hand;
        _autoArchiveBtn.Padding = new Thickness(12, 4, 12, 4); _autoArchiveBtn.FontSize = 12;
        _autoArchiveBtn.FontWeight = FontWeights.SemiBold; _autoArchiveBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _autoArchiveBtn.Margin = new Thickness(0, 2, 0, 4);
        _autoArchiveBtn.Template = FlatButtonTemplate();
        _autoArchiveBtn.ToolTip = _lang == 0
            ? "ラン終了時に完了カードを自動で履歴へ移動（既定OFF）。"
            : "Move completed cards to History automatically when the run finishes (default OFF).";
        _autoArchiveBtn.Click += delegate { _autoArchive = !_autoArchive; SaveKey("autoarchive", _autoArchive ? "1" : "0"); PaintAutoArchiveBtn(); };
        PaintAutoArchiveBtn();
        col.Children.Add(_autoArchiveBtn);

        // ── Capacity guard: NEW user-editable disk floor (GB) ──
        col.Children.Add(SectionHeader(T("set_capacity_section")));
        var dfMinus = MiniButton("−"); dfMinus.Click += delegate { SetDiskFloor(_diskFloor - 1.0); };
        var dfPlus = MiniButton("+"); dfPlus.Click += delegate { SetDiskFloor(_diskFloor + 1.0); };
        _diskFloorVal = new TextBlock(); _diskFloorVal.Text = FmtFloor(_diskFloor);
        col.Children.Add(SettingsStepperRow(T("disk_floor"), _diskFloorVal, dfMinus, dfPlus, "disk_floor_gb"));
        // RAM floor (MB) -- the free-RAM reserve the autoscale keeps for the user (256 MB steps)
        var rfMinus = MiniButton("−"); rfMinus.Click += delegate { SetRamFloor(_ramFloor - 256.0); };
        var rfPlus = MiniButton("+"); rfPlus.Click += delegate { SetRamFloor(_ramFloor + 256.0); };
        _ramFloorVal = new TextBlock(); _ramFloorVal.Text = ((int)_ramFloor).ToString();
        col.Children.Add(SettingsStepperRow(T("ram_floor"), _ramFloorVal, rfMinus, rfPlus, "ram_floor_mb"));
        var hint = new TextBlock(); hint.Text = T("disk_floor_hint"); hint.Foreground = Muted;
        hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap; hint.Margin = new Thickness(0, 0, 0, 2);
        hint.MaxWidth = 300;   // the cap its four sibling notes already carry
        col.Children.Add(hint);

        // ── Approval policy, WHERE IT CAN BE CHANGED WHEN NOBODY IS WAITING.
        //
        // The same three choices live in ApprovalPromptWindow, and until now that was the ONLY
        // place they lived -- so the policy could only be changed at the moment an approval was
        // being demanded. A setting that is read at every gate (see tools/settings_keys.py:
        // job_approval_mode is each_gate) could be reconsidered only while someone was being
        // hurried. That is backwards for an approval control, and the operator said so.
        //
        // ONE OWNER FOR THE VALUE. This calls ApprovalPromptWindow's own ReadPolicy/SavePolicy
        // rather than growing a third reader of job_approval_mode -- five copies of one
        // settings path is how 2026-09-16 happened, and a policy is a worse thing to have two
        // opinions about than a path.
        col.Children.Add(SectionHeader(T("set_approval_section")));
        var policyRow = new ComboBox();
        policyRow.FontSize = 12;
        policyRow.MinWidth = 210;
        policyRow.HorizontalAlignment = HorizontalAlignment.Left;
        policyRow.Margin = new Thickness(0, 2, 0, 2);
        policyRow.Background = BtnBg; policyRow.Foreground = Fg; policyRow.BorderBrush = Border;
        policyRow.Cursor = Cursors.Hand;
        policyRow.ToolTip = T("set_approval_hint");
        // SAME ORDER AND SAME WORDS as the dialog. Two lists that agree on the values and
        // disagree on the labels teach an operator that they are different settings.
        var policyVals = new[] { "auto", "bypass", "default" };
        foreach (string v in policyVals)
        {
            string label = v == "auto" ? (_lang == 0 ? "自動（推奨）" : "Auto (recommended)")
                        : v == "bypass" ? (_lang == 0 ? "バイパス" : "Bypass")
                        : (_lang == 0 ? "毎回確認（非推奨）" : "Confirm every time (not recommended)");
            policyRow.Items.Add(new ComboBoxItem { Content = label, Tag = v });
        }
        string cur = ApprovalPromptWindow.ReadPolicy();
        for (int i = 0; i < policyRow.Items.Count; i++)
        {
            var it = policyRow.Items[i] as ComboBoxItem;
            if (it != null && string.Equals((string)it.Tag, cur, StringComparison.OrdinalIgnoreCase))
            { policyRow.SelectedIndex = i; break; }
        }
        if (policyRow.SelectedIndex < 0) policyRow.SelectedIndex = 0;
        policyRow.SelectionChanged += delegate
        {
            var it = policyRow.SelectedItem as ComboBoxItem;
            if (it == null) return;
            string v = (string)it.Tag;
            if (string.Equals(v, ApprovalPromptWindow.ReadPolicy(), StringComparison.OrdinalIgnoreCase)) return;
            ApprovalPromptWindow.SavePolicy(v);
        };
        StyleFlatCombo(policyRow);
        col.Children.Add(policyRow);
        var policyNote = new TextBlock();
        policyNote.Text = T("set_approval_note");
        policyNote.Foreground = Muted; policyNote.FontSize = 10.5;
        policyNote.TextWrapping = TextWrapping.Wrap; policyNote.MaxWidth = 300;
        policyNote.Margin = new Thickness(0, 2, 0, 2);
        col.Children.Add(policyNote);

        // ── Effort policy. The effort level itself stays in the header; the policy that may
        // adjust it per worker (off|shadow|on) lives here, with the runner's "in effect" line.
        col.Children.Add(SectionHeader(L("推論", "Effort")));
        col.Children.Add(EffortPolicyControl());

        // ── Fan-out / 分割: every fan-out control in ONE section. They used to sit in the cockpit
        // header (one slice added one control each) and crowded it out; new fan-out settings
        // go here, not in the header (relay/test_cockpit_header_controls.py pins the header).
        col.Children.Add(SectionHeader(L("分割 / Fan-out", "Fan-out")));
        col.Children.Add(FanoutControl());
        col.Children.Add(FanoutDepthControl());
        col.Children.Add(HierarchicalMergeControl());
        col.Children.Add(WriteScopeControl());
        col.Children.Add(LoadRoutingControl());
        col.Children.Add(TeamModeControl());
        col.Children.Add(AgentBoardControl());
        col.Children.Add(OrgVersionControl());
        col.Children.Add(FanoutBudgetControl());
        col.Children.Add(MergeConversationControl());
        // the controls' Paint* run on every build, but fill the "in effect" lines from the
        // latest status.json too, so a freshly opened popup is never blank until the next tick
        PaintEffortPolicyInEffect(_lastRoot); PaintFanoutInEffect(_lastRoot);
        PaintFanoutBudgetInEffect(_lastRoot); PaintFanoutDepthInEffect(_lastRoot);
        PaintHierarchicalMergeInEffect(_lastRoot); PaintWriteScopeInEffect(_lastRoot);
        PaintLoadRoutingInEffect(_lastRoot); PaintAgentBoardInEffect(_lastRoot);
        PaintTeamModeInEffect(_lastRoot);
        PaintMergeConversationInEffect(_lastRoot);

        // ── Recovery / 復旧: what happens to a run whose coordinator died. One control for now;
        // like the fan-out ones it lives in this popup, never in the header.
        col.Children.Add(SectionHeader(L("復旧 / Recovery", "Recovery")));
        col.Children.Add(AutoResumeControl());
        PaintAutoResumeInEffect(_lastRoot);
        col.Children.Add(SupervisorSelfRestartControl());
        col.Children.Add(ToolProbeControl());
        PaintToolProbeInEffect();

        // ── Tool route / ツール: how a fleet conversation reaches its tools (connector or connector-less).
        col.Children.Add(SectionHeader(L("ツール", "Tools")));
        col.Children.Add(ToolRouteControl());

        // THE RE-UNLOCK CONTROL WAS REMOVED HERE, DELIBERATELY, AND MUST NOT COME BACK.
        //
        // It was a worker-name box and a "send re-unlock" button, for the case its own tooltip
        // described: a worker stopped by a lock that automatic recovery had not noticed. That
        // is the harness handing its own failure to a person -- and the person is the part of
        // this system least able to know which worker, if any, is the stuck one. The operator
        // said so, twice.
        //
        // The hole it covered was real: twice on 2026-09-15 a worker was refused for lock, no
        // recovery fired, and the run carried on, once producing a deliverable that claimed to
        // have verified content it had never read. That hole is now closed on the harness side
        // by relay/fleet_runner.py:sweep_unclaimed_refusals, which asks every sweep whether a
        // refusal the server logged was picked up by any reader, and delivers the unlock itself
        // when none was. A button is not needed to answer a question the machine can ask.

        // ── Chat: always-available manual bridge reconnect. Unlike the Fix button (only shown
        // when a dot is red/yellow), this fires on demand regardless of the Tool dot's state --
        // it may be GRAY (self-probe feature not yet active on this machine) or the user may just
        // want to force a reconnect without waiting for the next probe cycle. Warning-outline pill,
        // matching the visual language the inline Fix pill already uses for repair actions.
        col.Children.Add(SectionHeader(T("set_chat_section")));
        _reconnectChatBtn = new Button();
        _reconnectChatBtn.Content = T("reconnect_chat");
        _reconnectChatBtn.FontSize = 12; _reconnectChatBtn.FontWeight = FontWeights.SemiBold;
        _reconnectChatBtn.Cursor = Cursors.Hand; _reconnectChatBtn.BorderThickness = new Thickness(1);
        _reconnectChatBtn.Padding = new Thickness(12, 4, 12, 4);
        _reconnectChatBtn.HorizontalAlignment = HorizontalAlignment.Left;
        _reconnectChatBtn.Margin = new Thickness(0, 2, 0, 4);
        _reconnectChatBtn.Template = FlatButtonTemplate();
        _reconnectChatBtn.Background = Brushes.Transparent;
        _reconnectChatBtn.ToolTip = T("reconnect_chat_hint");
        // COLOURED BY WHETHER IT IS NEEDED, NOT BY WHETHER IT EXISTS. These two lines were
        // `Theme.Warning` unconditionally, so the control sat amber from the moment the panel
        // opened, on a machine with nothing wrong. A warning that is always on is not a warning;
        // the operator asked why it is never grey, which is the question a permanently-lit
        // indicator always eventually produces, and by then it has taught everyone to ignore it.
        //
        // The button STAYS clickable in every state -- that was a deliberate decision (see
        // RunBridgeReconnectManual: the Tool dot can read Gray on a machine whose bridge
        // self-probe is not active, and the operator may simply want to force a reconnect). What
        // was wrong was tying the colour to the same always-true fact as the availability.
        RefreshReconnectChatTint();
        System.Windows.Automation.AutomationProperties.SetName(_reconnectChatBtn, T("reconnect_chat"));
        _reconnectChatBtn.Click += delegate { RunBridgeReconnectManual(); };
        col.Children.Add(_reconnectChatBtn);

        // ── 表示サイズ / UI scale: [自動 ▸] then [−] [自動 (130%)] [+] (step 0.1, live-apply + persist) ──
        // Auto toggle keeps a CONSTANT physical size across monitors (target/monitorScale). When Auto is
        // on the value shows "自動 (NNN%)" (the current effective %) and the −/+ steppers are disabled;
        // clicking −/+ (or toggling Auto off) drops to manual at the current number.
        col.Children.Add(SectionHeader(T("ui_scale_section")));
        _uiAutoBtn = MiniButton(_uiAuto ? T("ui_auto_on") : T("ui_auto_off"));
        _uiAutoBtn.Width = double.NaN; _uiAutoBtn.MinWidth = 78; _uiAutoBtn.Padding = new Thickness(8, 0, 8, 0);
        _uiAutoBtn.Click += delegate
        {
            if (_uiAuto) ApplyUiScale(_uiScale, true);   // Auto -> manual at the current effective number
            else EnableAutoScale(true);                  // manual -> Auto (recompute for this monitor)
        };
        var autoRow = new DockPanel(); autoRow.Margin = new Thickness(0, 5, 0, 0); autoRow.LastChildFill = false;
        DockPanel.SetDock(_uiAutoBtn, Dock.Right); autoRow.Children.Add(_uiAutoBtn);
        col.Children.Add(autoRow);
        _uiScaleMinus = MiniButton("−"); _uiScaleMinus.Click += delegate { ApplyUiScale(_uiScale - 0.1, true); };
        _uiScalePlus = MiniButton("+"); _uiScalePlus.Click += delegate { ApplyUiScale(_uiScale + 0.1, true); };
        _uiScaleVal = new TextBlock(); _uiScaleVal.Text = AutoLabelText();
        _uiScaleVal.MinWidth = 84;   // wider: holds "自動 (130%)"
        col.Children.Add(SettingsStepperRow(T("ui_scale"), _uiScaleVal, _uiScaleMinus, _uiScalePlus));
        var usHint = new TextBlock(); usHint.Text = T("ui_scale_hint"); usHint.Foreground = Muted;
        usHint.FontSize = 10.5; usHint.TextWrapping = TextWrapping.Wrap; usHint.Margin = new Thickness(0, 0, 0, 2);
        col.Children.Add(usHint);
        RefreshUiScaleControls();   // set label/toggle/stepper-enabled to match the current mode

        // ── 詳細設定 / Advanced ──
        // A submenu ROW, not an inline section. Inlined, it added roughly a screen of folder
        // list + client list to a panel that is already long, so the everyday settings above
        // it got pushed off and the operator could not find it at all. It opens to the side
        // like a context-menu submenu, which is where people expect "more, over here".
        col.Children.Add(SectionHeader(L("詳細設定", "Advanced")));
        col.Children.Add(AdvancedSubmenuRow());
        // Nothing is added after this point, which is exactly why it was the section that
        // vanished: see the ScrollViewer added below `card.Child`.

        // A POPUP DOES NOT SCROLL ON ITS OWN, and this one had grown past the screen.
        //
        // The settings panel is a StackPanel of sections that has only ever got longer, and the
        // last of them -- 詳細設定, which is where アクセス範囲 and 接続クライアント live -- fell
        // off the bottom of the display. The operator reported it as "詳細設定 が消えている",
        // and every check said otherwise: the section is in the source (26 tests assert it), the
        // binary was built after the source, the running process was started from that binary,
        // and both `詳細設定` and `アクセス範囲・接続クライアント` are present as UTF-16 strings
        // inside the running executable. It was built, it was there, and it was unreachable.
        //
        // The same trap is already written down 200 lines below, on the popup this row OPENS:
        // "a popup does not scroll on its own -- without this it grows past the screen and the
        // buttons at the bottom become unreachable." The child was given a ScrollViewer and the
        // parent was not.
        //
        // MaxHeight comes from the work area rather than a constant so this cannot silently
        // return on a shorter display or at a larger UI scale -- the panel scales with
        // `ui_scale`, so a fixed pixel budget is only ever right for one setting of it.
        var panelScroll = new ScrollViewer();
        panelScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        panelScroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        panelScroll.MaxHeight = Math.Max(320, SystemParameters.WorkArea.Height - 120);
        panelScroll.Content = col;
        card.Child = panelScroll;
        UpdateAutoEnabled();   // grey the ceiling stepper if autoscale is off
        return card;
    }

    // ══════════════════════════════════════════════════════════════════════════════════
    // 詳細設定 / Advanced
    // ══════════════════════════════════════════════════════════════════════════════════
    //
    // アクセス範囲 (access scope): the folder allow-list tools/folder_policy.py reads from
    // .fleet/folder_access.json ({"enabled":bool, "global":[...], "scopes":{...}}). This panel
    // only ever touches "enabled" and "global" -- "scopes" (per fleet-lane overrides) is read
    // back and re-written byte-for-byte untouched on every save, never edited here.
    //
    // フルアクセス (full access, enabled:false) is the operator's explicit choice and MUST stay
    // the default and MUST stay reachable with one click and no confirmation dialog -- do not
    // "improve" this into a warning prompt or flip the default to restricted.
    //
    // 接続クライアント (connected clients): tools/security.py's grant_ip/revoke_ip are LOCAL
    // admin functions (never MCP tools -- see that file's header comment) that this panel shells
    // out to via `python -m tools.security ...`, exactly like the file's own _cli() docstring
    // says the cockpit does. The cockpit never opens or writes .unlock_state.json itself, so the
    // atomic tmp-file+os.replace write tools/security.py already does is the only writer.
    UIElement BuildAdvancedSettingsSection()
    {
        var col = new StackPanel();
        col.Children.Add(BuildAccessScopeControls());
        col.Children.Add(BuildConnectedClientsControls());
        return col;
    }

    static string FmtFloor(double v)
    {
        return v.ToString("0.#", System.Globalization.CultureInfo.InvariantCulture);
    }

    // Disk floor (GB) setter: clamp, persist via SaveKey, refresh the panel value, AND push the new
    // floor LIVE to a running fleet via {"set_disk_floor_gb":N} through the SAME one-file-per-command
    // writer Pause/ForceStart use, so the runner (fleet_runner.py ~L561) picks it up on its next ~1s
    // poll. Mirrors how 強制開始 writes the floor live.
    void SetDiskFloor(double v)
    {
        _diskFloor = Math.Max(0.0, Math.Min(100.0, Math.Round(v, 1)));
        SaveKey("disk_floor_gb", FmtFloor(_diskFloor));
        if (_diskFloorVal != null) _diskFloorVal.Text = FmtFloor(_diskFloor);
        if (RunIsLive())
        {
            SendCommand(Cmd1("set_disk_floor_gb", _diskFloor));
        }
    }

    // RAM floor (MB) setter -- exact mirror of SetDiskFloor: clamp, persist, refresh panel value,
    // and push LIVE to a running fleet via {"set_ram_floor_mb":N} (fleet_runner consumes it next
    // sweep -> ram_box[0] -> autoscale keeps that much RAM free). Stepped by 256 MB.
    TextBlock _ramFloorVal;
    void SetRamFloor(double v)
    {
        _ramFloor = Math.Max(0.0, Math.Min(65536.0, Math.Round(v)));
        SaveKey("ram_floor_mb", ((int)_ramFloor).ToString(System.Globalization.CultureInfo.InvariantCulture));
        if (_ramFloorVal != null) _ramFloorVal.Text = ((int)_ramFloor).ToString();
        if (RunIsLive())
        {
            SendCommand(Cmd1("set_ram_floor_mb", _ramFloor));
        }
    }
}
