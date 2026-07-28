// CopilotChat.cs -- native Windows (WPF) chat front-end for an M365 Copilot agent.
// NO Node, NO JS, NO browser engine: pure .NET (built into Windows). Talks to the
// Python bridge's SSE endpoint over HTTP and renders the streamed answer natively.
//
//   [ this WPF app ] --HTTP/SSE--> [ Python bridge ] --CDP--> [ Copilot ]
//
// Palette = ShuttleScope Design Language v1.1 (grayscale-first N_GRAY slate; the
// single E_EMPHASIS orange #ea580c for the primary action). The waiting indicator
// matches ShuttleScope's chat loader (three slate dots, animate-bounce, staggered);
// the rest leans toward a clean, refined Claude-like layout.
// Build with the Windows-only csc.exe (legacy C# 5): ui\build_and_run.bat
using System;
using System.Collections.Generic;
using System.IO;
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

class Program { [STAThread] static void Main() { new Application().Run(new ChatWindow()); } }

class Msg { public string Role; public string Text; public Msg(string r, string t) { Role = r; Text = t; } }

class Conversation
{
    public string Id = Guid.NewGuid().ToString("N").Substring(0, 12);
    public string Title = "";        // empty = untitled (shows the localized default)
    public string ConvUrl = "";
    public string Source = "";
    public double Ts = 0;
    public string Transcript = "";   // disk jsonl path (fleet convs) -> open from disk, no scrape
    public string Name = "";         // worker name (fallback to resolve the transcript by name)
    public List<Msg> Messages = new List<Msg>();
    public bool Untitled() { return string.IsNullOrEmpty(Title); }
}

