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
        try { _openMtime = File.Exists(_openPath) ? File.GetLastWriteTimeUtc(_openPath).Ticks : 0; }
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

    // Re-render the open LOCAL conversation so per-message chrome (the copy-button tooltip) re-localizes
    // on a language toggle. Skip fleet/steer views: re-rendering those could re-fetch or drop steer mode.
    void RerenderActiveConversation()
    {
        if (_conv != null && _activeFleetUrl == null && _conv.Messages.Count > 0)
            OpenConversation(_conv);
    }
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

    // THE REGISTRY, READ WITHOUT SWALLOWING. This used to be a bare `catch { }` returning an empty
    // list, and the file crossed the serializer's 2,097,152-character default: every read threw,
    // nothing was logged, and the sidebar showed no registry rows at all with no sign why. A
    // failure is now logged (.fleet/chat_listing.log) and shown as a one-line notice in the
    // sidebar; a later successful read clears it.
    List<object> ReadConvsRegistry()
    {
        string err;
        var rows = ConvListing.ReadRegistry(_convsPath, out err);
        if (err.Length > 0)
        {
            if (err != _lastRegistryError)
                ConvListing.Diag(Path.GetDirectoryName(_convsPath), "registry read failed: " + err);
            _lastRegistryError = err;
            _historyNotice = ConvListing.UnreadableNotice(_lang == 0, err);
        }
        else if (_lastRegistryError.Length > 0)
        {
            _lastRegistryError = "";
            _historyNotice = "";
        }
        return rows;
    }
    string _lastRegistryError = "";
    string _historyNotice = "";
    // Add this conversation to the shared registry so the cockpit/other side lists it.
    void RegisterConv(string url, string title, string source)
    {
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            var list = ReadConvsRegistry();
            if (_lastRegistryError.Length > 0) return;   // never write back what could not be read: that replaces the file with this one row
            foreach (var o in list) { var d = o as Dictionary<string, object>; if (d != null && SS(d, "url") == url) return; }
            var e = new Dictionary<string, object>(); e["url"] = url; e["title"] = title ?? ""; e["source"] = source; e["ts"] = 0;
            list.Add(e);
            File.WriteAllText(_convsPath, _cjs.Serialize(list), new System.Text.UTF8Encoding(false)); // no BOM (Python reads this)
            _convsMtime = File.GetLastWriteTimeUtc(_convsPath).Ticks;
        }
        catch { }
    }
    // Remove entries whose url is in 'urls' from the shared registry and persist, so
    // bulk-deleted conversations don't reappear via SyncRegistry.
    void UnregisterConvs(System.Collections.Generic.HashSet<string> urls)
    {
        if (urls == null || urls.Count == 0) return;
        try
        {
            var list = ReadConvsRegistry();
            if (_lastRegistryError.Length > 0) return;   // same: an unreadable registry must not be overwritten with the remainder
            var keep = new List<object>();
            foreach (var o in list)
            {
                var d = o as Dictionary<string, object>;
                if (d != null && urls.Contains(SS(d, "url"))) continue;
                keep.Add(o);
            }
            File.WriteAllText(_convsPath, _cjs.Serialize(keep), new System.Text.UTF8Encoding(false)); // no BOM
            _convsMtime = File.GetLastWriteTimeUtc(_convsPath).Ticks;
        }
        catch { }
    }
    // Pull any conversations from the shared registry (e.g. fleet ones) into the sidebar
    // as lazy placeholders -- clicking loads their content via /history.
    void SyncRegistry()
    {
        try
        {
            if (!File.Exists(_convsPath)) return;
            long m = File.GetLastWriteTimeUtc(_convsPath).Ticks;
            if (m == _convsMtime) return;
            _convsMtime = m;
            bool added = false;
            string noticeBefore = _historyNotice;
            foreach (var o in ReadConvsRegistry())
            {
                var d = o as Dictionary<string, object>;
                if (d == null) continue;
                string url = SS(d, "url");
                string transcript = SS(d, "transcript");
                string regSource = SS(d, "source");
                string regName = SS(d, "name");
                var regLineage = RegistryTranscriptLineage(d);
                // A socket-driven fleet worker never gets a conv_url: relay_fleet.py's
                // _capture_url only ever fires when the worker holds a browser `page`, and a
                // socket worker's page is None by construction (MCP_FLEET_SOCKET has defaulted
                // on since 2026-08-21). Requiring url here silently dropped every such worker
                // from the ONLY live-update path -- DiscoverTranscripts only runs once, at
                // startup, capped at 80 -- so the fleet section froze at whatever that one scan
                // found and never grew again. transcript alone is already a valid identity for
                // opening a conversation: the click handler tries c.Transcript's on-disk read
                // BEFORE it ever needs ConvUrl (see the "a registry/fleet conversation we
                // haven't loaded yet" branch below), so a transcript-only row is not degraded,
                // just openable a different way. A row with neither is still skipped -- there
                // is nothing to show or open for it either way.
                if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(transcript) && regLineage.Count == 0) continue;
                // THE FULL GOAL TEXT (relay/fleet_runner.py::_register_convs, added alongside
                // this read). NOT "title": make_title() truncates that for display, and
                // DecideFleetSend (ChatSend.cs) addresses a fleet conversation by the goal, not
                // the title -- a follow-up or a mid-run interrupt sent through a row with no
                // goal was refused (fleet_no_goal) even while the worker was live, because this
                // registry poll is the ONLY feed that updates while the window is already open
                // (DiscoverTranscripts only scans once, at startup).
                string regGoal = SS(d, "goal");
                Conversation existingC = null;
                foreach (var c in _all)
                {
                    if (!string.IsNullOrEmpty(url) && c.ConvUrl == url) { existingC = c; break; }
                    if (string.IsNullOrEmpty(url) && regSource == c.Source && regName == c.Name
                        && !string.IsNullOrEmpty(regName)) { existingC = c; break; }
                    if (string.IsNullOrEmpty(url) && !string.IsNullOrEmpty(transcript)
                        && c.Transcript == transcript) { existingC = c; break; }
                }
                if (existingC != null)
                {
                    // BACKFILL ONLY: a row discovered before the registry carried "goal", or one
                    // whose identity was never set by OpenFromFleet (see there), must not stay
                    // permanently unaddressable just because it already exists. Never overwrite
                    // a goal/transcript this row already has -- the decision itself lives in
                    // FleetConvIdentity.MergeBackfillOnly (see ui/FleetConvIdentity.cs) so a
                    // test can run it directly instead of only reading this call site as text.
                    existingC.Goal = FleetConvIdentity.MergeBackfillOnly(existingC.Goal, regGoal);
                    existingC.Source = FleetConvIdentity.MergeBackfillOnly(existingC.Source, regSource);
                    existingC.Name = FleetConvIdentity.MergeBackfillOnly(existingC.Name, regName);
                    // Seed a legacy row's single latest pointer into the lineage BEFORE applying
                    // the fresh registry chain, so a stale poll can never rewind a newer pointer.
                    var priorLineage = FleetConvIdentity.MergeTranscriptLineage(
                        existingC.Transcripts, null, existingC.Transcript);
                    existingC.Transcripts = FleetConvIdentity.MergeTranscriptLineage(
                        priorLineage, regLineage, transcript);
                    existingC.Transcript = FleetConvIdentity.LatestTranscript(
                        existingC.Transcripts, FleetConvIdentity.MergeBackfillOnly(existingC.Transcript, transcript));
                    continue;
                }
                {
                    var c = new Conversation();
                    c.ConvUrl = url;
                    c.Title = SS(d, "title");
                    c.Source = regSource;
                    c.Transcripts = regLineage;
                    c.Transcript = FleetConvIdentity.LatestTranscript(c.Transcripts, transcript);
                    c.Name = regName;
                    c.Goal = regGoal;
                    double tsv;
                    c.Ts = (d.ContainsKey("ts") && d["ts"] != null
                            && double.TryParse(Convert.ToString(d["ts"], System.Globalization.CultureInfo.InvariantCulture),
                                               System.Globalization.NumberStyles.Float,
                                               System.Globalization.CultureInfo.InvariantCulture, out tsv)) ? tsv : 0;
                    _all.Insert(0, c);   // newest on top (registry/fleet convs were appended below)
                    added = true;
                }
            }
            if (added || _historyNotice != noticeBefore) RefreshConvList();
        }
        catch (Exception ex)
        {
            ConvListing.Diag(Path.GetDirectoryName(_convsPath), "SyncRegistry failed: " + ex);
            _historyNotice = ConvListing.UnreadableNotice(_lang == 0, ex.Message);
        }
    }
    readonly JavaScriptSerializer _cjs = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

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

    // Poll .fleet/open.json (written by the cockpit when a card is clicked) and, when it
    // changes, load that conversation into the main chat (view + steerable via /switch).
    void CheckOpenRequest()
    {
        try
        {
            if (!File.Exists(_openPath)) return;
            long m = File.GetLastWriteTimeUtc(_openPath).Ticks;
            if (m == _openMtime) return;
            _openMtime = m;
            var d = _cjs.DeserializeObject(File.ReadAllText(_openPath, Encoding.UTF8)) as Dictionary<string, object>;
            if (d == null) return;
            string url = (d.ContainsKey("url") && d["url"] != null) ? d["url"].ToString() : "";
            string worker = (d.ContainsKey("worker") && d["worker"] != null) ? d["worker"].ToString() : "";
            string transcript = (d.ContainsKey("transcript") && d["transcript"] != null) ? d["transcript"].ToString() : "";
            // WHICH ENTRY WAS CHOSEN, when one was. The cockpit card lists what happened and
            // holds no body; clicking an entry has to land the reader ON it, not at the end of a
            // conversation they then have to search. Absent means "just open it", which is what
            // the card header and the history list still ask for.
            int turnIndex = -1;
            if (d.ContainsKey("turn_index") && d["turn_index"] != null)
                int.TryParse(d["turn_index"].ToString(), out turnIndex);
            if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(worker) && string.IsNullOrEmpty(transcript)) return;
            // Bring this chat window to the front so a cockpit "▶ 開く" click lands you here without
            // an alt-tab -- the two-window round-trip was the biggest gap vs Claude Code's one pane.
            try
            {
                if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
                Activate(); Topmost = true; Topmost = false; Focus();
            }
            catch { }
            int wantIdx = turnIndex;
            new Thread((ThreadStart)delegate { OpenFromFleet(url, worker, transcript); ScrollToTurn(wantIdx); })
            { IsBackground = true }.Start();
        }
        catch { }
    }

    // Put the chosen entry at the top of the view and mark it, so a click on a card lands on the
    // thing that was clicked.
    //
    // AND STOP FOLLOWING THE TAIL. Arriving at turn 4 of 40 and then being dragged to the bottom
    // by the next status write is the same defect as opening at the end, with an extra step. The
    // scroll handler re-arms following on its own the moment the reader returns to the bottom,
    // so this suppresses it exactly until they do.
    //
    // Index is over the whole transcript, counting the same entries the card counted: the
    // conversation panel holds one child per turn plus a note at the top for a fleet view, so
    // the mapping is stated here rather than assumed, and a value that does not land inside the
    // panel is ignored rather than clamped -- a silent jump to the wrong entry is worse than no
    // jump, because it reads as the right one.
    void ScrollToTurn(int index)
    {
        if (index < 0) return;
        try
        {
            Dispatcher.BeginInvoke(new Action(delegate
            {
                try
                {
                    int offset = 0;
                    if (_messages.Children.Count > 0 && _messages.Children[0] is TextBlock) offset = 1;
                    int at = index + offset;
                    if (at < 0 || at >= _messages.Children.Count) return;
                    var target = _messages.Children[at] as FrameworkElement;
                    if (target == null) return;
                    _stickBottom = false;
                    target.BringIntoView();
                    HighlightBriefly(target);
                }
                catch { }
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }
        catch { }
    }

    // A short, self-clearing mark on the entry that was opened. Without it the reader has to
    // work out which of several similar blocks they were sent to.
    void HighlightBriefly(FrameworkElement target)
    {
        var border = target as Border;
        if (border == null)
        {
            // Assistant turns are a StackPanel, not a Border; wrap nothing and mark the panel's
            // own background instead of restructuring the tree under the reader.
            var panel = target as Panel;
            if (panel == null) return;
            var was = panel.Background;
            panel.SetResourceReference(Panel.BackgroundProperty, "PanelAlt");
            var t = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(1600) };
            t.Tick += delegate { t.Stop(); panel.Background = was; };
            t.Start();
            return;
        }
        var prev = border.Background;
        border.SetResourceReference(Border.BackgroundProperty, "PanelAlt");
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(1600) };
        timer.Tick += delegate { timer.Stop(); border.Background = prev; };
        timer.Start();
    }

    void OpenFromFleet(string url, string worker) { OpenFromFleet(url, worker, null); }

    void OpenFromFleet(string url, string worker, string transcriptHint)
    {
        // Normalize a "fleet:<name>" url back into a worker key (no real Copilot URL was captured).
        if (!string.IsNullOrEmpty(url) && url.StartsWith("fleet:"))
        { if (string.IsNullOrEmpty(worker)) worker = url.Substring(6); url = ""; }
        // Robust key: prefer the real conv_url (for /history); else a synthetic worker key that
        // ReadFleetWorker resolves by NAME so the click always lands on the live snapshot.
        string key = !string.IsNullOrEmpty(url) ? url : ("fleet:" + (worker ?? ""));

        // Resolve the live worker dict (by conv_url when we have a real URL, else by name).
        var wkr = ReadFleetWorker(key);
        bool running = FleetRunningFresh();
        string liveTranscriptPath = wkr != null ? SS(wkr, "transcript") : "";
        if (string.IsNullOrEmpty(liveTranscriptPath) && !string.IsNullOrEmpty(worker))
            liveTranscriptPath = NewestTranscriptForWorker(worker);

        // Registry lineage is the conversation identity (old -> new). A cockpit history click's
        // transcriptHint is DISPLAY LOCATION only: it must never rewind the Conversation.Transcript
        // pointer that ChatSend later uses for live-worker/follow-up routing.
        var transcriptLineage = RegistryTranscriptLineageFor(url, worker, liveTranscriptPath);
        string identityTranscriptPath = FleetConvIdentity.LatestTranscript(transcriptLineage, liveTranscriptPath);
        bool exactTranscriptView = !string.IsNullOrEmpty(transcriptHint);
        string displayTranscriptPath = exactTranscriptView ? transcriptHint : identityTranscriptPath;
        if (transcriptLineage.Count == 0 && !string.IsNullOrEmpty(identityTranscriptPath))
            transcriptLineage = FleetConvIdentity.MergeTranscriptLineage(null, null, identityTranscriptPath);

        // THE GOAL TEXT THAT IDENTIFIES THIS CONVERSATION TO THE FLEET (ChatSend.cs's
        // DecideFleetSend addresses a follow-up / a live steer by c.Goal and c.Transcript, not
        // by c.Title). Neither field was ever copied onto the Conversation object below -- the
        // live worker dict and the transcript file both had the answer right here, and it was
        // simply never read. That is what made an interrupt sent while the worker was still
        // running, and a follow-up sent after it finished, both refuse identically: this
        // method built the conversation the chat window actually sends through, and it always
        // carried an empty Goal and an empty Transcript regardless of the worker's real state.
        string liveGoal = wkr != null ? SS(wkr, "goal") : "";
        // Decision itself (which source wins) lives in FleetConvIdentity.ResolveGoal so a test
        // can run it without WPF; see ui/FleetConvIdentity.cs.
        string goalTranscriptPath = !string.IsNullOrEmpty(identityTranscriptPath)
            ? identityTranscriptPath : displayTranscriptPath;
        string bestGoal = FleetConvIdentity.ResolveGoal(liveGoal, TranscriptMetaGoal(goalTranscriptPath));

        // SOURCE PRIORITY:
        //  1. Persisted full-text transcript (jsonl) -- ALWAYS preferred when present. It is the
        //     whole conversation, untruncated, and reading it touches only disk (never the live
        //     companion Edge), so it is safe even mid-run.
        //  2. /switch + /history DOM scrape -- routed through the BRIDGE Edge (:9223), which is
        //     fully separate from the fleet's :9222, so it is safe even mid-run (it no longer
        //     PAGE.goto's the shared companion Edge). Only used when there is no disk transcript.
        //  3. status.json snapshot fragment -- fallback for older workers with no transcript.
        var msgs = exactTranscriptView
            ? ReadTranscriptLineage(FleetConvIdentity.MergeTranscriptLineage(null, null, displayTranscriptPath))
            : ReadTranscriptLineage(transcriptLineage);
        bool fromTranscript = msgs.Count > 0;
        bool historyScraped = false;   // true only when the /history call below actually ran and succeeded
        if (!fromTranscript && !string.IsNullOrEmpty(url))   // scrape via the separate bridge Edge, mid-run safe
        {
            // No /switch -- /history navigates the bridge itself, so the extra call only doubled the
            // wait. Bounded to ~25s so an UNREACHABLE conversation fails FAST to the clear note below
            // instead of hanging for minutes (the old /switch(60s)+/history(60s) + bridge goto retries
            // is what made clicking a past chat "load forever, then error").
            try
            {
                string hist = BridgeCall("/history?url=" + Uri.EscapeDataString(url), 25000);
                var root = _cjs.DeserializeObject(hist) as Dictionary<string, object>;
                if (root != null && root.ContainsKey("messages") && root["messages"] is object[])
                    foreach (object o in (object[])root["messages"])
                    {
                        var md = o as Dictionary<string, object>;
                        if (md == null) continue;
                        string role = md.ContainsKey("role") && md["role"] != null ? md["role"].ToString() : "assistant";
                        string text = md.ContainsKey("text") && md["text"] != null ? md["text"].ToString() : "";
                        msgs.Add(new Msg(role.StartsWith("user") ? "U" : "A", text));
                    }
                historyScraped = true;   // the bridge page now actually shows this conversation
            }
            catch { }
        }
        var loaded = msgs;
        // Keep the live status tail (and arm live re-render) whenever the fleet is running and we
        // have a worker dict -- so the user sees turn/verify/status update under the transcript.
        bool keepLive = running && wkr != null;
        Dispatcher.BeginInvoke(new Action(delegate
        {
            // Guard: do not steal the view while a send is in flight or the composer has unsent
            // text -- a fleet-card open landing here mid-send is exactly the wrong-conversation
            // bug this pinning scheme exists to prevent. No-op the reassignment entirely.
            if (_sendInFlight || (_input != null && _input.Text.Trim().Length > 0)) return;
            // reuse the existing sidebar entry for this conversation if present (dedup by key)
            Conversation c = null;
            foreach (var x in _all) { if (x.ConvUrl == key) { c = x; break; } }
            if (c == null) { c = new Conversation(); c.ConvUrl = key; c.Title = T("fleetview"); _all.Insert(0, c); }
            // A FLEET CONVERSATION BY CONSTRUCTION -- UNLESS THIS ROW IS SOMETHING ELSE. This
            // method is reached only for a fleet worker (cockpit "open") or a registry row with
            // no cached messages yet (OpenConversation's "haven't loaded yet" branch, which also
            // covers a plain Copilot-side orphan row when one shares this exact ConvUrl). DO NOT
            // stamp Source over a row that already carries a real, different one -- only a stub
            // with no source yet (the default "") is claimed here. DecideDoor (ChatSend.cs)
            // routes a send by c.Source == "fleet"; a stub left at "" would fall through to the
            // page-pinning doors instead, which have no key for it either (send_unknown_conv).
            // The merge decisions (claim Source only for an unclaimed row; never blank the
            // Transcript/Goal the row already had) live in FleetConvIdentity.MergeBackfillOnly
            // and .MergeForward -- see ui/FleetConvIdentity.cs -- so a test can run them
            // directly instead of only reading this call site as text.
            c.Source = FleetConvIdentity.MergeBackfillOnly(c.Source, "fleet");
            c.Name = FleetConvIdentity.MergeBackfillOnly(c.Name, worker);
            var priorLineage = FleetConvIdentity.MergeTranscriptLineage(c.Transcripts, null, c.Transcript);
            c.Transcripts = FleetConvIdentity.MergeTranscriptLineage(
                priorLineage, transcriptLineage, identityTranscriptPath);
            c.Transcript = FleetConvIdentity.LatestTranscript(
                c.Transcripts, FleetConvIdentity.MergeForward(c.Transcript, identityTranscriptPath));
            c.Goal = FleetConvIdentity.MergeForward(c.Goal, bestGoal);
            c.Messages.Clear();
            foreach (var m in loaded) c.Messages.Add(m);
            _conv = c;
            if (historyScraped) _pageConv = c;   // bridge page was navigated here by /history; else leave _pageConv as-is
            _messages.Children.Clear();
            var note = new TextBlock { Text = T("fleetview_note"), TextWrapping = TextWrapping.Wrap, FontSize = 12.5, Margin = new Thickness(2, 2, 2, 12) };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            if (wkr != null) { string t = FleetTitle(wkr); if (!string.IsNullOrEmpty(t)) c.Title = t; }
            foreach (var m in loaded) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }

            if (loaded.Count > 0)
            {
                // Full conversation is shown. If running, append a live status block (without the
                // truncated `last`, already in the transcript) and keep it refreshing.
                if (keepLive)
                {
                    AddAssistant(BuildFleetStatusTail(wkr, includeLast: false));
                    _activeFleetUrl = key;
                    try { string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json"); _statusMtime = File.Exists(sp) ? File.GetLastWriteTimeUtc(sp).Ticks : 0; }
                    catch { _statusMtime = 0; }
                }
                else { _activeFleetUrl = null; }   // finished conversation; nothing to keep live
            }
            else if (wkr != null)
            {
                // No transcript and (running or scrape empty): show the LIVE per-worker snapshot
                // fragment from status.json and keep it refreshing.
                RenderFleetSnapshot(wkr);
                _activeFleetUrl = key;
                try { string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json"); _statusMtime = File.Exists(sp) ? File.GetLastWriteTimeUtc(sp).Ticks : 0; }
                catch { _statusMtime = 0; }
            }
            else
            {
                _activeFleetUrl = null;
                StackPanel noTxOuter;
                var noTxContent = AddAssistantContainer(out noTxOuter);
                RenderAssistantBody(noTxContent, noTxOuter, _lang == 0
                    ? "（この会話の本文はまだ取得できません。進行中のため履歴が空の可能性があります。）"
                    : "(This conversation's transcript isn't available yet -- it may be empty while the run is in progress.)");
                // OpenFromFleet is idempotent -- offer a reload instead of leaving this a dead end.
                // url/worker/transcriptHint are this method's own parameters (already normalized
                // above), so no extra fields are needed to replay the same call.
                var reload = Btn(T("reload_transcript"), "PanelAlt", "Muted", true);
                reload.HorizontalAlignment = HorizontalAlignment.Left;
                reload.Margin = new Thickness(0, 8, 0, 0);
                reload.Click += delegate { new Thread((ThreadStart)delegate { OpenFromFleet(url, worker, transcriptHint); }) { IsBackground = true }.Start(); };
                noTxContent.Children.Add(reload);
            }
            // Follow this transcript's tail live from here on -- appends land as they are
            // written, no re-open needed to see them. No-ops (and stops any previous follow)
            // when this worker is no longer live-tracked or its transcript is already archived.
            MaybeFollowConversation(c, identityTranscriptPath);
            RefreshConvList();
            RefreshSteerVisual();   // tint the input border if this is a live steerable worker
            StickToEnd();
        }));
    }

    // A concise title for a fleet worker snapshot: the Copilot-generated conv_title if present,
    // else the issue heading derived from the goal (mirrors the cockpit's CardTitle).
    string FleetTitle(Dictionary<string, object> w)
    {
        string ct = SS(w, "conv_title");
        if (!string.IsNullOrEmpty(ct)) return ct.Length > 90 ? ct.Substring(0, 90) : ct;
        string goal = SS(w, "goal");
        if (string.IsNullOrEmpty(goal)) return "";
        string[] lines = goal.Replace("\r", "").Split('\n');
        for (int i = 0; i < lines.Length; i++)
        {
            string ln = lines[i].Trim();
            if (ln.StartsWith("==") && ln.IndexOf("issue", StringComparison.OrdinalIgnoreCase) >= 0)
                for (int j = i + 1; j < lines.Length; j++)
                {
                    string nx = lines[j].Trim();
                    if (nx.Length > 0) return nx.Length > 90 ? nx.Substring(0, 90) : nx;
                }
        }
        foreach (string l in lines)
        {
            string s = l.Trim();
            if (s.Length > 0 && !s.StartsWith("==")) return s.Length > 90 ? s.Substring(0, 90) : s;
        }
        return "";
    }

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
    HashSet<string> _sectionExpanded = new HashSet<string>();
    // How many rows a sidebar section shows before "+N more". Chosen in the cockpit settings popup
    // (settings.txt sidebar_section_cap, default 8; 0 = no cap) and re-read when that file changes.
    int _sectionCap = 8;
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
                        : (!string.IsNullOrEmpty(c.Name) ? NewestTranscriptForWorker(c.Name) : "");
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
    static readonly TimeSpan UndoWindow = TimeSpan.FromSeconds(8);
    Conversation _pendingUndo;                 // removed from the list, file not yet deleted
    int _pendingUndoIndex = -1;
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

    static readonly FontFamily BODY_FACE = new FontFamily(Theme.UiFont);
    static readonly Thickness BODY_PAD = new Thickness(0);
    Border _routerBar; bool _routerShown; string _routerText = "";
    string _lastNotSavedReason = "";
    DateTime _bridgeProblemShown = DateTime.MinValue;
}
