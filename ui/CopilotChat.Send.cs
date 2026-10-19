// CopilotChat.Send.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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

partial class ChatWindow
{
    // ── slash-command autocomplete (type "/" to see commands, like Claude Code) ──
    Popup _cmdPopup; ListBox _cmdList;

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
        var border = new Border { Child = _cmdList, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(Theme.RadCard), Padding = new Thickness(4) };
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

    // ── send / stream ───────────────────────────────────────────────────────────
    //
    // THE DECISIONS AND THEIR ORDER LIVE IN ChatSend.cs, where a test runs them. What is left
    // here is the window's half: the real effects behind IChatSendEffects (below). DoSend is
    // what Enter and the Send button reach; SendText is also what the router bar's buttons call.
    // Untested by execution: the WPF event reaching DoSend, and the effects' real bodies.
    void DoSend() { ChatSend.DoSend(this); }

    void SendText(string text) { ChatSend.SendText(this, text); }

    // ── IChatSendEffects: the real effects of a send ─────────────────────────────
    string IChatSendEffects.InputText() { return _input.Text; }
    bool IChatSendEffects.SendEnabled() { return _send.IsEnabled; }
    Conversation IChatSendEffects.CurrentConversation() { return _conv; }
    Conversation IChatSendEffects.PageConversation() { return _pageConv; }
    List<Conversation> IChatSendEffects.AllConversations() { return _all; }
    bool IChatSendEffects.BridgeReachable() { return _bridgeReachable; }
    bool IChatSendEffects.RouterShown() { return _routerShown; }
    int IChatSendEffects.Lang() { return _lang; }
    string IChatSendEffects.T(string key) { return T(key); }
    string IChatSendEffects.CommandHelpText() { return CommandHelpText(); }

    // .fleet/status.json beside the exe's parent -- the same path the fleet writes and the
    // cockpit reads. Null when there is none; throws when it cannot be read.
    string IChatSendEffects.ReadFleetStatus()
    {
        return ChatSend.ReadStatusFile(Path.Combine(FleetStateDir(), "status.json"));
    }

    void IChatSendEffects.ClearInput() { _input.Clear(); }
    void IChatSendEffects.RestoreInput(string text)
    {
        _input.Text = text;
        _input.CaretIndex = _input.Text.Length;
    }
    void IChatSendEffects.AddUser(string text) { AddUser(text); PersistSentTurn(); }
    void IChatSendEffects.AddAssistant(string text) { AddAssistant(text); }
    void IChatSendEffects.NewChat() { NewChat(); }
    // The interface keeps its historical name (the send-path oracle records "HttpGet|..."); it is
    // an authenticated POST now, like every other bridge call from this window.
    string IChatSendEffects.HttpGet(string path, int timeoutMs) { return BridgeCall(path, timeoutMs); }
    void IChatSendEffects.SetBridgeReachable(bool ok) { _bridgeReachable = ok; }
    void IChatSendEffects.SetDot(string state) { SetDot(state); }
    void IChatSendEffects.RefreshIdleDot() { RefreshIdleDot(); }
    void IChatSendEffects.HideRouter() { HideRouter(); }
    void IChatSendEffects.ShowRouter(string text) { ShowRouter(text); }
    bool IChatSendEffects.AppendCommand(string key, object item) { return AppendCommand(key, item); }
    void IChatSendEffects.SetPageConversation(Conversation c) { _pageConv = c; }
    void IChatSendEffects.MarkSendInFlight() { _sendInFlight = true; }
    void IChatSendEffects.RefreshConvList() { RefreshConvList(); }
    void IChatSendEffects.StickToEnd() { StickToEnd(); }

    // The send was refused because the bridge is down. One-click recovery: bring the whole
    // stack (bridge + relay) back up instead of leaving the user to go find a terminal.
    // Idempotent -- safe even if some of the stack is already running.
    void IChatSendEffects.ShowStartStackBanner(string message, string buttonLabel)
    {
        ShowRecoveryBanner(message, buttonLabel, delegate
        {
            HideBanner();
            // wscript + the VBS, NOT start_all.bat, AND NOT UseShellExecute. This
            // process is WPF and has no console of its own, so shell-executing a .bat
            // makes Windows give cmd.exe a brand new one -- a black window on the
            // operator's desktop, which is the single thing start_all.bat's own header
            // says it was rewritten to stop ("No console lingers"). The bat's entire
            // body is this same wscript line, so calling it directly loses nothing.
            // FleetCockpit.RunStartAll already does exactly this; this site was the
            // one copy that did not.
            try
            {
                var psi = new System.Diagnostics.ProcessStartInfo();
                psi.FileName = "wscript.exe";
                psi.Arguments = "\"" + Path.Combine(RepoRoot(), "scripts", "start_all_hidden.vbs") + "\"";
                psi.WorkingDirectory = RepoRoot();
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                System.Diagnostics.Process.Start(psi);
            }
            catch { }
        });
    }

