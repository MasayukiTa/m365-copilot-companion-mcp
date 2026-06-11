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
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using System.Web.Script.Serialization;

class Program { [STAThread] static void Main() { new Application().Run(new ChatWindow()); } }

class Msg { public string Role; public string Text; public Msg(string r, string t) { Role = r; Text = t; } }

class Conversation
{
    public string Id = Guid.NewGuid().ToString("N").Substring(0, 12);
    public string Title = "";        // empty = untitled (shows the localized default)
    public string ConvUrl = "";
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
    TextBox _input;
    Button _send;
    Border _statusDot;
    Conversation _conv = new Conversation();
    List<Conversation> _all = new List<Conversation>();
    string _renamingId = null;
    int _deleteMode = 1;                 // 1=local only, 2=open in Copilot, 3=auto (experimental)
    int _lang = 0;                       // 0=Japanese, 1=English
    Border _banner; StackPanel _bannerBody;
    Button _newBtn, _themeBtn, _langBtn;
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
        if (k == "fleet_queued") return ja ? "⏳ 並列実行が満杯のため待機列に追加しました（空き枠で実行）。先頭に ! を付けると強制優先。" : "⏳ Fleet is full — queued (runs when a slot frees). Prefix ! to force priority.";
        if (k == "fleet_forced") return ja ? "⏳ 強制優先で待機列の先頭に追加しました。" : "⏳ Forced to the front of the queue.";
        if (k == "router_q") return ja ? "🔍 これは調査向きの依頼です。researcher で深掘りしますか？" : "🔍 This looks like research. Run it on the researcher?";
        if (k == "router_research") return ja ? "researcher で深掘り" : "Deep research";
        if (k == "router_normal") return ja ? "そのまま送信" : "Send as-is";
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

        _newBtn = Btn(T("newchat_btn"), "Panel", "Fg", true);
        _newBtn.Height = 40; _newBtn.Margin = new Thickness(12, 14, 12, 8); _newBtn.FontWeight = FontWeights.SemiBold;
        _newBtn.Click += delegate { NewChat(); };
        Grid.SetRow(_newBtn, 0); side.Children.Add(_newBtn);

        _convList = new StackPanel { Margin = new Thickness(8, 4, 8, 4) };
        var convScroll = new ScrollViewer { Content = _convList, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(convScroll, 1); side.Children.Add(convScroll);

        var bottom = new StackPanel { Margin = new Thickness(12, 8, 12, 12) };
        _langBtn = Btn(T("lang"), "Panel", "Muted", true);
        _langBtn.Height = 34; _langBtn.Margin = new Thickness(0, 0, 0, 6); _langBtn.FontSize = 12;
        _langBtn.Click += delegate { _lang = _lang == 0 ? 1 : 0; SaveSettings(); UpdateChrome(); RefreshConvList(); };
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

        var bar = new Grid { Margin = new Thickness(0, 10, 0, 16), MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Center };
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _input = new TextBox
        {
            MinHeight = 50, MaxHeight = 180, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, FontSize = 14, Padding = new Thickness(13, 12, 13, 12),
            BorderThickness = new Thickness(1), VerticalContentAlignment = VerticalAlignment.Center, MinWidth = 560
        };
        SetRef(_input, BackgroundProperty, "Panel");
        SetRef(_input, ForegroundProperty, "Fg");
        SetRef(_input, TextBox.CaretBrushProperty, "Fg");
        SetRef(_input, Control.BorderBrushProperty, "Border");
        _input.PreviewKeyDown += delegate (object s, KeyEventArgs e)
        { if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0) { e.Handled = true; DoSend(); } };
        Grid.SetColumn(_input, 0); bar.Children.Add(_input);
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

