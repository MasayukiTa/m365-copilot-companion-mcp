// CopilotChat.cs -- native Windows (WPF) chat front-end for an M365 Copilot agent.
// NO Node, NO JS, NO browser engine: pure .NET (built into Windows). Talks to the
// Python bridge's SSE endpoint over HTTP and renders the streamed answer natively.
//
//   [ this WPF app ] --HTTP/SSE--> [ Python bridge ] --CDP--> [ Copilot ]
//
// Design language follows the sibling app v1.1 (grayscale-first N_GRAY slate, with the
// single E_EMPHASIS orange reserved for the primary action). Build with the C#
// compiler that ships with Windows: ui\build_and_run.bat
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
using System.Windows.Threading;

class Program
{
    [STAThread]
    static void Main() { new Application().Run(new ChatWindow()); }
}

class Msg { public string Role; public string Text; public Msg(string r, string t) { Role = r; Text = t; } }

class Conversation
{
    public string Id = Guid.NewGuid().ToString("N").Substring(0, 12);
    public string Title = "新しいチャット";
    public string ConvUrl = "";
    public List<Msg> Messages = new List<Msg>();
}

class ChatWindow : Window
{
    readonly string _bridge = Environment.GetEnvironmentVariable("MCP_BRIDGE_URL") ?? "http://127.0.0.1:8765";
    static readonly string StoreDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "copilot-bridge", "chats");

    // ── the sibling app N_GRAY (slate) + E_EMPHASIS ────────────────────────────────
    static Color C(string hex) { return (Color)ColorConverter.ConvertFromString(hex); }
    bool _dark = true;
    StackPanel _messages, _convList;
    ScrollViewer _scroll;
    TextBox _input;
    Button _send;
    Border _statusDot;
    Conversation _conv = new Conversation();
    List<Conversation> _all = new List<Conversation>();

    // streaming/thinking state
    TextBox _pending;
    volatile bool _started;
    bool _thinking;
    int _frame;
    DispatcherTimer _think;

    public ChatWindow()
    {
        Title = "Copilot — native (WPF, no JS)";
        Width = 1080; Height = 760;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        SetRef(this, BackgroundProperty, "Bg");
        ApplyTheme();

        var root = new Grid();
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(248) });
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // ── sidebar ──
        var side = new Grid();
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        side.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        side.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        SetRef(side, BackgroundProperty, "PanelAlt");

        var newBtn = new Button
        {
            Content = "＋  新しいチャット", Height = 40, Margin = new Thickness(12, 14, 12, 8),
            FontWeight = FontWeights.SemiBold, Cursor = Cursors.Hand, BorderThickness = new Thickness(1)
        };
        SetRef(newBtn, BackgroundProperty, "Panel");
        SetRef(newBtn, ForegroundProperty, "Fg");
        SetRef(newBtn, Control.BorderBrushProperty, "Border");
        newBtn.Click += (s, e) => NewChat();
        Grid.SetRow(newBtn, 0); side.Children.Add(newBtn);

        _convList = new StackPanel { Margin = new Thickness(8, 4, 8, 4) };
        var convScroll = new ScrollViewer { Content = _convList, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(convScroll, 1); side.Children.Add(convScroll);

        var themeBtn = new Button
        {
            Content = "☀  ライト / ダーク", Height = 36, Margin = new Thickness(12, 8, 12, 12),
            Cursor = Cursors.Hand, BorderThickness = new Thickness(1)
        };
        SetRef(themeBtn, BackgroundProperty, "Panel");
        SetRef(themeBtn, ForegroundProperty, "Muted");
        SetRef(themeBtn, Control.BorderBrushProperty, "Border");
        themeBtn.Click += (s, e) => { _dark = !_dark; ApplyTheme(); themeBtn.Content = _dark ? "☀  ライト / ダーク" : "☾  ライト / ダーク"; };
        Grid.SetRow(themeBtn, 2); side.Children.Add(themeBtn);

        var sideBorder = new Border { Child = side, BorderThickness = new Thickness(0, 0, 1, 0) };
        SetRef(sideBorder, Border.BorderBrushProperty, "Border");
        Grid.SetColumn(sideBorder, 0); root.Children.Add(sideBorder);

        // ── main column ──
        var main = new Grid();
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        main.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var headPanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(18, 12, 18, 12) };
        _statusDot = new Border { Width = 10, Height = 10, CornerRadius = new CornerRadius(5), Margin = new Thickness(0, 4, 8, 0) };
        SetRef(_statusDot, BackgroundProperty, "Accent");
        var headText = new TextBlock { Text = "Copilot  ·  native bridge (.NET WPF, no Node / no JS)", FontWeight = FontWeights.SemiBold, FontSize = 14 };
        SetRef(headText, TextBlock.ForegroundProperty, "Fg");
        headPanel.Children.Add(_statusDot); headPanel.Children.Add(headText);
        var headBorder = new Border { Child = headPanel, BorderThickness = new Thickness(0, 0, 0, 1) };
        SetRef(headBorder, Border.BorderBrushProperty, "Border");
        Grid.SetRow(headBorder, 0); main.Children.Add(headBorder);

        _messages = new StackPanel { Margin = new Thickness(18, 12, 18, 12), MaxWidth = 820, HorizontalAlignment = HorizontalAlignment.Stretch };
        _scroll = new ScrollViewer { Content = _messages, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        Grid.SetRow(_scroll, 1); main.Children.Add(_scroll);

        var bar = new Grid { Margin = new Thickness(14, 10, 14, 14) };
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _input = new TextBox
        {
            MinHeight = 48, MaxHeight = 170, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, FontSize = 14, Padding = new Thickness(11),
            BorderThickness = new Thickness(1), VerticalContentAlignment = VerticalAlignment.Center
        };
        SetRef(_input, BackgroundProperty, "Panel");
        SetRef(_input, ForegroundProperty, "Fg");
        SetRef(_input, TextBox.CaretBrushProperty, "Fg");
        SetRef(_input, Control.BorderBrushProperty, "Border");
        _input.PreviewKeyDown += (s, e) =>
        { if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0) { e.Handled = true; DoSend(); } };
        Grid.SetColumn(_input, 0); bar.Children.Add(_input);
        _send = new Button
        {
            Content = "送信", Width = 92, Margin = new Thickness(8, 0, 0, 0), FontWeight = FontWeights.SemiBold,
            BorderThickness = new Thickness(0), Cursor = Cursors.Hand
        };
        SetRef(_send, BackgroundProperty, "Accent");
        SetRef(_send, ForegroundProperty, "AccentFg");
        _send.Click += (s, e) => DoSend();
        Grid.SetColumn(_send, 1); bar.Children.Add(_send);
        var barBorder = new Border { Child = bar, BorderThickness = new Thickness(0, 1, 0, 0) };
        SetRef(barBorder, Border.BorderBrushProperty, "Border");
        Grid.SetRow(barBorder, 2); main.Children.Add(barBorder);

        Grid.SetColumn(main, 1); root.Children.Add(main);
        Content = root;

        _think = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(280) };
        _think.Tick += (s, e) =>
        {
            if (!_thinking || _pending == null) return;
            string[] f = { "●  ∙  ∙", "∙  ●  ∙", "∙  ∙  ●", "∙  ●  ∙" };
            _pending.Text = "考えています   " + f[_frame++ % f.Length];
        };

        LoadConversations();
        Loaded += (s, e) => _input.Focus();
    }

    // ── theming (DynamicResource swap) ─────────────────────────────────────────
    void SetRef(FrameworkElement el, DependencyProperty p, string key) { el.SetResourceReference(p, key); }
    void Set(string key, string hex) { Application.Current.Resources[key] = new SolidColorBrush(C(hex)); }
    void ApplyTheme()
    {
        if (_dark)
        {
            Set("Bg", "#0f172a"); Set("Panel", "#1e293b"); Set("PanelAlt", "#0b1220");
            Set("Border", "#334155"); Set("Fg", "#f1f5f9"); Set("Muted", "#94a3b8");
            Set("UserFg", "#8db0fe"); Set("Accent", "#ea580c"); Set("AccentFg", "#ffffff");
        }
        else
        {
            Set("Bg", "#f8fafc"); Set("Panel", "#ffffff"); Set("PanelAlt", "#f1f5f9");
            Set("Border", "#cbd5e1"); Set("Fg", "#0f172a"); Set("Muted", "#475569");
            Set("UserFg", "#3b4cc0"); Set("Accent", "#ea580c"); Set("AccentFg", "#ffffff");
        }
    }

    // ── conversation list (left) ────────────────────────────────────────────────
    void RefreshConvList()
    {
        _convList.Children.Clear();
        foreach (var c in _all)
        {
            var cc = c;
            var b = new Button
            {
                Content = c.Title.Length > 26 ? c.Title.Substring(0, 26) + "…" : c.Title,
                HorizontalContentAlignment = HorizontalAlignment.Left, Height = 34, Margin = new Thickness(0, 2, 0, 2),
                Padding = new Thickness(8, 0, 8, 0), BorderThickness = new Thickness(0), Cursor = Cursors.Hand,
                FontWeight = cc.Id == _conv.Id ? FontWeights.SemiBold : FontWeights.Normal
            };
            SetRef(b, BackgroundProperty, cc.Id == _conv.Id ? "Panel" : "PanelAlt");
            SetRef(b, ForegroundProperty, cc.Id == _conv.Id ? "Fg" : "Muted");
            b.Click += (s, e) => OpenConversation(cc);
            _convList.Children.Add(b);
        }
    }

    void NewChat()
    {
        new Thread(() => HttpGet("/new")) { IsBackground = true }.Start();  // reset bridge conversation
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
        foreach (var m in c.Messages) AddBubble(m.Role == "U" ? "You" : "Copilot", m.Text, m.Role == "U");
        RefreshConvList();
        if (!string.IsNullOrEmpty(c.ConvUrl))
            new Thread(() => HttpGet("/switch?url=" + Uri.EscapeDataString(c.ConvUrl))) { IsBackground = true }.Start();
    }

    // ── send / stream ───────────────────────────────────────────────────────────
    void DoSend()
    {
        var text = _input.Text.Trim();
        if (text.Length == 0 || !_send.IsEnabled) return;
        _input.Clear();
        _conv.Messages.Add(new Msg("U", text));
        if (_conv.Title == "新しいチャット") { _conv.Title = text; RefreshConvList(); }
        if (!_all.Contains(_conv)) { _all.Insert(0, _conv); RefreshConvList(); }
        AddBubble("You", text, true);
        _pending = AddBubble("Copilot", "", false);
        _started = false; _thinking = true; _frame = 0; _think.Start();
        _send.IsEnabled = false;
        SetRef(_statusDot, BackgroundProperty, "Accent");
        new Thread(() => Stream(text, _pending)) { IsBackground = true }.Start();
    }

    TextBox AddBubble(string who, string text, bool isUser)
    {
        var stack = new StackPanel();
        var lbl = new TextBlock { Text = who, FontSize = 12, Margin = new Thickness(0, 0, 0, 4) };
        SetRef(lbl, TextBlock.ForegroundProperty, "Muted");
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false,
            FontFamily = new FontFamily(isUser ? "Segoe UI" : "Cascadia Mono, Consolas"),
            FontSize = isUser ? 14 : 13.5
        };
        SetRef(tb, ForegroundProperty, isUser ? "UserFg" : "Fg");
        stack.Children.Add(lbl); stack.Children.Add(tb);
        var border = new Border { Child = stack, CornerRadius = new CornerRadius(10), Padding = new Thickness(13, 9, 13, 11), Margin = new Thickness(0, 6, 0, 6) };
        SetRef(border, BackgroundProperty, "Panel");
        _messages.Children.Add(border);
        _scroll.ScrollToEnd();
        return tb;
    }

    void Stream(string msg, TextBox target)
    {
        var full = new StringBuilder();
        try
        {
            var url = _bridge + "/stream?msg=" + Uri.EscapeDataString(msg);
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = 600000; req.ReadWriteTimeout = 600000;
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
                            Dispatcher.BeginInvoke(new Action(() =>
                            {
                                if (!_started) { _started = true; _thinking = false; _think.Stop(); target.Text = ""; }
                                target.AppendText(d); _scroll.ScrollToEnd();
                            }));
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Dispatcher.BeginInvoke(new Action(() => { _thinking = false; _think.Stop(); target.AppendText("\n[bridge error: " + ex.Message + "]"); }));
        }
        var answer = full.ToString();
        Dispatcher.BeginInvoke(new Action(() =>
        {
            _thinking = false; _think.Stop(); _send.IsEnabled = true; _input.Focus();
            SetRef(_statusDot, BackgroundProperty, "Border");
            _conv.Messages.Add(new Msg("A", answer));
            SaveConversation(_conv);
        }));
        try { var j = HttpGet("/conv"); var u = ExtractField(j, "url"); if (!string.IsNullOrEmpty(u)) { _conv.ConvUrl = u; Dispatcher.BeginInvoke(new Action(() => SaveConversation(_conv))); } } catch { }
    }

    // ── persistence (manual, base64 -- no JSON lib needed) ──────────────────────
    string Path_(string id) { return Path.Combine(StoreDir, id + ".chat"); }
    static string B64(string s) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(s ?? "")); }
    static string UnB64(string s) { try { return Encoding.UTF8.GetString(Convert.FromBase64String(s)); } catch { return ""; } }

    void SaveConversation(Conversation c)
    {
        try
        {
            Directory.CreateDirectory(StoreDir);
            var sb = new StringBuilder();
            sb.Append("CONV\t").Append(c.ConvUrl ?? "").Append('\n');
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
                files.Sort((a, b) => File.GetLastWriteTime(b).CompareTo(File.GetLastWriteTime(a)));
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
        if (_all.Count > 0) { _conv = _all[0]; foreach (var m in _conv.Messages) AddBubble(m.Role == "U" ? "You" : "Copilot", m.Text, m.Role == "U"); }
        else { _conv = new Conversation(); _all.Add(_conv); }
        RefreshConvList();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────
    string HttpGet(string path)
    {
        var req = (HttpWebRequest)WebRequest.Create(_bridge + path);
        req.Timeout = 60000;
        using (var resp = (HttpWebResponse)req.GetResponse())
        using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            return sr.ReadToEnd();
    }

    // pull the string value of "field" out of a small json.dumps(ensure_ascii=False) object
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