    // The window's half of a send that is going ahead: the assistant block with the typing
    // dots, the Stop button, the busy dot, and the Stream thread for `target`.
    void IChatSendEffects.BeginStream(string text, Conversation target)
    {
        StackPanel outer;
        _pendingContent = AddAssistantContainer(out outer);
        _pendingOuter = outer;
        _pendingContent.Children.Add(MakeTyping());   // <- the sibling app waiting indicator, shown immediately
        _pendingText = null; _started = false;
        _generating = true; _send.Content = T("stop"); _send.IsEnabled = true;   // distinct from Send; _send now acts as Stop (also Esc)
        PaintSend();   // stay accent while generating
        SetDot("busy");
        new Thread((ThreadStart)delegate { Stream(text, target); }) { IsBackground = true }.Start();
        ClearChips();   // the attached file(s) go with this message; reset the chip row
    }

    // ── the fleet command channel ────────────────────────────────────────────────
    // Send ONE command ({key: [item]}) to the running fleet, as a file of its own.
    //
    // This used to read all of .fleet/commands.json, append to `key` and write the file back --
    // and so did EnqueueToFleet (now the capacity branch of ChatSend.DoSend), and so did
    // FleetCockpit.cs's ReadCommands/WriteCommands.
    // FleetCockpit.exe and CopilotChat.exe are SEPARATELY BUILT PROCESSES (ui/rebuild_ui.ps1),
    // so no amount of care inside one of them could order the other: whichever wrote second
    // deleted the other's queued command, and a lost add_goal/steer looks exactly like one that
    // was never sent. FleetCommands.Write drops each command into .fleet/commands.d/ under a
    // unique name instead, which removes the read-modify-write rather than trying to guard it.
    // See ui/FleetCommands.cs.
    bool AppendCommand(string key, object item)
    {
        var items = new List<object>();
        items.Add(item);
        var patch = new Dictionary<string, object>();
        patch[key] = items;
        return FleetCommands.Write(FleetStateDir(), patch);
    }

    // The .fleet state dir, beside the exe's parent. The command channel lives under it.
    static string FleetStateDir()
    {
        return Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".fleet"));
    }

    // ── #2 research-intent confirm bar (the detection is ChatSend.DetectResearch) ──

    Button _routerResearch, _routerNormal; TextBlock _routerLbl;

    UIElement BuildRouterBar()
    {
        _routerBar = new Border { Visibility = Visibility.Collapsed, CornerRadius = new CornerRadius(Theme.RadPopover), BorderThickness = new Thickness(1), Padding = new Thickness(12, 8, 12, 8), Margin = new Thickness(0, 0, 0, 8) };
        SetRef(_routerBar, BackgroundProperty, "Panel"); SetRef(_routerBar, Border.BorderBrushProperty, "Accent");
        var dp = new DockPanel();
        var btns = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        DockPanel.SetDock(btns, Dock.Right);
        _routerResearch = Btn("", "AccentFill", "AccentFg", false);
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
            string path = Path.Combine(ChatTempHome.Dir(), "copilot_paste_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".png");
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
            try { r = BridgeCall("/upload?path=" + Uri.EscapeDataString(path)); } catch { }
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
        var b = new Border { CornerRadius = new CornerRadius(Theme.RadSmall), Padding = new Thickness(8, 3, 8, 3), Margin = new Thickness(6, 0, 0, 0), BorderThickness = new Thickness(1) };
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
        string persistErr = null;      // the bridge's `persist_error`: this turn is NOT in its session store
        bool persistAcked = false;     // the bridge's `persist`: it is
        try
        {
            // /review and /security-review run a full-repo fleet pass that can exceed the
            // default 10-minute cap -- give those two commands a 60-minute window instead.
            string msgTrim = (msg ?? "").TrimStart();
            bool isLongReview = msgTrim.StartsWith("/review", StringComparison.OrdinalIgnoreCase)
                || msgTrim.StartsWith("/security-review", StringComparison.OrdinalIgnoreCase);
            int reqTimeoutMs = isLongReview ? 3600000 : 600000;
            // An authenticated POST (ui/BridgeClient.cs). Every request it makes is kept in
            // _activeReq as it is made, so Stop can Abort() whichever one is in flight.
            HttpWebResponse opened;
            try
            {
                opened = BridgeClient.Open(_bridge, "/stream?msg=" + Uri.EscapeDataString(msg), reqTimeoutMs,
                                           delegate(HttpWebRequest r) { _activeReq = r; });
            }
            catch (BridgeClientException bex) { NoteBridgeProblem(bex); throw; }
            using (var resp = opened)
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
                        var pe = ExtractField(jsonData, "persist_error");
                        if (!string.IsNullOrEmpty(pe)) persistErr = pe;
                        else if (ExtractField(jsonData, "persist") == "ok") persistAcked = true;
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
            // The bridge keeps its own copy of every chat turn; when it says it could not, the
            // person is told here. An answer that arrives with neither word is logged, not shown:
            // slash commands that never reach the session store look exactly like that.
            if (persistErr != null) NoteNotSaved(target, "bridge: " + persistErr);
            else if (answer.Length > 0 && !persistAcked)
            {
                try { File.AppendAllText(Path.Combine(StoreDir, "chat_save_errors.log"), DateTime.Now.ToString("s") + "\t" + target.Id + "\tbridge sent no persist word\n", new UTF8Encoding(false)); } catch (Exception) { }
            }
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
            var j = BridgeCall("/conv");
            var u = ExtractField(j, "url");
            if (!string.IsNullOrEmpty(u))
            {
                target.ConvUrl = u;
                Dispatcher.BeginInvoke(new Action(delegate { SaveConversation(target); RegisterConv(u, target.Title, "chat"); }));
            }
        }
        catch { }
    }
}
