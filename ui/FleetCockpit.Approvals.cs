// FleetCockpit.Approvals.cs -- approval gates: banner, approval center, decision cards, AnswerGate.
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

    // Persistent decision entry point.  Unlike the run-mode combo, this button represents actual
    // human decisions and remains useful while the fleet is idle.
    UIElement ApprovalCenterControl()
    {
        _approvalCenterBtn = new Button();
        _approvalCenterBtn.Cursor = Cursors.Hand;
        _approvalCenterBtn.BorderThickness = new Thickness(1);
        _approvalCenterBtn.Padding = new Thickness(12, 3, 8, 3);
        _approvalCenterBtn.Margin = new Thickness(0, 0, 12, 0);
        _approvalCenterBtn.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBtn.Template = FlatButtonTemplate();

        var row = new StackPanel(); row.Orientation = Orientation.Horizontal;
        row.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterLabel = new TextBlock();
        _approvalCenterLabel.FontSize = 12; _approvalCenterLabel.FontWeight = FontWeights.SemiBold;
        _approvalCenterLabel.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(_approvalCenterLabel);

        _approvalCenterBadge = new Border();
        _approvalCenterBadge.CornerRadius = new CornerRadius(Theme.RadCard);
        _approvalCenterBadge.MinWidth = 18; _approvalCenterBadge.Height = 18;
        _approvalCenterBadge.Margin = new Thickness(7, 0, 0, 0);
        _approvalCenterBadge.Padding = new Thickness(4, 0, 4, 0);
        _approvalCenterBadgeText = new TextBlock();
        _approvalCenterBadgeText.FontSize = 10; _approvalCenterBadgeText.FontWeight = FontWeights.Bold;
        _approvalCenterBadgeText.HorizontalAlignment = HorizontalAlignment.Center;
        _approvalCenterBadgeText.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterBadge.Child = _approvalCenterBadgeText;
        row.Children.Add(_approvalCenterBadge);
        _approvalCenterBtn.Content = row;
        _approvalCenterBtn.Click += delegate { CloseHeaderPopups(null); ShowApprovalCenter(); };
        PaintApprovalCenterButton(0);
        return _approvalCenterBtn;
    }

    void PaintApprovalCenterButton(int pending)
    {
        if (_approvalCenterBtn == null) return;
        bool hasPending = pending > 0;
        _approvalCenterBtn.Background = hasPending ? Theme.Br(Theme.SurfaceSubtle(_dark)) : BtnBg;
        _approvalCenterBtn.BorderBrush = hasPending ? Theme.Br(Theme.Warning(_dark)) : Border;
        _approvalCenterBtn.Foreground = Fg;
        if (_approvalCenterLabel != null)
        {
            _approvalCenterLabel.Text = _lang == 0 ? "承認" : "Approvals";
            _approvalCenterLabel.Foreground = hasPending ? Theme.Br(Theme.Warning(_dark)) : Fg;
        }
        if (_approvalCenterBadge != null)
            _approvalCenterBadge.Background = hasPending ? Theme.Br(Theme.Warning(_dark)) : Theme.Br(Theme.SurfaceSubtle(_dark));
        if (_approvalCenterBadgeText != null)
        {
            _approvalCenterBadgeText.Text = pending.ToString();
            _approvalCenterBadgeText.Foreground = hasPending ? White : Muted;
        }
        string accessible = _lang == 0
            ? ("承認センター、未処理 " + pending + " 件")
            : ("Approval Center, " + pending + " pending");
        _approvalCenterBtn.ToolTip = accessible;
        System.Windows.Automation.AutomationProperties.SetName(_approvalCenterBtn, accessible);
    }

    // ── TASK 2 (Bucket C): pending-gate approval banner ──────────────────────────────────
    // When status.json has a non-empty `pending_gates` array, a worker is blocked waiting for
    // human approval. This banner surfaces the gate prominently so it cannot be missed.
    Border _gateBanner;
    ScrollViewer _gateScroll;           // bounds the banner so it cannot starve the rest of the window
    StackPanel _gateCardsPanel;         // holds one card per pending gate (or first gate + count)
    string _gateSig = "";               // last rendered gate set (by token); rebuild only on change
    // COLLAPSED BY DEFAULT. Expanded, three Skill cards -- each carrying a question, a path, a
    // digest and an Approve/Deny row -- measured taller than the run list they sit above, so the
    // window was mostly approval and the work underneath was unreadable. Capping the height was
    // not enough: the cap is a fraction of the window, so it scales with it and still crowds out
    // the list. The banner now opens as one summary line and shows cards only when asked.
    bool _gateExpanded = false;
    Dictionary<string, object> _gateRootCache;   // so the toggle can repaint without waiting a tick

    // ── APPROVAL CENTER: durable gates, available even while the fleet is idle ───────────
    //
    // ONE RESOLUTION, USED BY BOTH CLASSES. ApprovalPromptWindow cannot call an instance method
    // of CockpitWindow, so it had its own rule -- "the gate's folder must be named
    // .companion_gates" -- while the WRITER (tools/gate_ops.py) resolves
    // `MCP_GATE_DIR or ALLOWED_BASE/.companion_gates`. Whenever MCP_GATE_DIR pointed elsewhere
    // the two disagreed, the prompt threw "invalid approval-gate path", and the window closed
    // itself in `Loaded` before painting anything: the operator clicked a notification and
    // nothing happened, with no message anywhere. Two places deciding where gates live, in one
    // file, because one of them could not reach the other's method.
    string GateDirectory() { return ResolveGateDirectory(EnvValue("MCP_ALLOWED_BASE"),
                                                         EnvValue("MCP_GATE_DIR")); }

    //: `fileBase` / `fileGateDir` are what the .env says; the process environment wins, exactly
    //: as it does in gate_ops.py. Static so the approval window can ask the same question.
    internal static string ResolveGateDirectory(string fileBase, string fileGateDir)
    {
        try
        {
            // MCP_GATE_DIR FIRST, because that is the order the writer uses. It is what the
            // test suite sets to keep its gates out of the operator's queue, and a cockpit that
            // ignores it can never open a gate written under it.
            string over = Environment.GetEnvironmentVariable("MCP_GATE_DIR") ?? "";
            if (string.IsNullOrWhiteSpace(over)) over = fileGateDir ?? "";
            over = (over ?? "").Trim().Trim('"');
            if (over.Length > 0) return Path.GetFullPath(over);

            string raw = Environment.GetEnvironmentVariable("MCP_ALLOWED_BASE") ?? "";
            if (string.IsNullOrWhiteSpace(raw)) raw = fileBase ?? "";
            raw = (raw ?? "").Trim();
            string basePath;
            if (raw.Length == 0 || raw == "*")
                basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            else
            {
                string[] roots = raw.Split(Path.PathSeparator);
                basePath = roots.Length > 0 ? roots[0].Trim().Trim('"') : "";
                if (basePath == "~") basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                else if (basePath.StartsWith("~" + Path.DirectorySeparatorChar) || basePath.StartsWith("~/"))
                    basePath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), basePath.Substring(2));
                if (basePath.Length == 2 && basePath[1] == ':') basePath += Path.DirectorySeparatorChar;
                if (basePath.Length == 0) basePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            }
            return Path.Combine(Path.GetFullPath(basePath), ".companion_gates");
        }
        catch (Exception)
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".companion_gates");
        }
    }

    List<Dictionary<string, object>> ReadAllGates(Dictionary<string, object> statusRoot)
    {
        // A 1.5-second cache keeps the 700ms UI tick from repeatedly parsing a large audit history,
        // while still surfacing new approvals promptly. AnswerGate invalidates it immediately.
        double now = NowUnix();
        if (now - _gateCacheAt < 1.5) return new List<Dictionary<string, object>>(_gateCache);
        var gates = new List<Dictionary<string, object>>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            string dir = GateDirectory();
            if (Directory.Exists(dir))
            {
                foreach (string p in Directory.GetFiles(dir, "gate_*.json"))
                {
                    try
                    {
                        string text;
                        using (var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                        using (var sr = new StreamReader(fs, Encoding.UTF8)) text = sr.ReadToEnd();
                        var g = _js.DeserializeObject(text) as Dictionary<string, object>;
                        if (g == null) continue;
                        string token = S(g, "token");
                        if (token.Length == 0) token = Path.GetFileNameWithoutExtension(p);
                        g["token"] = token; g["path"] = p;
                        if (seen.Add(token)) gates.Add(g);
                    }
                    catch (Exception) { }
                }
            }
        }
        catch (Exception) { }

        gates.Sort(delegate (Dictionary<string, object> a, Dictionary<string, object> b)
        { return Dbl(b, "asked_at").CompareTo(Dbl(a, "asked_at")); });
        _gateCache = gates; _gateCacheAt = now;
        return new List<Dictionary<string, object>>(_gateCache);
    }

    static bool GateAnswered(Dictionary<string, object> gate)
    {
        try { return gate.ContainsKey("answered") && Convert.ToBoolean(gate["answered"]); }
        catch (Exception) { return false; }
    }

    static bool GateExpired(Dictionary<string, object> gate)
    {
        double expiry = Dbl(gate, "expires_at");
        return expiry > 0 && expiry < NowUnix();
    }

    string GateKind(Dictionary<string, object> gate)
    {
        string token = S(gate, "token").ToLowerInvariant();
        string hay = (S(gate, "context") + " " + S(gate, "question")).ToLowerInvariant();
        if (token.StartsWith("gate_skill_") || hay.Contains("skill approval")) return _lang == 0 ? "Skill" : "Skill";
        if (hay.Contains("contract gate: delete") || hay.Contains(" delete")) return _lang == 0 ? "削除" : "Delete";
        if (hay.Contains("outbound") || hay.Contains("send_immediately")) return _lang == 0 ? "外部送信" : "External send";
        if (hay.Contains("shell_destructive") || hay.Contains("destructive shell")) return _lang == 0 ? "破壊的shell" : "Destructive shell";
        if (hay.Contains("task_router job class")) return _lang == 0 ? "ジョブ" : "Job";
        return _lang == 0 ? "確認" : "Decision";
    }

    bool GateNeedsSecondConfirmation(Dictionary<string, object> gate)
    {
        string kind = GateKind(gate);
        return kind == "削除" || kind == "Delete" || kind == "外部送信" ||
               kind == "External send" || kind == "破壊的shell" || kind == "Destructive shell";
    }

    string GateAge(Dictionary<string, object> gate)
    {
        double age = Math.Max(0, NowUnix() - Dbl(gate, "asked_at"));
        if (age < 60) return _lang == 0 ? "たった今" : "just now";
        if (age < 3600) return ((int)(age / 60)).ToString() + (_lang == 0 ? "分前" : " min ago");
        if (age < 86400) return ((int)(age / 3600)).ToString() + (_lang == 0 ? "時間前" : " hr ago");
        return ((int)(age / 86400)).ToString() + (_lang == 0 ? "日前" : " days ago");
    }

    List<Dictionary<string, object>> PendingGates(Dictionary<string, object> root)
    {
        var pending = new List<Dictionary<string, object>>();
        foreach (var gate in ReadAllGates(root)) if (!GateAnswered(gate)) pending.Add(gate);
        return pending;
    }

    // First two lines of a gate context, ellipsised. Keeps the banner card a fixed, predictable
    // height so the Approve/Deny row below it stays on screen no matter how long the context is.
    // lang is passed in because this is static: the counter below is user-visible text
    // and was pinned to Japanese, so it needs to know the mode like every other label.
    static string GateContextPreview(string context, int lang)
    {
        if (string.IsNullOrEmpty(context)) return "";
        string[] lines = context.Replace("\r\n", "\n").Split('\n');
        var kept = new List<string>();
        foreach (string line in lines)
        {
            if (line.Trim().Length == 0) continue;
            kept.Add(line.Length > 110 ? line.Substring(0, 110) + "..." : line);
            if (kept.Count == 2) break;
        }
        string text = string.Join("\n", kept.ToArray());
        int shown = kept.Count, total = 0;
        foreach (string line in lines) if (line.Trim().Length > 0) total++;
        // 助数詞が日本語固定だった。英語表示でも「行)」が出ていた。
        if (total > shown)
            text += "\n... (+" + (total - shown) + (lang == 0 ? " 行)" : " more)");
        return text;
    }

    // Builds the outer gate banner container (always Collapsed until a gate appears).
    UIElement BuildGateBanner()
    {
        _gateBanner = new Border();
        _gateBanner.Visibility = Visibility.Collapsed;
        _gateBanner.CornerRadius = new CornerRadius(Theme.RadPopover);
        // Uniform, not a thicker left edge: a one-sided stripe reads as sticky-note
        // decoration. The banner's border colour already carries the urgency.
        _gateBanner.BorderThickness = new Thickness(1);
        _gateBanner.Padding = new Thickness(16, 12, 12, 12);
        // 18px left gutter: the same one _pinnedToolbarHost and the card list use, so inside the
        // run column the banner shares its left edge with the cards it is about.
        _gateBanner.Margin = new Thickness(16, 6, 16, 6);
        DockPanel.SetDock(_gateBanner, Dock.Top);
        _gateCardsPanel = new StackPanel();
        // The banner is DOCKED, so it sits outside the card list's ScrollViewer -- the only
        // scroller in the window. Left unbounded it takes whatever height it wants: with three
        // pending Skill gates at 1.5x zoom the health strip and counters were measured at zero
        // height, the list's viewport collapsed to nothing, and controls landed at y=1199 on a
        // 1080-tall screen. On a shorter display the Approve/Deny rows themselves cross the
        // bottom edge, and with the list scroller gone there is nothing left to scroll -- the
        // request becomes impossible to answer. Its own scroller bounds it instead.
        _gateScroll = new ScrollViewer();
        _gateScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _gateScroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        _gateScroll.Content = _gateCardsPanel;
        _gateBanner.Child = _gateScroll;
        return _gateBanner;
    }

    // Called each OnTick. Rebuilds the gate banner cards ONLY when the pending token set changes
    // (sig check), so the buttons don't flicker every 700ms while a gate is active.
    void UpdateGateBanner(Dictionary<string, object> root)
    {
        if (_gateBanner == null || _gateCardsPanel == null) return;
        _gateRootCache = root;

        // Read the durable gate directory directly.  This is deliberately independent of
        // status.json so Skill/import approvals remain visible before and after a fleet run.
        List<Dictionary<string, object>> gates = PendingGates(root);
        PaintApprovalCenterButton(gates.Count);

        // Re-cap on every tick: the window can be resized and the zoom changed at any time.
        // ActualHeight is in physical units while the banner lives inside the scaled root, so
        // divide by the zoom to get the units this MaxHeight is actually measured in.
        if (_gateScroll != null)
        {
            double zoom = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
            double usable = (ActualHeight > 0 ? ActualHeight : 760) / zoom;
            // Collapsed the banner is a single summary line, so it needs no fraction of the
            // window at all. Expanded, a quarter -- 0.45 left the run list at a few dozen px on
            // a 760-tall window once the header, health strip and composer took their share.
            _gateScroll.MaxHeight = _gateExpanded ? Math.Max(150, usable * 0.25) : 72;
        }
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
            RefreshApprovalCenter();

        // Build a signature from the current token set (order-insensitive for stability).
        var sb2 = new StringBuilder();
        foreach (var g in gates) sb2.Append(S(g, "token")).Append(';');
        // The expand state is part of what is rendered, so it belongs in the signature -- without
        // it the toggle changes a bool and the sig check returns before anything is redrawn.
        sb2.Append(_gateExpanded ? "|open" : "|shut");
        string newSig = sb2.ToString();
        if (newSig == _gateSig) return;   // nothing changed; skip rebuild to avoid flicker
        _gateSig = newSig;

        _gateCardsPanel.Children.Clear();

        if (gates.Count == 0)
        {
            _gateBanner.Visibility = Visibility.Collapsed;
            return;
        }

        // Heading row: a single line carrying the count and the expand toggle. Collapsed, this row
        // IS the whole banner -- the count is what the owner needs at a glance, and the cards are
        // one click away either here or in the Approval Center.
        bool ja3 = _lang == 0;
        var headRow = new StackPanel();
        headRow.Orientation = Orientation.Horizontal;
        headRow.Margin = new Thickness(0, 0, 0, _gateExpanded ? 8 : 0);

        var headTb = new TextBlock();
        headTb.Text = ja3
            ? ("承認待ち " + gates.Count + " 件 / Approval needed")
            : ("Approval needed: " + gates.Count + " / 承認待ち");
        headTb.FontSize = 13;
        headTb.FontWeight = FontWeights.SemiBold;
        headTb.Foreground = Theme.Br(Theme.Warning(_dark));
        headTb.VerticalAlignment = VerticalAlignment.Center;
        headRow.Children.Add(headTb);

        var toggleBtn = new Button();
        toggleBtn.Cursor = Cursors.Hand;
        toggleBtn.FontSize = 11;
        toggleBtn.BorderThickness = new Thickness(0);
        toggleBtn.Background = Brushes.Transparent;
        toggleBtn.Foreground = Theme.Br(Theme.Warning(_dark));
        toggleBtn.Padding = new Thickness(0);
        toggleBtn.Margin = new Thickness(12, 0, 0, 0);
        toggleBtn.VerticalAlignment = VerticalAlignment.Center;
        toggleBtn.Content = _gateExpanded ? (ja3 ? "たたむ" : "Collapse") : (ja3 ? "開く" : "Expand");
        toggleBtn.Click += delegate (object s3, RoutedEventArgs e3)
        {
            e3.Handled = true;
            _gateExpanded = !_gateExpanded;
            // Repaint now rather than on the next 700ms tick, so the click feels like it did
            // something. The cached root is the one this method was last called with.
            if (_gateRootCache != null) UpdateGateBanner(_gateRootCache);
        };
        headRow.Children.Add(toggleBtn);

        var centerBtn = new Button();
        centerBtn.Cursor = Cursors.Hand;
        centerBtn.FontSize = 11;
        centerBtn.BorderThickness = new Thickness(0);
        centerBtn.Background = Brushes.Transparent;
        centerBtn.Foreground = Theme.Br(Theme.Muted(_dark));
        centerBtn.Padding = new Thickness(0);
        centerBtn.Margin = new Thickness(12, 0, 0, 0);
        centerBtn.VerticalAlignment = VerticalAlignment.Center;
        centerBtn.Content = ja3 ? "承認センター" : "Approval Center";
        centerBtn.Click += delegate (object s3, RoutedEventArgs e3) { e3.Handled = true; ShowApprovalCenter(); };
        headRow.Children.Add(centerBtn);

        _gateCardsPanel.Children.Add(headRow);

        if (!_gateExpanded)
        {
            _gateBanner.Visibility = Visibility.Visible;
            return;
        }

        // Show first gate (or all). If more than 1, show a count note below.
        int showCount = gates.Count > 3 ? 3 : gates.Count;
        for (int gi = 0; gi < showCount; gi++)
        {
            var g2 = gates[gi];
            string question = S(g2, "question");
            string context2 = S(g2, "context");
            string gatePath = S(g2, "path");

            // Question card.
            var gCard = new Border();
            gCard.BorderBrush = Theme.Br(Theme.Warning(_dark));
            gCard.BorderThickness = new Thickness(1);
            gCard.CornerRadius = new CornerRadius(Theme.RadSmall);
            gCard.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            gCard.Padding = new Thickness(12, 8, 12, 8);
            gCard.Margin = new Thickness(0, 0, 0, gi < showCount - 1 ? 8 : 0);

            var gInner = new StackPanel();

            var qTb = new TextBlock();
            qTb.Text = question;
            qTb.FontSize = 13;
            qTb.Foreground = Theme.Br(Theme.Text(_dark));
            qTb.TextWrapping = TextWrapping.Wrap;
            gInner.Children.Add(qTb);

            if (!string.IsNullOrEmpty(context2))
            {
                var ctxTb = new TextBlock();
                // Preview only. A Skill gate's context is a multi-line file/hash manifest; printed
                // in full it pushed the Approve/Deny row below the fold. The complete text is one
                // click away in the Approval Center, which scrolls.
                ctxTb.Text = GateContextPreview(context2, _lang);
                ctxTb.ToolTip = context2;
                ctxTb.FontSize = 11;
                ctxTb.Foreground = Theme.Br(Theme.Muted(_dark));
                ctxTb.TextWrapping = TextWrapping.Wrap;
                ctxTb.Margin = new Thickness(0, 3, 0, 0);
                gInner.Children.Add(ctxTb);
            }

            // Approve / Deny buttons.
            var btnRow = new StackPanel();
            btnRow.Orientation = Orientation.Horizontal;
            btnRow.Margin = new Thickness(0, 8, 0, 0);

            var approveBtn = new Button();
            approveBtn.Cursor = Cursors.Hand;
            approveBtn.BorderThickness = new Thickness(0);
            approveBtn.Padding = new Thickness(16, 4, 16, 4);
            approveBtn.FontWeight = FontWeights.SemiBold;
            approveBtn.FontSize = 12;
            approveBtn.Background = Theme.Br(Theme.AccentFill(_dark));
            approveBtn.Foreground = AccentFg;
            bool reviewFirst = GateNeedsSecondConfirmation(g2) || GateKind(g2) == "Skill";
            approveBtn.Content = reviewFirst
                ? (ja3 ? "詳細を確認" : "Review details")
                : (ja3 ? "承認 / Approve" : "Approve / 承認");
            approveBtn.IsEnabled = !GateExpired(g2);
            if (!approveBtn.IsEnabled)
                approveBtn.ToolTip = ja3 ? "期限切れです。元の操作から承認を再要求してください。" : "Expired. Request approval again from the originating action.";
            string gPath2 = gatePath;
            Dictionary<string, object> capturedGate = g2;
            approveBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                // High-impact operations require their exact scope to be reviewed in the
                // Approval Center before the second confirmation is offered.
                if (GateNeedsSecondConfirmation(capturedGate) || GateKind(capturedGate) == "Skill") ShowApprovalCenter();
                else AnswerGate(gPath2, "approved");
            };
            btnRow.Children.Add(approveBtn);

            var denyBtn = new Button();
            denyBtn.Cursor = Cursors.Hand;
            denyBtn.BorderThickness = new Thickness(1);
            denyBtn.Padding = new Thickness(16, 4, 16, 4);
            denyBtn.FontSize = 12;
            denyBtn.Margin = new Thickness(8, 0, 0, 0);
            denyBtn.Background = Brushes.Transparent;
            denyBtn.Foreground = Theme.Br(Theme.Danger(_dark));
            denyBtn.BorderBrush = Theme.Br(Theme.Danger(_dark));
            denyBtn.Content = ja3 ? "拒否 / Deny" : "Deny / 拒否";
            string gPath3 = gatePath;
            denyBtn.Click += delegate (object s2, RoutedEventArgs e2)
            {
                e2.Handled = true;
                AnswerGate(gPath3, "denied");
            };
            btnRow.Children.Add(denyBtn);

            gInner.Children.Add(btnRow);
            gCard.Child = gInner;
            _gateCardsPanel.Children.Add(gCard);
        }

        if (gates.Count > showCount)
        {
            var moreBtn = new Button();
            moreBtn.Content = (ja3
                ? ("+ さらに " + (gates.Count - showCount) + " 件の承認待ち")
                : ("+ " + (gates.Count - showCount) + " more gate(s) pending"));
            moreBtn.FontSize = 11; moreBtn.Cursor = Cursors.Hand;
            moreBtn.Foreground = Theme.Br(Theme.Warning(_dark));
            moreBtn.Background = Brushes.Transparent; moreBtn.BorderThickness = new Thickness(0);
            moreBtn.HorizontalAlignment = HorizontalAlignment.Left;
            moreBtn.Padding = new Thickness(0); moreBtn.Margin = new Thickness(0, 6, 0, 0);
            moreBtn.Click += delegate { ShowApprovalCenter(); };
            _gateCardsPanel.Children.Add(moreBtn);
        }

        _gateBanner.Visibility = Visibility.Visible;
    }

    void ShowApprovalCenter()
    {
        if (_approvalCenterWindow != null && _approvalCenterWindow.IsVisible)
        {
            _approvalCenterWindow.Activate();
            return;
        }
        var w = new Window();
        _approvalCenterWindow = w;
        w.Title = _lang == 0 ? "承認センター" : "Approval Center";
        // Clamp to the work area. A fixed 760x720 already filled a 1080-tall screen once
        // the UI scale (1.5 by default) was applied, pushing the Approve/Deny row off the
        // bottom -- and because the footer is docked rather than inside the scroller,
        // there was nothing to scroll to reach it: the request became undecidable.
        double waW = SystemParameters.WorkArea.Width;
        double waH = SystemParameters.WorkArea.Height;
        double scale = (_rootScale != null && _rootScale.ScaleY > 0.1) ? _rootScale.ScaleY : 1.0;
        w.Width = Math.Min(760, Math.Max(480, waW / scale - 60));
        w.Height = Math.Min(720, Math.Max(380, waH / scale - 60));
        w.MinWidth = Math.Min(560, w.Width);
        w.MinHeight = Math.Min(460, w.Height);
        w.MaxHeight = waH;
        w.WindowStartupLocation = WindowStartupLocation.CenterOwner; w.Owner = this;
        w.Background = Bg;
        w.KeyDown += delegate (object sender, KeyEventArgs e)
        { if (e.Key == Key.Escape) { w.Close(); e.Handled = true; } };
        w.Closed += delegate
        {
            _approvalCenterWindow = null; _approvalPendingHost = null; _approvalRecentHost = null;
            _approvalCenterSummary = null; _approvalCenterSig = "";
        };

        var root = new DockPanel();
        var head = new Border();
        head.Background = CardBg; head.BorderBrush = Border;
        head.BorderThickness = new Thickness(0, 0, 0, 1);
        head.Padding = new Thickness(24, 16, 24, 16);
        DockPanel.SetDock(head, Dock.Top);
        var headGrid = new Grid();
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        headGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titleCol = new StackPanel();
        var title = new TextBlock(); title.Text = w.Title; title.Foreground = Fg;
        title.FontSize = 20; title.FontWeight = FontWeights.SemiBold;
        titleCol.Children.Add(title);
        var note = new TextBlock();
        note.Text = _lang == 0
            ? "実際の操作許可をここで判断します。run / plan / auto の実行方式とは別です。"
            : "Decide actual operation permissions here. This is separate from run / plan / auto mode.";
        note.Foreground = Muted; note.FontSize = 12; note.Margin = new Thickness(0, 4, 0, 0);
        note.TextWrapping = TextWrapping.Wrap; titleCol.Children.Add(note);
        headGrid.Children.Add(titleCol);
        _approvalCenterSummary = new TextBlock();
        _approvalCenterSummary.Foreground = Theme.Br(Theme.Warning(_dark));
        _approvalCenterSummary.FontWeight = FontWeights.SemiBold;
        _approvalCenterSummary.VerticalAlignment = VerticalAlignment.Center;
        _approvalCenterSummary.Margin = new Thickness(16, 0, 0, 0);
        Grid.SetColumn(_approvalCenterSummary, 1); headGrid.Children.Add(_approvalCenterSummary);
        head.Child = headGrid; root.Children.Add(head);

        var scroll = new ScrollViewer(); scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        var body = new StackPanel(); body.Margin = new Thickness(24, 20, 24, 28);
        body.Children.Add(BuildJobApprovalPolicyPanel());
        var pendingTitle = new TextBlock();
        pendingTitle.Text = _lang == 0 ? "判断が必要" : "Needs your decision";
        pendingTitle.Foreground = Fg; pendingTitle.FontSize = 14; pendingTitle.FontWeight = FontWeights.SemiBold;
        pendingTitle.Margin = new Thickness(0, 0, 0, 12); body.Children.Add(pendingTitle);
        _approvalPendingHost = new StackPanel(); body.Children.Add(_approvalPendingHost);

        var recentTitle = new TextBlock();
        recentTitle.Text = _lang == 0 ? "最近の判断" : "Recent decisions";
        recentTitle.Foreground = Fg; recentTitle.FontSize = 14; recentTitle.FontWeight = FontWeights.SemiBold;
        recentTitle.Margin = new Thickness(0, 24, 0, 12); body.Children.Add(recentTitle);
        _approvalRecentHost = new StackPanel(); body.Children.Add(_approvalRecentHost);
        scroll.Content = body; root.Children.Add(scroll);
        w.Content = root;
        _approvalCenterSig = "";
        RefreshApprovalCenter();
        w.ShowDialog();
    }

    UIElement BuildJobApprovalPolicyPanel()
    {
        var box = new Border(); box.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        box.BorderBrush = Border; box.BorderThickness = new Thickness(1);
        box.CornerRadius = new CornerRadius(Theme.RadCard); box.Padding = new Thickness(16, 12, 16, 12);
        box.Margin = new Thickness(0, 0, 0, 20);
        var col = new StackPanel();
        var title = new TextBlock(); title.Text = _lang == 0 ? "操作承認ポリシー" : "Operation approval policy";
        title.Foreground = Fg; title.FontSize = 13; title.FontWeight = FontWeights.SemiBold;
        col.Children.Add(title);
        var note = new TextBlock(); note.Foreground = Muted; note.FontSize = 11;
        note.TextWrapping = TextWrapping.Wrap; note.Margin = new Thickness(0, 4, 0, 0);
        note.Text = _lang == 0
            ? "ローカルジョブと自律契約に即時反映。STOP・パス制限・外部Skillの初回/変更承認は常に残ります。"
            : "Applies live to local jobs and autonomy contracts. STOP, path limits, and external-Skill trust remain enforced.";
        col.Children.Add(note);

        var selector = new ComboBox(); selector.MinWidth = 230; selector.HorizontalAlignment = HorizontalAlignment.Left;
        selector.Margin = new Thickness(0, 12, 0, 0); selector.Background = CardBg; selector.Foreground = Fg;
        // SAME ORDER AND SAME LABELS AS ApprovalPromptWindow's list. This is the second
        // selector for one setting, each with its own hard-coded array, and a test caught the
        // two disagreeing about which mode is recommended after only one was changed -- on
        // the surface an operator actually opens to change it.
        var labels = _lang == 0
            ? new string[] { "自動（推奨）", "バイパス", "毎回確認（非推奨）" }
            : new string[] { "Auto (recommended)", "Bypass", "Confirm every time (not recommended)" };
        var values = new string[] { "auto", "bypass", "default" };
        string current = ApprovalPromptWindow.ReadPolicy();
        for (int i = 0; i < values.Length; i++)
        {
            var item = new ComboBoxItem(); item.Content = labels[i]; item.Tag = values[i];
            selector.Items.Add(item); if (values[i] == current) selector.SelectedItem = item;
        }
        StyleFlatCombo(selector); col.Children.Add(selector);
        var help = new TextBlock(); help.Foreground = Muted; help.FontSize = 11;
        help.TextWrapping = TextWrapping.Wrap; help.Margin = new Thickness(0, 7, 0, 0);
        col.Children.Add(help);
        Action updateHelp = delegate
        {
            var selected = selector.SelectedItem as ComboBoxItem;
            string mode = selected == null ? "auto" : selected.Tag as string;
            if (mode == "auto") help.Text = _lang == 0
                ? "安全な操作は自動実行、要確認は承認待ち、禁止判定は拒否。"
                : "Safe operations run automatically; risky ones ask; prohibited ones are denied.";
            else if (mode == "bypass") help.Text = _lang == 0
                ? "手動確認を省略。常時有効な安全境界は解除しません。"
                : "Skip manual confirmations. Always-on safety boundaries remain.";
            else help.Text = _lang == 0
                ? "初回クラスを毎回確認。非推奨: 確認が多すぎると内容が読まれなくなります。"
                : "Asks about every first-seen class. Not recommended: an approval that is always there stops being read.";
        };
        updateHelp();
        bool reverting = false;
        selector.SelectionChanged += delegate
        {
            if (reverting) return;
            var item = selector.SelectedItem as ComboBoxItem; if (item == null) return;
            string next = item.Tag as string; string previous = ApprovalPromptWindow.ReadPolicy();
            if (next == "bypass")
            {
                string warning = _lang == 0
                    ? "バイパスではローカルジョブと自律契約の手動確認を省略します。\nSTOP条件、ファイル範囲、外部Skill承認は残ります。\n\n有効にしますか？"
                    : "Bypass skips manual approval for local jobs and autonomy contracts.\nSTOP rules, path limits, and external-Skill approval remain.\n\nEnable it?";
                if (MessageBox.Show(_approvalCenterWindow, warning, _lang == 0 ? "バイパスを有効化" : "Enable bypass",
                    MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
                {
                    reverting = true;
                    foreach (object obj in selector.Items)
                    { var old = obj as ComboBoxItem; if (old != null && (old.Tag as string) == previous) selector.SelectedItem = old; }
                    reverting = false; updateHelp(); return;
                }
            }
            ApprovalPromptWindow.SavePolicy(next); updateHelp();
        };
        box.Child = col; return box;
    }

    void RefreshApprovalCenter()
    {
        if (_approvalCenterWindow == null || _approvalPendingHost == null || _approvalRecentHost == null) return;
        var all = ReadAllGates(ReadStatus());
        var sig = new StringBuilder();
        foreach (var gate in all)
        {
            // Expiry belongs in the signature. Without it a card drawn while the request
            // was still valid kept its Approve button ENABLED after the deadline passed:
            // the click then wrote "approved" that the trust store refused, so the user
            // saw an approval that never took effect. Redraw when the deadline crosses.
            sig.Append(S(gate, "token")).Append(':')
               .Append(GateAnswered(gate) ? S(gate, "answer") : "open").Append(':')
               .Append(GateExpired(gate) ? "exp" : "live").Append(';');
        }
        string currentSig = sig.ToString() + (_dark ? "D" : "L") + _lang;
        if (currentSig == _approvalCenterSig) return;
        _approvalCenterSig = currentSig;

        _approvalPendingHost.Children.Clear(); _approvalRecentHost.Children.Clear();
        var pending = new List<Dictionary<string, object>>();
        var recent = new List<Dictionary<string, object>>();
        // RECENT DECISIONS ARE DE-DUPLICATED BY CONTENT, NOT DELETED FROM DISK. A gate answered
        // under `auto`/`bypass` job policy (see task_router.job_gate) never reaches this list to
        // begin with -- those modes return ALLOW without ever writing a gate file, so there is
        // nothing here to hide for them. What DOES flood this list in practice is the SAME
        // decision recorded over and over: an external Skill's approval expires (APPROVAL_TTL in
        // relay/skills.py) and gets re-approved against the identical, unchanged bundle, or a
        // background loop repeats the identical request. Measured on one machine: 2169 of 2175
        // gate files were Skill approvals, and 2167 of those were the SAME skill at the SAME
        // content digest -- one signature, answered thousands of times, burying everything else
        // under the 20-card cap below.
        //
        // `all` is sorted newest-first (ReadAllGates), so keeping the FIRST occurrence of a key
        // keeps the MOST RECENT instance of that decision as the list's representative. A Skill's
        // first-ever approval, or its re-approval after the bundle CHANGED, carries a different
        // digest and therefore a different question string -- a different key -- so it is never
        // folded away here. Nothing is written back to the gate file or deleted: the on-disk
        // gate_*.json is the durable log this collapsing must not touch.
        var seenRecentKeys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var gate in all)
        {
            if (!GateAnswered(gate)) { pending.Add(gate); continue; }
            string recentKey = GateKind(gate) + "" + S(gate, "question") + "" + S(gate, "answer");
            if (seenRecentKeys.Add(recentKey)) recent.Add(gate);
        }
        if (_approvalCenterSummary != null)
            _approvalCenterSummary.Text = _lang == 0 ? (pending.Count + " 件 未処理") : (pending.Count + " pending");
        PaintApprovalCenterButton(pending.Count);

        if (pending.Count == 0)
        {
            var empty = new Border(); empty.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
            empty.BorderBrush = Border; empty.BorderThickness = new Thickness(1);
            empty.CornerRadius = new CornerRadius(Theme.RadCard); empty.Padding = new Thickness(16, 16, 16, 16);
            var text = new TextBlock();
            text.Text = _lang == 0 ? "未処理の承認はありません。" : "No approvals are waiting.";
            text.Foreground = Muted; text.FontSize = 13; empty.Child = text;
            _approvalPendingHost.Children.Add(empty);
        }
        else foreach (var gate in pending) _approvalPendingHost.Children.Add(BuildGateDecisionCard(gate, true));

        int limit = Math.Min(20, recent.Count);
        if (limit == 0)
        {
            var none = new TextBlock(); none.Text = _lang == 0 ? "履歴はまだありません。" : "No decision history yet.";
            none.Foreground = Muted; none.FontSize = 12; _approvalRecentHost.Children.Add(none);
        }
        else for (int i = 0; i < limit; i++) _approvalRecentHost.Children.Add(BuildGateDecisionCard(recent[i], false));
    }

    UIElement BuildGateDecisionCard(Dictionary<string, object> gate, bool actionable)
    {
        bool expired = GateExpired(gate);
        var card = new Border();
        card.Background = CardBg;
        card.BorderBrush = actionable ? Theme.Br(expired ? Theme.Danger(_dark) : Theme.Warning(_dark)) : Border;
        // Actionable cards are marked by border COLOUR, not by a fatter left edge --
        // a one-sided stripe reads as sticky-note decoration.
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadCard); card.Padding = new Thickness(16, 12, 16, 12);
        card.Margin = new Thickness(0, 0, 0, 12);
        var col = new StackPanel();

        var meta = new DockPanel(); meta.LastChildFill = true;
        var chip = new Border(); chip.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        chip.BorderBrush = actionable ? Theme.Br(Theme.Warning(_dark)) : Border;
        chip.BorderThickness = new Thickness(1); chip.CornerRadius = new CornerRadius(Theme.RadCard);
        chip.Padding = new Thickness(7, 2, 7, 2); chip.Margin = new Thickness(0, 0, 8, 0);
        var chipText = new TextBlock(); chipText.Text = GateKind(gate); chipText.FontSize = 10;
        chipText.FontWeight = FontWeights.SemiBold; chipText.Foreground = actionable ? Theme.Br(Theme.Warning(_dark)) : Muted;
        chip.Child = chipText; DockPanel.SetDock(chip, Dock.Left); meta.Children.Add(chip);
        var age = new TextBlock();
        age.Text = (expired ? (_lang == 0 ? "期限切れ · " : "Expired · ") : "") + GateAge(gate);
        age.Foreground = expired ? Theme.Br(Theme.Danger(_dark)) : Muted; age.FontSize = 11;
        age.VerticalAlignment = VerticalAlignment.Center; meta.Children.Add(age);
        col.Children.Add(meta);

        var question = new TextBlock(); question.Text = S(gate, "question");
        question.Foreground = Fg; question.FontSize = 13; question.FontWeight = FontWeights.SemiBold;
        question.TextWrapping = TextWrapping.Wrap; question.Margin = new Thickness(0, 8, 0, 0);
        col.Children.Add(question);

        string context = S(gate, "context");
        if (context.Length > 0)
        {
            var details = new Expander(); details.Header = _lang == 0 ? "対象と詳細を確認" : "Review scope and details";
            details.Foreground = Muted; details.FontSize = 11; details.Margin = new Thickness(0, 7, 0, 0);
            details.IsExpanded = actionable && (GateNeedsSecondConfirmation(gate) || GateKind(gate) == "Skill");
            var detailText = new TextBlock(); detailText.Text = context; detailText.Foreground = Muted;
            detailText.FontFamily = new FontFamily("Cascadia Mono, Consolas"); detailText.FontSize = 11;
            detailText.TextWrapping = TextWrapping.Wrap; detailText.Margin = new Thickness(12, 6, 0, 0);
            details.Content = detailText; col.Children.Add(details);
        }

        if (actionable)
        {
            var row = new StackPanel(); row.Orientation = Orientation.Horizontal; row.Margin = new Thickness(0, 12, 0, 0);
            var approve = new Button(); approve.Content = _lang == 0 ? "承認" : "Approve";
            approve.Cursor = Cursors.Hand; approve.Padding = new Thickness(16, 6, 16, 6);
            approve.BorderThickness = new Thickness(0); approve.Background = AccentFill; approve.Foreground = AccentFg;
            approve.FontWeight = FontWeights.SemiBold; approve.IsEnabled = !expired;
            System.Windows.Automation.AutomationProperties.SetName(approve, _lang == 0 ? "この操作を承認" : "Approve this operation");
            Dictionary<string, object> captured = gate;
            approve.Click += delegate
            {
                if (GateNeedsSecondConfirmation(captured))
                {
                    string msg = (_lang == 0 ? "影響の大きい操作です。対象を確認したうえで本当に承認しますか？\n\n" :
                        "This is a high-impact operation. Approve after reviewing the exact scope?\n\n") + S(captured, "question");
                    if (MessageBox.Show(_approvalCenterWindow, msg, _lang == 0 ? "最終確認" : "Final confirmation",
                        MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes) return;
                }
                AnswerGate(S(captured, "path"), "approved"); RefreshApprovalCenter();
            };
            row.Children.Add(approve);

            var deny = new Button(); deny.Content = _lang == 0 ? "拒否" : "Deny";
            deny.Cursor = Cursors.Hand; deny.Padding = new Thickness(16, 6, 16, 6);
            deny.Margin = new Thickness(8, 0, 0, 0); deny.Background = Brushes.Transparent;
            deny.Foreground = Theme.Br(Theme.Danger(_dark)); deny.BorderBrush = Theme.Br(Theme.Danger(_dark));
            deny.BorderThickness = new Thickness(1);
            System.Windows.Automation.AutomationProperties.SetName(deny, _lang == 0 ? "この操作を拒否" : "Deny this operation");
            deny.Click += delegate { AnswerGate(S(captured, "path"), "denied"); RefreshApprovalCenter(); };
            row.Children.Add(deny); col.Children.Add(row);

            if (expired)
            {
                var expiredNote = new TextBlock();
                expiredNote.Text = _lang == 0 ? "期限切れのため承認できません。拒否するか、元の操作から再要求してください。" :
                    "This request expired. Deny it or request approval again from the originating action.";
                expiredNote.Foreground = Theme.Br(Theme.Danger(_dark)); expiredNote.FontSize = 11;
                expiredNote.Margin = new Thickness(0, 7, 0, 0); expiredNote.TextWrapping = TextWrapping.Wrap;
                col.Children.Add(expiredNote);
            }
        }
        else
        {
            var verdict = new TextBlock();
            string answer = S(gate, "answer");
            bool approved = answer.Equals("approved", StringComparison.OrdinalIgnoreCase);
            verdict.Text = approved ? (_lang == 0 ? "承認済み" : "Approved") : (_lang == 0 ? "拒否済み" : "Denied");
            verdict.Foreground = Theme.Br(approved ? Theme.Success(_dark) : Theme.Danger(_dark));
            verdict.FontSize = 11; verdict.FontWeight = FontWeights.SemiBold; verdict.Margin = new Thickness(0, 8, 0, 0);
            col.Children.Add(verdict);
        }
        card.Child = col; return card;
    }

    // Atomic gate answer: read the file at `path`, set answered=true + answer=<verdict>, write back.
    // Uses temp-file + rename for atomicity (consistent with the relay's reader expectation).
    void AnswerGate(string path, string verdict)
    {
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            // Normalize: forward slashes may be in the path from status.json.
            string localPath = Path.GetFullPath(path.Replace('/', Path.DirectorySeparatorChar));
            string gateRoot = Path.GetFullPath(GateDirectory()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!localPath.StartsWith(gateRoot, StringComparison.OrdinalIgnoreCase)) return;
            if (!File.Exists(localPath)) return;

            string text;
            using (var fs2 = new FileStream(localPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr2 = new System.IO.StreamReader(fs2, Encoding.UTF8))
                text = sr2.ReadToEnd();

            var gd = _js.DeserializeObject(text) as Dictionary<string, object>;
            if (gd == null) gd = new Dictionary<string, object>();
            gd["answered"] = true;
            gd["answer"] = verdict;
            gd["answered_at"] = NowUnix();

            string updated = _js.Serialize(gd);
            // Atomic: write to a temp file in the same directory, then rename.
            string dir2 = Path.GetDirectoryName(localPath);
            string tmp = Path.Combine(dir2, Path.GetFileName(localPath) + ".tmp");
            File.WriteAllText(tmp, updated, new UTF8Encoding(false));
            try { File.Replace(tmp, localPath, null); }
            catch (Exception)
            {
                // Older/non-NTFS environments may not implement Replace; keep the same-directory
                // fallback for compatibility, then remove the temporary file.
                File.Copy(tmp, localPath, true);
                try { File.Delete(tmp); } catch (Exception) { }
            }

            // Force a sig reset so the banner refreshes on the next tick (the relay
            // will drop this gate from pending_gates once it sees answered=true).
            _gateSig = "";
            _approvalCenterSig = "";
            _gateCacheAt = 0;
        }
        catch (Exception) { }
    }
}