        // ① watch for the cockpit asking to open a parallel-task conversation here, and
        // sync the session-shared conversation registry (fleet + chat conversations).
        string fleetDir = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet"));
        _openPath = Path.Combine(fleetDir, "open.json");
        _convsPath = Path.Combine(fleetDir, "conversations.json");
        try { _openMtime = File.Exists(_openPath) ? File.GetLastWriteTimeUtc(_openPath).Ticks : 0; }
        catch { _openMtime = 0; }
        var openTimer = new DispatcherTimer();
        openTimer.Interval = TimeSpan.FromMilliseconds(800);
        openTimer.Tick += delegate { CheckOpenRequest(); SyncRegistry(); };
        openTimer.Start();
        SyncRegistry();
    }

    string _openPath; long _openMtime;
    string _convsPath; long _convsMtime;

    static string SS(Dictionary<string, object> d, string k)
    { return (d.ContainsKey(k) && d[k] != null) ? d[k].ToString() : ""; }

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
            File.WriteAllText(_convsPath, _cjs.Serialize(list), Encoding.UTF8);
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
                    _all.Add(c);
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
            if (d == null || !d.ContainsKey("url") || d["url"] == null) return;
            string url = d["url"].ToString();
            if (string.IsNullOrEmpty(url)) return;
            new Thread((ThreadStart)delegate { OpenFromFleet(url); }) { IsBackground = true }.Start();
        }
        catch { }
    }

    void OpenFromFleet(string url)
    {
        try { HttpGet("/switch?url=" + Uri.EscapeDataString(url)); } catch { }
        var msgs = new List<Msg>();
        try
        {
            string hist = HttpGet("/history?url=" + Uri.EscapeDataString(url));
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
        var loaded = msgs;
        Dispatcher.BeginInvoke(new Action(delegate
        {
            // reuse the existing sidebar entry for this conversation if present (dedup
            // by URL) so a fleet/registry conversation isn't duplicated
            Conversation c = null;
            foreach (var x in _all) { if (x.ConvUrl == url) { c = x; break; } }
            if (c == null) { c = new Conversation(); c.ConvUrl = url; c.Title = T("fleetview"); _all.Insert(0, c); }
            c.Messages.Clear();
            foreach (var m in loaded) c.Messages.Add(m);
            _conv = c;
            _messages.Children.Clear();
            var note = new TextBlock { Text = T("fleetview_note"), TextWrapping = TextWrapping.Wrap, FontSize = 12.5, Margin = new Thickness(2, 2, 2, 10) };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            foreach (var m in loaded) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
            RefreshConvList();
            _scroll.ScrollToEnd();
        }));
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
        if (_dark)
        {
            Set("Bg", "#0f172a"); Set("Panel", "#1e293b"); Set("PanelAlt", "#0b1220");
            Set("Border", "#334155"); Set("Fg", "#e2e8f0"); Set("Muted", "#94a3b8");
            Set("UserBg", "#334155"); Set("Accent", "#ea580c"); Set("AccentFg", "#ffffff");
            Set("Hover", "#26ffffff"); Set("Press", "#3dffffff");   // translucent white overlay
            Set("CodeBg", "#0b1220");
        }
        else
        {
            Set("Bg", "#ffffff"); Set("Panel", "#f8fafc"); Set("PanelAlt", "#f1f5f9");
            Set("Border", "#e2e8f0"); Set("Fg", "#0f172a"); Set("Muted", "#64748b");
            Set("UserBg", "#eef2f7"); Set("Accent", "#ea580c"); Set("AccentFg", "#ffffff");
            Set("Hover", "#18000000"); Set("Press", "#2b000000");   // translucent black overlay
            Set("CodeBg", "#f1f5f9");
        }
    }

    // ── sidebar list with rename / delete ───────────────────────────────────────
    void RefreshConvList()
    {
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
            var b = new Button
            {
                Content = dt.Length > 26 ? dt.Substring(0, 26) + "…" : dt,
                HorizontalContentAlignment = HorizontalAlignment.Left, Height = 36,
                Padding = new Thickness(9, 0, 9, 0), BorderThickness = new Thickness(0), Cursor = Cursors.Hand,
                Background = Brushes.Transparent, FontSize = 13,
                FontWeight = cc.Id == _conv.Id ? FontWeights.SemiBold : FontWeights.Normal, ToolTip = dt
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
                Cursor = Cursors.Hand, Visibility = Visibility.Hidden, ToolTip = T("delete")
            };
            SetRef(trash, ForegroundProperty, "Muted");
            trash.Click += delegate { ShowDeleteBanner(cc); };
            Grid.SetColumn(trash, 1); rowGrid.Children.Add(trash);
            rowBorder.Child = rowGrid;
            // empty/untitled new chat is not a deletable target -> never reveal its trash
            rowBorder.MouseEnter += delegate { if (cc.Messages.Count > 0 || !string.IsNullOrEmpty(cc.ConvUrl)) trash.Visibility = Visibility.Visible; };
            rowBorder.MouseLeave += delegate { trash.Visibility = Visibility.Hidden; };
            _convList.Children.Add(rowBorder);
        }
    }

    void NewChat()
    {
        new Thread((ThreadStart)delegate { try { HttpGet("/new"); } catch { } }) { IsBackground = true }.Start();
        _conv = new Conversation();
        _all.Insert(0, _conv);
        _messages.Children.Clear();
        RefreshConvList();
        _input.Focus();
    }

    void OpenConversation(Conversation c)
    {
        _conv = c;
        _messages.Children.Clear();
        // a registry/fleet conversation we haven't loaded yet -> pull it via /history
        if (c.Messages.Count == 0 && !string.IsNullOrEmpty(c.ConvUrl))
        {
            var note = new TextBlock { Text = T("loadingconv"), FontSize = 12.5, Margin = new Thickness(2, 2, 2, 10) };
            SetRef(note, TextBlock.ForegroundProperty, "Muted");
            _messages.Children.Add(note);
            RefreshConvList();
            string url = c.ConvUrl;
            new Thread((ThreadStart)delegate { OpenFromFleet(url); }) { IsBackground = true }.Start();
            return;
        }
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
        try { Directory.CreateDirectory(Path.GetDirectoryName(SettingsFile)); File.WriteAllText(SettingsFile, "deletemode=" + _deleteMode + "\nlang=" + _lang + "\ndark=" + (_dark ? "1" : "0") + "\n", Encoding.UTF8); }
        catch { }
    }
    void UpdateChrome()
    {
        _newBtn.Content = T("newchat_btn"); _themeBtn.Content = T("theme"); _langBtn.Content = T("lang"); _send.Content = T("send");
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
            try { var j = HttpGet("/delete?url=" + Uri.EscapeDataString(url)); ok = j != null && j.Contains("\"ok\": true"); } catch { }
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
        _scroll.ScrollToEnd();
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
        _scroll.ScrollToEnd();
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
        _scroll.ScrollToEnd();
        outer = block;
        return content;
    }

    // convenience overload for callers that only need the content panel
    Panel AddAssistantContainer() { StackPanel ignore; return AddAssistantContainer(out ignore); }

    void AddAssistant(string text)
    {
        StackPanel outer;
        var content = AddAssistantContainer(out outer);
        outer.Tag = text;                          // copy button reads this on click
        content.Children.Add(Md.Render(text));     // rendered markdown (code blocks, lists, bold, ...)
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
        _generating = true; _send.Content = T("stop"); _send.IsEnabled = true;   // _send now acts as Stop
        SetRef(_statusDot, BackgroundProperty, "Accent");
        new Thread((ThreadStart)delegate { Stream(text); }) { IsBackground = true }.Start();
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
                                _pendingText.AppendText(d); _scroll.ScrollToEnd();
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
            if (answer.Length > 0) { content.Children.Add(Md.Render(answer)); _scroll.ScrollToEnd(); }
            else if (errFinal != null) { content.Children.Add(MakeText("[bridge error: " + errFinal + "]")); }
            if (outer != null) outer.Tag = answer;   // copy button reads this on click
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
        if (_all.Count > 0) { _conv = _all[0]; foreach (var m in _conv.Messages) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); } }
        else { _conv = new Conversation(); _all.Add(_conv); }
        RefreshConvList();
    }

    string HttpGet(string path)
    {
        var req = (HttpWebRequest)WebRequest.Create(_bridge + path);
        req.Timeout = 60000;
        using (var resp = (HttpWebResponse)req.GetResponse())
        using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            return sr.ReadToEnd();
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
