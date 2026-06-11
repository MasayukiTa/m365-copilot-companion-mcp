// CopilotChat.cs -- native Windows (WPF) chat front-end for an M365 Copilot agent.
// NO Node, NO JS, NO browser engine: pure .NET (built into Windows). It talks to
// the Python bridge's SSE endpoint (bridge/copilot_bridge.py) over HTTP and renders
// the streamed answer in a native desktop window.
//
// Build with the C# compiler that ships with Windows (no Visual Studio / .NET SDK):
//   ui\build_and_run.bat
//
// Architecture:  [ this WPF app ] --HTTP/SSE--> [ Python bridge ] --CDP--> [ Copilot ]
using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

class Program
{
    [STAThread]
    static void Main()
    {
        var app = new Application();
        app.Run(new ChatWindow());
    }
}

class ChatWindow : Window
{
    readonly string _bridge = Environment.GetEnvironmentVariable("MCP_BRIDGE_URL") ?? "http://127.0.0.1:8765";
    StackPanel _messages;
    ScrollViewer _scroll;
    TextBox _input;
    Button _send;

    static readonly Brush Bg     = new SolidColorBrush(Color.FromRgb(0x1a, 0x1a, 0x1a));
    static readonly Brush Panel  = new SolidColorBrush(Color.FromRgb(0x25, 0x25, 0x25));
    static readonly Brush Accent = new SolidColorBrush(Color.FromRgb(0xc9, 0xa3, 0x6a));
    static readonly Brush Fg     = new SolidColorBrush(Color.FromRgb(0xe8, 0xe8, 0xe8));
    static readonly Brush UserFg = new SolidColorBrush(Color.FromRgb(0x9e, 0xcb, 0xff));
    static readonly Brush Muted  = new SolidColorBrush(Color.FromRgb(0x88, 0x88, 0x88));

    public ChatWindow()
    {
        Title = "Copilot - native (WPF, no JS)";
        Width = 940; Height = 740;
        Background = Bg;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;

        var root = new Grid();
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var header = new TextBlock
        {
            Text = "●  Copilot — native bridge (.NET WPF, no Node / no JS)",
            Foreground = Accent, FontWeight = FontWeights.SemiBold, FontSize = 14,
            Padding = new Thickness(16, 12, 16, 12)
        };
        var headerBorder = new Border { Child = header, BorderBrush = Panel, BorderThickness = new Thickness(0, 0, 0, 1) };
        Grid.SetRow(headerBorder, 0); root.Children.Add(headerBorder);

        _messages = new StackPanel { Margin = new Thickness(14) };
        _scroll = new ScrollViewer
        {
            Content = _messages, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        };
        Grid.SetRow(_scroll, 1); root.Children.Add(_scroll);

        var bar = new Grid { Margin = new Thickness(12) };
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _input = new TextBox
        {
            MinHeight = 46, MaxHeight = 160, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            Background = Panel, Foreground = Fg, CaretBrush = Fg, FontSize = 14,
            BorderBrush = new SolidColorBrush(Color.FromRgb(0x3a, 0x3a, 0x3a)),
            Padding = new Thickness(10), VerticalContentAlignment = VerticalAlignment.Center
        };
        _input.PreviewKeyDown += (s, e) =>
        {
            if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0) { e.Handled = true; DoSend(); }
        };
        Grid.SetColumn(_input, 0); bar.Children.Add(_input);
        _send = new Button
        {
            Content = "Send", Width = 88, Margin = new Thickness(8, 0, 0, 0),
            Background = Accent, Foreground = Bg, FontWeight = FontWeights.SemiBold,
            BorderThickness = new Thickness(0), Cursor = Cursors.Hand
        };
        _send.Click += (s, e) => DoSend();
        Grid.SetColumn(_send, 1); bar.Children.Add(_send);
        var barBorder = new Border { Child = bar, BorderBrush = Panel, BorderThickness = new Thickness(0, 1, 0, 0) };
        Grid.SetRow(barBorder, 2); root.Children.Add(barBorder);

        Content = root;
        Loaded += (s, e) => _input.Focus();
    }

    void DoSend()
    {
        var text = _input.Text.Trim();
        if (text.Length == 0 || !_send.IsEnabled) return;
        _input.Clear();
        AddBubble("You", text, true);
        var target = AddBubble("Copilot", "", false);
        _send.IsEnabled = false;
        var th = new Thread(() => Stream(text, target)) { IsBackground = true };
        th.Start();
    }

    TextBox AddBubble(string who, string text, bool isUser)
    {
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock { Text = who, Foreground = Muted, FontSize = 12, Margin = new Thickness(0, 0, 0, 4) });
        var tb = new TextBox
        {
            Text = text, IsReadOnly = true, BorderThickness = new Thickness(0),
            Background = Brushes.Transparent, Foreground = isUser ? UserFg : Fg,
            TextWrapping = TextWrapping.Wrap, IsTabStop = false,
            FontFamily = new FontFamily(isUser ? "Segoe UI" : "Consolas"),
            FontSize = isUser ? 14 : 13.5
        };
        stack.Children.Add(tb);
        var border = new Border
        {
            Child = stack, Background = Panel, CornerRadius = new CornerRadius(10),
            Padding = new Thickness(12, 8, 12, 10), Margin = new Thickness(0, 6, 0, 6)
        };
        _messages.Children.Add(border);
        _scroll.ScrollToEnd();
        return tb;
    }

    void Stream(string msg, TextBox target)
    {
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
                    if (line.StartsWith("event: done")) { done = true; }
                    else if (line.StartsWith("data:"))
                    {
                        var delta = ExtractDelta(line.Substring(5).Trim());
                        if (!string.IsNullOrEmpty(delta))
                            Dispatcher.BeginInvoke(new Action(() => { target.AppendText(delta); _scroll.ScrollToEnd(); }));
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Dispatcher.BeginInvoke(new Action(() => target.AppendText("\n[bridge error: " + ex.Message + "]")));
        }
        finally
        {
            Dispatcher.BeginInvoke(new Action(() => { _send.IsEnabled = true; _input.Focus(); }));
        }
    }

    // Parse {"delta": "..."} (json.dumps, ensure_ascii=False) -> the unescaped string.
    static string ExtractDelta(string json)
    {
        int k = json.IndexOf("\"delta\"", StringComparison.Ordinal);
        if (k < 0) return null;
        int colon = json.IndexOf(':', k);
        if (colon < 0) return null;
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
                    case 'n': sb.Append('\n'); break;
                    case 't': sb.Append('\t'); break;
                    case 'r': sb.Append('\r'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'u':
                        if (i + 4 < json.Length)
                        {
                            sb.Append((char)Convert.ToInt32(json.Substring(i + 1, 4), 16));
                            i += 4;
                        }
                        break;
                    default: sb.Append(n); break;
                }
            }
            else if (c == '"') break;
            else sb.Append(c);
        }
        return sb.ToString();
    }
}