class ChatWindow : Window
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
    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "copilot-bridge", "settings.txt");

    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "newchat") return ja ? "新しいチャット" : "New chat";
        if (k == "newchat_btn") return ja ? "＋   新しいチャット" : "＋   New chat";
        if (k == "send") return ja ? "送信" : "Send";
        // (theme/lang: iconized in Wave 2 — kept without emoji as tooltips live under tip_theme/tip_lang)
        if (k == "theme") return ja ? "テーマ (ダーク/ライト)" : "Theme (dark/light)";
        if (k == "lang") return ja ? "English へ" : "日本語へ";
        if (k == "rename") return ja ? "名前を変更" : "Rename";
        if (k == "delete") return ja ? "削除" : "Delete";
        if (k == "generating") return ja ? "生成中" : "Generating";
        if (k == "cancel") return ja ? "キャンセル" : "Cancel";
        if (k == "copy") return ja ? "コピー" : "Copy";
        if (k == "stop") return ja ? "停止" : "Stop";
        if (k == "ui_auto") return ja ? "自動" : "Auto";
        if (k == "fleet_queued") return ja ? "並列実行が満杯のため待機列に追加しました（空き枠で実行）。先頭に ! を付けると強制優先。" : "Fleet is full — queued (runs when a slot frees). Prefix ! to force priority.";
        if (k == "fleet_forced") return ja ? "強制優先で待機列の先頭に追加しました。" : "Forced to the front of the queue.";
        if (k == "router_q") return ja ? "これは調査向きの依頼です。researcher で深掘りしますか？" : "This looks like research. Run it on the researcher?";
        if (k == "router_research") return ja ? "researcher で深掘り" : "Deep research";
        if (k == "router_normal") return ja ? "そのまま送信" : "Send as-is";
        if (k == "attach") return ja ? "ファイル/画像を添付（画像は Ctrl+V で貼り付けも可）" : "Attach a file/image (or paste an image with Ctrl+V)";
        if (k == "attach_btn") return ja ? "＋ 添付" : "+ Attach";
        if (k == "attach_fail") return ja ? "添付に失敗:" : "Attach failed:";
        if (k == "open_cockpit") return ja ? "並列実行を開く" : "Open parallel execution";
        if (k == "loadingconv") return ja ? "会話を読み込み中…" : "Loading conversation…";
        if (k == "fleetview") return ja ? "並列タスクの会話" : "Parallel task";
        if (k == "fleetview_note") return ja ? "▼ 並列タスクの会話を表示中。ここに入力すると、この会話に割り込めます。" : "▼ Viewing a parallel-task conversation. Type here to steer it.";
        if (k == "del_head") return ja ? "を削除 — 方法を選んでください" : " — choose how to delete";
        if (k == "m1t") return ja ? "このアプリからのみ削除" : "Delete from this app only";
        if (k == "m1s") return ja ? "Copilot 側の会話は残す（最も安全）" : "Keeps the Copilot conversation (safest)";
        if (k == "m2t") return ja ? "Copilot で開いて手動削除" : "Open in Copilot to delete manually";
        if (k == "m2s") return ja ? "その会話を Copilot で開く。1クリックで削除" : "Opens it in Copilot; delete it there in one click";
        if (k == "m3t") return ja ? "Copilot 会話も自動削除" : "Also auto-delete the Copilot conversation";
        if (k == "m3s") return ja ? "Copilot 側の会話も実際に消えます（失敗時は開いて手動）" : "Actually removes it on the Copilot side too (opens it for manual delete if that fails)";
        if (k == "del_note") return (ja ? "選んだ方法が次回の既定になります（現在: モード " : "Your choice becomes the default (current: mode ") + _deleteMode + "）";
        if (k == "t_local") return ja ? "ローカルから削除しました（Copilot 側は残しています）。" : "Deleted locally (kept on the Copilot side).";
        if (k == "t_open") return ja ? "Copilot で開きました。Copilot 上で会話を削除してください。" : "Opened in Copilot. Delete the conversation there.";
        if (k == "t_nourl") return ja ? "Copilot 会話 URL 不明のため、ローカルのみ削除しました。" : "No Copilot URL; deleted locally only.";
        if (k == "t_auto_ok") return ja ? "Copilot 会話も自動削除しました。" : "Auto-deleted the Copilot conversation too.";
        if (k == "t_auto_fail") return ja ? "自動削除はできませんでした。Copilot で開きます（手動で削除してください）。" : "Auto-delete failed. Opening in Copilot (delete it manually).";
        if (k == "manage_btn") return ja ? "会話を整理" : "Manage";
        if (k == "manage_title") return ja ? "会話の一括削除" : "Bulk delete conversations";
        if (k == "show_all") return ja ? "すべて表示（自分の会話も含む）" : "Show all (incl. your own)";
        if (k == "period") return ja ? "期間" : "Period";
        if (k == "period_all") return ja ? "全期間" : "All";
        if (k == "period_24h") return ja ? "過去24時間" : "Last 24h";
        if (k == "period_7d") return ja ? "過去7日" : "Last 7d";
        if (k == "period_30d") return ja ? "過去30日" : "Last 30d";
        if (k == "running_note") return ja ? "走行中：Copilot側の削除は停止します（ローカル一覧からのみ削除）。" : "A run is live: Copilot-side delete is disabled (local list only).";
        if (k == "select_all") return ja ? "全選択" : "Select all";
        if (k == "clear_all") return ja ? "全解除" : "Clear";
        if (k == "fetch_copilot") return ja ? "Copilot側から一覧取得" : "Fetch from Copilot";
        if (k == "del_selected") return ja ? "選択した会話を削除" : "Delete selected";
        if (k == "close") return ja ? "閉じる" : "Close";
        if (k == "untitled") return ja ? "(無題)" : "(untitled)";
        // ── empty state (fresh window / after New chat) ──────────────────────────
        if (k == "empty_title") return ja ? "何でも頼んでください — ローカルPCも操作できます" : "Ask me anything — I can operate this PC too";
        if (k == "empty_slash") return ja ? "「/」でコマンド一覧" : "Type \"/\" for the command list";
        if (k == "empty_s1") return ja ? "デスクトップのファイルを整理する計画を立てて" : "Plan how to organize the files on my Desktop";
        if (k == "empty_s2") return ja ? "このPCの空き容量を調べて大きいフォルダを一覧して" : "Check this PC's free space and list the largest folders";
        if (k == "empty_s3") return ja ? "Excelファイルを読んで要約して" : "Read an Excel file and summarize it";
        // ── status dot state labels (tooltip) ────────────────────────────────────
        if (k == "dot_idle")     return ja ? "接続済み・待機中" : "Connected · idle";
        if (k == "dot_busy")     return ja ? "生成中"           : "Generating";
        if (k == "dot_offline")  return ja ? "ブリッジ未接続"   : "Bridge unreachable";
        if (k == "dot_signin")   return ja ? "サインイン切れの可能性" : "Possible sign-in / refusal";
        if (k == "signin_banner") return ja ? "Copilotが応答を拒否しています。サインイン切れ/接続切れの可能性 — Fleet Cockpitの健康表示を確認してください" : "Copilot is refusing to respond. Sign-in or connection may have expired — check the health view in Fleet Cockpit.";
        if (k == "signin_open")   return ja ? "Fleet Cockpit を開く" : "Open Fleet Cockpit";
        // ── send-target pinning / reachability fallback errors (nothing was sent) ────
        if (k == "send_wrong_page")  return ja ? "送信先の会話に接続できませんでした — 送信は行われていません" : "Could not connect to the target conversation — nothing was sent.";
        if (k == "send_unknown_conv") return ja ? "この会話の送信先を特定できません。会話を開き直してください。" : "Can't identify where to send this — please reopen the conversation.";
        if (k == "send_offline") return ja ? "ブリッジに接続できません。送信していません。" : "Can't reach the bridge. Nothing was sent.";
        if (k == "retry_start_stack") return ja ? "スタックを起動して再試行" : "Start the stack and retry";
        if (k == "reload_transcript") return ja ? "🔄 再読み込み" : "🔄 Reload";
        // ── sidebar section / action labels ──────────────────────────────────────
        if (k == "sec_pinned")   return ja ? "ピン留め"   : "Pinned";
        if (k == "sec_today")    return ja ? "最近"        : "Recent";
        if (k == "sec_fleet")    return ja ? "フリート"    : "Fleet runs";
        if (k == "sec_archived") return ja ? "アーカイブ"  : "Archived";
        if (k == "pin")          return ja ? "ピン留め"    : "Pin";
        if (k == "unpin")        return ja ? "ピン解除"    : "Unpin";
        if (k == "archive")      return ja ? "アーカイブ"  : "Archive";
        if (k == "unarchive")    return ja ? "アーカイブ解除" : "Unarchive";
        // ── Wave 2: header dot tooltip, iconized footer tooltips, list scannability ──
        if (k == "dot_click_tip") return ja ? "状態の詳細は Fleet Cockpit へ" : "Open Fleet Cockpit for connection details";
        if (k == "tip_cockpit")   return ja ? "並列実行を開く" : "Open parallel execution";
        if (k == "tip_lang")      return ja ? "English へ切り替え" : "Switch to Japanese";
        if (k == "tip_theme")     return _dark ? (ja ? "ライトテーマへ" : "Switch to light theme") : (ja ? "ダークテーマへ" : "Switch to dark theme");
        if (k == "rename_link")   return ja ? "名前変更" : "Rename";
        if (k == "show_more")     return ja ? ("+" + "{0}" + " 件を表示") : ("+{0} more");
        return k;
    }

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
        Width = 1120; Height = 780;
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

        var headerStack = new StackPanel { Margin = new Thickness(12, 14, 12, 8) };
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
        var bottom = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(8, 8, 8, 10) };
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
            Content = "☰",   // ☰ trigram for heaven (hamburger glyph)
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
        _statusDot = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(5), VerticalAlignment = VerticalAlignment.Center };
        SetRef(_statusDot, BackgroundProperty, "Faint");
        var dotHit = new Border
        {
            Child = _statusDot, Background = Brushes.Transparent,
            Padding = new Thickness(0, 3, 9, 3), Margin = new Thickness(0, 1, 0, 0),
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
        var settingsBtn = Btn("⚙", "PanelAlt", "Muted", true);
        settingsBtn.Padding = new Thickness(10, 3, 10, 3); settingsBtn.FontSize = 13;
        settingsBtn.ToolTip = _lang == 0 ? "Fleet / コックピットを開く" : "Open Fleet / Cockpit";
        settingsBtn.Click += delegate { OpenCockpit(); };
        headRight.Children.Add(settingsBtn);
        // Fleet active chip ("Fleet: N") -- collapsed when no active workers or status.json absent
        _fleetChipLabel = new TextBlock { FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
        SetRef(_fleetChipLabel, TextBlock.ForegroundProperty, "Muted");
        _fleetChip = new Border
        {
            Child = _fleetChipLabel, BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6), Padding = new Thickness(10, 3, 10, 3),
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
        Grid.SetRow(_scroll, 1); main.Children.Add(_scroll);
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
        _send = Btn(T("send"), "Accent", "AccentFg", false);
        _send.Height = 32; _send.Padding = new Thickness(14, 0, 14, 0);
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
            CornerRadius = new CornerRadius(12),
            // Constant 1px at rest (Border token) — gives a stable boundary (fixes the weak
            // light-theme edge) and, crucially, a constant footprint. The 1px rest/1px focus/
            // 2px steer progression only ever changes thickness by 1 (steer), which SetComposerRing
            // compensates with a matching -1 padding so ActualHeight never moves.
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 6, 12, 6),
            Margin = new Thickness(0, 10, 0, 16),
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
            Margin = new Thickness(24, 12, 24, 0), CornerRadius = new CornerRadius(12),
            BorderThickness = new Thickness(1), Padding = new Thickness(16, 13, 16, 15),
            MaxWidth = 560, HorizontalAlignment = HorizontalAlignment.Center
        };
        SetRef(_banner, BackgroundProperty, "Panel"); SetRef(_banner, Border.BorderBrushProperty, "Accent");
        _bannerBody = new StackPanel(); _banner.Child = _bannerBody;
        Panel.SetZIndex(_banner, 100);
        Grid.SetRow(_banner, 1); main.Children.Add(_banner);

        Grid.SetColumn(main, 1); root.Children.Add(main);
        Content = root;

        // Set _convsPath BEFORE LoadConversations so DiscoverTranscripts and LoadSidebarState work.
        string fleetDir = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet"));
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
    }

    string _openPath; long _openMtime;
    long _settingsMtime;                 // follow external lang/theme edits (e.g. the cockpit toggled it)
    string _convsPath; long _convsMtime;

    // Keep the chat in language/theme sync with the cockpit: both share settings.txt, so when the
    // other window toggles, re-read and re-apply here too (lang -> re-label all chrome + conv list).
    void CheckSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            long m = File.GetLastWriteTimeUtc(SettingsFile).Ticks;
            if (m == _settingsMtime) return;
            _settingsMtime = m;
            int l0 = _lang;
            double s0 = _uiScale;
            bool a0 = _uiAuto; double t0 = _scaleTarget;
            LoadSettings();                              // re-reads lang/dark/ui_scale/target (+ApplyTheme)
            if (_lang != l0) { UpdateChrome(); RefreshConvList(); RerenderActiveConversation(); }
            // Cockpit changed the shared zoom -> mirror it silently. In AUTO recompute for THIS monitor
            // (the per-monitor effective scale, not the other window's); in MANUAL push the shared number.
            if (_uiAuto)
            {
                if (!a0 || System.Math.Abs(t0 - _scaleTarget) > 0.001
                        || System.Math.Abs(s0 - EffectiveAutoScale(CurrentMonitorScale())) > 0.001)
                    ApplyAutoScale(false);
            }
            else if (a0 || _uiScale != s0) ApplyScale(false);
        }
        catch { }
    }

    // Re-render the open LOCAL conversation so per-message chrome (the copy-button tooltip) re-localizes
    // on a language toggle. Skip fleet/steer views: re-rendering those could re-fetch or drop steer mode.
    void RerenderActiveConversation()
    {
        if (_conv != null && _activeFleetUrl == null && _conv.Messages.Count > 0)
            OpenConversation(_conv);
    }
    string _activeFleetUrl;              // conv URL of the fleet snapshot currently shown (null = none)
    long _statusMtime;                   // last-seen mtime of status.json (for live re-render)

    static string SS(Dictionary<string, object> d, string k)
    { return (d.ContainsKey(k) && d[k] != null) ? d[k].ToString() : ""; }

    // status.json lives next to conversations.json (.fleet/). Read the worker dict whose
    // "conv_url" matches 'url' (the cockpit cards render exactly this live per-worker state).
    // Returns null when status.json is missing/unreadable or no worker matches.
    Dictionary<string, object> ReadFleetWorker(string key)
    {
        if (string.IsNullOrEmpty(key)) return null;
        // key is either a real Copilot conv_url, or a synthetic "fleet:<worker-name>" used when the
        // conv_url was never captured -- in that case resolve the worker by NAME so click-to-open
        // always finds its live status.json snapshot regardless of URL scraping.
        string wantName = key.StartsWith("fleet:") ? key.Substring(6) : null;
        try
        {
            string statusPath = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(statusPath)) return null;
            string txt;
            using (var fsr = new FileStream(statusPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null || !d.ContainsKey("workers") || !(d["workers"] is object[])) return null;
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                if (wantName != null) { if (SS(w, "name") == wantName) return w; }
                else if (SS(w, "conv_url") == key) return w;
            }
        }
        catch { }
        return null;
    }

    // Locate a worker's on-disk transcript by NAME, independent of the live status.json worker
    // entry. Transcripts are named "<runid>_a<agent>_w<N>.jsonl" and OUTLIVE the live worker dict,
    // so this lets click-to-open load the full conversation for a finished/restarted/history worker
    // (when ReadFleetWorker returns null) instead of falling back to the "not available" placeholder.
    // Returns the NEWEST matching file (the latest run for that worker), or "" if none.
    string NewestTranscriptForWorker(string worker)
    {
        try
        {
            if (string.IsNullOrEmpty(worker)) return "";
            string tdir = Path.Combine(Path.GetDirectoryName(_convsPath), "transcripts");
            if (!Directory.Exists(tdir)) return "";
            string suffix = "_" + worker + ".jsonl";   // exact suffix: "w1" must not match "w10"
            string newest = null; DateTime best = DateTime.MinValue;
            foreach (string f in Directory.GetFiles(tdir, "*" + suffix))
            {
                if (!Path.GetFileName(f).EndsWith(suffix, StringComparison.Ordinal)) continue;
                DateTime t = File.GetLastWriteTimeUtc(f);
                if (t > best) { best = t; newest = f; }
            }
            return newest ?? "";
        }
        catch { return ""; }
    }

    // True while a fleet run is actively driving the companion Edge: status.json says
    // running AND it was updated recently (the runner rewrites it ~1/s). When this holds we
    // Scroll to the latest ONLY while the user is following along (at the bottom). If they have
    // scrolled up to read back, _stickBottom is false and we leave the viewport where they put it
    // -- the ScrollChanged handler re-arms following when they return to the bottom.
    void StickToEnd()
    {
        if (_stickBottom && _scroll != null) _scroll.ScrollToEnd();
    }

    // must NOT call /switch+/history -- doing so would PAGE.goto the shared companion Edge
    // onto this conversation and clobber the live send. Stale/!running -> safe to scrape.
    bool FleetRunningFresh()
    {
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return false;
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return false;
            bool running = d.ContainsKey("running") && d["running"] != null && Convert.ToBoolean(d["running"]);
            if (!running) return false;
            double updated = (d.ContainsKey("updated") && d["updated"] != null) ? Convert.ToDouble(d["updated"]) : 0;
            double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
            return (now - updated) <= 20.0;   // fresh within 20s -> the runner is live
        }
        catch { return false; }
    }

    // Read a worker's persisted full-text transcript (.fleet/transcripts/<key>.jsonl, one
    // JSON object per line) into ordered messages. Untruncated. Returns an empty list if the
    // path is empty/missing/unreadable (caller falls back to /history or the snapshot).
    // Fully isolated: a parse hiccup on one line is skipped, never thrown.
    List<Msg> ReadTranscript(string path)
    {
        var msgs = new List<Msg>();
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return msgs;
        try
        {
            string[] lines;
            using (var fsr = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (string ln in lines)
            {
                if (string.IsNullOrEmpty(ln)) continue;
                Dictionary<string, object> o;
                try { o = _cjs.DeserializeObject(ln) as Dictionary<string, object>; }
                catch { continue; }
                if (o == null) continue;
                if (!o.ContainsKey("role")) continue;   // skip meta / guid marker lines
                string role = o["role"] != null ? o["role"].ToString() : "assistant";
                string text = (o.ContainsKey("text") && o["text"] != null) ? o["text"].ToString() : "";
                msgs.Add(new Msg(role.StartsWith("user") ? "U" : "A", text));
            }
        }
        catch { }
        return msgs;
    }

    // Append any captured sub-agent (research) conversations for this worker. Each deep-dive is
    // persisted to "<key>__sub_research_t<turn>_<n>.jsonl" next to the parent transcript, so we
    // glob the siblings and show them under a header -- this is what makes "research が見えない"
    // visible (the side page used to close without a trace). Best-effort.
    void AppendSubAgentTranscripts(List<Msg> msgs, string transcriptPath)
    {
        try
        {
            if (string.IsNullOrEmpty(transcriptPath)) return;
            string dir = Path.GetDirectoryName(transcriptPath);
            string stem = Path.GetFileNameWithoutExtension(transcriptPath);
            if (string.IsNullOrEmpty(dir) || string.IsNullOrEmpty(stem) || !Directory.Exists(dir)) return;
            var subs = Directory.GetFiles(dir, stem + "__sub_*.jsonl");
            Array.Sort(subs);
            foreach (string sf in subs)
            {
                var sm = ReadTranscript(sf);
                if (sm.Count == 0) continue;
                string kind = sf.Contains("__sub_research_") ? "🔎 research" : "🧪 sub-agent";
                msgs.Add(new Msg("A", "──────── " + kind + (_lang == 0 ? "（サブエージェント）" : " (sub-agent)") + " ────────"));
                msgs.AddRange(sm);
            }
        }
        catch { }
    }

    // Build a readable live snapshot from a status.json worker dict: goal/status/outcome,
    // turn N/max, plan steps, latest response, verification state, and reason. Mirrors the
    // cockpit card so the main chat shows progress when /history can't be scraped.
    void RenderFleetSnapshot(Dictionary<string, object> w)
    {
        AddUser(SS(w, "goal"));
        AddAssistant(BuildFleetStatusTail(w, includeLast: true));
    }

    // The status/turn/plan/verify/reason block for a worker, as a string. When includeLast
    // is false the "latest response" (the truncated `last`) is omitted -- used when the full
    // transcript is already shown above, so we don't duplicate the last turn in truncated form.
    string BuildFleetStatusTail(Dictionary<string, object> w, bool includeLast)
    {
        bool ja = _lang == 0;
        var sb = new StringBuilder();
        string status = SS(w, "status");
        string outcome = SS(w, "outcome");
        string head = string.IsNullOrEmpty(outcome) ? status : (status + " / " + outcome);
        sb.Append(ja ? "状態: " : "Status: ").Append(string.IsNullOrEmpty(head) ? (ja ? "(不明)" : "(unknown)") : head);
        string turn = SS(w, "turn"); string maxt = SS(w, "max_turns");
        if (!string.IsNullOrEmpty(turn))
        {
            sb.Append(ja ? "　ターン " : "   Turn ").Append(turn);
            if (!string.IsNullOrEmpty(maxt)) sb.Append("/").Append(maxt);
        }
        sb.Append('\n');

        // plan steps
        if (w.ContainsKey("plan") && w["plan"] is object[])
        {
            var steps = (object[])w["plan"];
            if (steps.Length > 0)
            {
                sb.Append('\n').Append(ja ? "計画:" : "Plan:").Append('\n');
                int n = 1;
                foreach (object s in steps)
                {
                    if (s == null) continue;
                    sb.Append("  ").Append(n).Append(". ").Append(s.ToString()).Append('\n');
                    n++;
                }
            }
        }

        // latest scraped response (skipped when the full transcript is already rendered above)
        string last = SS(w, "last");
        if (includeLast && !string.IsNullOrEmpty(last))
            sb.Append('\n').Append(ja ? "最新の応答:" : "Latest response:").Append('\n').Append(last).Append('\n');

        // verification state
        string verified = SS(w, "verified");
        string vattempts = SS(w, "verify_attempts");
        if (!string.IsNullOrEmpty(verified) || !string.IsNullOrEmpty(vattempts))
        {
            string vstr;
            if (verified == "True") vstr = ja ? "検証OK" : "verified";
            else if (verified == "False") vstr = ja ? "未検証" : "not verified";
            else vstr = ja ? "検証中/未判定" : "pending";
            sb.Append('\n').Append(ja ? "検証: " : "Verification: ").Append(vstr);
            if (!string.IsNullOrEmpty(vattempts)) sb.Append(ja ? "（試行 " : " (attempts ").Append(vattempts).Append(ja ? "）" : ")");
            sb.Append('\n');
        }

        // reason
        string reason = SS(w, "reason");
        if (!string.IsNullOrEmpty(reason))
            sb.Append('\n').Append(ja ? "理由: " : "Reason: ").Append(reason).Append('\n');

        return sb.ToString().TrimEnd('\n');
    }

    List<object> ReadConvsRegistry()
    {
        try
        {
            if (File.Exists(_convsPath))
            {
                var a = _cjs.DeserializeObject(File.ReadAllText(_convsPath, Encoding.UTF8)) as object[];
                if (a != null) return new List<object>(a);
            }
        }
        catch { }
        return new List<object>();
    }
    // Add this conversation to the shared registry so the cockpit/other side lists it.
    void RegisterConv(string url, string title, string source)
    {
        if (string.IsNullOrEmpty(url)) return;
        try
        {
            var list = ReadConvsRegistry();
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
            foreach (var o in ReadConvsRegistry())
            {
                var d = o as Dictionary<string, object>;
                if (d == null) continue;
                string url = SS(d, "url");
                if (string.IsNullOrEmpty(url)) continue;
                bool exists = false;
                foreach (var c in _all) if (c.ConvUrl == url) { exists = true; break; }
                if (!exists)
                {
                    var c = new Conversation();
                    c.ConvUrl = url;
                    c.Title = SS(d, "title");
                    c.Source = SS(d, "source");
                    c.Transcript = SS(d, "transcript");   // disk jsonl -> open from disk, no scrape
                    c.Name = SS(d, "name");
                    try { c.Ts = (d.ContainsKey("ts") && d["ts"] != null) ? Convert.ToDouble(d["ts"]) : 0; }
                    catch { c.Ts = 0; }
                    _all.Insert(0, c);   // newest on top (registry/fleet convs were appended below)
                    added = true;
                }
            }
            if (added) RefreshConvList();
        }
        catch { }
    }
    readonly JavaScriptSerializer _cjs = new JavaScriptSerializer();

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
            if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(worker) && string.IsNullOrEmpty(transcript)) return;
            // Bring this chat window to the front so a cockpit "▶ 開く" click lands you here without
            // an alt-tab -- the two-window round-trip was the biggest gap vs Claude Code's one pane.
            try
            {
                if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
                Activate(); Topmost = true; Topmost = false; Focus();
            }
            catch { }
            new Thread((ThreadStart)delegate { OpenFromFleet(url, worker, transcript); }) { IsBackground = true }.Start();
        }
        catch { }
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
        string transcriptPath = wkr != null ? SS(wkr, "transcript") : "";
        // A history click carries the EXACT transcript path -- prefer it (correct even when several
        // runs share a worker name, which the name-newest fallback below could otherwise confuse).
        if (string.IsNullOrEmpty(transcriptPath) && !string.IsNullOrEmpty(transcriptHint))
            transcriptPath = transcriptHint;
        // FALLBACK by worker name: if no live worker dict resolved (finished/restarted run, a
        // history click, or a transient status.json read) the transcript field is unavailable even
        // though the .jsonl is on disk -- so locate it by name. This is what was dropping users to
        // the "transcript not available" placeholder so often; the full conversation was right there.
        if (string.IsNullOrEmpty(transcriptPath) && !string.IsNullOrEmpty(worker))
            transcriptPath = NewestTranscriptForWorker(worker);

        // SOURCE PRIORITY:
        //  1. Persisted full-text transcript (jsonl) -- ALWAYS preferred when present. It is the
        //     whole conversation, untruncated, and reading it touches only disk (never the live
        //     companion Edge), so it is safe even mid-run.
        //  2. /switch + /history DOM scrape -- routed through the BRIDGE Edge (:9223), which is
        //     fully separate from the fleet's :9222, so it is safe even mid-run (it no longer
        //     PAGE.goto's the shared companion Edge). Only used when there is no disk transcript.
        //  3. status.json snapshot fragment -- fallback for older workers with no transcript.
        var msgs = ReadTranscript(transcriptPath);
        AppendSubAgentTranscripts(msgs, transcriptPath);   // show captured research sub-conversations
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
                string hist = HttpGet("/history?url=" + Uri.EscapeDataString(url), 25000);
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
            c.Messages.Clear();
            foreach (var m in loaded) c.Messages.Add(m);
            _conv = c;
            if (historyScraped) _pageConv = c;   // bridge page was navigated here by /history; else leave _pageConv as-is
            _messages.Children.Clear();
            var note = new TextBlock { Text = T("fleetview_note"), TextWrapping = TextWrapping.Wrap, FontSize = 12.5, Margin = new Thickness(2, 2, 2, 10) };
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

    // Re-render the live fleet snapshot in place (called from the poll timer when
    // status.json changes and a fleet snapshot is the active view). Cheap: only touches
    // the message panel when the matching worker is still present.
    void RefreshFleetSnapshot()
    {
        if (string.IsNullOrEmpty(_activeFleetUrl)) return;
        if (_conv == null || _conv.ConvUrl != _activeFleetUrl) return;   // user navigated away
        var w = ReadFleetWorker(_activeFleetUrl);
        if (w == null) return;
        _messages.Children.Clear();
        var note = new TextBlock { Text = T("fleetview_note"), TextWrapping = TextWrapping.Wrap, FontSize = 12.5, Margin = new Thickness(2, 2, 2, 10) };
        SetRef(note, TextBlock.ForegroundProperty, "Muted");
        _messages.Children.Add(note);
        // If this worker has a persisted transcript, re-render the WHOLE conversation from disk
        // (untruncated) and append the live status tail -- otherwise fall back to the snapshot
        // fragment. Reading the jsonl touches only disk, never the live companion Edge.
        var tx = ReadTranscript(SS(w, "transcript"));
        if (tx.Count > 0)
        {
            foreach (var m in tx) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
            AddAssistant(BuildFleetStatusTail(w, includeLast: false));
        }
        else
        {
            RenderFleetSnapshot(w);
        }
        StickToEnd();
    }

    // If a fleet snapshot is the active view, re-render it when status.json's mtime changes
    // so the user sees progress update live in the main chat (mirrors the cockpit refresh).
    void CheckFleetSnapshot()
    {
        try
        {
            if (string.IsNullOrEmpty(_activeFleetUrl)) return;
            if (_conv == null || _conv.ConvUrl != _activeFleetUrl) { _activeFleetUrl = null; RefreshSteerVisual(); return; }
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return;
            long m = File.GetLastWriteTimeUtc(sp).Ticks;
            if (m == _statusMtime) return;
            _statusMtime = m;
            RefreshFleetSnapshot();
        }
        catch { }
    }

    // Read status.json and count ACTIVE fleet workers (status not terminal / not "pending").
    // Terminal statuses: "done", "resolved", "failed", "error", "cancelled", "stopped".
    // "pending" means queued but not yet started. Anything else (e.g. "running", "verifying",
    // "planning") is considered actively working. Returns 0 when status.json is absent.
    int ReadActiveFleetWorkerCount()
    {
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return 0;
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null || !d.ContainsKey("workers") || !(d["workers"] is object[])) return 0;
            int count = 0;
            foreach (object o in (object[])d["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                string st = SS(w, "status").ToLowerInvariant();
                if (st == "pending" || st == "done" || st == "resolved" || st == "failed"
                    || st == "error" || st == "cancelled" || st == "stopped") continue;
                count++;
            }
            return count;
        }
        catch { return 0; }
    }

    // Update the Fleet chip in the main header. Called on the 800ms poll timer.
    // Shows "Fleet: N" (collapsed when N == 0). Clicking opens the cockpit.
    void RefreshFleetChip()
    {
        if (_fleetChip == null || _fleetChipLabel == null) return;
        try
        {
            int n = ReadActiveFleetWorkerCount();
            if (n > 0)
            {
                _fleetChipLabel.Text = "Fleet: " + n;
                _fleetChip.Visibility = Visibility.Visible;
            }
            else
            {
                _fleetChip.Visibility = Visibility.Collapsed;
            }
        }
        catch { }
    }

    // ── Fleet strip (compact one-liner above the composer) ────────────────────
    // A single ~28px-tall band showing fleet state inline. Collapses when no
    // workers exist or when the run completed more than 30 min ago.
    UIElement BuildFleetStrip()
    {
        _fleetStripBody = new StackPanel { Margin = new Thickness(0, 0, 0, 0) };

        _fleetStrip = new Border
        {
            Child = _fleetStripBody,
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 4, 10, 4),
            Margin = new Thickness(0, 0, 0, 6),
            Visibility = Visibility.Collapsed
        };
        SetRef(_fleetStrip, BackgroundProperty, "PanelAlt");
        SetRef(_fleetStrip, Border.BorderBrushProperty, "Border");
        return _fleetStrip;
    }

    // Compute a lightweight signature from status.json so we only rebuild the strip's
    // DOM when something actually changed (avoids per-tick flicker / GC pressure).
    // Signature: "<running>|<updated>|<run_label>|<goal_count>|<w0key:w0status>|..."
    string FleetStripSignature(Dictionary<string, object> statusDoc)
    {
        if (statusDoc == null) return "";
        var sb = new StringBuilder();
        sb.Append(SS(statusDoc, "running")).Append('|');
        sb.Append(SS(statusDoc, "updated")).Append('|');
        sb.Append(SS(statusDoc, "run_label")).Append('|');
        sb.Append(SS(statusDoc, "goal_count")).Append('|');
        if (statusDoc.ContainsKey("workers") && statusDoc["workers"] is object[])
        {
            foreach (object o in (object[])statusDoc["workers"])
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                sb.Append(SS(w, "name")).Append(':').Append(SS(w, "status")).Append('|');
            }
        }
        return sb.ToString();
    }

    // Refresh the fleet strip from status.json. Called on the 800ms timer tick.
    // Rebuilds the inner DOM only when the signature (running/updated/worker statuses) changes.
    void RefreshFleetStrip()
    {
        if (_fleetStrip == null || _fleetStripBody == null) return;
        try
        {
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp))
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed)
                {
                    _fleetStrip.Visibility = Visibility.Collapsed;
                    _fleetStripSig = null;
                }
                return;
            }
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var doc = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (doc == null)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed) { _fleetStrip.Visibility = Visibility.Collapsed; _fleetStripSig = null; }
                return;
            }

            // Collect workers array
            object[] workers = null;
            if (doc.ContainsKey("workers") && doc["workers"] is object[])
                workers = (object[])doc["workers"];

            // No workers -> collapse and return
            if (workers == null || workers.Length == 0)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed) { _fleetStrip.Visibility = Visibility.Collapsed; _fleetStripSig = null; }
                return;
            }

            // ── Compute summary values ──────────────────────────────────────────
            bool running = doc.ContainsKey("running") && doc["running"] != null && Convert.ToBoolean(doc["running"]);
            double updated = (doc.ContainsKey("updated") && doc["updated"] != null) ? Convert.ToDouble(doc["updated"]) : 0;
            double nowEpoch = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            double ageSec = (updated > 0) ? (nowEpoch - updated) : -1;

            // Auto-hide stale completed runs — check BEFORE signature gate so it
            // fires every tick even when status.json has not changed.
            if (!running && ageSec > 1800)
            {
                if (_fleetStrip.Visibility != Visibility.Collapsed)
                {
                    _fleetStrip.Visibility = Visibility.Collapsed;
                    _fleetStripSig = null;
                }
                return;
            }

            // Check signature -- skip rebuild if nothing changed
            string sig = FleetStripSignature(doc);
            if (sig == _fleetStripSig && _fleetStrip.Visibility == Visibility.Visible) return;
            _fleetStripSig = sig;

            // run_label / goal_count from top-level fields (new schema)
            string runLabel = SS(doc, "run_label");
            string goalCountStr = SS(doc, "goal_count");

            // Fallback label: first worker's goal, truncated
            if (string.IsNullOrEmpty(runLabel) && workers.Length > 0)
            {
                var fw = workers[0] as Dictionary<string, object>;
                if (fw != null)
                {
                    string fg = SS(fw, "goal");
                    if (!string.IsNullOrEmpty(fg))
                    {
                        // use first non-empty line
                        string[] fgLines = fg.Replace("\r", "").Split('\n');
                        foreach (string fgl in fgLines)
                        {
                            string trimmed = fgl.Trim();
                            if (trimmed.Length > 0) { runLabel = trimmed; break; }
                        }
                    }
                }
            }
            // Truncate to ~50 chars
            if (!string.IsNullOrEmpty(runLabel) && runLabel.Length > 50)
                runLabel = runLabel.Substring(0, 50) + "…";

            // Worker counts
            int doneCount2 = 0, attentionCount2 = 0, runningCount2 = 0, pendingCount2 = 0;
            foreach (object o in workers)
            {
                var w = o as Dictionary<string, object>;
                if (w == null) continue;
                string st = SS(w, "status").ToLowerInvariant();
                if (st == "done" || st == "resolved" || st == "cancelled" || st == "freed") doneCount2++;
                else if (st == "stuck" || st == "maxturns" || st == "error" || st == "awaiting") attentionCount2++;
                else if (st == "pending") pendingCount2++;
                else runningCount2++;
            }
            // Freshness string (shared by both branches)
            string freshStr = "";
            if (ageSec >= 0)
            {
                string ageStr;
                if (ageSec < 60) ageStr = ((int)ageSec) + (_lang == 0 ? "秒前" : "s ago");
                else if (ageSec < 3600) ageStr = ((int)(ageSec / 60)) + (_lang == 0 ? "分前" : "m ago");
                else ageStr = ((int)(ageSec / 3600)) + (_lang == 0 ? "時間前" : "h ago");
                freshStr = _lang == 0 ? ("最終更新 " + ageStr) : ("updated " + ageStr);
            }

            // ── Rebuild inner DOM — single compact row ──────────────────────────
            bool allDone = !running;
            bool ja = _lang == 0;

            _fleetStripBody.Children.Clear();

            // Determine status dot color. pending (queued / not yet started) must NOT read as
            // Success green — that hides the most anxious state of a long delegation ("not started
            // yet"). When nothing is running but work is queued, use Info (blue/neutral).
            string dotColor;
            if (runningCount2 > 0)
                dotColor = Theme.Info(_dark);
            else if (attentionCount2 > 0)
                dotColor = Theme.Warning(_dark);
            else if (pendingCount2 > 0)
                dotColor = Theme.Info(_dark);
            else
                dotColor = Theme.Success(_dark);

            // Build status summary text. Only show "done" (Success) once nothing is running AND
            // nothing is pending; while queued show "待機中 N" / "Queued N".
            string summaryText;
            if (runningCount2 > 0)
                summaryText = ja ? ("実行中 " + runningCount2) : (runningCount2 + " running");
            else if (pendingCount2 > 0)
                summaryText = ja ? ("待機中 " + pendingCount2) : ("Queued " + pendingCount2);
            else
                summaryText = ja ? (doneCount2 + "件完了") : (doneCount2 + " done");

            // Build tooltip text (run_label + goal_count) — replaces the old label line
            string tipText = runLabel;
            if (!string.IsNullOrEmpty(tipText) && !string.IsNullOrEmpty(goalCountStr))
                tipText = tipText + (ja ? " ・" : " ·") + goalCountStr + (ja ? "件" : " goals");

            // ── Single DockPanel row ────────────────────────────────────────────
            var row = new DockPanel { Height = 26, VerticalAlignment = VerticalAlignment.Center };
            if (!string.IsNullOrEmpty(tipText)) row.ToolTip = tipText;

            // RIGHT: compact open button
            var openBtn = new Button
            {
                Content = ja ? "開く" : "Open",
                FontSize = 11,
                Height = 20,
                Padding = new Thickness(8, 0, 8, 0),
                Cursor = Cursors.Hand,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
                FontWeight = FontWeights.Normal
            };
            SetRef(openBtn, BackgroundProperty, "PanelAlt");
            SetRef(openBtn, ForegroundProperty, "Accent");
            SetRef(openBtn, Control.BorderBrushProperty, "Border");
            openBtn.Click += delegate { OpenCockpit(); };
            DockPanel.SetDock(openBtn, Dock.Right);
            row.Children.Add(openBtn);

            // LEFT: status dot + "フリート" label + summary + freshness
            var leftStack = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                VerticalAlignment = VerticalAlignment.Center
            };

            // Status dot (8px circle)
            var dot = new Border
            {
                Width = 8, Height = 8,
                CornerRadius = new CornerRadius(4),
                Margin = new Thickness(0, 0, 5, 0),
                VerticalAlignment = VerticalAlignment.Center
            };
            dot.Background = new SolidColorBrush(Theme.Col(dotColor));
            leftStack.Children.Add(dot);

            // "フリート" prefix
            var fleetLabel = new TextBlock
            {
                Text = "フリート",
                FontSize = 11,
                FontWeight = FontWeights.SemiBold,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(fleetLabel, TextBlock.ForegroundProperty, "Muted");
            leftStack.Children.Add(fleetLabel);

            // Show a short truncated run label inline for delegation context. Shown while RUNNING
            // and for the completed window too: the strip itself auto-hides after 30 min of being
            // not-running (see the ageSec > 1800 guard above), so any non-running strip that reaches
            // here is within the completed-30-min window — no extra age check needed here.
            if (!string.IsNullOrEmpty(runLabel))
            {
                string shortLabel = runLabel.Length > 20 ? runLabel.Substring(0, 20) + "…" : runLabel;
                var sepRL = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sepRL, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sepRL);

                var runLabelTb = new TextBlock
                {
                    Text = shortLabel,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(runLabelTb, TextBlock.ForegroundProperty, "Muted");
                leftStack.Children.Add(runLabelTb);
            }

            // separator dot
            var sep1 = new TextBlock
            {
                Text = " · ",
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(sep1, TextBlock.ForegroundProperty, "Faint");
            leftStack.Children.Add(sep1);

            // Status summary text
            var summaryTb = new TextBlock
            {
                Text = summaryText,
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(summaryTb, TextBlock.ForegroundProperty, "Muted");
            leftStack.Children.Add(summaryTb);

            // Attention segment (Warning color when >0)
            if (attentionCount2 > 0)
            {
                var sep2 = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sep2, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sep2);

                string attText = ja ? ("要対応 " + attentionCount2) : (attentionCount2 + " attention");
                var attTb = new TextBlock
                {
                    Text = attText,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center,
                    Foreground = new SolidColorBrush(Theme.Col(Theme.Warning(_dark)))
                };
                leftStack.Children.Add(attTb);
            }

            // Freshness
            if (!string.IsNullOrEmpty(freshStr))
            {
                var sep3 = new TextBlock
                {
                    Text = " · ",
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(sep3, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(sep3);

                var freshTb = new TextBlock
                {
                    Text = freshStr,
                    FontSize = 11,
                    VerticalAlignment = VerticalAlignment.Center
                };
                SetRef(freshTb, TextBlock.ForegroundProperty, "Faint");
                leftStack.Children.Add(freshTb);
            }

            row.Children.Add(leftStack);
            _fleetStripBody.Children.Add(row);
            _fleetStrip.Visibility = Visibility.Visible;
        }
        catch { }
    }

    // ── small helpers ───────────────────────────────────────────────────────────
    Button Btn(string content, string bg, string fg, bool bordered)
    {
        var b = new Button { Content = content, Cursor = Cursors.Hand, BorderThickness = new Thickness(bordered ? 1 : 0) };
        SetRef(b, BackgroundProperty, bg); SetRef(b, ForegroundProperty, fg);
        if (bordered) SetRef(b, Control.BorderBrushProperty, "Border");
        return b;
    }
    void SetRef(FrameworkElement el, DependencyProperty p, string key) { el.SetResourceReference(p, key); }

    // ── Material Symbols glyphs (vector paths, NO emoji) — mirrors FleetCockpit's loader so the
    //    sidebar footer icons match the cockpit exactly. Reads the shared 8-glyph subset font
    //    (ui/assets/material_glyphs.json). Best-effort: a missing/unreadable file just yields
    //    empty placeholders (the tooltip still carries the label).
    Dictionary<string, string> _glyphs = new Dictionary<string, string>();
    double _upm = 960;
    void LoadGlyphs()
    {
        try
        {
            string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets", "material_glyphs.json");
            if (!File.Exists(p)) return;
            var o = _cjs.DeserializeObject(File.ReadAllText(p, Encoding.UTF8)) as Dictionary<string, object>;
            if (o == null) return;
            if (o.ContainsKey("unitsPerEm")) _upm = Convert.ToDouble(o["unitsPerEm"]);
            var g = o["glyphs"] as Dictionary<string, object>;
            if (g == null) return;
            foreach (KeyValuePair<string, object> kv in g) _glyphs[kv.Key] = kv.Value.ToString();
        }
        catch { }
    }
    UIElement MakeIcon(string name, double size)
    {
        if (!_glyphs.ContainsKey(name)) { var ph = new Border(); ph.Width = size; ph.Height = size; return ph; }
        var path = new System.Windows.Shapes.Path();
        Geometry geo = Geometry.Parse(_glyphs[name]).Clone();   // Geometry.Parse returns FROZEN -> Clone before Transform
        double s = size / _upm;
        geo.Transform = new MatrixTransform(s, 0, 0, -s, 0, s * _upm);   // font y-up -> WPF y-down
        path.Data = geo; path.Stretch = Stretch.None;
        path.Width = size; path.Height = size;
        path.HorizontalAlignment = HorizontalAlignment.Center;
        path.VerticalAlignment = VerticalAlignment.Center;
        SetRef(path, System.Windows.Shapes.Shape.FillProperty, "Muted");
        return path;
    }
    // Compact icon button matching FleetCockpit's IconButton look (quiet, hover surface via the
    // shared Button template). glyph names come from the 8-glyph subset.
    Button IconButton(string glyph, double size, string tip)
    {
        var b = new Button
        {
            Content = MakeIcon(glyph, size),
            Width = 36, Height = 32, Cursor = Cursors.Hand,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            ToolTip = tip, Tag = glyph
        };
        return b;
    }

    // First line only, ellipsis-trimmed to `max` chars. Shared by SendText (stored title) and the
    // sidebar/header DISPLAY of long saved titles so a whole first message never fills a row.
    static string TrimTitle(string s, int max)
    {
        if (string.IsNullOrEmpty(s)) return s;
        int nl = s.IndexOfAny(new[] { '\r', '\n' });
        if (nl >= 0) s = s.Substring(0, nl);
        s = s.Trim();
        if (s.Length > max) s = s.Substring(0, max) + "…";
        return s;
    }

    // Header title tracks the active conversation (Wave 2). Untitled -> localized "New chat".
    void RefreshHeadTitle()
    {
        if (_headTitle == null) return;
        _headTitle.Text = (_conv == null || _conv.Untitled()) ? T("newchat") : TrimTitle(_conv.Title, 60);
    }

    // ── status dot: 4 states (Theme tokens) + tooltip ───────────────────────────
    //   "idle"    Success  — bridge reachable, nothing streaming
    //   "busy"    Accent   — a reply is streaming
    //   "offline" Danger   — bridge unreachable / last stream errored
    //   "signin"  Warning  — last answer looked like a sign-in / canned refusal
    // Precedence while idle: signin > offline > idle. "busy" always wins while generating.
    // MUST be called on the UI thread (background probe marshals via Dispatcher).
    void SetDot(string state)
    {
        _dotState = state;
        if (_statusDot == null) return;
        string token, tip;
        if (state == "busy")         { token = "Accent";  tip = T("dot_busy"); }
        else if (state == "offline") { token = "Danger";  tip = T("dot_offline"); }
        else if (state == "signin")  { token = "Warning"; tip = T("dot_signin"); }
        else                         { token = "Success"; tip = T("dot_idle"); }
        SetRef(_statusDot, BackgroundProperty, token);
        _statusDot.ToolTip = tip;
    }

    // Re-derive the IDLE dot color from the latest signals (reachability + last-answer verdict).
    // Never called while generating (busy owns the dot then). 'signinLatch' persists a sign-in
    // warning until the next clean answer clears it.
    bool _signinLatch = false;
    void RefreshIdleDot()
    {
        if (_generating) return;
        if (_signinLatch) { SetDot("signin"); return; }
        SetDot(_bridgeReachable ? "idle" : "offline");
    }

    // Heuristic: did this answer look like a Copilot sign-in prompt / canned refusal?
    static bool LooksLikeRefusal(string ans)
    {
        if (string.IsNullOrEmpty(ans)) return false;
        if (ans.Contains("それに応答できませんでした")) return true;
        if (ans.IndexOf("I couldn't respond to that", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (ans.Contains("サインイン") && ans.Contains("必要")) return true;
        return false;
    }

    // Low-cadence bridge reachability probe (piggybacked on the 800ms timer at a ~15s cadence).
    // Runs the actual GET on a background thread so a hung bridge never freezes the UI; the
    // result is marshalled back to update the dot. Proxy=null so loopback isn't routed through a
    // corporate proxy (which would make a local bridge look unreachable).
    void ProbeBridge()
    {
        if (_reachProbing) return;
        _reachProbing = true;
        new Thread((ThreadStart)delegate
        {
            bool ok = false;
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(_bridge + "/conv");
                req.Timeout = 2500; req.ReadWriteTimeout = 2500; req.Proxy = null;
                using (var resp = (HttpWebResponse)req.GetResponse())
                    ok = (int)resp.StatusCode < 500;
            }
            catch (WebException wex)
            {
                // A protocol response (even 4xx) still proves the bridge is up and answering.
                ok = wex.Response != null;
            }
            catch { ok = false; }
            _bridgeReachable = ok;
            _reachProbing = false;
            try { Dispatcher.BeginInvoke(new Action(delegate { RefreshIdleDot(); })); } catch { }
        }) { IsBackground = true }.Start();
    }

    // Generic inline recovery banner: a message + a single action button. Reused by the
    // sign-in/refusal banner (ShowSigninBanner) and the bridge-offline recovery banner
    // (send path in DoSend) -- both are "something is wrong, here is the one-click fix" UI.
    void ShowRecoveryBanner(string message, string buttonLabel, Action onClick)
    {
        if (_banner == null || _bannerBody == null) return;
        _bannerBody.Children.Clear();
        var head = new TextBlock
        {
            Text = message, FontWeight = FontWeights.SemiBold, FontSize = 13,
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 10)
        };
        SetRef(head, TextBlock.ForegroundProperty, "Fg");
        _bannerBody.Children.Add(head);
        var open = Btn(buttonLabel, "Accent", "AccentFg", false);
        open.Height = 30; open.Padding = new Thickness(14, 0, 14, 0); open.FontWeight = FontWeights.SemiBold;
        open.HorizontalAlignment = HorizontalAlignment.Left;
        open.Click += delegate { onClick(); };
        _bannerBody.Children.Add(open);
        SetRef(_banner, Border.BorderBrushProperty, "Warning");
        _banner.Visibility = Visibility.Visible;
    }

    // Show the sign-in / refusal banner with an actionable "Open Fleet Cockpit" link.
    void ShowSigninBanner()
    {
        ShowRecoveryBanner(T("signin_banner"), T("signin_open"), delegate { HideBanner(); OpenCockpit(); });
        _signinBannerShown = true;
    }

    // Composer focus ring WITHOUT a layout jump. Three states:
    //   "rest"  -> 1px Border token + full padding (12,6,12,6)
    //   "focus" -> 1px Accent       + full padding (12,6,12,6)
    //   "steer" -> 2px Accent       + padding-1    (11,5,11,5)   (compensates the +1 border)
    // Border+padding sums are identical in every state (13 horiz, 7 vert), so the composer's
    // ActualHeight/width never move as focus/steer change. BorderBrush swaps between the Border
    // and Accent tokens so both palette modes track the theme.
    void SetComposerRing(string state)
    {
        if (_composerBorder == null) return;
        if (state == "steer")
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Accent");
            _composerBorder.BorderThickness = new Thickness(2);
            _composerBorder.Padding = new Thickness(11, 5, 11, 5);
        }
        else if (state == "focus")
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Accent");
            _composerBorder.BorderThickness = new Thickness(1);
            _composerBorder.Padding = new Thickness(12, 6, 12, 6);
        }
        else // "rest"
        {
            SetRef(_composerBorder, Border.BorderBrushProperty, "Border");
            _composerBorder.BorderThickness = new Thickness(1);
            _composerBorder.Padding = new Thickness(12, 6, 12, 6);
        }
    }

    // Accent fill only when there is text to send OR while generating (Stop affordance).
    // Otherwise the button shows a neutral/disabled-looking state so the empty-input state
    // is visually distinct from the "ready to send" state. Also drives real enable/disable:
    // the button is disabled ONLY when idle with an empty input (nothing to send). While
    // generating it stays enabled because it doubles as the Stop control (see SendText/DoSend).
    void PaintSend()
    {
        if (_send == null) return;
        bool hasText = _input != null && _input.Text.Trim().Length > 0;
        bool active = _generating || hasText;
        // Enabled: generating (acts as Stop) OR there is text to send. Disabled: idle & empty.
        _send.IsEnabled = _generating || hasText;
        _send.Cursor = _send.IsEnabled ? Cursors.Hand : Cursors.Arrow;
        if (active)
        {
            SetRef(_send, BackgroundProperty, "Accent");
            SetRef(_send, ForegroundProperty, "AccentFg");
        }
        else
        {
            SetRef(_send, BackgroundProperty, "PanelAlt");
            SetRef(_send, ForegroundProperty, "Faint");   // disabled: faint foreground
        }
    }

    // ── slash-command autocomplete (type "/" to see commands, like Claude Code) ──
    Popup _cmdPopup; ListBox _cmdList;
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

    // Read on demand: changing .env takes effect the next time the slash palette or /help opens,
    // even when CopilotChat is already running. Keep the same precedence/semantics as the bridge:
    // a repo .env value wins; the process environment is only a fallback. 0=off, 1=deep,
    // 2=full validation. Invalid values fail closed.
    int P2cReviewLevel()
    {
        string rawValue = null;
        try
        {
            string path = Path.Combine(RepoRoot(), ".env");
            if (File.Exists(path))
            {
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string line = (raw ?? "").Trim().TrimStart('\uFEFF');
                    if (line.StartsWith("MCP_REVIEW_P2C=", StringComparison.Ordinal))
                    {
                        rawValue = line.Substring("MCP_REVIEW_P2C=".Length).Trim();
                        break;
                    }
                }
            }
        }
        catch { }
        if (rawValue == null)
            rawValue = (Environment.GetEnvironmentVariable("MCP_REVIEW_P2C") ?? "0").Trim();
        int level;
        if (!Int32.TryParse(rawValue, out level) || level < 0 || level > 2) return 0;
        return level;
    }

    bool P2cReviewEnabled() { return P2cReviewLevel() > 0; }

    // Display-only descriptions (insert uses Tag=name), localized at access time. P2c commands
    // are deliberately absent from the base arrays and are materialized only when the flag is on.
    string[][] _commands
    {
        get
        {
            string[][] baseCommands = _lang == 0 ? _commandsJa : _commandsEn;
            if (!P2cReviewEnabled()) return baseCommands;
            var commands = new List<string[]>(baseCommands);
            int reviewFix = commands.FindIndex(delegate(string[] c) { return c[0] == "/review-fix"; });
            if (reviewFix < 0) reviewFix = commands.Count;
            commands.Insert(reviewFix, _lang == 0 ? _p2cCommandJaSecurity : _p2cCommandEnSecurity);
            commands.Insert(reviewFix, _lang == 0 ? _p2cCommandJaReview : _p2cCommandEnReview);
            return commands.ToArray();
        }
    }
    void BuildCmdPopup()
    {
        _cmdList = new ListBox { MaxHeight = 240, BorderThickness = new Thickness(0) };
        SetRef(_cmdList, BackgroundProperty, "Panel");
        _cmdList.PreviewMouseLeftButtonUp += delegate { AcceptCommand(); };
        var border = new Border { Child = _cmdList, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(4) };
        SetRef(border, BackgroundProperty, "Panel"); SetRef(border, Border.BorderBrushProperty, "Accent");
        _cmdPopup = new Popup { PlacementTarget = _input, Placement = PlacementMode.Top, StaysOpen = false, Width = 560 };
        _cmdPopup.Child = border;
    }
    ListBoxItem MakeCmdItem(string name, string desc)
    {
        var sp = new StackPanel { Orientation = Orientation.Horizontal };
        var n = new TextBlock { Text = name, FontWeight = FontWeights.SemiBold, MinWidth = 110 };
        SetRef(n, TextBlock.ForegroundProperty, "Accent");
        var d = new TextBlock { Text = desc, Margin = new Thickness(8, 0, 0, 0) };
        SetRef(d, TextBlock.ForegroundProperty, "Muted");
        sp.Children.Add(n); sp.Children.Add(d);
        return new ListBoxItem { Content = sp, Tag = name, Padding = new Thickness(6, 4, 6, 4) };
    }
    void UpdateCmdPopup()
    {
        try
        {
            string t = _input.Text;
            if (t.Length >= 1 && t[0] == '/' && t.IndexOf(' ') < 0 && t.IndexOf('\n') < 0)
            {
                string pre = t.ToLower();
                _cmdList.Items.Clear();
                foreach (var c in _commands)
                    if (c[0].StartsWith(pre)) _cmdList.Items.Add(MakeCmdItem(c[0], c[1]));
                if (_cmdList.Items.Count > 0) { _cmdList.SelectedIndex = 0; _cmdPopup.IsOpen = true; }
                else _cmdPopup.IsOpen = false;
            }
            else if (_cmdPopup != null) _cmdPopup.IsOpen = false;
        }
        catch { }
    }
    void AcceptCommand()
    {
        if (_cmdList.SelectedItem == null && _cmdList.Items.Count > 0) _cmdList.SelectedIndex = 0;
        var item = _cmdList.SelectedItem as ListBoxItem;
        if (item != null)
        {
            _input.Text = (item.Tag as string) + " ";
            _input.CaretIndex = _input.Text.Length;
        }
        if (_cmdPopup != null) _cmdPopup.IsOpen = false;
        _input.Focus();
    }

    string CommandHelpText()
    {
        string p2c = "";
        if (P2cReviewEnabled())
            p2c = _lang != 0
                ? "/deep-review [diff|<path>] - deep review\n"
                    + "/deep-security-review [diff|<path>] - deep security review\n"
                : "/deep-review [diff|<パス>] - 深掘りレビュー\n"
                    + "/deep-security-review [diff|<パス>] - 深掘りセキュリティレビュー\n";
        if (_lang != 0)
            return "Chat commands:\n"
                + "/help - this list\n"
                + "/research - deep research with the Claude researcher\n"
                + "/review [diff|<path>] - review and summarize\n"
                + "/security-review [diff|<path>] - security-focused review\n"
                + p2c
                + "/review-fix [high|verified] - fix review findings (2-step confirm, auto-backup + undo)\n"
                + "/analyze - analyze a file\n"
                + "/summarize - summarize\n"
                + "/translate <lang> <text> - translate\n"
                + "/plan - step-by-step plan\n"
                + "/critique - critical review\n"
                + "/proofread - proofread\n"
                + "/rewrite - change the style\n"
                + "/brainstorm - generate ideas\n"
                + "/steps - break into steps\n"
                + "/eli5 - explain simply\n"
                + "/proscons - pros/cons table\n"
                + "/table - make a table\n"
                + "\nThe fleet goal box also has /help. The fleet side expands /code /fix /test /refactor /doc /review /research.";
        return "チャットコマンド:\n"
            + "/help - この一覧\n"
            + "/research - Claude researcher で深掘り調査\n"
            + "/review [diff|<パス>] - レビューして要約\n"
            + "/security-review [diff|<パス>] - セキュリティレビュー\n"
            + p2c
            + "/review-fix [high|verified] - 指摘を修正（2段階確認・自動バックアップ＆undo）\n"
            + "/analyze - ファイル分析\n"
            + "/summarize - 要約\n"
            + "/translate <言語> <文> - 翻訳\n"
            + "/plan - ステップ計画\n"
            + "/critique - 批判的レビュー\n"
            + "/proofread - 校正\n"
            + "/rewrite - 文体変更\n"
            + "/brainstorm - アイデア出し\n"
            + "/steps - 手順化\n"
            + "/eli5 - やさしく説明\n"
            + "/proscons - 賛否表\n"
            + "/table - 表作成\n"
            + "\nフリート入力欄にも /help を追加済み。フリート側は /code /fix /test /refactor /doc /review /research を展開します。";
    }

    // Repo root: this exe runs from <repo>\ui, so one level up is <repo> -- same convention
    // already used for .fleet\status.json / .fleet\commands.json below.
    string RepoRoot() { return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..")); }

    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int nCmdShow);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool RedrawWindow(IntPtr h, IntPtr lprc, IntPtr hrgn, uint flags);

    // The daily launcher is windowless: start_all_hidden.vbs -> start_all.ps1 (-WindowStyle
    // Hidden = SW_HIDE) -> this app. A child of an SW_HIDE parent INHERITS that show-state
    // via STARTUPINFO.wShowWindow, so WPF builds the HWND without a real first paint: DWM
    // has no composed surface and the window shows as black / stale rectangles even though
    // the visual tree is intact (PrintWindow of the same HWND returns correct content).
    // One SW_SHOW + full redraw right after load discards the inherited state. Idempotent,
    // and a no-op when the app was started normally.
    void ForceVisibleOnce()
    {
        try
        {
            IntPtr h = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            if (h == IntPtr.Zero) return;
            const int SW_SHOW = 5;
            const uint RDW_INVALIDATE = 0x0001, RDW_ERASE = 0x0004,
                       RDW_ALLCHILDREN = 0x0080, RDW_UPDATENOW = 0x0100;
            ShowWindow(h, SW_SHOW);
            RedrawWindow(h, IntPtr.Zero, IntPtr.Zero,
                         RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW);
        }
        catch { }
    }
    // Launch (or focus) the parallel-execution cockpit -- so the user never has to close
    // everything and restart just to reach it.
    void OpenCockpit()
    {
        try
        {
            var existing = System.Diagnostics.Process.GetProcessesByName("FleetCockpit");
            if (existing.Length > 0)
            {
                try { if (existing[0].MainWindowHandle != IntPtr.Zero) SetForegroundWindow(existing[0].MainWindowHandle); } catch { }
                return;
            }
            string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "FleetCockpit.exe");
            if (File.Exists(exe)) System.Diagnostics.Process.Start(exe);
        }
        catch { }
    }

    // Replace the default WPF button chrome (which forces an unreadable light-blue
    // hover highlight) with a flat template: the button's own Background + a
    // color-independent translucent overlay on hover/press, content always on top.
    void AddButtonStyle()
    {
        const string xaml =
            "<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'" +
            " xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml' TargetType='Button'>" +
            "<Setter Property='Template'><Setter.Value>" +
            "<ControlTemplate TargetType='Button'>" +
            "<Border Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}'" +
            " BorderThickness='{TemplateBinding BorderThickness}' CornerRadius='7' SnapsToDevicePixels='True'>" +
            "<Grid><Border x:Name='ov' Background='Transparent' CornerRadius='7'/>" +
            "<ContentPresenter Margin='{TemplateBinding Padding}'" +
            " HorizontalAlignment='{TemplateBinding HorizontalContentAlignment}'" +
            " VerticalAlignment='{TemplateBinding VerticalContentAlignment}'/></Grid></Border>" +
            "<ControlTemplate.Triggers>" +
            "<Trigger Property='IsMouseOver' Value='True'>" +
            "<Setter TargetName='ov' Property='Background' Value='{DynamicResource Hover}'/></Trigger>" +
            "<Trigger Property='IsPressed' Value='True'>" +
            "<Setter TargetName='ov' Property='Background' Value='{DynamicResource Press}'/></Trigger>" +
            "<Trigger Property='IsEnabled' Value='False'><Setter Property='Opacity' Value='0.5'/></Trigger>" +
            "</ControlTemplate.Triggers></ControlTemplate>" +
            "</Setter.Value></Setter></Style>";
        var style = (Style)System.Windows.Markup.XamlReader.Parse(xaml);
        Application.Current.Resources[typeof(Button)] = style;
    }
    void Set(string key, string hex) { Application.Current.Resources[key] = new SolidColorBrush(C(hex)); }
    void ApplyTheme()
    {
        // Single source of truth: Theme.cs (calm warm-neutral palette, spec Design Tokens).
        Set("Bg", Theme.Bg(_dark));
        Set("Panel", Theme.Surface(_dark));
        Set("PanelAlt", Theme.SurfaceSubtle(_dark));
        Set("Selected", Theme.Selected(_dark));   // active sidebar row card fill (no rail/colored border)
        Set("Border", Theme.Border(_dark));
        Set("BorderStrong", Theme.BorderStrong(_dark));
        Set("Fg", Theme.Text(_dark));
        Set("Muted", Theme.Muted(_dark));
        Set("Faint", Theme.Faint(_dark));
        Set("UserBg", Theme.SurfaceSubtle(_dark));
        Set("Accent", Theme.Accent(_dark));
        Set("AccentSoft", Theme.AccentSoft(_dark));
        Set("AccentFg", Theme.AccentFg(_dark));
        Set("Success", Theme.Success(_dark));   // status dot: idle & bridge reachable
        Set("Warning", Theme.Warning(_dark));   // status dot: sign-in / canned refusal
        Set("Danger", Theme.Danger(_dark));     // status dot: bridge unreachable / stream error
        Set("Hover", Theme.Hover(_dark));
        Set("Press", Theme.Press(_dark));
        Set("CodeBg", Theme.SurfaceSubtle(_dark));
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

        // Render sections in order: Pinned, Recent, Fleet, Archived.
        // Header always shows when the section is non-empty (so a collapsed section can be expanded).
        // When a section is COLLAPSED, its rows are hidden EXCEPT the active conversation's row, which
        // always renders beneath the header -- otherwise the user couldn't click back to the open conv.
        RenderSection(pinnedList,   "sec_pinned",   "pinned",   false, false, true);
        RenderSection(todayList,    "sec_today",    "today",    false, false, false);
        RenderSection(fleetList,    "sec_fleet",    "fleet",    true,  false, false);
        RenderSection(archivedList, "sec_archived", "archived", false, true,  false);

        RefreshHeadTitle();   // keep the header title in sync with the active conversation (Wave 2)
    }

    // ITEM 3c: per-section "show all" override for the session. When a section holds >8 items we
    // render the first 8 + a muted "+N more" row; clicking it flips the section here (for the session)
    // so the full list renders. COLLAPSING the section (chevron) resets the override — MakeSectionHeader
    // clears the entry so re-expanding starts capped again.
    HashSet<string> _sectionExpanded = new HashSet<string>();
    const int SectionCap = 8;

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
            Padding = new Thickness(15, 5, 6, 6), Margin = new Thickness(0, 2, 0, 2),
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
        string chevron = collapsed ? "▸" : "▾";

        var chevronBlock = new TextBlock
        {
            Text = chevron, FontSize = 10,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 5, 0)
        };
        SetRef(chevronBlock, TextBlock.ForegroundProperty, "Faint");

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
            var pinMark = new TextBlock { Text = "\uE718", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 5, 0) };            SetRef(pinMark, TextBlock.ForegroundProperty, "Muted");
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
            Padding = new Thickness(9, 0, 9, 0), BorderThickness = new Thickness(0),
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
            else ExecuteDelete(cc, 1);
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

    // ── whole-UI zoom (4K readability) ───────────────────────────────────────────
    // A single ScaleTransform applied as the root content's LayoutTransform scales EVERYTHING
    // (text, icons, paddings) AND reflows layout (LayoutTransform participates in measure, unlike
    // RenderTransform). Persisted as ui_scale in the shared settings.txt (the cockpit honors the
    // same key). Clamp 0.8–2.0.
    static double ClampScale(double s)
    {
        if (s < 0.8) return 0.8;
        if (s > 2.0) return 2.0;
        return s;
    }

    // Current monitor scale = this window's device pixels per DIP (DPI/96). 1.0 at 100%, 1.5 at 150%.
    // Read from the live PresentationSource; falls back to 1.0 before the window is sourced.
    double CurrentMonitorScale()
    {
        try
        {
            var src = PresentationSource.FromVisual(this);
            if (src != null && src.CompositionTarget != null)
            {
                double m11 = src.CompositionTarget.TransformToDevice.M11;
                if (m11 > 0.01) return m11;
            }
        }
        catch { }
        return 1.0;
    }

    // AUTO effective scale for a monitor: target physical size / that monitor's own DPI scale, clamped.
    // monitorScale (WPF DPI) × effective ≈ _scaleTarget -> constant physical size across monitors. The
    // clamp can cap it on extreme monitors (see numeric proof in the report).
    double EffectiveAutoScale(double monitorScale)
    {
        if (monitorScale < 0.01) monitorScale = 1.0;
        return ClampScale(_scaleTarget / monitorScale);
    }

    // Apply _uiScale to the root LayoutTransform. showToast=true flashes the "NNN%" overlay
    // (interactive changes); false is silent (initial apply / external cockpit mirror / DPI recompute).
    void ApplyScale(bool showToast)
    {
        _uiScale = ClampScale(_uiScale);
        if (_rootScale == null) _rootScale = new ScaleTransform(1.0, 1.0);
        _rootScale.ScaleX = _uiScale; _rootScale.ScaleY = _uiScale;
        if (Content is FrameworkElement)
        {
            var fe = (FrameworkElement)Content;
            if (!ReferenceEquals(fe.LayoutTransform, _rootScale)) fe.LayoutTransform = _rootScale;
        }
        if (showToast) ShowScaleToast();
    }

    // Recompute + apply the AUTO effective scale for THIS window's current monitor. Silent by default.
    void ApplyAutoScale(bool toast)
    {
        _uiScale = EffectiveAutoScale(CurrentMonitorScale());
        ApplyScale(false);
        if (toast) ShowScaleToastText(T("ui_auto"));
    }

    // Nudge the zoom by delta (±0.1 typical) -> MANUAL mode. Clamp, apply, persist, flash the overlay.
    void BumpScale(double delta)
    {
        double next = ClampScale(_uiScale + delta);
        // Snap to a clean 0.05 grid so repeated notches don't drift (0.7999999…).
        next = System.Math.Round(next * 20.0) / 20.0;
        // If we were in auto, a +/- always transitions to manual even at a clamp edge.
        if (!_uiAuto && System.Math.Abs(next - _uiScale) < 0.0001) { ShowScaleToast(); return; }
        _uiAuto = false;
        _uiScale = next;
        ApplyScale(true);
        SaveSettings();
    }

    // Ctrl+0 = "back to automatic" (NOT 1.0): re-enter auto, recompute for this monitor, brief 自動 toast.
    void ResetScale()
    {
        _uiAuto = true;
        SaveSettings();          // persist ui_scale=auto
        ApplyAutoScale(true);    // recompute for the current monitor + "自動" toast
    }

    // PMv2 fires this when the window is dragged onto a differently-scaled monitor. In AUTO mode we
    // recompute the effective scale from the NEW monitor's DPI (newDpi.DpiScaleX = DPI/96) so physical
    // size stays constant -- SILENTLY (an automatic recompute must not flash the toast). Manual mode is
    // untouched. Never throw from here.
    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        try { base.OnDpiChanged(oldDpi, newDpi); } catch { }
        try
        {
            if (_uiAuto)
            {
                double ms = (newDpi.DpiScaleX > 0.01) ? newDpi.DpiScaleX : CurrentMonitorScale();
                _uiScale = EffectiveAutoScale(ms);
                ApplyScale(false);   // silent: no toast on automatic DPI-change recompute
            }
        }
        catch { }
    }

    // Small fading "NNN%" overlay, anchored top-center over the content. Reuses one Border/timer.
    void ShowScaleToast() { ShowScaleToastText(null); }
    // txt != null shows that literal (e.g. "自動" when entering auto); null shows the current %.
    void ShowScaleToastText(string txt)
    {
        if (txt == null) txt = ((int)System.Math.Round(_uiScale * 100.0)) + "%";
        if (_scaleToast == null)
        {
            _scaleToastText = new TextBlock
            {
                Text = txt, FontSize = 13, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center
            };
            SetRef(_scaleToastText, TextBlock.ForegroundProperty, "Fg");
            _scaleToast = new Border
            {
                Child = _scaleToastText, CornerRadius = new CornerRadius(Theme.RadCard),
                Padding = new Thickness(14, 7, 14, 7), BorderThickness = new Thickness(1),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Top,
                Margin = new Thickness(0, 14, 0, 0), IsHitTestVisible = false,
                Visibility = Visibility.Collapsed
            };
            SetRef(_scaleToast, BackgroundProperty, "Panel");
            SetRef(_scaleToast, Border.BorderBrushProperty, "Border");
            Panel.SetZIndex(_scaleToast, 200);
            // Anchor inside the scaled root Grid so it lives above the columns (and scales with the UI,
            // which is fine — it reads as part of the same zoomed surface).
            if (_rootGrid != null)
            {
                Grid.SetColumn(_scaleToast, 0);
                Grid.SetColumnSpan(_scaleToast, System.Math.Max(1, _rootGrid.ColumnDefinitions.Count));
                _rootGrid.Children.Add(_scaleToast);
            }
        }
        _scaleToastText.Text = txt;
        _scaleToast.Visibility = Visibility.Visible;
        _scaleToast.Opacity = 1.0;
        if (_scaleToastTimer == null)
        {
            _scaleToastTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(900) };
            _scaleToastTimer.Tick += delegate
            {
                _scaleToastTimer.Stop();
                var fade = new DoubleAnimation(1.0, 0.0, new Duration(TimeSpan.FromMilliseconds(280)));
                fade.Completed += delegate { if (_scaleToast != null) _scaleToast.Visibility = Visibility.Collapsed; };
                if (_scaleToast != null) _scaleToast.BeginAnimation(UIElement.OpacityProperty, fade);
            };
        }
        _scaleToast.BeginAnimation(UIElement.OpacityProperty, null);   // cancel any in-flight fade
        _scaleToast.Opacity = 1.0;
        _scaleToastTimer.Stop(); _scaleToastTimer.Start();
    }

    // STEER-mode signal: when viewing a parallel-task conversation, anything you type interrupts
    // that running worker -- a very different action from a normal chat. Make it unmistakable by
    // tinting the composer border Accent (orange) and thickening it, so the user is never surprised
    // that their message went to a background agent. (GAP 8)
    // Now operates on _composerBorder (the outer rounded wrapper) rather than the inner transparent
    // TextBox, because Task 1 made the TextBox borderless.
    void RefreshSteerVisual()
    {
        bool steer = !string.IsNullOrEmpty(_activeFleetUrl);
        if (_composerBorder != null)
        {
            // Steer mode: 2px Accent (unmistakable orange; overrides focus/rest states).
            // Normal mode: 1px Border at rest, 1px Accent when the input has focus.
            // SetComposerRing keeps the footprint constant across all three (padding compensates).
            if (steer) SetComposerRing("steer");
            else SetComposerRing(_input != null && _input.IsKeyboardFocused ? "focus" : "rest");
        }
        // Placeholder + destination indicator follow the steer state. When the active fleet run
        // finishes (OnSelect sets _activeFleetUrl = null) or the user opens a normal local chat
        // (OpenConversation / NewChat null it out), this reconciles back to the normal placeholder
        // and hides the indicator — every steer-exit path calls RefreshSteerVisual().
        if (_inputHint != null)
        {
            if (steer)
                _inputHint.Text = _lang == 0 ? "この会話への割り込みを送信…" : "Send an interrupt to this conversation…";
            else
                _inputHint.Text = _lang == 0 ? "メッセージを入力…" : "Type a message…";
        }
        if (_steerHint != null)
        {
            if (steer)
            {
                string target = (_conv != null && !string.IsNullOrEmpty(_conv.Name)) ? _conv.Name : "";
                string dest;
                if (_lang == 0)
                    dest = string.IsNullOrEmpty(target) ? "送信先: Fleet会話" : ("送信先: " + target + " / Fleet会話");
                else
                    dest = string.IsNullOrEmpty(target) ? "To: Fleet" : ("To: " + target + " / Fleet");
                _steerHint.Text = dest;
                _steerHint.Visibility = Visibility.Visible;
            }
            else
            {
                _steerHint.Visibility = Visibility.Collapsed;
            }
        }
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
            Child = tb, CornerRadius = new CornerRadius(6), BorderThickness = new Thickness(1),
            Padding = new Thickness(14, 9, 14, 9), Margin = new Thickness(0, 4, 0, 4),
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
            try { HttpGet("/new"); _pageConv = newConv; } catch { }
        }) { IsBackground = true }.Start();
        _conv = newConv;
        _conv.Ts = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        _all.Insert(0, _conv);
        _messages.Children.Clear();
        _emptyState = null;             // cleared with the children above; rebuild fresh below
        ShowEmptyState();               // fresh chat -> show the empty state again
        _activeFleetUrl = null; RefreshSteerVisual();
        RefreshConvList();
        _input.Focus();
    }

    void OpenConversation(Conversation c)
    {
        _conv = c;
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
                var tm = ReadTranscript(tp);
                AppendSubAgentTranscripts(tm, tp);
                foreach (var mm in tm) c.Messages.Add(mm);
            }
        }
        // a registry/fleet conversation we haven't loaded yet -> pull it via /history
        if (c.Messages.Count == 0 && !string.IsNullOrEmpty(c.ConvUrl))
        {
            var note = new TextBlock { Text = T("loadingconv"), FontSize = 12.5, Margin = new Thickness(2, 2, 2, 10) };
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
                    ? "この会話の本文はここからは取得できません。\n並列タスクの全文は、コックピットの該当カード（▶ 開く）から開くと表示されます。"
                    : "This conversation's body isn't fetchable here.\nOpen it from its cockpit card (▶ Open) to see the full transcript.",
                FontSize = 12.5, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(2, 2, 2, 10)
            };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            RefreshConvList();
            return;
        }
        _activeFleetUrl = null; RefreshSteerVisual();   // a normal local conversation is NOT steer mode
        foreach (var m in c.Messages) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
        RefreshConvList();
        if (!string.IsNullOrEmpty(c.ConvUrl))
            new Thread((ThreadStart)delegate
            {
                try { HttpGet("/switch?url=" + Uri.EscapeDataString(c.ConvUrl)); _pageConv = c; } catch { }
            }) { IsBackground = true }.Start();
    }

    void DeleteLocal(Conversation c)
    {
        try { var p = Path_(c.Id); if (File.Exists(p)) File.Delete(p); } catch { }
        // Clean up sidebar state for the deleted conversation.
        bool _sc = _pinned.Remove(c.Id) | _archived.Remove(c.Id) | _forcedToday.Remove(c.Id);
        if (_sc) SaveSidebarState();
        if (_renamingId == c.Id) _renamingId = null;   // clear any in-flight rename of the deleted conv
        _all.Remove(c);
        if (_conv.Id == c.Id)
        {
            if (_all.Count > 0) OpenConversation(_all[0]);
            else { _conv = new Conversation(); _all.Add(_conv); _messages.Children.Clear(); }
        }
        RefreshConvList();
    }

    // ── bulk conversation manager (multi-select delete) ─────────────────────────
    bool BulkRunLive()
    {
        try
        {
            string statusPath = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(statusPath)) return false;
            var d = _cjs.DeserializeObject(File.ReadAllText(statusPath, Encoding.UTF8)) as Dictionary<string, object>;
            if (d != null && d.ContainsKey("running") && d["running"] != null) return Convert.ToBoolean(d["running"]);
        }
        catch { }
        return false;
    }

    static string BulkDateStr(double ts)
    {
        if (ts == 0) return "—";
        try { return new DateTime(1970, 1, 1).AddSeconds(ts).ToLocalTime().ToString("MM/dd HH:mm"); }
        catch { return "—"; }
    }

    void ShowManageConversations()
    {
        bool localOnly = BulkRunLive();

        var win = new Window
        {
            Title = T("manage_title"), Owner = this, Width = 620, Height = 620,
            WindowStartupLocation = WindowStartupLocation.CenterOwner
        };
        win.Resources = this.Resources;
        SetRef(win, BackgroundProperty, "Bg");

        var dock = new DockPanel { Margin = new Thickness(16, 14, 16, 14) };

        // FILTER ROW (top)
        var filterRow = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
        var showAll = new CheckBox { Content = T("show_all"), IsChecked = false, FontSize = 12.5, Margin = new Thickness(0, 0, 0, 6) };
        SetRef(showAll, ForegroundProperty, "Fg");
        var periodWrap = new StackPanel { Orientation = Orientation.Horizontal };
        var periodLbl = new TextBlock { Text = T("period"), FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
        SetRef(periodLbl, TextBlock.ForegroundProperty, "Muted");
        var period = new ComboBox { Width = 160, FontSize = 12.5 };
        period.Items.Add(T("period_all"));
        period.Items.Add(T("period_24h"));
        period.Items.Add(T("period_7d"));
        period.Items.Add(T("period_30d"));
        period.SelectedIndex = 0;
        periodWrap.Children.Add(periodLbl); periodWrap.Children.Add(period);
        filterRow.Children.Add(showAll); filterRow.Children.Add(periodWrap);

        if (localOnly)
        {
            var rn = new TextBlock { Text = T("running_note"), FontSize = 11.5, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 7, 0, 0) };
            SetRef(rn, TextBlock.ForegroundProperty, "Accent");
            filterRow.Children.Add(rn);
        }
        DockPanel.SetDock(filterRow, Dock.Top);
        dock.Children.Add(filterRow);

        // FOOTER (bottom) -- built before the list so its labels can be referenced
        var footer = new DockPanel { Margin = new Thickness(0, 10, 0, 0) };
        var selAll = new Button { Content = T("select_all"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 6, 0), BorderThickness = new Thickness(1) };
        SetRef(selAll, BackgroundProperty, "PanelAlt"); SetRef(selAll, ForegroundProperty, "Fg"); SetRef(selAll, Control.BorderBrushProperty, "Border");
        var clrAll = new Button { Content = T("clear_all"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 10, 0), BorderThickness = new Thickness(1) };
        SetRef(clrAll, BackgroundProperty, "PanelAlt"); SetRef(clrAll, ForegroundProperty, "Fg"); SetRef(clrAll, Control.BorderBrushProperty, "Border");
        var fetchBtn = new Button { Content = T("fetch_copilot"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 10, 0), BorderThickness = new Thickness(1) };
        SetRef(fetchBtn, BackgroundProperty, "PanelAlt"); SetRef(fetchBtn, ForegroundProperty, "Fg"); SetRef(fetchBtn, Control.BorderBrushProperty, "Border");
        if (localOnly) fetchBtn.IsEnabled = false;   // a run is live -> don't touch the page
        var countLbl = new TextBlock { Text = "", FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center };
        SetRef(countLbl, TextBlock.ForegroundProperty, "Muted");
        var closeBtn = new Button { Content = T("close"), Cursor = Cursors.Hand, FontSize = 12.5, Padding = new Thickness(16, 6, 16, 7), Margin = new Thickness(8, 0, 0, 0), BorderThickness = new Thickness(1), FontWeight = FontWeights.SemiBold };
        SetRef(closeBtn, BackgroundProperty, "PanelAlt"); SetRef(closeBtn, ForegroundProperty, "Fg"); SetRef(closeBtn, Control.BorderBrushProperty, "Border");
        closeBtn.Click += delegate { win.Close(); };
        var delBtn = new Button { Content = T("del_selected"), Cursor = Cursors.Hand, FontSize = 12.5, Padding = new Thickness(16, 6, 16, 7), BorderThickness = new Thickness(0), FontWeight = FontWeights.SemiBold };
        SetRef(delBtn, BackgroundProperty, "Accent"); SetRef(delBtn, ForegroundProperty, "AccentFg");
        DockPanel.SetDock(closeBtn, Dock.Right);
        DockPanel.SetDock(delBtn, Dock.Right);
        footer.Children.Add(closeBtn);
        footer.Children.Add(delBtn);
        footer.Children.Add(selAll);
        footer.Children.Add(clrAll);
        footer.Children.Add(fetchBtn);
        footer.Children.Add(countLbl);
        DockPanel.SetDock(footer, Dock.Bottom);
        dock.Children.Add(footer);

        // LIST (fills the middle)
        var listPanel = new StackPanel { Margin = new Thickness(0, 2, 0, 2) };
        var listScroll = new ScrollViewer { Content = listPanel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        SetRef(listScroll, Control.BorderBrushProperty, "Border");
        listScroll.BorderThickness = new Thickness(1);
        var listBorder = new Border { Child = listScroll, CornerRadius = new CornerRadius(8) };
        SetRef(listBorder, Border.BorderBrushProperty, "Border"); listBorder.BorderThickness = new Thickness(1);
        dock.Children.Add(listBorder);

        // parallel structure: each checkbox -> its conversation
        var boxes = new List<CheckBox>();
        var convOf = new Dictionary<CheckBox, Conversation>();
        // orphans fetched from the Copilot side (agent rail) -- not in the local registry.
        var fetched = new List<Conversation>();

        // count label updater (declared via array so inner delegates can call it)
        var updateCount = new Action[1];
        updateCount[0] = delegate
        {
            int n = 0;
            foreach (var cb in boxes) if (cb.IsChecked == true) n++;
            countLbl.Text = (_lang == 0) ? (n + "件選択") : (n + " selected");
        };

        // (re)build the filtered list
        var rebuild = new Action[1];
        rebuild[0] = delegate
        {
            listPanel.Children.Clear();
            boxes.Clear();
            convOf.Clear();
            bool all = showAll.IsChecked == true;
            double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
            double cutoff = 0;
            int pi = period.SelectedIndex;
            if (pi == 1) cutoff = now - 24 * 3600;
            else if (pi == 2) cutoff = now - 7 * 24 * 3600;
            else if (pi == 3) cutoff = now - 30 * 24 * 3600;
            // build a row for one conversation; orphan rows (fetched) always show.
            Action<Conversation, bool> addRow = delegate (Conversation cc, bool isOrphan)
            {
                var row = new DockPanel { Margin = new Thickness(8, 5, 8, 5) };
                var cb = new CheckBox { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
                cb.Checked += delegate { updateCount[0](); };
                cb.Unchecked += delegate { updateCount[0](); };
                DockPanel.SetDock(cb, Dock.Left);
                var raw = string.IsNullOrEmpty(cc.Title) ? T("untitled") : cc.Title;
                if (raw.Length > 50) raw = raw.Substring(0, 50) + "…";
                var meta = new TextBlock { FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
                SetRef(meta, TextBlock.ForegroundProperty, isOrphan ? "Accent" : "Muted");
                meta.Text = BulkDateStr(cc.Ts) + (string.IsNullOrEmpty(cc.Source) ? "" : ("  " + cc.Source));
                DockPanel.SetDock(meta, Dock.Right);
                var tt = new TextBlock { Text = raw, FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
                SetRef(tt, TextBlock.ForegroundProperty, "Fg");
                row.Children.Add(cb);
                row.Children.Add(meta);
                row.Children.Add(tt);
                listPanel.Children.Add(row);
                boxes.Add(cb);
                convOf[cb] = cc;
            };
            var shownUrls = new System.Collections.Generic.HashSet<string>();
            foreach (var c in _all)
            {
                if (string.IsNullOrEmpty(c.ConvUrl)) continue;
                if (!all && c.Source != "fleet") continue;
                if (!(c.Ts == 0 || c.Ts >= cutoff)) continue;
                shownUrls.Add(c.ConvUrl);
                addRow(c, false);
            }
            // append Copilot-side orphans not already shown above (always visible, ignoring
            // the fleet/period filter -- the user explicitly asked Copilot for these).
            foreach (var c in fetched)
            {
                if (string.IsNullOrEmpty(c.ConvUrl)) continue;
                if (shownUrls.Contains(c.ConvUrl)) continue;
                bool inAll = false;
                foreach (var x in _all) if (x.ConvUrl == c.ConvUrl) { inAll = true; break; }
                if (inAll) continue;
                addRow(c, true);
            }
            updateCount[0]();
        };

        showAll.Checked += delegate { rebuild[0](); };
        showAll.Unchecked += delegate { rebuild[0](); };
        period.SelectionChanged += delegate { rebuild[0](); };
        selAll.Click += delegate { foreach (var cb in boxes) cb.IsChecked = true; };
        clrAll.Click += delegate { foreach (var cb in boxes) cb.IsChecked = false; };

        var progressLbl = countLbl; // reuse the live label for progress during deletion

        // "Copilot側から一覧取得": pull the agent's own conversation rail (incl. orphans not
        // in the local registry) and show them as checkbox rows for selective deletion.
        fetchBtn.Click += delegate
        {
            fetchBtn.IsEnabled = false;
            progressLbl.Text = (_lang == 0) ? "Copilotから取得中…" : "Fetching from Copilot…";
            new Thread((ThreadStart)delegate
            {
                string j = null; string err = null;
                try { j = HttpGet("/agent_conversations", 120000); }
                catch (Exception ex) { err = ex.GetType().Name; }
                var orphans = new List<Conversation>();
                int n = 0;
                if (j != null)
                {
                    try
                    {
                        var root = _cjs.DeserializeObject(j) as Dictionary<string, object>;
                        if (root != null && root.ContainsKey("conversations") && root["conversations"] is object[])
                        {
                            foreach (var o in (object[])root["conversations"])
                            {
                                var d = o as Dictionary<string, object>;
                                if (d == null) continue;
                                string url = SS(d, "url"); string title = SS(d, "title");
                                if (string.IsNullOrEmpty(url)) continue;
                                orphans.Add(new Conversation { ConvUrl = url, Title = title, Source = "copilot", Ts = 0 });
                                n++;
                            }
                        }
                        else if (root != null && root.ContainsKey("error")) err = SS(root, "error");
                    }
                    catch (Exception ex) { err = "parse: " + ex.GetType().Name; }
                }
                int dn = n; string derr = err;
                Dispatcher.Invoke(new Action(delegate
                {
                    fetched.Clear();
                    fetched.AddRange(orphans);
                    rebuild[0]();
                    progressLbl.Text = (derr != null)
                        ? ((_lang == 0) ? ("取得失敗: " + derr) : ("Fetch failed: " + derr))
                        : ((_lang == 0) ? ("Copilotから " + dn + "件取得（橙=ローカル未登録の孤児）") : (dn + " fetched from Copilot (orange = orphans)"));
                    fetchBtn.IsEnabled = !BulkRunLive();
                }));
            }) { IsBackground = true }.Start();
        };

        delBtn.Click += delegate
        {
            var selected = new List<Conversation>();
            foreach (var cb in boxes) if (cb.IsChecked == true) selected.Add(convOf[cb]);
            if (selected.Count == 0) return;
            string confirm = (_lang == 0)
                ? (selected.Count + "件の会話を削除します。よろしいですか？")
                : ("Delete " + selected.Count + " conversation(s)?");
            var res = MessageBox.Show(win, confirm, T("manage_title"), MessageBoxButton.OKCancel, MessageBoxImage.Warning);
            if (res != MessageBoxResult.OK) return;

            // disable controls during the run
            delBtn.IsEnabled = false; selAll.IsEnabled = false; clrAll.IsEnabled = false;
            showAll.IsEnabled = false; period.IsEnabled = false; closeBtn.IsEnabled = false;

            int total = selected.Count;
            new Thread((ThreadStart)delegate
            {
                int deleted = 0, copilotFail = 0;
                var deletedUrls = new System.Collections.Generic.HashSet<string>();
                var failReasons = new Dictionary<string, int>();  // reason bucket -> count
                bool[] sidebarChanged = new bool[] { false };     // mutable closure cell (purge sidebar state once)
                for (int i = 0; i < selected.Count; i++)
                {
                    var c = selected[i];
                    int idx = i + 1;
                    Dispatcher.Invoke(new Action(delegate
                    {
                        progressLbl.Text = (_lang == 0) ? ("削除中 " + idx + "/" + total) : ("Deleting " + idx + "/" + total);
                        // local removal (mirror DeleteLocal's file delete + _all.Remove + sidebar-state purge)
                        try { var p = Path_(c.Id); if (File.Exists(p)) File.Delete(p); } catch { }
                        bool changed = _pinned.Remove(c.Id) | _archived.Remove(c.Id) | _forcedToday.Remove(c.Id);
                        if (changed) sidebarChanged[0] = true;
                        if (_renamingId == c.Id) _renamingId = null;
                        _all.Remove(c);
                        if (!string.IsNullOrEmpty(c.ConvUrl)) deletedUrls.Add(c.ConvUrl);
                        if (_conv.Id == c.Id) { _conv = new Conversation(); _messages.Children.Clear(); }
                    }));
                    deleted++;
                    if (!localOnly && !string.IsNullOrEmpty(c.ConvUrl))
                    {
                        bool ok = false; string reason = null;
                        try
                        {
                            // a Copilot-side delete (goto + menu ops + GUID-disappearance verify) can take
                            // ~40s; give it 120s so the HTTP call doesn't time out mid-delete.
                            var j = HttpGet("/delete?url=" + Uri.EscapeDataString(c.ConvUrl) + "&title=" + Uri.EscapeDataString(c.Title ?? ""), 120000);
                            ok = j != null && j.Contains("\"ok\": true");
                            if (!ok) reason = ExtractField(j, "reason") ?? ExtractField(j, "error");
                        }
                        catch (Exception ex) { reason = "timeout/http: " + ex.GetType().Name; }
                        if (!ok)
                        {
                            copilotFail++;
                            string bucket = DeleteFailBucket(reason);
                            failReasons[bucket] = (failReasons.ContainsKey(bucket) ? failReasons[bucket] : 0) + 1;
                        }
                    }
                }
                int dDeleted = deleted, dFail = copilotFail;
                string failDetail = SummarizeFailReasons(failReasons);
                Dispatcher.Invoke(new Action(delegate
                {
                    UnregisterConvs(deletedUrls);
                    if (sidebarChanged[0]) SaveSidebarState();   // persist pinned/archived/forcedToday purge once
                    if (_all.Count == 0) { _conv = new Conversation(); _all.Add(_conv); _messages.Children.Clear(); }
                    RefreshConvList();
                    rebuild[0]();
                    string summary = (_lang == 0)
                        ? (dDeleted + "件削除しました" + (dFail > 0 ? ("（Copilot側 " + dFail + "件失敗" + (failDetail.Length > 0 ? (": " + failDetail) : "") + "）") : ""))
                        : ("Deleted " + dDeleted + (dFail > 0 ? (" (" + dFail + " Copilot-side failed" + (failDetail.Length > 0 ? (": " + failDetail) : "") + ")") : ""));
                    progressLbl.Text = summary;
                    delBtn.IsEnabled = true; selAll.IsEnabled = true; clrAll.IsEnabled = true;
                    showAll.IsEnabled = true; period.IsEnabled = true; closeBtn.IsEnabled = true;
                }));
            }) { IsBackground = true }.Start();
        };

        rebuild[0]();
        win.Content = dock;
        win.ShowDialog();
    }

    // ── delete-mode banner (3 choices, like Claude Code's modes) ────────────────
    void LoadSettings()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return;
            foreach (var ln in File.ReadAllLines(SettingsFile))
            {
                int v;
                if (ln.StartsWith("deletemode=") && int.TryParse(ln.Substring(11).Trim(), out v)) _deleteMode = v;
                else if (ln.StartsWith("lang=") && int.TryParse(ln.Substring(5).Trim(), out v)) _lang = v;
                else if (ln.StartsWith("dark=")) _dark = ln.Substring(5).Trim() != "0";
                else if (ln.StartsWith("sidebar_collapsed=")) _sidebarCollapsed = ln.Substring(18).Trim() == "1";
                else if (ln.StartsWith("ui_scale="))
                {
                    string sv = ln.Substring(9).Trim();
                    if (sv.Equals("auto", StringComparison.OrdinalIgnoreCase))
                    { _uiAuto = true; _uiScaleLoaded = true; }
                    else
                    {
                        double d;
                        if (double.TryParse(sv, System.Globalization.NumberStyles.Float,
                                            System.Globalization.CultureInfo.InvariantCulture, out d))
                        { _uiAuto = false; _uiScale = ClampScale(d); _uiScaleLoaded = true; }
                    }
                }
                else if (ln.StartsWith("ui_scale_target="))
                {
                    double ut;
                    if (double.TryParse(ln.Substring(16).Trim(), System.Globalization.NumberStyles.Float,
                                        System.Globalization.CultureInfo.InvariantCulture, out ut))
                    { _scaleTarget = System.Math.Max(0.8, System.Math.Min(3.0, ut)); _scaleTargetLoaded = true; }
                }
            }
            ApplyTheme();     // _dark may have changed -> re-apply (shared with the cockpit)
        }
        catch { }
    }
    // Shared with the cockpit -> preserve the 'dark' key and write all three.
    void SaveSettings()
    {
        // settings.txt is SHARED with the cockpit, which stores many more keys (autoscale, maxtabs,
        // disk/ram floor, effort, approval, autoscale_per_tab_mb, ...). A full overwrite here would
        // wipe all of them -> every fleet setting resets on the next launch. So preserve every other
        // line and update only our three. No BOM (a BOM breaks the python-side reader, cf. .env rule).
        try
        {
            var want = new Dictionary<string, string> {
                { "deletemode", _deleteMode.ToString() },
                { "lang", _lang.ToString() },
                { "dark", _dark ? "1" : "0" },
                { "sidebar_collapsed", _sidebarCollapsed ? "1" : "0" },
                // ui_scale holds the literal "auto" in auto mode, else the fixed number (manual).
                { "ui_scale", _uiAuto ? "auto" : _uiScale.ToString("0.00", System.Globalization.CultureInfo.InvariantCulture) },
                { "ui_scale_target", _scaleTarget.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture) },
            };
            var lines = new List<string>();
            var seen = new HashSet<string>();
            if (File.Exists(SettingsFile))
                foreach (string ln in File.ReadAllLines(SettingsFile))
                {
                    int eq = ln.IndexOf('=');
                    string k = eq > 0 ? ln.Substring(0, eq) : null;
                    if (k != null && want.ContainsKey(k)) { lines.Add(k + "=" + want[k]); seen.Add(k); }
                    else lines.Add(ln);
                }
            foreach (var kv in want) if (!seen.Contains(kv.Key)) lines.Add(kv.Key + "=" + kv.Value);
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile));
            File.WriteAllText(SettingsFile, string.Join("\n", lines.ToArray()) + "\n", new UTF8Encoding(false));
        }
        catch { }
    }
    void UpdateChrome()
    {
        _newBtn.Content = T("newchat_btn"); _send.Content = T("send");
        if (_manageBtn != null) _manageBtn.Content = T("manage_btn");
        // Iconized footer (Wave 2): icons don't hold text, so re-localize their TOOLTIPS and refresh
        // the theme glyph on a language toggle (the glyph itself is state- not language-driven).
        if (_themeBtn != null) { _themeBtn.Content = MakeIcon(_dark ? "light_mode" : "dark_mode", 18); _themeBtn.ToolTip = T("tip_theme"); }
        if (_langBtn != null) _langBtn.ToolTip = T("tip_lang");
        if (_cockpitBtn != null) _cockpitBtn.ToolTip = T("tip_cockpit");
        if (_dotHit != null) _dotHit.ToolTip = T("dot_click_tip");   // header dot tooltip follows the language
        if (_attachBtn != null) _attachBtn.ToolTip = T("attach");   // "+" glyph is language-agnostic; re-localize tooltip only
        // Re-derive composer placeholder + steer destination indicator in the current language.
        // Routing through RefreshSteerVisual keeps the steer placeholder ("…割り込みを送信…")
        // from being clobbered by the normal placeholder during a language toggle.
        RefreshSteerVisual();
    }
    void HideBanner() { _banner.Visibility = Visibility.Collapsed; _signinBannerShown = false; }

    void ShowDeleteBanner(Conversation c)
    {
        _signinBannerShown = false;   // this is the delete banner, not the sign-in banner
        _bannerBody.Children.Clear();
        var raw = c.Untitled() ? T("newchat") : c.Title;
        var title = raw.Length > 24 ? raw.Substring(0, 24) + "…" : raw;
        var head = new TextBlock { Text = (_lang == 0 ? "「" + title + "」" : "\"" + title + "\"") + T("del_head"), FontWeight = FontWeights.SemiBold, FontSize = 13.5, Margin = new Thickness(0, 0, 0, 10), TextWrapping = TextWrapping.Wrap };
        SetRef(head, TextBlock.ForegroundProperty, "Fg");
        _bannerBody.Children.Add(head);
        _bannerBody.Children.Add(ModeButton(c, 1, T("m1t"), T("m1s")));
        _bannerBody.Children.Add(ModeButton(c, 2, T("m2t"), T("m2s")));
        _bannerBody.Children.Add(ModeButton(c, 3, T("m3t"), T("m3s")));
        var foot = new DockPanel { Margin = new Thickness(0, 10, 0, 0) };
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
        var b = new Button { Content = sp, HorizontalContentAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 3, 0, 3), Padding = new Thickness(11, 8, 11, 9), BorderThickness = new Thickness(1), Cursor = Cursors.Hand };
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
            if (!string.IsNullOrEmpty(url)) new Thread((ThreadStart)delegate { try { HttpGet("/switch?url=" + Uri.EscapeDataString(url)); } catch { } }) { IsBackground = true }.Start();
            Toast(T("t_open"));
            return;
        }
        // mode 3: best-effort auto, fall back to "open in Copilot"
        if (string.IsNullOrEmpty(url)) { Toast(T("t_nourl")); return; }
        new Thread((ThreadStart)delegate
        {
            bool ok = false;
            try { var j = HttpGet("/delete?url=" + Uri.EscapeDataString(url) + "&title=" + Uri.EscapeDataString(title)); ok = j != null && j.Contains("\"ok\": true"); } catch { }
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (ok) Toast(T("t_auto_ok"));
                else
                {
                    Toast(T("t_auto_fail"));
                    new Thread((ThreadStart)delegate { try { HttpGet("/switch?url=" + Uri.EscapeDataString(url)); } catch { } }) { IsBackground = true }.Start();
                }
            }));
        }) { IsBackground = true }.Start();
    }

    void Toast(string text)
    {
        var tb = new TextBlock { Text = text, FontSize = 12, TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 10, 0, 4), TextWrapping = TextWrapping.Wrap };
        SetRef(tb, TextBlock.ForegroundProperty, "Muted");
        _messages.Children.Add(tb);
        StickToEnd();
    }

    // ── message rendering (Claude-like: user bubble, assistant plain) ───────────
    void AddUser(string text)
    {
        RemoveEmptyState();   // first real message -> the empty state must go
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false, FontFamily = new FontFamily("Segoe UI Variable, Segoe UI"), FontSize = 14
        };
        SetRef(tb, ForegroundProperty, "Fg");
        var bubble = new Border { Child = tb, CornerRadius = new CornerRadius(14), Padding = new Thickness(14, 11, 14, 11), Margin = new Thickness(40, 6, 0, 24), HorizontalAlignment = HorizontalAlignment.Right, MaxWidth = 560 };
        SetRef(bubble, BackgroundProperty, "UserBg");
        _messages.Children.Add(bubble);
        StickToEnd();
    }

    // assistant: small "Copilot" label (+ hover copy button) + full-width plain text
    // (no bubble). Returns the content panel so a live turn can swap dots -> text; also
    // hands back the outer block via 'outer' so callers can stash the message text on
    // its .Tag for the copy button to read at click time.
    Panel AddAssistantContainer(out StackPanel outer)
    {
        RemoveEmptyState();   // an assistant turn is real content -> clear the empty state
        var block = new StackPanel { Margin = new Thickness(0, 6, 40, 24) };
        // header row: "Copilot" label on the left, hover-revealed copy button on the right
        var header = new DockPanel { Margin = new Thickness(0, 0, 0, 7) };
        var lbl = new TextBlock { Text = "Copilot", FontSize = 11, Margin = new Thickness(2, 0, 0, 0), FontWeight = FontWeights.Normal, VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Faint");
        var blockRef = block;
        var copy = new Button
        {
            Content = "\uE8C8", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 12,
            Width = 26, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            Cursor = Cursors.Hand, Visibility = Visibility.Hidden, ToolTip = T("copy")
        };
        SetRef(copy, ForegroundProperty, "Muted");
        copy.Click += delegate
        {
            var txt = blockRef.Tag as string;
            if (txt == null) txt = "";
            try { System.Windows.Clipboard.SetText(txt); } catch { }
        };
        DockPanel.SetDock(copy, Dock.Right);
        header.Children.Add(copy); header.Children.Add(lbl);
        var content = new StackPanel { Margin = new Thickness(2, 0, 0, 0) };
        block.Children.Add(header); block.Children.Add(content);
        var copyRef = copy;
        block.MouseEnter += delegate { copyRef.Visibility = Visibility.Visible; };
        block.MouseLeave += delegate { copyRef.Visibility = Visibility.Hidden; };
        _messages.Children.Add(block);
        StickToEnd();
        outer = block;
        return content;
    }

    // convenience overload for callers that only need the content panel
    Panel AddAssistantContainer() { StackPanel ignore; return AddAssistantContainer(out ignore); }

    void AddAssistant(string text)
    {
        StackPanel outer;
        var content = AddAssistantContainer(out outer);
        RenderAssistantBody(content, outer, text);
    }

    // Render the settled assistant answer as a SELECTABLE read-only RichTextBox.
    // TextBlock is not selectable; plain TextBox does not support LineHeight/paragraph spacing.
    // RichTextBox + FlowDocument solves both: text is selectable and line-height is honored.
    // Streaming still uses a plain TextBox (MakeText / _pendingText); on stream completion
    // RenderAssistantBody is called to replace it with the richer layout — no flicker during
    // streaming, rich rendering for the settled message.
    // The copy button still copies the full original markdown via outer.Tag.
    void RenderAssistantBody(Panel content, StackPanel outer, string text)
    {
        content.Children.Clear();
        if (outer != null) outer.Tag = text;

        var rtb = new RichTextBox();
        rtb.IsReadOnly = true;
        rtb.IsTabStop = false;
        rtb.IsDocumentEnabled = false;
        rtb.BorderThickness = new Thickness(0);
        rtb.Background = Brushes.Transparent;
        rtb.Padding = new Thickness(0);
        rtb.Focusable = true;   // required for text selection to work
        rtb.HorizontalAlignment = HorizontalAlignment.Stretch;
        rtb.FontFamily = new FontFamily("Segoe UI Variable, Segoe UI");
        rtb.FontSize = 14;
        // Disable scrollbars so the RichTextBox auto-sizes to its content height
        // instead of clipping to a fixed viewport.
        ScrollViewer.SetVerticalScrollBarVisibility(rtb, ScrollBarVisibility.Disabled);
        ScrollViewer.SetHorizontalScrollBarVisibility(rtb, ScrollBarVisibility.Disabled);
        rtb.VerticalScrollBarVisibility = ScrollBarVisibility.Disabled;
        rtb.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled;
        SetRef(rtb, ForegroundProperty, "Fg");
        SetRef(rtb, TextBoxBase.SelectionBrushProperty, "Accent");

        // Build a FlowDocument from the plain text.
        // Paragraphs are separated by one-or-more blank lines; within a paragraph,
        // single newlines become soft line breaks (LineBreak inlines).
        var doc = new FlowDocument();
        doc.PagePadding = new Thickness(0);
        doc.FontFamily = rtb.FontFamily;
        doc.FontSize = 14;
        // Ensure the document foreground picks up the theme color.
        // FlowDocument is DependencyObject but not FrameworkElement, so call
        // SetResourceReference directly rather than through the SetRef helper.
        doc.SetResourceReference(FlowDocument.ForegroundProperty, "Fg");

        string plain = PlainText(text);

        // Split on runs of 2+ newlines to identify paragraph boundaries.
        // We do this manually without Regex (C# 5 compatible, no extra imports).
        var paragraphBlocks = new List<string>();
        var lines = plain.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
        var blockLines = new List<string>();
        for (int i = 0; i < lines.Length; i++)
        {
            if (lines[i].Length == 0)
            {
                // blank line: flush current block if non-empty
                if (blockLines.Count > 0)
                {
                    paragraphBlocks.Add(string.Join("\n", blockLines.ToArray()));
                    blockLines.Clear();
                }
            }
            else
            {
                blockLines.Add(lines[i]);
            }
        }
        // flush any trailing block
        if (blockLines.Count > 0)
            paragraphBlocks.Add(string.Join("\n", blockLines.ToArray()));

        if (paragraphBlocks.Count == 0)
        {
            // Empty text: add a single empty paragraph to avoid an empty-document edge case.
            doc.Blocks.Add(new Paragraph());
        }
        else
        {
            for (int pi = 0; pi < paragraphBlocks.Count; pi++)
            {
                bool isLast = (pi == paragraphBlocks.Count - 1);
                var para = new Paragraph();
                para.LineHeight = 22;
                para.LineStackingStrategy = LineStackingStrategy.BlockLineHeight;
                // Paragraph bottom margin: small on last to avoid excess trailing space.
                para.Margin = isLast ? new Thickness(0, 0, 0, 2) : new Thickness(0, 0, 0, 10);

                // Within a block, split on "\n" for soft line breaks.
                string[] segments = paragraphBlocks[pi].Split('\n');
                for (int si = 0; si < segments.Length; si++)
                {
                    para.Inlines.Add(new Run(segments[si]));
                    if (si < segments.Length - 1)
                        para.Inlines.Add(new LineBreak());
                }
                doc.Blocks.Add(para);
            }
        }

        rtb.Document = doc;
        content.Children.Add(rtb);
    }

    static string PlainText(string md)
    {
        if (md == null) return "";
        var sb = new StringBuilder();
        string[] lines = md.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
        foreach (var raw in lines)
        {
            string ln = raw;
            int h = 0; while (h < ln.Length && ln[h] == '#') h++;
            if (h > 0 && h < ln.Length && ln[h] == ' ') ln = ln.Substring(h + 1);   // heading -> plain
            ln = ln.Replace("**", "").Replace("`", "");                              // drop bold/code markers
            sb.Append(ln).Append('\n');
        }
        return sb.ToString().TrimEnd('\n');
    }

    TextBox MakeText(string text)
    {
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false, FontFamily = new FontFamily("Segoe UI"), FontSize = 14, Padding = new Thickness(2, 0, 0, 0)
        };
        SetRef(tb, ForegroundProperty, "Fg");
        return tb;
    }

    // ShuttleScope chat loader: three slate dots, staggered animate-bounce.
    FrameworkElement MakeTyping()
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(2, 4, 0, 4) };
        for (int i = 0; i < 3; i++)
        {
            var dot = new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4), Margin = new Thickness(0, 0, 5, 0), VerticalAlignment = VerticalAlignment.Center };
            SetRef(dot, BackgroundProperty, "Muted");
            var tt = new TranslateTransform();
            dot.RenderTransform = tt;
            var anim = new DoubleAnimation
            {
                From = 3, To = -4, Duration = new Duration(TimeSpan.FromMilliseconds(420)),
                AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever,
                BeginTime = TimeSpan.FromMilliseconds(i * 150), EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut }
            };
            tt.BeginAnimation(TranslateTransform.YProperty, anim);
            row.Children.Add(dot);
        }
        var lbl = new TextBlock { Text = T("generating"), FontSize = 11.5, Margin = new Thickness(6, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Faint");
        row.Children.Add(lbl);
        return row;
    }

    // ── send / stream ───────────────────────────────────────────────────────────
    void DoSend()
    {
        var text = _input.Text.Trim();
        if (text.Length == 0 || !_send.IsEnabled) return;
        if (text.Equals("/help", StringComparison.OrdinalIgnoreCase))
        {
            _input.Clear();
            AddUser(text);
            AddAssistant(CommandHelpText());
            return;
        }

        // #4: new-chat fallback -- nothing to send into yet, so start a fresh conversation first
        // (also seeds _pageConv once /new succeeds).
        if (_conv == null) NewChat();

        // #4: bridge-reachability fallback -- re-probe once synchronously before refusing the send,
        // since _bridgeReachable is only updated by the low-cadence background probe and may be stale.
        if (!_bridgeReachable)
        {
            bool ok;
            try { HttpGet("/conv", 5000); ok = true; } catch { ok = false; }
            _bridgeReachable = ok;
            if (!ok)
            {
                SetDot("offline");
                AddAssistant(T("send_offline"));
                _input.Text = text;   // put the trimmed text back so it isn't lost
                _input.CaretIndex = _input.Text.Length;
                // One-click recovery: bring the whole stack (bridge + relay) back up instead of
                // leaving the user to go find a terminal. Idempotent -- safe even if some of the
                // stack is already running.
                ShowRecoveryBanner(T("send_offline"), T("retry_start_stack"), delegate
                {
                    HideBanner();
                    try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(Path.Combine(RepoRoot(), "start_all.bat")) { UseShellExecute = true }); }
                    catch { }
                });
                return;
            }
            RefreshIdleDot();
        }

        // #3: while a fleet is at capacity, a native send would open a 4th heavy tab
        // and blow the memory budget -> route it into the fleet queue instead. Prefix
        // "!" forces priority (jumps the queue). Slash-commands are never rerouted.
        int[] fs = FleetState();
        if (fs[0] == 1 && fs[2] > 0 && fs[1] >= fs[2] && !text.StartsWith("/"))
        {
            bool force = text.StartsWith("!");
            string body = force ? text.Substring(1).Trim() : text;
            if (body.Length == 0) return;
            _input.Clear(); HideRouter();
            EnqueueToFleet(body, force);
            AddUser(text);
            AddAssistant(force ? T("fleet_forced") : T("fleet_queued"));
            return;
        }

        // #2: research-intent auto-router -- propose the researcher (confirm, not auto,
        // to avoid false positives), the way Claude Code surfaces a tool.
        if (!text.StartsWith("/") && !_routerShown && DetectResearch(text))
        {
            ShowRouter(text);
            return;
        }
        HideRouter();
        _input.Clear();
        SendText(text);
    }

    void SendText(string text)
    {
        // Snapshot the conversation this send targets ONCE, up front. Everything below (and
        // everything in Stream) must operate on `target`, never re-read the shared `_conv` field --
        // a fleet-card open landing mid-send must not be able to redirect this reply elsewhere.
        Conversation target = _conv;

        // ── page pinning: make sure the bridge page actually shows `target` before we send ──
        if (!ReferenceEquals(target, _pageConv))
        {
            if (!string.IsNullOrEmpty(target.ConvUrl))
            {
                try { HttpGet("/switch?url=" + Uri.EscapeDataString(target.ConvUrl), 15000); _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else if (target.Messages.Count == 0)
            {
                try { HttpGet("/new", 15000); _pageConv = target; }
                catch { AddAssistant(T("send_wrong_page")); return; }
            }
            else
            {
                AddAssistant(T("send_unknown_conv"));
                return;
            }
        }

        _sendInFlight = true;
        target.Messages.Add(new Msg("U", text));
        if (target.Untitled()) { target.Title = TrimTitle(text, 40); }   // ITEM 3a: first line, max 40 + ellipsis
        if (!_all.Contains(target)) { _all.Insert(0, target); }
        RefreshConvList();
        AddUser(text);
        StackPanel outer;
        _pendingContent = AddAssistantContainer(out outer);
        _pendingOuter = outer;
        _pendingContent.Children.Add(MakeTyping());   // <- ShuttleScope waiting indicator, shown immediately
        _pendingText = null; _started = false;
        _generating = true; _send.Content = "■ " + T("stop"); _send.IsEnabled = true;   // distinct from Send; _send now acts as Stop (also Esc)
        PaintSend();   // stay accent while generating
        SetDot("busy");
        new Thread((ThreadStart)delegate { Stream(text, target); }) { IsBackground = true }.Start();
        ClearChips();   // the attached file(s) go with this message; reset the chip row
    }

    // ── #3 fleet-aware routing ───────────────────────────────────────────────────
    // returns [running(0/1), openTabs, maxConcurrent]
    int[] FleetState()
    {
        try
        {
            string sp = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet", "status.json"));
            if (!File.Exists(sp)) return new int[] { 0, 0, 0 };
            string txt;
            using (var fsr = new FileStream(sp, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fsr, Encoding.UTF8)) txt = sr.ReadToEnd();
            var d = _cjs.DeserializeObject(txt) as Dictionary<string, object>;
            if (d == null) return new int[] { 0, 0, 0 };
            bool running = d.ContainsKey("running") && Convert.ToBoolean(d["running"]);
            bool idle = d.ContainsKey("idle") && Convert.ToBoolean(d["idle"]);
            int open = d.ContainsKey("open_tabs") && d["open_tabs"] != null ? Convert.ToInt32(d["open_tabs"]) : 0;
            int maxc = d.ContainsKey("max_concurrent") && d["max_concurrent"] != null ? Convert.ToInt32(d["max_concurrent"]) : 0;
            return new int[] { (running && !idle) ? 1 : 0, open, maxc };
        }
        catch { return new int[] { 0, 0, 0 }; }
    }

    void EnqueueToFleet(string text, bool priority)
    {
        try
        {
            string cp = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet", "commands.json"));
            var cmd = new Dictionary<string, object>();
            if (File.Exists(cp))
            {
                try { var ex = _cjs.DeserializeObject(File.ReadAllText(cp, Encoding.UTF8)) as Dictionary<string, object>; if (ex != null) cmd = ex; } catch { }
            }
            var adds = new List<object>();
            if (cmd.ContainsKey("add_goal") && cmd["add_goal"] is object[]) foreach (var o in (object[])cmd["add_goal"]) adds.Add(o);
            var item = new Dictionary<string, object>(); item["text"] = text; item["priority"] = priority;
            adds.Add(item);
            cmd["add_goal"] = adds;
            File.WriteAllText(cp, _cjs.Serialize(cmd), Encoding.UTF8);
        }
        catch { }
    }

    // ── #2 research-intent detection + confirm bar ───────────────────────────────
    static readonly string[] _researchHints = {
        "調査", "調べて", "深掘り", "リサーチ", "最新情報", "出典", "比較して", "下調べ",
        "research", "investigate", "look up", "deep dive", "find out", "compare "
    };
    bool DetectResearch(string msg)
    {
        string m = msg.ToLower();
        foreach (var h in _researchHints) if (m.Contains(h.ToLower())) return true;
        return false;
    }

    Border _routerBar; bool _routerShown; string _routerText = "";
    Button _routerResearch, _routerNormal; TextBlock _routerLbl;

    UIElement BuildRouterBar()
    {
        _routerBar = new Border { Visibility = Visibility.Collapsed, CornerRadius = new CornerRadius(10), BorderThickness = new Thickness(1), Padding = new Thickness(12, 8, 10, 8), Margin = new Thickness(0, 0, 0, 8) };
        SetRef(_routerBar, BackgroundProperty, "Panel"); SetRef(_routerBar, Border.BorderBrushProperty, "Accent");
        var dp = new DockPanel();
        var btns = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        DockPanel.SetDock(btns, Dock.Right);
        _routerResearch = Btn("", "Accent", "AccentFg", false);
        _routerResearch.Padding = new Thickness(12, 3, 12, 3); _routerResearch.FontSize = 12; _routerResearch.FontWeight = FontWeights.SemiBold;
        _routerResearch.Click += delegate { var t = _routerText; HideRouter(); _input.Clear(); SendText("/research " + t); };
        _routerNormal = Btn("", "Panel", "Muted", true);
        _routerNormal.Padding = new Thickness(12, 3, 12, 3); _routerNormal.FontSize = 12; _routerNormal.Margin = new Thickness(8, 0, 0, 0);
        _routerNormal.Click += delegate { var t = _routerText; HideRouter(); _input.Clear(); SendText(t); };
        btns.Children.Add(_routerResearch); btns.Children.Add(_routerNormal);
        dp.Children.Add(btns);
        _routerLbl = new TextBlock { VerticalAlignment = VerticalAlignment.Center, FontSize = 12.5, TextWrapping = TextWrapping.Wrap };
        SetRef(_routerLbl, TextBlock.ForegroundProperty, "Fg");
        dp.Children.Add(_routerLbl);
        _routerBar.Child = dp;
        return _routerBar;
    }
    void ShowRouter(string text)
    {
        _routerText = text;
        _routerLbl.Text = T("router_q");
        _routerResearch.Content = T("router_research");
        _routerNormal.Content = T("router_normal");
        _routerBar.Visibility = Visibility.Visible;
        _routerShown = true;
    }
    void HideRouter()
    {
        if (_routerBar != null) _routerBar.Visibility = Visibility.Collapsed;
        _routerShown = false;
    }

    // ── attachments: file picker + image paste -> upload to the composer ─────────
    StackPanel _attachChips;
    // The attached-file CHIP strip only. The attach BUTTON itself now lives in the composer footer
    // (ITEM 4); this panel holds the file chips added by AddChip and is otherwise empty.
    UIElement BuildAttachRow()
    {
        _attachChips = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 6) };
        return _attachChips;
    }

    void AttachFile()
    {
        var dlg = new Microsoft.Win32.OpenFileDialog();
        dlg.Filter = (_lang == 0 ? "対応ファイル" : "Supported files") + "|*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp;*.pdf;*.docx;*.xlsx;*.pptx;*.csv;*.txt;*.md;*.json;*.xml;*.py;*.js;*.ts;*.cs;*.html;*.eml|" + (_lang == 0 ? "すべて (*.*)" : "All (*.*)") + "|*.*";
        if (dlg.ShowDialog() == true) UploadFile(dlg.FileName);
    }

    void PasteImage()
    {
        try
        {
            var img = Clipboard.GetImage();
            if (img == null) return;
            string path = Path.Combine(Path.GetTempPath(), "copilot_paste_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".png");
            using (var fs = new FileStream(path, FileMode.Create))
            {
                var enc = new PngBitmapEncoder();
                enc.Frames.Add(BitmapFrame.Create(img));
                enc.Save(fs);
            }
            UploadFile(path);
        }
        catch { }
    }

    void UploadFile(string path)
    {
        var chip = AddChip(Path.GetFileName(path), true);
        new Thread((ThreadStart)delegate
        {
            string r = null;
            try { r = HttpGet("/upload?path=" + Uri.EscapeDataString(path)); } catch { }
            bool ok = r != null && r.Contains("\"ok\": true");
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (ok) { var t = chip.Child as TextBlock; if (t != null) t.Text = "" + Path.GetFileName(path); }
                else { _attachChips.Children.Remove(chip); AddAssistant(T("attach_fail") + " " + Path.GetFileName(path)); }
            }));
        }) { IsBackground = true }.Start();
    }

    Border AddChip(string name, bool pending)
    {
        var b = new Border { CornerRadius = new CornerRadius(7), Padding = new Thickness(8, 3, 8, 3), Margin = new Thickness(6, 0, 0, 0), BorderThickness = new Thickness(1) };
        SetRef(b, BackgroundProperty, "Panel"); SetRef(b, Border.BorderBrushProperty, "Border");
        var t = new TextBlock { Text = (pending ? "… " : "") + name, FontSize = 12 };
        SetRef(t, TextBlock.ForegroundProperty, "Fg");
        b.Child = t;
        _attachChips.Children.Add(b);
        return b;
    }

    void ClearChips()
    {
        if (_attachChips == null) return;
        // The attach button no longer sits in this panel (ITEM 4) — it holds only file chips now,
        // so clear ALL of them (previously index 0 was the button and was preserved).
        _attachChips.Children.Clear();
    }

    void Stream(string msg, Conversation target)
    {
        var full = new StringBuilder();
        var content = _pendingContent;
        var outer = _pendingOuter;
        string errMsg = null;
        try
        {
            var url = _bridge + "/stream?msg=" + Uri.EscapeDataString(msg);
            var req = (HttpWebRequest)WebRequest.Create(url);
            // /review and /security-review run a full-repo fleet pass that can exceed the
            // default 10-minute cap -- give those two commands a 60-minute window instead.
            string msgTrim = (msg ?? "").TrimStart();
            bool isLongReview = msgTrim.StartsWith("/review", StringComparison.OrdinalIgnoreCase)
                || msgTrim.StartsWith("/security-review", StringComparison.OrdinalIgnoreCase);
            int reqTimeoutMs = isLongReview ? 3600000 : 600000;
            req.Timeout = reqTimeoutMs; req.ReadWriteTimeout = reqTimeoutMs;
            _activeReq = req;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            {
                string line; bool done = false;
                while (!done && (line = sr.ReadLine()) != null)
                {
                    if (line.StartsWith("event: done")) done = true;
                    else if (line.StartsWith("data:"))
                    {
                        var jsonData = line.Substring(5).Trim();
                        var d = ExtractField(jsonData, "delta");
                        if (!string.IsNullOrEmpty(d))
                        {
                            full.Append(d);
                            Dispatcher.BeginInvoke(new Action(delegate
                            {
                                // Only touch the visible pending bubble while `target` is still the
                                // conversation on screen -- if the user has switched away, the reply
                                // is still being accumulated into `full` and will land in `target`'s
                                // saved messages at completion, just not painted here.
                                if (!ReferenceEquals(_conv, target)) return;
                                if (!_started) { _started = true; content.Children.Clear(); _pendingText = MakeText(""); content.Children.Add(_pendingText); }
                                _pendingText.AppendText(d); StickToEnd();
                            }));
                        }
                        var rep = ExtractField(jsonData, "replace");
                        if (!string.IsNullOrEmpty(rep))
                        {
                            full.Clear(); full.Append(rep);
                            var repCopy = rep;
                            Dispatcher.BeginInvoke(new Action(delegate
                            {
                                if (!ReferenceEquals(_conv, target)) return;
                                if (_pendingText != null) { _pendingText.Text = repCopy; StickToEnd(); }
                            }));
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            // An aborted request (Stop) throws here; keep whatever streamed so far.
            // Only surface a bridge error when it wasn't a deliberate abort.
            bool aborted = ex is WebException && ((WebException)ex).Status == WebExceptionStatus.RequestCanceled;
            if (!aborted && full.Length == 0) errMsg = ex.Message;
        }
        _activeReq = null;
        var answer = full.ToString();
        var errFinal = errMsg;
        Dispatcher.BeginInvoke(new Action(delegate
        {
            _sendInFlight = false;   // unconditionally, for both success and error paths -- never leave this wedged true
            bool visible = ReferenceEquals(_conv, target);
            if (visible)
            {
                _generating = false; _send.Content = T("send"); _input.Focus();
                PaintSend();   // revert to neutral (input was cleared before send); also re-enables/disables
                // render whatever we got (full / partial / error); always clear the typing indicator
                content.Children.Clear();
                if (answer.Length > 0) { RenderAssistantBody(content, outer, answer); StickToEnd(); }
                else if (errFinal != null) { content.Children.Add(MakeText("[bridge error: " + errFinal + "]")); if (outer != null) outer.Tag = errFinal; }
            }
            // Data must land in the right conversation's saved file even when it is not the one
            // currently shown -- these run unconditionally, keyed on `target`, never on `_conv`.
            target.Messages.Add(new Msg("A", answer));
            SaveConversation(target);
            // ── status dot outcome (reflects the VISIBLE conversation only) ──────────────
            if (!visible) return;
            if (errFinal != null)
            {
                // A stream error means the bridge (or its Copilot tab) is not answering.
                _bridgeReachable = false;
                SetDot("offline");
            }
            else if (LooksLikeRefusal(answer))
            {
                // Copilot answered but with a sign-in / canned refusal -> Warning + actionable banner.
                _signinLatch = true;
                SetDot("signin");
                ShowSigninBanner();
            }
            else
            {
                // Clean answer clears any latched sign-in warning and the sign-in banner
                // (but never a delete banner the user may have opened).
                _signinLatch = false;
                if (_signinBannerShown) HideBanner();
                _bridgeReachable = true;
                RefreshIdleDot();
            }
        }));
        try
        {
            var j = HttpGet("/conv");
            var u = ExtractField(j, "url");
            if (!string.IsNullOrEmpty(u))
            {
                target.ConvUrl = u;
                Dispatcher.BeginInvoke(new Action(delegate { SaveConversation(target); RegisterConv(u, target.Title, "chat"); }));
            }
        }
        catch { }
    }

    // ── persistence (manual base64 store) ───────────────────────────────────────
    string Path_(string id) { return Path.Combine(StoreDir, id + ".chat"); }
    static string B64(string s) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(s == null ? "" : s)); }
    static string UnB64(string s) { try { return Encoding.UTF8.GetString(Convert.FromBase64String(s)); } catch { return ""; } }

    void SaveConversation(Conversation c)
    {
        try
        {
            // bump last-activity so the recency-sorted sidebar floats this chat to the top on save
            c.Ts = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            Directory.CreateDirectory(StoreDir);
            var sb = new StringBuilder();
            sb.Append("CONV\t").Append(c.ConvUrl == null ? "" : c.ConvUrl).Append('\n');
            sb.Append("TITLE\t").Append(B64(c.Title)).Append('\n');
            foreach (var m in c.Messages) sb.Append(m.Role).Append('\t').Append(B64(m.Text)).Append('\n');
            File.WriteAllText(Path_(c.Id), sb.ToString(), Encoding.UTF8);
        }
        catch { }
    }

    void LoadConversations()
    {
        _all = new List<Conversation>();
        try
        {
            if (Directory.Exists(StoreDir))
            {
                var files = new List<string>(Directory.GetFiles(StoreDir, "*.chat"));
                files.Sort(delegate (string a, string b) { return File.GetLastWriteTime(b).CompareTo(File.GetLastWriteTime(a)); });
                foreach (var f in files)
                {
                    var c = new Conversation { Id = Path.GetFileNameWithoutExtension(f), Messages = new List<Msg>() };
                    // stamp last-activity from the file mtime so the sidebar can sort by RECENCY
                    // (newest first) rather than alphabetically. Fleet/registry convs already carry Ts.
                    try { c.Ts = (File.GetLastWriteTimeUtc(f) - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }
                    catch { c.Ts = 0; }
                    foreach (var ln in File.ReadAllLines(f, Encoding.UTF8))
                    {
                        var tab = ln.IndexOf('\t'); if (tab < 0) continue;
                        var k = ln.Substring(0, tab); var v = ln.Substring(tab + 1);
                        if (k == "CONV") c.ConvUrl = v;
                        else if (k == "TITLE") c.Title = UnB64(v);
                        else if (k == "U" || k == "A") c.Messages.Add(new Msg(k, UnB64(v)));
                    }
                    _all.Add(c);
                }
            }
        }
        catch { }
        DiscoverTranscripts();   // surface every fleet worker's disk transcript as a past chat
        if (_all.Count > 0) { _conv = _all[0]; foreach (var m in _conv.Messages) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); } }
        else { _conv = new Conversation(); _all.Add(_conv); }
        ShowEmptyState();        // no-op if the active conversation rendered any real message
        RefreshConvList();
    }

    // Surface every fleet worker's disk transcript as a sidebar conversation, so PAST chats are
    // browsable straight from disk. The old path re-scraped live via the bridge, which fails for any
    // conversation whose agent the bridge is not on -> not one past chat was retrievable. Newest
    // first, capped so a huge dir doesn't flood the list; dedup by transcript path; sub-agent
    // (research) child transcripts are skipped (they nest under their parent on open).
    void DiscoverTranscripts()
    {
        try
        {
            string tdir = Path.Combine(Path.GetDirectoryName(_convsPath), "transcripts");
            if (!Directory.Exists(tdir)) return;
            var files = new List<string>(Directory.GetFiles(tdir, "*.jsonl"));
            files.Sort(delegate (string a, string b) { return File.GetLastWriteTimeUtc(b).CompareTo(File.GetLastWriteTimeUtc(a)); });
            int budget = 80;
            foreach (var f in files)
            {
                if (budget-- <= 0) break;
                if (f.IndexOf("__sub_", StringComparison.Ordinal) >= 0) continue;   // research children
                bool exists = false;
                foreach (var c in _all) if (c.Transcript == f) { exists = true; break; }
                if (exists) continue;
                string goal = "", name = "";
                try
                {
                    using (var sr = new StreamReader(f, Encoding.UTF8))
                    {
                        string first = sr.ReadLine();
                        if (!string.IsNullOrEmpty(first))
                        {
                            var meta = _cjs.DeserializeObject(first) as Dictionary<string, object>;
                            if (meta != null) { goal = SS(meta, "goal"); name = SS(meta, "name"); }
                        }
                    }
                }
                catch { }
                string title = goal.Length > 0 ? (goal.Length > 54 ? goal.Substring(0, 54) + "…" : goal)
                                               : Path.GetFileNameWithoutExtension(f);
                double ts = 0;
                try { ts = (File.GetLastWriteTimeUtc(f) - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }
                catch { }
                _all.Add(new Conversation { Transcript = f, Name = name, Title = title, Source = "fleet", Ts = ts });
            }
        }
        catch { }
    }

    string HttpGet(string path) { return HttpGet(path, 60000); }

    string HttpGet(string path, int timeoutMs)
    {
        var req = (HttpWebRequest)WebRequest.Create(_bridge + path);
        req.Timeout = timeoutMs;
        req.ReadWriteTimeout = timeoutMs;
        using (var resp = (HttpWebResponse)req.GetResponse())
        using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            return sr.ReadToEnd();
    }

    // Map a raw bridge delete reason to a short, stable bucket key for the summary.
    static string DeleteFailBucket(string reason)
    {
        if (string.IsNullOrEmpty(reason)) return "unknown";
        string r = reason.ToLowerInvariant();
        if (r.Contains("guid mismatch") || r.Contains("unreachable") || r.Contains("row absent")) return "guid mismatch";
        if (r.Contains("timeout") || r.Contains("timed out")) return "timeout";
        if (r.Contains("busy")) return "busy";
        if (r.Contains("not found") || r.Contains("not found in history")) return "not found";
        if (r.Contains("may not have applied") || r.Contains("still present")) return "unverified";
        if (r.Contains("confirm button")) return "confirm failed";
        if (r.Contains("menuitem")) return "menu failed";
        return "other";
    }

    static string SummarizeFailReasons(Dictionary<string, int> buckets)
    {
        if (buckets == null || buckets.Count == 0) return "";
        var parts = new List<string>();
        foreach (var kv in buckets) parts.Add(kv.Key + " ×" + kv.Value);
        return string.Join(", ", parts.ToArray());
    }

    static string ExtractField(string json, string field)
    {
        if (json == null) return null;
        int k = json.IndexOf("\"" + field + "\"", StringComparison.Ordinal);
        if (k < 0) return null;
        int colon = json.IndexOf(':', k);
        int q1 = json.IndexOf('"', colon + 1);
        if (q1 < 0) return null;
        var sb = new StringBuilder();
        for (int i = q1 + 1; i < json.Length; i++)
        {
            char c = json[i];
            if (c == '\\')
            {
                i++; if (i >= json.Length) break;
                char n = json[i];
                switch (n)
                {
                    case 'n': sb.Append('\n'); break; case 't': sb.Append('\t'); break;
                    case 'r': sb.Append('\r'); break; case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break; case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break; case '/': sb.Append('/'); break;
                    case 'u': if (i + 4 < json.Length) { sb.Append((char)Convert.ToInt32(json.Substring(i + 1, 4), 16)); i += 4; } break;
                    default: sb.Append(n); break;
                }
            }
            else if (c == '"') break;
            else sb.Append(c);
        }
        return sb.ToString();
    }
}
