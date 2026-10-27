// CopilotChat.cs -- native Windows (WPF) chat front-end for an M365 Copilot agent.
// NO Node, NO JS, NO browser engine: pure .NET (built into Windows). Talks to the
// Python bridge's SSE endpoint over HTTP and renders the streamed answer natively.
//
//   [ this WPF app ] --HTTP/SSE--> [ Python bridge ] --CDP--> [ Copilot ]
//
// Palette = the sibling app Design Language v1.1 (grayscale-first N_GRAY slate; the
// single E_EMPHASIS orange #ea580c for the primary action). The waiting indicator
// matches the sibling app's chat loader (three slate dots, animate-bounce, staggered);
// the rest leans toward a clean, refined Claude-like layout.
// Build with the Windows-only csc.exe (legacy C# 5): ui\build_and_run.bat
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

class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        // --selftest: construct the window the ordinary way, pump once, exit. See WindowSelfTest.cs.
        if (args.Length >= 1 && args[0].Equals("--selftest", StringComparison.OrdinalIgnoreCase))
            Environment.Exit(WindowSelfTest.Run(delegate { return new ChatWindow(); }));
        new Application().Run(new ChatWindow());
    }
}

// Msg and Conversation live in ChatSend.cs, with the send path that is compiled without WPF.

partial class ChatWindow : Window, IChatSendEffects
{
    readonly string _bridge = Environment.GetEnvironmentVariable("MCP_BRIDGE_URL") ?? "http://127.0.0.1:8765";
    static readonly string StoreDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "copilot-bridge", "chats");

    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    bool _dark = true;
    StackPanel _messages, _convList;
    ScrollViewer _scroll;
    bool _stickBottom = true;   // auto-scroll follows new content ONLY while the user is at the bottom
    TextBox _input;
    Button _send;
    Border _statusDot;
    TextBlock _headTitle;                 // header conversation-title label (Wave 2); tracks the active conv
    Border _dotHit;                       // header status-dot hit target (Wave 2); tooltip re-localized on lang toggle
    Conversation _conv = new Conversation();
    Conversation _pageConv = null;       // the conversation the bridge PAGE is believed to be showing right now
    List<Conversation> _all = new List<Conversation>();
    string _renamingId = null;
    int _deleteMode = 1;                 // 1=local only, 2=open in Copilot, 3=auto (experimental)
    int _lang = 0;                       // 0=Japanese, 1=English
    double _uiScale = 1.0;               // whole-UI EFFECTIVE zoom (ScaleTransform on the root)
    bool _uiScaleLoaded = false;         // true once ui_scale was found in settings.txt (skip first-run default)
    // AUTO mode (shared semantics with the cockpit; see settings.txt ui_scale=auto). In auto mode the
    // effective LayoutTransform scale is per-monitor so PHYSICAL size stays constant across displays:
    //   effective = clamp(_scaleTarget / currentMonitorScale, 0.8, 2.0)
    // monitorScale (WPF DPI = DPI/96) × effective ≈ _scaleTarget on every monitor. The divide-by-
    // monitorScale exactly counteracts WPF's own per-monitor DPI relayout -> no double-apply. This app
    // has no gear popup, so the only controls are the keyboard ones (Ctrl+±/wheel = manual, Ctrl+0 = auto).
    bool _uiAuto = true;                 // true = auto mode (default for new users / ui_scale=auto)
    double _scaleTarget = 1.5;           // desired constant physical scale (persisted as ui_scale_target)
    bool _scaleTargetLoaded = false;     // true once ui_scale_target was read (else seed from primary DPI)
    ScaleTransform _rootScale;           // LayoutTransform on the root content -> everything scales+reflows
    Border _scaleToast;                  // small fading "NNN%" overlay shown on a zoom change
    TextBlock _scaleToastText;
    DispatcherTimer _scaleToastTimer;
    Border _banner; StackPanel _bannerBody;
    FrameworkElement _emptyState;        // centered quiet empty-state block (fresh window / after New chat)
    string _dotState = "idle";           // status-dot state: "idle" | "busy" | "offline" | "signin"
    volatile bool _bridgeReachable = true; // updated by the low-cadence background reachability probe
    int _reachTick = 0;                  // counter on the 800ms timer -> probe at a low cadence
    volatile bool _reachProbing = false; // guard so overlapping probes don't stack
    bool _signinBannerShown = false;     // true only while the sign-in banner (not the delete banner) is up
    Button _newBtn, _themeBtn, _langBtn, _manageBtn, _cockpitBtn, _attachBtn;
    TextBlock _inputHint;                 // goal-box watermark; localized -> must update on lang toggle
    TextBlock _steerHint;                 // composer footer "送信先: ..." indicator; visible only in steer mode
    Border _composerBorder;              // outer integrated composer wrapper (Task 1)
    TextBlock _fleetChipLabel;            // "Fleet: N" chip in the main header; updated on timer tick
    Border _fleetChip;                    // the chip border (collapsed when count == 0)
    Border _fleetStrip;                   // "CURRENT DELEGATION" strip above the composer (Option B)
    StackPanel _fleetStripBody;           // inner panel rebuilt on each refresh
    string _fleetStripSig = null;         // last-rendered signature; skip rebuild when unchanged
    // ── sidebar collapse/expand (Codex/Claude-desktop style) ──
    bool _sidebarCollapsed = false;
    string _lastOpenId = "";             // conversation to reopen on next launch (see LoadAll)
    Border _sideBorderRef;               // the sidebar Border in root Grid col0
    Grid _rootGrid;                      // root two-column Grid
    Button _sideToggleBtn;              // hamburger toggle in main header far-left
    // ── sidebar section state (pinned / archived / collapsed) ────────────────────
    HashSet<string> _pinned    = new HashSet<string>();   // convId -> pinned section
    HashSet<string> _archived  = new HashSet<string>();   // convId -> manually archived
    HashSet<string> _forcedToday = new HashSet<string>(); // convId -> user explicitly unarchived; skip auto-archive
    Dictionary<string, bool> _sectionCollapsed = new Dictionary<string, bool>
    {
        { "pinned",   false },
        { "today",    false },
        { "fleet",    true  },   // default collapsed (many items; user can expand)
        { "archived", true  },   // default collapsed to hide old eval/bench clutter
    };
    string _sidebarStatePath;   // set after _convsPath is known (ctor / timer init)

    // streaming state
    Panel _pendingContent;   // holds the typing dots, then the streamed text
    TextBox _pendingText;
    volatile bool _started;
    StackPanel _pendingOuter;            // the assistant block for the in-flight turn (Tag holds final text)
    bool _generating;                    // true while a reply is streaming; _send acts as Stop
    volatile bool _sendInFlight = false; // true from the moment a send is committed until Stream completes
    System.Net.HttpWebRequest _activeReq;// the in-flight stream request (Abort to stop)

    public ChatWindow()
    {
        Title = "Copilot — native (WPF, no JS)";
        // Clamp to the work area rather than trusting a fixed size: 1120x780 overflows a
        // shorter desktop (and the UI scale multiplies it), leaving the composer and its
        // send button off-screen. SystemParameters are already device-independent units.
        Width = Math.Min(1120, Math.Max(760, SystemParameters.WorkArea.Width - 80));
        Height = Math.Min(780, Math.Max(540, SystemParameters.WorkArea.Height - 80));
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        SetRef(this, BackgroundProperty, "Bg");
        ApplyTheme();
        AddButtonStyle();
        LoadSettings();
        LoadGlyphs();   // Wave 2: Material Symbols subset for the iconized sidebar footer

        var root = new Grid();
        _rootGrid = root;
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(260) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // ── sidebar ──
        var side = new Grid();
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        side.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        SetRef(side, BackgroundProperty, "PanelAlt");

        var headerStack = new StackPanel { Margin = new Thickness(12, 16, 12, 8) };
        _newBtn = Btn(T("newchat_btn"), "PanelAlt", "Muted", true);
        _newBtn.Height = 40; _newBtn.Margin = new Thickness(0, 0, 0, 6); _newBtn.FontWeight = FontWeights.Normal;
        _newBtn.Click += delegate { NewChat(); };
        headerStack.Children.Add(_newBtn);
        _manageBtn = Btn(T("manage_btn"), "PanelAlt", "Muted", true);
        _manageBtn.Height = 30; _manageBtn.FontSize = 12;
        _manageBtn.Click += delegate { ShowManageConversations(); };
        headerStack.Children.Add(_manageBtn);
        Grid.SetRow(headerStack, 0); side.Children.Add(headerStack);

        _convList = new StackPanel { Margin = new Thickness(8, 4, 8, 4) };
        var convScroll = new ScrollViewer { Content = _convList, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(convScroll, 1); side.Children.Add(convScroll);

        // Compact icon-button row (Wave 2): cockpit / language / theme as Material-Symbols icons,
        // replacing the three full-width emoji text buttons. Each carries a localized tooltip with the
        // old label text. Accent stays reserved for the one primary action (Send).
        //   • cockpit  = grid_view (4 tiles = parallel execution; account_tree is FleetCockpit's
        //                self-improve glyph — avoid collision)
        //   • language = translate
        //   • theme    = light_mode/dark_mode, swapped by state (shows the mode you'd switch TO)
        var bottom = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(8, 8, 8, 12) };
        _cockpitBtn = IconButton("grid_view", 18, T("tip_cockpit"));
        _cockpitBtn.Click += delegate { OpenCockpit(); };
        _langBtn = IconButton("translate", 18, T("tip_lang"));
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveSettings(); UpdateChrome(); RefreshConvList(); RerenderActiveConversation(); if (_emptyState != null) { RemoveEmptyState(); ShowEmptyState(); } };
        _themeBtn = IconButton(_dark ? "light_mode" : "dark_mode", 18, T("tip_theme"));
        _themeBtn.Click += delegate { _dark = !_dark; ApplyTheme(); _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18); _themeBtn.ToolTip = T("tip_theme"); SaveSettings(); };
        bottom.Children.Add(_cockpitBtn); bottom.Children.Add(_langBtn); bottom.Children.Add(_themeBtn);
        Grid.SetRow(bottom, 2); side.Children.Add(bottom);

        var sideBorder = new Border { Child = side, BorderThickness = new Thickness(0, 0, 1, 0) };
        _sideBorderRef = sideBorder;
        SetRef(sideBorder, Border.BorderBrushProperty, "Border");
        Grid.SetColumn(sideBorder, 0); root.Children.Add(sideBorder);

        // ── main column ──
        var main = new Grid();
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        main.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        // Header: DockPanel so the right side can hold the Fleet chip + settings button.
        var headPanel = new DockPanel { Margin = new Thickness(6, 0, 16, 0), MinHeight = 48 };
        // Far-left: sidebar toggle button (hamburger). Always visible even when sidebar is collapsed.
        _sideToggleBtn = new Button
        {
            // THE ICON, NOT A CHARACTER THAT LOOKS LIKE ONE. This was U+2630, the I Ching trigram
            // for heaven, borrowed because it has three horizontal lines. Its weight, size and
            // vertical placement come from whichever font on the machine happens to contain it,
            // so it never matched the drawn icons three inches below it in the same window. This
            // app already ships a glyph subset and a renderer for it; the button now uses them.
            Content = MakeIcon("menu", 18),
            FontSize = 14,
            Width = 36, Height = 36,
            Cursor = Cursors.Hand,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            ToolTip = (_lang == 0 ? "サイドバーを切り替える (Ctrl+B)" : "Toggle sidebar (Ctrl+B)")
        };
        SetRef(_sideToggleBtn, ForegroundProperty, "Muted");
        _sideToggleBtn.Click += delegate { ToggleSidebar(); };
        DockPanel.SetDock(_sideToggleBtn, Dock.Left);
        headPanel.Children.Add(_sideToggleBtn);
        // Left: clickable status dot (connection state -> click opens Cockpit) + CURRENT CONVERSATION
        // TITLE (Wave 2). The window chrome already says "Copilot", so the header now carries the live
        // conversation title (Muted-strong, ellipsis-trimmed) instead of a redundant "Copilot" label.
        var headLeft = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(4, 0, 0, 0), MaxWidth = 420 };
        // The dot itself is small; wrap it in a slightly larger transparent hit target so it is easy
        // to click. The click opens the Fleet Cockpit health view (single dot + click-through design).
        // A circle, so the radius is half the width and follows it if the width ever changes.
        // Geometry, not style: deliberately not a token.
        _statusDot = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(9 / 2.0), VerticalAlignment = VerticalAlignment.Center };
        SetRef(_statusDot, BackgroundProperty, "Faint");
        var dotHit = new Border
        {
            Child = _statusDot, Background = Brushes.Transparent,
            Padding = new Thickness(0, 3, 8, 3), Margin = new Thickness(0, 1, 0, 0),
            Cursor = Cursors.Hand, VerticalAlignment = VerticalAlignment.Center,
            ToolTip = T("dot_click_tip")
        };
        dotHit.MouseLeftButtonUp += delegate { OpenCockpit(); };
        _dotHit = dotHit;   // re-localized on language toggle (UpdateChrome)
        _headTitle = new TextBlock
        {
            Text = T("newchat"), FontWeight = FontWeights.SemiBold, FontSize = 14.5,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap
        };
        SetRef(_headTitle, TextBlock.ForegroundProperty, "Muted");
        headLeft.Children.Add(dotHit); headLeft.Children.Add(_headTitle);
        DockPanel.SetDock(headLeft, Dock.Left);
        headPanel.Children.Add(headLeft);
        // Right: settings icon + Fleet chip (built right-to-left in DockPanel terms)
        var headRight = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
        DockPanel.SetDock(headRight, Dock.Right);
        // Settings button (opens cockpit)
        var settingsBtn = Btn("", "PanelAlt", "Muted", true);
        // U+2699 GEAR was standing in for the settings icon that this app already carries.
        settingsBtn.Content = MakeIcon("settings", 18);
        settingsBtn.Padding = new Thickness(12, 3, 12, 3); settingsBtn.FontSize = 13;
        settingsBtn.ToolTip = _lang == 0 ? "Fleet / コックピットを開く" : "Open Fleet / Cockpit";
        settingsBtn.Click += delegate { OpenCockpit(); };
        headRight.Children.Add(settingsBtn);
        // Fleet active chip ("Fleet: N") -- collapsed when no active workers or status.json absent
        _fleetChipLabel = new TextBlock { FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
        SetRef(_fleetChipLabel, TextBlock.ForegroundProperty, "Muted");
        _fleetChip = new Border
        {
            Child = _fleetChipLabel, BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(Theme.RadSmall), Padding = new Thickness(12, 3, 12, 3),
            Margin = new Thickness(0, 0, 8, 0), Cursor = Cursors.Hand,
            Visibility = Visibility.Collapsed
        };
        SetRef(_fleetChip, BackgroundProperty, "PanelAlt"); SetRef(_fleetChip, Border.BorderBrushProperty, "Border");
        _fleetChip.MouseLeftButtonUp += delegate { OpenCockpit(); };
        headRight.Children.Add(_fleetChip);
        headPanel.Children.Add(headRight);
        var headBorder = new Border { Child = headPanel, BorderThickness = new Thickness(0, 0, 0, 1) };
        SetRef(headBorder, Border.BorderBrushProperty, "Border");
        Grid.SetRow(headBorder, 0); main.Children.Add(headBorder);

        _messages = new StackPanel { Margin = new Thickness(0, 8, 0, 8), MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Center };
        _scroll = new ScrollViewer { Content = _messages, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(24, 16, 24, 16) };

        // ── THE STATE OF THE WORK, OUT OF THE CONVERSATION ──────────────────────────────
        //
        // A status line was being appended to the transcript as an assistant TURN: "状態: running
        // ターン 3/1000" arrived as though the agent had said it. Status is a value that is true
        // NOW; a message is a record of a moment. Mixing them forces the whole transcript to be
        // rebuilt whenever the status moves, which is what destroyed the reader's text selection
        // once a second, and it puts the least durable thing in the most permanent place.
        //
        // So it lives here: one band, above the transcript, updated in place. Nothing about it
        // touches _messages, so a status change cannot disturb what the reader is looking at.
        _statusBand = new Border
        {
            // 4-MULTIPLE SPACING, from the set already in use. The first draft used 14/7 and the
            // repository's own design test refused it: "new spacing values appeared: [14.0] --
            // pick one already in use". A band that introduces its own rhythm is exactly the
            // "全く違うものになっている" this restructure is meant to stop.
            Padding = new Thickness(16, 8, 16, 8),
            BorderThickness = new Thickness(0, 0, 0, 1),
            Visibility = Visibility.Collapsed,
        };
        SetRef(_statusBand, Border.BorderBrushProperty, "Border");
        _statusText = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 12.5 };
        SetRef(_statusText, TextBlock.ForegroundProperty, "Muted");
        _statusBand.Child = _statusText;
        var chatCol = new Grid();
        chatCol.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        chatCol.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        Grid.SetRow(_statusBand, 0); chatCol.Children.Add(_statusBand);
        Grid.SetRow(_scroll, 1); chatCol.Children.Add(_scroll);
        Grid.SetRow(chatCol, 1); main.Children.Add(chatCol);
        // Auto-scroll, but yield to the user. ScrollChanged fires for BOTH user scrolls and content
        // growth: when the extent didn't change it was the USER moving -> stick only if they're at
        // the bottom; when content grew -> pin to the bottom ONLY if still sticking. So while a reply
        // streams the view follows along, but the moment the user scrolls up to read back, following
        // stops and stays put -- until they scroll back to the bottom, which re-arms it.
        _scroll.ScrollChanged += (s, e) =>
        {
            if (e.ExtentHeightChange == 0)
                _stickBottom = _scroll.VerticalOffset >= _scroll.ScrollableHeight - 2.0;
            else if (_stickBottom)
                _scroll.ScrollToVerticalOffset(_scroll.ScrollableHeight);
        };

        // ── integrated composer (spec Task 1: one rounded surfaceSubtle border, text area on top,
        //    footer row below with "/" affordance left and Send right — mirrors the Fleet composer).
        _input = new TextBox
        {
            MinHeight = 40, MaxHeight = 180, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, FontSize = 14,
            Padding = new Thickness(4, 6, 4, 4),
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            VerticalContentAlignment = VerticalAlignment.Top, MinWidth = 0
        };
        SetRef(_input, ForegroundProperty, "Fg");
        SetRef(_input, TextBox.CaretBrushProperty, "Fg");
        _input.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            // slash-command autocomplete navigation (when the popup is open)
            if (_cmdPopup != null && _cmdPopup.IsOpen && _cmdList.Items.Count > 0)
            {
                if (e.Key == Key.Down) { _cmdList.SelectedIndex = Math.Min(_cmdList.SelectedIndex + 1, _cmdList.Items.Count - 1); _cmdList.ScrollIntoView(_cmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Up) { _cmdList.SelectedIndex = Math.Max(_cmdList.SelectedIndex - 1, 0); _cmdList.ScrollIntoView(_cmdList.SelectedItem); e.Handled = true; return; }
                if (e.Key == Key.Enter || e.Key == Key.Tab) { AcceptCommand(); e.Handled = true; return; }
                if (e.Key == Key.Escape) { _cmdPopup.IsOpen = false; e.Handled = true; return; }
            }
            if (e.Key == Key.V && (Keyboard.Modifiers & ModifierKeys.Control) != 0 && Clipboard.ContainsImage())
            { e.Handled = true; PasteImage(); return; }
            // Esc interrupts a streaming reply, the way Claude-Code users expect (the Send button
            // also doubles as Stop, but Esc is the muscle-memory gesture).
            if (e.Key == Key.Escape && _generating)
            { e.Handled = true; try { if (_activeReq != null) _activeReq.Abort(); } catch { } return; }
            if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0) { e.Handled = true; DoSend(); }
        };
        _input.TextChanged += delegate { UpdateCmdPopup(); PaintSend(); };
        // Placeholder hint advertising slash commands (WPF TextBox has no native placeholder). The
        // single most Claude-Code-defining feature was invisible until you happened to type "/".
        _inputHint = new TextBlock
        {
            Text = _lang == 0 ? "メッセージを入力…" : "Type a message…",
            IsHitTestVisible = false, FontSize = 13.5, Margin = new Thickness(6, 6, 0, 0),
            VerticalAlignment = VerticalAlignment.Top, HorizontalAlignment = HorizontalAlignment.Left
        };
        SetRef(_inputHint, TextBlock.ForegroundProperty, "Muted");
        _input.TextChanged += delegate { _inputHint.Visibility = string.IsNullOrEmpty(_input.Text) ? Visibility.Visible : Visibility.Collapsed; };
        // Overlay grid: input + hint share the same cell so the hint sits under the caret.
        var inputOverlay = new Grid();
        inputOverlay.Children.Add(_input);
        inputOverlay.Children.Add(_inputHint);
        BuildCmdPopup();
        // Footer row: left = "/" affordance, right = Send button.
        _send = Btn(T("send"), "AccentFill", "AccentFg", false);
        _send.Height = 32; _send.Padding = new Thickness(16, 0, 16, 0);
        _send.FontWeight = FontWeights.SemiBold;
        _send.Click += delegate
        {
            if (_generating) { try { if (_activeReq != null) _activeReq.Abort(); } catch { } }
            else DoSend();
        };
        var slashBtn = new Button
        {
            Content = "/", FontSize = 12, FontWeight = FontWeights.SemiBold,
            Height = 30, Width = 30, Cursor = Cursors.Hand,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            ToolTip = _lang == 0 ? "スラッシュコマンド" : "Slash commands"
        };
        SetRef(slashBtn, ForegroundProperty, "Faint");
        slashBtn.Click += delegate
        {
            if (!_input.Text.StartsWith("/")) { _input.Text = "/"; _input.CaretIndex = 1; }
            _input.Focus();
        };
        // ITEM 4: attach button lives INSIDE the composer footer (left, next to the slash button),
        // quiet Faint styling — replacing the standalone row above the composer. Same AttachFile()
        // handler; the paste-image (Ctrl+V) and any drag-drop paths are unchanged (they call
        // UploadFile directly and don't depend on this button's location).
        _attachBtn = new Button
        {
            Content = "+", FontSize = 15, FontWeight = FontWeights.SemiBold,
            Height = 30, Width = 30, Cursor = Cursors.Hand,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            ToolTip = T("attach")
        };
        SetRef(_attachBtn, ForegroundProperty, "Faint");
        _attachBtn.Click += delegate { AttachFile(); };
        var footerRow = new DockPanel { Margin = new Thickness(0, 6, 0, 0) };
        var footLeft = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        footLeft.Children.Add(slashBtn);
        footLeft.Children.Add(_attachBtn);
        // Steer-mode destination indicator ("送信先: W1 / Fleet会話"). Collapsed unless steering;
        // RefreshSteerVisual() sets the text and visibility.
        _steerHint = new TextBlock
        {
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(4, 0, 0, 0),
            Visibility = Visibility.Collapsed
        };
        SetRef(_steerHint, TextBlock.ForegroundProperty, "Accent");
        footLeft.Children.Add(_steerHint);
        DockPanel.SetDock(footLeft, Dock.Left);
        DockPanel.SetDock(_send, Dock.Right);
        footerRow.Children.Add(_send);
        footerRow.Children.Add(footLeft);
        // Integrated composer border (rounded surfaceSubtle surface, mirrors Fleet composer).
        var composerInner = new StackPanel();
        composerInner.Children.Add(inputOverlay);
        composerInner.Children.Add(footerRow);
        _composerBorder = new Border
        {
            Child = composerInner,
            CornerRadius = new CornerRadius(Theme.RadBubble),
            // Constant 1px at rest (Border token) — gives a stable boundary (fixes the weak
            // light-theme edge) and, crucially, a constant footprint. The 1px rest/1px focus/
            // 2px steer progression only ever changes thickness by 1 (steer), which SetComposerRing
            // compensates with a matching -1 padding so ActualHeight never moves.
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 6, 12, 6),
            Margin = new Thickness(0, 12, 0, 16),
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Effect = new System.Windows.Media.Effects.DropShadowEffect
            {
                Color = Colors.Black, BlurRadius = 14, ShadowDepth = 2,
                Opacity = 0.16, Direction = 270, RenderingBias = System.Windows.Media.Effects.RenderingBias.Performance
            }
        };
        SetRef(_composerBorder, BackgroundProperty, "PanelAlt");
        SetComposerRing("rest");   // 1px Border token, full padding
        // Focus ring: 1px Accent while focused, 1px Border at rest, 2px Accent in steer mode.
        // Thickness never grows total size (SetComposerRing swaps padding to compensate).
        _input.GotKeyboardFocus += delegate { if (string.IsNullOrEmpty(_activeFleetUrl)) SetComposerRing("focus"); };
        _input.LostKeyboardFocus += delegate { if (string.IsNullOrEmpty(_activeFleetUrl)) SetComposerRing("rest"); };
        // Clicking the border surface focuses the text input (nice-to-have).
        _composerBorder.MouseLeftButtonDown += delegate { _input.Focus(); };
        PaintSend();   // initial state: input empty -> neutral
        // Stretch (not Center) so the column actually fills the available width; MaxWidth=760 caps it
        // and WPF auto-centers a Stretch element once its content is narrower than the cap. With Center
        // the StackPanel would shrink to its content, yielding the narrow ~280px box regression.
        var barStack = new StackPanel { MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Stretch };
        barStack.Children.Add(BuildFleetStrip());
        barStack.Children.Add(BuildRouterBar());
        barStack.Children.Add(BuildAttachRow());
        barStack.Children.Add(_composerBorder);
        var barBorder = new Border { Child = barStack, BorderThickness = new Thickness(0, 1, 0, 0) };
        SetRef(barBorder, Border.BorderBrushProperty, "Border");
        Grid.SetRow(barBorder, 2); main.Children.Add(barBorder);

        // delete-mode banner (overlays the top of the message area)
        _banner = new Border
        {
            Visibility = Visibility.Collapsed, VerticalAlignment = VerticalAlignment.Top,
            Margin = new Thickness(24, 12, 24, 0), CornerRadius = new CornerRadius(Theme.RadBubble),
            BorderThickness = new Thickness(1), Padding = new Thickness(16, 12, 16, 16),
            MaxWidth = 560, HorizontalAlignment = HorizontalAlignment.Center
        };
        SetRef(_banner, BackgroundProperty, "Panel"); SetRef(_banner, Border.BorderBrushProperty, "Accent");
        _bannerBody = new StackPanel(); _banner.Child = _bannerBody;
        Panel.SetZIndex(_banner, 100);
        Grid.SetRow(_banner, 1); main.Children.Add(_banner);

        Grid.SetColumn(main, 1); root.Children.Add(main);
        Content = root;

        // Set _convsPath BEFORE LoadConversations so DiscoverTranscripts and LoadSidebarState work.
        // --selftest reads and writes an empty scratch directory instead of the real .fleet.
        string fleetDir = WindowSelfTest.Active ? WindowSelfTest.ScratchDir("chat-fleet")
            : Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet"));
        _openPath = Path.Combine(fleetDir, "open.json");
        _convsPath = Path.Combine(fleetDir, "conversations.json");
        _sidebarStatePath = Path.Combine(fleetDir, "sidebar_state.json");
        LoadSidebarState();    // load pinned/archived/collapsed before first RefreshConvList()

        LoadConversations();
        Loaded += delegate
        {
            ForceVisibleOnce();
            _input.Focus();
            // UI-scale first run. AUTO is the default for NEW users: seed _scaleTarget from the PRIMARY
            // monitor's scale (the size the user is used to) and compute the per-monitor effective scale.
            // If a scale was already persisted (auto OR manual, from either app) honor it and just reflect
            // it on the live transform. Runs in Loaded because PresentationSource/DPI is available now.
            double monitorScale = CurrentMonitorScale();
            if (!_scaleTargetLoaded)
            {
                _scaleTarget = System.Math.Max(0.8, System.Math.Min(3.0, monitorScale));
                if (_scaleTarget < 0.81) _scaleTarget = 1.5;   // 100% primary -> still target a comfy 1.5
                _scaleTargetLoaded = true;
                SaveSettings();   // persist ui_scale_target
            }
            if (!_uiScaleLoaded)
            {
                _uiAuto = true;               // new-user default = AUTO
                _uiScaleLoaded = true;
                ApplyAutoScale(false);        // silent apply for this monitor
                SaveSettings();               // persist ui_scale=auto (+ target)
            }
            else if (_uiAuto) ApplyAutoScale(false);   // reflect auto for THIS monitor silently
            else ApplyScale(false);                    // apply the persisted manual zoom silently
        };
        // Window-level Esc -> interrupt a streaming reply, REGARDLESS of focus. The input-level
        // handler only fires when the box is focused, but mid-stream focus is usually elsewhere, so
        // Esc was falling through to the sidebar (it navigated to "新しいチャット" instead of stopping).
        // PreviewKeyDown tunnels from the window down, so this fires first and swallows the key.
        PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Escape && _generating)
            { e.Handled = true; try { if (_activeReq != null) _activeReq.Abort(); } catch { } }
            // Ctrl+B: toggle sidebar (Claude Code / Codex style). Fire regardless of focus;
            // it does NOT interfere with composer Ctrl+V or Enter (different keys).
            if (e.Key == Key.B && (Keyboard.Modifiers & ModifierKeys.Control) != 0)
            { e.Handled = true; ToggleSidebar(); return; }
            // Whole-UI zoom shortcuts (Ctrl+±/0). Attached at window level so focus location doesn't
            // matter. These keys don't collide with the composer (Enter/Shift+Enter/Ctrl+V/Esc).
            if ((Keyboard.Modifiers & ModifierKeys.Control) != 0)
            {
                // '+' sits on OemPlus (and Add on the numpad); Plus is the abstract key on some layouts.
                if (e.Key == Key.OemPlus || e.Key == Key.Add)
                { e.Handled = true; BumpScale(+0.1); return; }
                if (e.Key == Key.OemMinus || e.Key == Key.Subtract)
                { e.Handled = true; BumpScale(-0.1); return; }
                if (e.Key == Key.D0 || e.Key == Key.NumPad0)
                { e.Handled = true; ResetScale(); return; }
            }
        };
        // Ctrl+MouseWheel -> ±0.1 per notch. PreviewMouseWheel at window level so it fires wherever
        // the pointer is; only acts when Ctrl is held so normal scrolling is untouched.
        PreviewMouseWheel += delegate (object s, MouseWheelEventArgs e)
        {
            if ((Keyboard.Modifiers & ModifierKeys.Control) != 0 && e.Delta != 0)
            { e.Handled = true; BumpScale(e.Delta > 0 ? +0.1 : -0.1); }
        };
        // Apply persisted sidebar collapsed state AFTER the window is fully constructed.
        if (_sidebarCollapsed) ApplySidebarState();

        // ① watch for the cockpit asking to open a parallel-task conversation here, and
        // sync the session-shared conversation registry (fleet + chat conversations).
        // Normally seed _openMtime to the current open.json so only CHANGES after launch are
        // acted on -- otherwise every start would replay whatever conversation was last opened.
        // EXCEPTION: when the cockpit starts this chat for a card Open (EnsureMainChatRunning in
        // ui/FleetCockpit.Commands.cs), it writes open.json an instant before/after we launch, so
        // that request is only seconds old. Treat a FRESH open.json (<= 10s) as unseen by leaving
        // _openMtime at 0, so the first CheckOpenRequest tick opens the surface the user clicked.
        // A STALE open.json (older than that, i.e. a previous session's leftover) is seeded as
        // already-seen and ignored -- so a plain launch does not reopen an old conversation.
        try
        {
            if (File.Exists(_openPath))
            {
                long t = File.GetLastWriteTimeUtc(_openPath).Ticks;
                bool fresh = (DateTime.UtcNow.Ticks - t) <= TimeSpan.FromSeconds(10).Ticks;
                _openMtime = fresh ? 0 : t;
            }
            else _openMtime = 0;
        }
        catch { _openMtime = 0; }
        try { _settingsMtime = File.Exists(SettingsFile) ? File.GetLastWriteTimeUtc(SettingsFile).Ticks : 0; }
        catch { _settingsMtime = 0; }
        var openTimer = new DispatcherTimer();
        openTimer.Interval = TimeSpan.FromMilliseconds(800);
        openTimer.Tick += delegate
        {
            CheckOpenRequest(); SyncRegistry(); CheckFleetSnapshot(); CheckSettings(); RefreshFleetChip(); RefreshFleetStrip();
            // Bridge reachability at a LOW cadence (~15s), not every 800ms: probe on the first
            // tick, then every ~19 ticks. The probe runs off-thread; RefreshIdleDot updates the dot.
            if (_reachTick == 0 || _reachTick % 19 == 0) ProbeBridge();
            _reachTick++;
        };
        openTimer.Start();
        SetDot("idle");   // optimistic idle at launch; the first ProbeBridge tick confirms/corrects
        SyncRegistry();
        // Dispose the transcript watcher on exit -- a FileSystemWatcher left running past window
        // close is a leaked OS handle, not just a leaked object.
        Closed += delegate { StopFollowingTranscript(); };
    }

    string _openPath; long _openMtime;
    long _settingsMtime;                 // follow external lang/theme edits (e.g. the cockpit toggled it)
    string _convsPath; long _convsMtime;

    string _activeFleetUrl;              // conv URL of the fleet snapshot currently shown (null = none)
    long _statusMtime;                   // last-seen mtime of status.json (for live re-render)
    //: What the fleet view last PUT ON SCREEN, one entry per rendered turn. The mtime above says
    //: the file moved; this says whether the reader would see anything different, and WHERE it
    //: differs -- a per-turn list is what lets new turns be appended instead of the panel being
    //: rebuilt around the reader. A single joined string could only answer "same or not".
    List<string> _fleetTurnSigs = new List<string>();
    //: The band above the transcript that carries the CURRENT state of a fleet run. Separate
    //: from the messages on purpose: a status is a value now, a message is a record of a moment,
    //: and giving them one place made every status tick rewrite the conversation.
    Border _statusBand;
    TextBlock _statusText;

    byte[] _txPartialLine = new byte[0];   // trailing bytes not yet terminated by '\n'
    readonly object _txFollowLock = new object();  // serializes catch-up passes

    string _lastRegistryError = "";
    string _historyNotice = "";
    readonly JavaScriptSerializer _cjs = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    Dictionary<string, string> _glyphs = new Dictionary<string, string>();
    double _upm = 960;
    bool _signinLatch = false;

    static readonly string[][] _commandsJa = {
        new[]{"/help","コマンド一覧を表示"},
        new[]{"/research","Claude researcher で深掘り調査"},
        new[]{"/review","全ファイル/diff/指定パスをレビューし要約"},
        new[]{"/security-review","セキュリティ観点でレビュー"},
        new[]{"/review-fix","レビューの指摘を修正（確認あり・自動バックアップ）"},
        new[]{"/analyze","アナリストでファイルを分析"},
        new[]{"/summarize","要約する"},
        new[]{"/translate","翻訳: /translate <言語> <文>"},
        new[]{"/plan","ステップ計画を作る"},
        new[]{"/critique","批判的にレビュー"},
        new[]{"/proofread","校正して修正版を返す"},
        new[]{"/rewrite","文体を変えて書き直す"},
        new[]{"/brainstorm","アイデアを10個出す"},
        new[]{"/steps","手順に分解"},
        new[]{"/eli5","やさしく説明"},
        new[]{"/proscons","賛否を表で"},
        new[]{"/table","表を作る"},
    };
    static readonly string[][] _commandsEn = {
        new[]{"/help","Show the command list"},
        new[]{"/research","Deep research with the Claude researcher"},
        new[]{"/review","Review all files / diff / a path"},
        new[]{"/security-review","Security-focused review"},
        new[]{"/review-fix","Fix review findings (confirm step, auto-backup)"},
        new[]{"/analyze","Analyze a file with the analyst"},
        new[]{"/summarize","Summarize"},
        new[]{"/translate","Translate: /translate <lang> <text>"},
        new[]{"/plan","Make a step-by-step plan"},
        new[]{"/critique","Review critically"},
        new[]{"/proofread","Proofread and return a corrected version"},
        new[]{"/rewrite","Rewrite in a different style"},
        new[]{"/brainstorm","Generate 10 ideas"},
        new[]{"/steps","Break into steps"},
        new[]{"/eli5","Explain simply"},
        new[]{"/proscons","Pros and cons as a table"},
        new[]{"/table","Make a table"},
    };
    static readonly string[] _p2cCommandJaReview =
        new[]{"/deep-review","深掘りレビュー（拒否時のみ再試行・分割）"};
    static readonly string[] _p2cCommandJaSecurity =
        new[]{"/deep-security-review","深掘りセキュリティレビュー"};
    static readonly string[] _p2cCommandEnReview =
        new[]{"/deep-review","Deep review (retry/split only after refusal)"};
    static readonly string[] _p2cCommandEnSecurity =
        new[]{"/deep-security-review","Deep security review"};

    HashSet<string> _sectionExpanded = new HashSet<string>();
    int _sectionCap = 8;
    static readonly TimeSpan UndoWindow = TimeSpan.FromSeconds(8);
    int _pendingUndoIndex = -1;

    static readonly FontFamily BODY_FACE = new FontFamily(Theme.UiFont);
    static readonly Thickness BODY_PAD = new Thickness(0);
    Border _routerBar; bool _routerShown; string _routerText = "";
    string _lastNotSavedReason = "";
    DateTime _bridgeProblemShown = DateTime.MinValue;
}
