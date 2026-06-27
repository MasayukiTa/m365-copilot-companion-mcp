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
    Conversation _conv = new Conversation();
    List<Conversation> _all = new List<Conversation>();
    string _renamingId = null;
    int _deleteMode = 1;                 // 1=local only, 2=open in Copilot, 3=auto (experimental)
    int _lang = 0;                       // 0=Japanese, 1=English
    Border _banner; StackPanel _bannerBody;
    Button _newBtn, _themeBtn, _langBtn, _manageBtn, _cockpitBtn, _attachBtn;
    TextBlock _inputHint;                 // goal-box watermark; localized -> must update on lang toggle
    static readonly string SettingsFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "copilot-bridge", "settings.txt");

    string T(string k)
    {
        bool ja = _lang == 0;
        if (k == "newchat") return ja ? "新しいチャット" : "New chat";
        if (k == "newchat_btn") return ja ? "＋   新しいチャット" : "＋   New chat";
        if (k == "send") return ja ? "送信" : "Send";
        if (k == "theme") return (_dark ? "☀" : "☾") + (ja ? "   テーマ (ダーク/ライト)" : "   Theme (dark/light)");
        if (k == "lang") return ja ? "🌐   English へ" : "🌐   日本語へ";
        if (k == "rename") return ja ? "名前を変更" : "Rename";
        if (k == "delete") return ja ? "削除" : "Delete";
        if (k == "generating") return ja ? "生成中" : "Generating";
        if (k == "cancel") return ja ? "キャンセル" : "Cancel";
        if (k == "copy") return ja ? "コピー" : "Copy";
        if (k == "stop") return ja ? "停止" : "Stop";
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
        return k;
    }

    // streaming state
    Panel _pendingContent;   // holds the typing dots, then the streamed text
    TextBox _pendingText;
    volatile bool _started;
    StackPanel _pendingOuter;            // the assistant block for the in-flight turn (Tag holds final text)
    bool _generating;                    // true while a reply is streaming; _send acts as Stop
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

        var root = new Grid();
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(260) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // ── sidebar ──
        var side = new Grid();
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        side.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        SetRef(side, BackgroundProperty, "PanelAlt");

        var headerStack = new StackPanel { Margin = new Thickness(12, 14, 12, 8) };
        _newBtn = Btn(T("newchat_btn"), "Panel", "Fg", true);
        _newBtn.Height = 40; _newBtn.Margin = new Thickness(0, 0, 0, 6); _newBtn.FontWeight = FontWeights.SemiBold;
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

        var bottom = new StackPanel { Margin = new Thickness(12, 8, 12, 12) };
        // Fleet entry is a QUIET navigation item, not a large orange CTA (spec). Accent is reserved
        // for the one primary action in the main column (Send).
        _cockpitBtn = Btn(T("open_cockpit"), "PanelAlt", "Muted", true);
        _cockpitBtn.Height = 36; _cockpitBtn.Margin = new Thickness(0, 0, 0, 8); _cockpitBtn.FontSize = 12.5;
        _cockpitBtn.Click += delegate { OpenCockpit(); };
        bottom.Children.Add(_cockpitBtn);
        _langBtn = Btn(T("lang"), "Panel", "Muted", true);
        _langBtn.Height = 34; _langBtn.Margin = new Thickness(0, 0, 0, 6); _langBtn.FontSize = 12;
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveSettings(); UpdateChrome(); RefreshConvList(); RerenderActiveConversation(); };
        _themeBtn = Btn(T("theme"), "Panel", "Muted", true);
        _themeBtn.Height = 34; _themeBtn.FontSize = 12;
        _themeBtn.Click += delegate { _dark = !_dark; ApplyTheme(); _themeBtn.Content = T("theme"); SaveSettings(); };
        bottom.Children.Add(_langBtn); bottom.Children.Add(_themeBtn);
        Grid.SetRow(bottom, 2); side.Children.Add(bottom);

        var sideBorder = new Border { Child = side, BorderThickness = new Thickness(0, 0, 1, 0) };
        SetRef(sideBorder, Border.BorderBrushProperty, "Border");
        Grid.SetColumn(sideBorder, 0); root.Children.Add(sideBorder);

        // ── main column ──
        var main = new Grid();
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        main.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var headPanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(22, 13, 22, 13) };
        _statusDot = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(5), Margin = new Thickness(0, 4, 9, 0) };
        SetRef(_statusDot, BackgroundProperty, "Border");
        var headText = new TextBlock { Text = "Copilot", FontWeight = FontWeights.SemiBold, FontSize = 14.5 };
        SetRef(headText, TextBlock.ForegroundProperty, "Fg");
        headPanel.Children.Add(_statusDot); headPanel.Children.Add(headText);
        var headBorder = new Border { Child = headPanel, BorderThickness = new Thickness(0, 0, 0, 1) };
        SetRef(headBorder, Border.BorderBrushProperty, "Border");
        Grid.SetRow(headBorder, 0); main.Children.Add(headBorder);

        _messages = new StackPanel { Margin = new Thickness(0, 8, 0, 8), MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Center };
        _scroll = new ScrollViewer { Content = _messages, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(24, 4, 24, 4) };
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

        var bar = new Grid { Margin = new Thickness(0, 10, 0, 16), MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Center };
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _input = new TextBox
        {
            MinHeight = 50, MaxHeight = 180, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, FontSize = 14, Padding = new Thickness(13, 12, 13, 12),
            BorderThickness = new Thickness(1), VerticalContentAlignment = VerticalAlignment.Center, MinWidth = 560
        };
        // Composer surface matches the Fleet composer (surfaceSubtle), so Main and Fleet read as the
        // same component (spec parity).
        SetRef(_input, BackgroundProperty, "PanelAlt");
        SetRef(_input, ForegroundProperty, "Fg");
        SetRef(_input, TextBox.CaretBrushProperty, "Fg");
        SetRef(_input, Control.BorderBrushProperty, "Border");
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
        _input.TextChanged += delegate { UpdateCmdPopup(); };
        Grid.SetColumn(_input, 0); bar.Children.Add(_input);
        // Placeholder hint advertising slash commands (WPF TextBox has no native placeholder). The
        // single most Claude-Code-defining feature was invisible until you happened to type "/".
        _inputHint = new TextBlock
        {
            Text = _lang == 0 ? "メッセージを入力 …   「/」でコマンド" : "Type a message …   \"/\" for commands",
            IsHitTestVisible = false, FontSize = 13.5, Margin = new Thickness(15, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center
        };
        SetRef(_inputHint, TextBlock.ForegroundProperty, "Muted");
        Grid.SetColumn(_inputHint, 0); bar.Children.Add(_inputHint);
        _input.TextChanged += delegate { _inputHint.Visibility = string.IsNullOrEmpty(_input.Text) ? Visibility.Visible : Visibility.Collapsed; };
        BuildCmdPopup();
        _send = Btn(T("send"), "Accent", "AccentFg", false);
        _send.Width = 92; _send.Margin = new Thickness(8, 0, 0, 0); _send.FontWeight = FontWeights.SemiBold; _send.MinHeight = 50;
        _send.Click += delegate
        {
            if (_generating) { try { if (_activeReq != null) _activeReq.Abort(); } catch { } }
            else DoSend();
        };
        Grid.SetColumn(_send, 1); bar.Children.Add(_send);
        var barStack = new StackPanel { MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Center };
        barStack.Children.Add(BuildRouterBar());
        barStack.Children.Add(BuildAttachRow());
        barStack.Children.Add(bar);
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

        LoadConversations();
        Loaded += delegate { _input.Focus(); };
        // Window-level Esc -> interrupt a streaming reply, REGARDLESS of focus. The input-level
        // handler only fires when the box is focused, but mid-stream focus is usually elsewhere, so
        // Esc was falling through to the sidebar (it navigated to "新しいチャット" instead of stopping).
        // PreviewKeyDown tunnels from the window down, so this fires first and swallows the key.
        PreviewKeyDown += delegate (object s, KeyEventArgs e)
        {
            if (e.Key == Key.Escape && _generating)
            { e.Handled = true; try { if (_activeReq != null) _activeReq.Abort(); } catch { } }
        };

        // ① watch for the cockpit asking to open a parallel-task conversation here, and
        // sync the session-shared conversation registry (fleet + chat conversations).
        string fleetDir = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet"));
        _openPath = Path.Combine(fleetDir, "open.json");
        _convsPath = Path.Combine(fleetDir, "conversations.json");
        try { _openMtime = File.Exists(_openPath) ? File.GetLastWriteTimeUtc(_openPath).Ticks : 0; }
        catch { _openMtime = 0; }
        try { _settingsMtime = File.Exists(SettingsFile) ? File.GetLastWriteTimeUtc(SettingsFile).Ticks : 0; }
        catch { _settingsMtime = 0; }
        var openTimer = new DispatcherTimer();
        openTimer.Interval = TimeSpan.FromMilliseconds(800);
        openTimer.Tick += delegate { CheckOpenRequest(); SyncRegistry(); CheckFleetSnapshot(); CheckSettings(); };
        openTimer.Start();
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
            LoadSettings();                              // re-reads lang/dark (+ApplyTheme for theme)
            if (_lang != l0) { UpdateChrome(); RefreshConvList(); RerenderActiveConversation(); }
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
            }
            catch { }
        }
        var loaded = msgs;
        // Keep the live status tail (and arm live re-render) whenever the fleet is running and we
        // have a worker dict -- so the user sees turn/verify/status update under the transcript.
        bool keepLive = running && wkr != null;
        Dispatcher.BeginInvoke(new Action(delegate
        {
            // reuse the existing sidebar entry for this conversation if present (dedup by key)
            Conversation c = null;
            foreach (var x in _all) { if (x.ConvUrl == key) { c = x; break; } }
            if (c == null) { c = new Conversation(); c.ConvUrl = key; c.Title = T("fleetview"); _all.Insert(0, c); }
            c.Messages.Clear();
            foreach (var m in loaded) c.Messages.Add(m);
            _conv = c;
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
                AddAssistant(_lang == 0
                    ? "（この会話の本文はまだ取得できません。進行中のため履歴が空の可能性があります。）"
                    : "(This conversation's transcript isn't available yet -- it may be empty while the run is in progress.)");
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
            if (_conv == null || _conv.ConvUrl != _activeFleetUrl) { _activeFleetUrl = null; return; }
            string sp = Path.Combine(Path.GetDirectoryName(_convsPath), "status.json");
            if (!File.Exists(sp)) return;
            long m = File.GetLastWriteTimeUtc(sp).Ticks;
            if (m == _statusMtime) return;
            _statusMtime = m;
            RefreshFleetSnapshot();
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

    // ── slash-command autocomplete (type "/" to see commands, like Claude Code) ──
    Popup _cmdPopup; ListBox _cmdList;
    static readonly string[][] _commandsJa = {
        new[]{"/help","コマンド一覧を表示"},
        new[]{"/research","Claude researcher で深掘り調査"},
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
    // Display-only descriptions (insert uses Tag=name), localized at access time.
    string[][] _commands { get { return _lang == 0 ? _commandsJa : _commandsEn; } }
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
        if (_lang != 0)
            return "Chat commands:\n"
                + "/help - this list\n"
                + "/research - deep research with the Claude researcher\n"
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

    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
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
        Set("Border", Theme.Border(_dark));
        Set("BorderStrong", Theme.BorderStrong(_dark));
        Set("Fg", Theme.Text(_dark));
        Set("Muted", Theme.Muted(_dark));
        Set("Faint", Theme.Faint(_dark));
        Set("UserBg", Theme.SurfaceSubtle(_dark));
        Set("Accent", Theme.Accent(_dark));
        Set("AccentSoft", Theme.AccentSoft(_dark));
        Set("AccentFg", Theme.AccentFg(_dark));
        Set("Hover", Theme.Hover(_dark));
        Set("Press", Theme.Press(_dark));
        Set("CodeBg", Theme.SurfaceSubtle(_dark));
    }

    // ── sidebar list with rename / delete ───────────────────────────────────────
    void RefreshConvList()
    {
        // Sort the sidebar by RECENCY (newest last-activity first) instead of the previous
        // alphabetical-by-title order. Every Conversation carries a Ts (unix secs): .chat convs from
        // their file mtime, fleet/registry convs from their transcript mtime, live convs bumped on
        // save/new. Stable: equal-Ts entries keep their insertion order (index tiebreak), so a Ts==0
        // conv (e.g. never-saved placeholder) preserves its position rather than jumping alphabetically.
        var idx = new Dictionary<Conversation, int>();
        for (int i = 0; i < _all.Count; i++) idx[_all[i]] = i;
        _all.Sort(delegate (Conversation a, Conversation b)
        {
            int c = b.Ts.CompareTo(a.Ts);            // descending Ts (newest first)
            return c != 0 ? c : idx[a].CompareTo(idx[b]);   // stable tiebreak on original index
        });
        _convList.Children.Clear();
        foreach (var c in _all)
        {
            var cc = c;
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
                continue;
            }
            // row = background border (active/inactive) holding [ title | hover trash ]
            var rowBorder = new Border { CornerRadius = new CornerRadius(7), Margin = new Thickness(0, 1, 0, 1) };
            SetRef(rowBorder, BackgroundProperty, cc.Id == _conv.Id ? "Panel" : "PanelAlt");
            var rowGrid = new Grid();
            rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var dt = cc.Untitled() ? T("newchat") : cc.Title;
            // Two-line wrapping label instead of a hard 26-char cut: many SWE goals share the same
            // prefix ("あなたは実在の Python ライブラリ ...") and a 1-line cut hid the distinguishing
            // library name, making rows indistinguishable. (GAP 4)
            var lbl = new TextBlock
            {
                Text = dt, TextWrapping = TextWrapping.Wrap, MaxHeight = 36,
                TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 13,
                FontWeight = cc.Id == _conv.Id ? FontWeights.SemiBold : FontWeights.Normal
            };
            var b = new Button
            {
                Content = lbl,
                HorizontalContentAlignment = HorizontalAlignment.Left, MinHeight = 36,
                Padding = new Thickness(9, 4, 9, 4), BorderThickness = new Thickness(0), Cursor = Cursors.Hand,
                Background = Brushes.Transparent, ToolTip = dt
            };
            SetRef(b, ForegroundProperty, cc.Id == _conv.Id ? "Fg" : "Muted");
            b.Click += delegate { OpenConversation(cc); };
            var miR = new MenuItem { Header = T("rename") };   // rename stays on right-click
            miR.Click += delegate { _renamingId = cc.Id; RefreshConvList(); };
            var menu = new ContextMenu(); menu.Items.Add(miR); b.ContextMenu = menu;
            Grid.SetColumn(b, 0); rowGrid.Children.Add(b);
            // trash icon (Segoe MDL2 Assets), revealed on row hover
            var trash = new Button
            {
                Content = "", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 13,
                Width = 32, Height = 36, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
                Cursor = Cursors.Hand, ToolTip = T("delete")
            };
            SetRef(trash, ForegroundProperty, "Muted");
            trash.Click += delegate { ShowDeleteBanner(cc); };
            // Delete is now PERSISTENTLY visible (not hover-only) so it is discoverable; rename
            // rides a right-click menu on the same visible icon (and still on the row). An
            // empty/untitled new chat has nothing to act on, so it shows no actions. (friction #17)
            // A conversation is actionable (deletable/renamable) if it is a REAL saved conversation
            // -- which includes registry/fleet convs that have a title but an empty conv_url (the new
            // agent never captured one). Keying only on messages/conv_url hid the trash icon for the
            // whole top section of the sidebar; a title is the reliable "this is a real chat" signal.
            bool actionable = cc.Messages.Count > 0 || !string.IsNullOrEmpty(cc.ConvUrl)
                              || !string.IsNullOrEmpty(cc.Title);
            trash.Visibility = actionable ? Visibility.Visible : Visibility.Collapsed;
            var trMenu = new ContextMenu();
            var trRename = new MenuItem { Header = T("rename") };
            trRename.Click += delegate { _renamingId = cc.Id; RefreshConvList(); };
            trMenu.Items.Add(trRename); trash.ContextMenu = trMenu;
            Grid.SetColumn(trash, 1); rowGrid.Children.Add(trash);
            rowBorder.Child = rowGrid;
            _convList.Children.Add(rowBorder);
        }
    }

    // STEER-mode signal: when viewing a parallel-task conversation, anything you type interrupts
    // that running worker -- a very different action from a normal chat. Make it unmistakable by
    // tinting the input border Accent (orange) and thickening it, so the user is never surprised
    // that their message went to a background agent. (GAP 8)
    void RefreshSteerVisual()
    {
        bool steer = !string.IsNullOrEmpty(_activeFleetUrl);
        SetRef(_input, Control.BorderBrushProperty, steer ? "Accent" : "Border");
        _input.BorderThickness = new Thickness(steer ? 2 : 1);
    }

    void NewChat()
    {
        new Thread((ThreadStart)delegate { try { HttpGet("/new"); } catch { } }) { IsBackground = true }.Start();
        _conv = new Conversation();
        _conv.Ts = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        _all.Insert(0, _conv);
        _messages.Children.Clear();
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
            new Thread((ThreadStart)delegate { try { HttpGet("/switch?url=" + Uri.EscapeDataString(c.ConvUrl)); } catch { } }) { IsBackground = true }.Start();
    }

    void DeleteLocal(Conversation c)
    {
        try { var p = Path_(c.Id); if (File.Exists(p)) File.Delete(p); } catch { }
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
                for (int i = 0; i < selected.Count; i++)
                {
                    var c = selected[i];
                    int idx = i + 1;
                    Dispatcher.Invoke(new Action(delegate
                    {
                        progressLbl.Text = (_lang == 0) ? ("削除中 " + idx + "/" + total) : ("Deleting " + idx + "/" + total);
                        // local removal (mirror DeleteLocal's file delete + _all.Remove)
                        try { var p = Path_(c.Id); if (File.Exists(p)) File.Delete(p); } catch { }
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
        _newBtn.Content = T("newchat_btn"); _themeBtn.Content = T("theme"); _langBtn.Content = T("lang"); _send.Content = T("send");
        if (_manageBtn != null) _manageBtn.Content = T("manage_btn");
        // Construction-time localized chrome the toggle would otherwise miss (the half-translated UI):
        if (_cockpitBtn != null) _cockpitBtn.Content = T("open_cockpit");
        if (_attachBtn != null) { _attachBtn.Content = T("attach_btn"); _attachBtn.ToolTip = T("attach"); }
        if (_inputHint != null)
            _inputHint.Text = _lang == 0 ? "メッセージを入力 …   「/」でコマンド" : "Type a message …   \"/\" for commands";
    }
    void HideBanner() { _banner.Visibility = Visibility.Collapsed; }

    void ShowDeleteBanner(Conversation c)
    {
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
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false, FontFamily = new FontFamily("Segoe UI"), FontSize = 14
        };
        SetRef(tb, ForegroundProperty, "Fg");
        var bubble = new Border { Child = tb, CornerRadius = new CornerRadius(14), Padding = new Thickness(14, 10, 14, 11), Margin = new Thickness(40, 10, 0, 10), HorizontalAlignment = HorizontalAlignment.Right, MaxWidth = 560 };
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
        var block = new StackPanel { Margin = new Thickness(0, 8, 40, 14) };
        // header row: "Copilot" label on the left, hover-revealed copy button on the right
        var header = new DockPanel { Margin = new Thickness(0, 0, 0, 5) };
        var lbl = new TextBlock { Text = "Copilot", FontSize = 12, Margin = new Thickness(2, 0, 0, 0), FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Muted");
        var blockRef = block;
        var copy = new Button
        {
            Content = "", FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 12,
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
        var content = new StackPanel();
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

    // Render the answer as a SELECTABLE read-only TextBox (TextBlocks can't be selected,
    // so the markdown render could not be copied). Light markdown cleanup for readability;
    // the copy button still copies the full text via outer.Tag.
    void RenderAssistantBody(Panel content, StackPanel outer, string text)
    {
        content.Children.Clear();
        var tb = new TextBox
        {
            Text = PlainText(text), IsReadOnly = true, IsTabStop = false,
            BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, FontSize = 14, Padding = new Thickness(0),
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        };
        SetRef(tb, ForegroundProperty, "Fg");
        SetRef(tb, TextBox.SelectionBrushProperty, "Accent");
        content.Children.Add(tb);
        if (outer != null) outer.Tag = text;
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
        var lbl = new TextBlock { Text = T("generating"), FontSize = 12.5, Margin = new Thickness(6, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center };
        SetRef(lbl, TextBlock.ForegroundProperty, "Muted");
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
        _conv.Messages.Add(new Msg("U", text));
        if (_conv.Untitled()) { _conv.Title = text; }
        if (!_all.Contains(_conv)) { _all.Insert(0, _conv); }
        RefreshConvList();
        AddUser(text);
        StackPanel outer;
        _pendingContent = AddAssistantContainer(out outer);
        _pendingOuter = outer;
        _pendingContent.Children.Add(MakeTyping());   // <- ShuttleScope waiting indicator, shown immediately
        _pendingText = null; _started = false;
        _generating = true; _send.Content = "■ " + T("stop"); _send.IsEnabled = true;   // distinct from Send; _send now acts as Stop (also Esc)
        SetRef(_statusDot, BackgroundProperty, "Accent");
        new Thread((ThreadStart)delegate { Stream(text); }) { IsBackground = true }.Start();
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
    UIElement BuildAttachRow()
    {
        _attachChips = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 6) };
        _attachBtn = new Button
        {
            Content = T("attach_btn"), FontSize = 12,
            Height = 30, Cursor = Cursors.Hand, BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 0, 10, 0), ToolTip = T("attach")
        };
        SetRef(_attachBtn, BackgroundProperty, "Panel"); SetRef(_attachBtn, ForegroundProperty, "Muted"); SetRef(_attachBtn, Control.BorderBrushProperty, "Border");
        _attachBtn.Click += delegate { AttachFile(); };
        _attachChips.Children.Add(_attachBtn);
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
        while (_attachChips.Children.Count > 1) _attachChips.Children.RemoveAt(_attachChips.Children.Count - 1);
    }

    void Stream(string msg)
    {
        var full = new StringBuilder();
        var content = _pendingContent;
        var outer = _pendingOuter;
        string errMsg = null;
        try
        {
            var url = _bridge + "/stream?msg=" + Uri.EscapeDataString(msg);
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = 600000; req.ReadWriteTimeout = 600000;
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
                        var d = ExtractField(line.Substring(5).Trim(), "delta");
                        if (!string.IsNullOrEmpty(d))
                        {
                            full.Append(d);
                            Dispatcher.BeginInvoke(new Action(delegate
                            {
                                if (!_started) { _started = true; content.Children.Clear(); _pendingText = MakeText(""); content.Children.Add(_pendingText); }
                                _pendingText.AppendText(d); StickToEnd();
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
            _generating = false; _send.Content = T("send"); _send.IsEnabled = true; _input.Focus();
            SetRef(_statusDot, BackgroundProperty, "Border");
            // render whatever we got (full / partial / error); always clear the typing indicator
            content.Children.Clear();
            if (answer.Length > 0) { RenderAssistantBody(content, outer, answer); StickToEnd(); }
            else if (errFinal != null) { content.Children.Add(MakeText("[bridge error: " + errFinal + "]")); if (outer != null) outer.Tag = errFinal; }
            _conv.Messages.Add(new Msg("A", answer));
            SaveConversation(_conv);
        }));
        try { var j = HttpGet("/conv"); var u = ExtractField(j, "url"); if (!string.IsNullOrEmpty(u)) { _conv.ConvUrl = u; Dispatcher.BeginInvoke(new Action(delegate { SaveConversation(_conv); RegisterConv(u, _conv.Title, "chat"); })); } } catch { }
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
