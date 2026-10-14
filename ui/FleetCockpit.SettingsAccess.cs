// FleetCockpit.SettingsAccess.cs -- settings: folder access scope and connected clients.
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

    // ── アクセス範囲 ──────────────────────────────────────────────────────────────────
    string FolderAccessPath() { return Path.Combine(RepoRoot(), ".fleet", "folder_access.json"); }

    Dictionary<string, object> ReadFolderAccessRaw()
    {
        try
        {
            string p = FolderAccessPath();
            if (File.Exists(p))
            {
                var d = _js.DeserializeObject(File.ReadAllText(p, Encoding.UTF8)) as Dictionary<string, object>;
                if (d != null) return d;
            }
        }
        catch (Exception) { }
        return new Dictionary<string, object>();
    }

    // DEFAULT-OPEN, mirroring folder_policy.py's own _normalise(): a missing/unreadable/
    // malformed policy file means unrestricted (フルアクセス), never restricted.
    bool AccessEnabled()
    {
        var d = ReadFolderAccessRaw();
        object v;
        if (d.TryGetValue("enabled", out v) && v != null)
        {
            try { return Convert.ToBoolean(v); } catch (Exception) { }
        }
        return false;
    }

    List<string> AccessGlobalFolders()
    {
        var d = ReadFolderAccessRaw();
        var outp = new List<string>();
        object v;
        if (d.TryGetValue("global", out v) && v is object[])
            foreach (object o in (object[])v) if (o != null) outp.Add(o.ToString());
        return outp;
    }

    // Whole-dict atomic write (tmp file in the same directory + File.Replace), same shape as
    // AnswerGate's gate-file write above -- .fleet/ is not written concurrently by the live
    // server for this particular file, but a torn write here would still corrupt the JSON a
    // reader (this same UI, or a future python reader) parses next, so the same discipline
    // applies. Never edits keys the caller didn't set (SetAccessScope/AddAccessFolder/
    // RemoveAccessFolder each read-modify-write a single key so "scopes" always survives).
    void WriteFolderAccessRaw(Dictionary<string, object> d)
    {
        try
        {
            string path = FolderAccessPath();
            string dir = Path.GetDirectoryName(path);
            Directory.CreateDirectory(dir);
            string json = _js.Serialize(d);
            string tmp = Path.Combine(dir, Path.GetFileName(path) + ".tmp");
            File.WriteAllText(tmp, json, new UTF8Encoding(false));
            if (File.Exists(path))
            {
                try { File.Replace(tmp, path, null); }
                catch (Exception)
                {
                    File.Copy(tmp, path, true);
                    try { File.Delete(tmp); } catch (Exception) { }
                }
            }
            else
            {
                File.Move(tmp, path);
            }
        }
        catch (Exception) { }
    }

    // Toggle ONLY "enabled". "global" (the saved folder list) and "scopes" are read back and
    // written out exactly as found -- choosing フルアクセス must never lose the folder list a
    // user built up under 指定フォルダのみ, so that re-enabling restriction restores it intact.
    void SetAccessScope(bool restricted)
    {
        var d = ReadFolderAccessRaw();
        d["enabled"] = restricted;
        if (!d.ContainsKey("global")) d["global"] = new object[0];
        if (!d.ContainsKey("scopes")) d["scopes"] = new Dictionary<string, object>();
        WriteFolderAccessRaw(d);
        PaintAccessScopeButtons(restricted);
    }

    void PaintAccessScopeButtons(bool restricted)
    {
        if (_accessFullBtn != null)
        {
            bool sel = !restricted;
            _accessFullBtn.Background = sel ? Theme.Br(Theme.AccentFill(_dark)) : BtnBg;
            _accessFullBtn.Foreground = sel ? White : Muted;
            _accessFullBtn.BorderBrush = sel ? Theme.Br(Theme.Accent(_dark)) : Border;
        }
        if (_accessRestrictedBtn != null)
        {
            bool sel = restricted;
            _accessRestrictedBtn.Background = sel ? Theme.Br(Theme.AccentFill(_dark)) : BtnBg;
            _accessRestrictedBtn.Foreground = sel ? White : Muted;
            _accessRestrictedBtn.BorderBrush = sel ? Theme.Br(Theme.Accent(_dark)) : Border;
        }
    }

    void RefreshFolderRows(List<string> folders)
    {
        if (_folderRowsPanel == null) return;
        _folderRowsPanel.Children.Clear();
        if (folders.Count == 0)
        {
            var none = new TextBlock();
            none.Text = L("(フォルダ未設定)", "(no folders yet)");
            none.Foreground = Muted; none.FontSize = 11; none.Margin = new Thickness(0, 2, 0, 2);
            _folderRowsPanel.Children.Add(none);
            return;
        }
        foreach (string f in folders)
        {
            var row = new DockPanel(); row.Margin = new Thickness(0, 2, 0, 2); row.LastChildFill = false;
            var rm = MiniButton("×");
            rm.Width = 22; rm.Height = 22; rm.FontSize = 12;
            rm.ToolTip = L("削除", "Remove");
            System.Windows.Automation.AutomationProperties.SetName(rm, L("削除", "Remove") + ": " + f);
            string captured = f;
            rm.Click += delegate { RemoveAccessFolder(captured); };
            DockPanel.SetDock(rm, Dock.Right); row.Children.Add(rm);
            var tb = new TextBlock(); tb.Text = f; tb.Foreground = Fg; tb.FontSize = 11.5;
            tb.VerticalAlignment = VerticalAlignment.Center; tb.TextTrimming = TextTrimming.CharacterEllipsis;
            tb.ToolTip = f; tb.Margin = new Thickness(0, 0, 6, 0);
            DockPanel.SetDock(tb, Dock.Left); row.Children.Add(tb);
            _folderRowsPanel.Children.Add(row);
        }
    }

    // Same "pick any file, use its parent folder" picker FolderToGoals() already uses --
    // reliable, no COM FolderBrowserDialog needed.
    void AddAccessFolder()
    {
        try
        {
            var ofd = new Microsoft.Win32.OpenFileDialog();
            ofd.Title = L("追加するフォルダ内の任意のファイルを選択（その親フォルダが追加されます）",
                          "Pick ANY file inside the folder to add (its parent folder is added)");
            ofd.Filter = L("すべてのファイル|*.*", "All files|*.*");
            ofd.CheckFileExists = true;
            if (ofd.ShowDialog() != true) return;
            string folder = Path.GetDirectoryName(ofd.FileName);
            if (string.IsNullOrEmpty(folder)) return;

            var d = ReadFolderAccessRaw();
            var list = new List<string>();
            object v;
            if (d.TryGetValue("global", out v) && v is object[])
                foreach (object o in (object[])v) if (o != null) list.Add(o.ToString());
            bool exists = false;
            foreach (string existing in list)
                if (string.Equals(existing, folder, StringComparison.OrdinalIgnoreCase)) { exists = true; break; }
            if (!exists) list.Add(folder);
            d["global"] = list.ToArray();
            if (!d.ContainsKey("enabled")) d["enabled"] = false;
            if (!d.ContainsKey("scopes")) d["scopes"] = new Dictionary<string, object>();
            WriteFolderAccessRaw(d);
            RefreshFolderRows(list);
        }
        catch (Exception) { }
    }

    void RemoveAccessFolder(string folder)
    {
        try
        {
            var d = ReadFolderAccessRaw();
            var list = new List<string>();
            object v;
            if (d.TryGetValue("global", out v) && v is object[])
                foreach (object o in (object[])v) if (o != null) list.Add(o.ToString());
            list.RemoveAll(delegate (string x) { return string.Equals(x, folder, StringComparison.OrdinalIgnoreCase); });
            d["global"] = list.ToArray();
            WriteFolderAccessRaw(d);
            RefreshFolderRows(list);
        }
        catch (Exception) { }
    }

    UIElement BuildAccessScopeControls()
    {
        var wrap = new StackPanel();
        var subHead = new TextBlock(); subHead.Text = L("アクセス範囲", "Access scope");
        subHead.Foreground = Fg; subHead.FontSize = 12.5; subHead.FontWeight = FontWeights.SemiBold;
        subHead.Margin = new Thickness(0, 4, 0, 4);
        wrap.Children.Add(subHead);

        bool restricted = AccessEnabled();
        var folders = AccessGlobalFolders();

        var choiceRow = new StackPanel(); choiceRow.Orientation = Orientation.Horizontal;
        choiceRow.Margin = new Thickness(0, 0, 0, 4);

        _accessFullBtn = new Button();
        _accessFullBtn.Content = L("フルアクセス", "Full access");
        _accessFullBtn.FontSize = 12; _accessFullBtn.FontWeight = FontWeights.SemiBold;
        _accessFullBtn.Cursor = Cursors.Hand; _accessFullBtn.BorderThickness = new Thickness(1);
        _accessFullBtn.Padding = new Thickness(12, 4, 12, 4); _accessFullBtn.Template = FlatButtonTemplate();
        _accessFullBtn.ToolTip = L("すべてのフォルダを操作できます（既定）。", "Can operate on any folder (default).");
        _accessFullBtn.Click += delegate { SetAccessScope(false); };
        choiceRow.Children.Add(_accessFullBtn);

        _accessRestrictedBtn = new Button();
        _accessRestrictedBtn.Content = L("指定フォルダのみ", "Selected folders only");
        _accessRestrictedBtn.FontSize = 12; _accessRestrictedBtn.FontWeight = FontWeights.SemiBold;
        _accessRestrictedBtn.Cursor = Cursors.Hand; _accessRestrictedBtn.BorderThickness = new Thickness(1);
        _accessRestrictedBtn.Padding = new Thickness(12, 4, 12, 4); _accessRestrictedBtn.Template = FlatButtonTemplate();
        _accessRestrictedBtn.Margin = new Thickness(6, 0, 0, 0);
        _accessRestrictedBtn.ToolTip = L("下のリストのフォルダだけに操作を限定します。", "Limits operations to the folders listed below.");
        _accessRestrictedBtn.Click += delegate { SetAccessScope(true); };
        choiceRow.Children.Add(_accessRestrictedBtn);

        wrap.Children.Add(choiceRow);
        PaintAccessScopeButtons(restricted);

        var hint = new TextBlock();
        hint.Text = L("フルアクセス: すべてのフォルダを操作可能（既定）。指定フォルダのみ: 下のリストのフォルダに限定します。",
                      "Full access: can operate on any folder (default). Selected folders only: limited to the list below.");
        hint.Foreground = Muted; hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap;
        hint.Margin = new Thickness(0, 4, 0, 6);
        wrap.Children.Add(hint);

        _folderRowsPanel = new StackPanel();
        RefreshFolderRows(folders);
        var listScroll = new ScrollViewer();
        listScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        listScroll.MaxHeight = 110;
        listScroll.Content = _folderRowsPanel;
        wrap.Children.Add(listScroll);

        var addBtn = new Button();
        addBtn.Content = L("＋ フォルダを追加", "+ Add folder");
        addBtn.FontSize = 12; addBtn.Cursor = Cursors.Hand; addBtn.BorderThickness = new Thickness(1);
        addBtn.Padding = new Thickness(12, 4, 12, 4); addBtn.HorizontalAlignment = HorizontalAlignment.Left;
        addBtn.Margin = new Thickness(0, 4, 0, 12); addBtn.Template = FlatButtonTemplate();
        addBtn.Background = BtnBg; addBtn.Foreground = Fg; addBtn.BorderBrush = Border;
        addBtn.Click += delegate { AddAccessFolder(); };
        wrap.Children.Add(addBtn);

        return wrap;
    }

    // ── 接続クライアント ──────────────────────────────────────────────────────────────
    // Never opens/writes .unlock_state.json directly -- shells out to
    // `python -m tools.security ...` / `python -m tools.lock_state show`, the exact CLI
    // surface tools/security.py's _cli() and tools/lock_state.py's _cli() document as being
    // for this panel. Those modules own the atomic write and the require_unlocked() gate;
    // this panel only ever reads their JSON output and asks them to grant/revoke.
    string RunPyModule(string module, string args, int timeoutMs)
    {
        try
        {
            string repo = RepoRoot();
            string py = Path.Combine(repo, ".venv", "Scripts", "python.exe");
            if (!File.Exists(py)) py = "python";
            var psi = new System.Diagnostics.ProcessStartInfo();
            psi.FileName = py;
            psi.Arguments = "-m " + module + (string.IsNullOrEmpty(args) ? "" : " " + args);
            psi.WorkingDirectory = repo;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            try { psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"; } catch (Exception) { }
            // BOTH STREAMS AT ONCE, AND A TIMEOUT THAT ACTUALLY BOUNDS THE CALL.
            //
            // This used to ReadToEnd() stdout, then ReadToEnd() stderr, then WaitForExit(timeout).
            // Two faults. The reads had no timeout, so WaitForExit -- the only bounded step --
            // was unreachable whenever the child did not finish, and the timeout argument was
            // decorative. And draining stdout to completion before touching stderr deadlocks if
            // the child fills the stderr pipe: the child blocks writing, so it never closes
            // stdout, so the reader never returns. Python writes to stderr here on every call
            // (import-time deprecation warnings), so the buffer was never empty.
            var sb = new StringBuilder();
            using (var p = System.Diagnostics.Process.Start(psi))
            {
                p.OutputDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e)
                { if (e.Data != null) lock (sb) sb.AppendLine(e.Data); };
                p.ErrorDataReceived += delegate (object _s, System.Diagnostics.DataReceivedEventArgs e) { };
                try { p.BeginOutputReadLine(); p.BeginErrorReadLine(); } catch (Exception) { }
                bool exited = false;
                try { exited = p.WaitForExit(timeoutMs); } catch (Exception) { }
                if (!exited)
                {
                    // A child that outlived its budget is not going to be waited on further --
                    // the caller has a UI to keep responsive.
                    try { p.Kill(); } catch (Exception) { }
                    return "";
                }
            }
            lock (sb) return sb.ToString();
        }
        catch (Exception) { return ""; }
    }

    List<Dictionary<string, object>> ListUnlockGrants()
    {
        var outp = new List<Dictionary<string, object>>();
        string stdout = RunPyModule("tools.security", "list", 15000);
        try
        {
            var arr = _js.DeserializeObject(stdout) as object[];
            if (arr != null)
                foreach (object o in arr)
                {
                    var d = o as Dictionary<string, object>;
                    if (d != null) outp.Add(d);
                }
        }
        catch (Exception) { }
        return outp;
    }

    // .fleet/lock_state.json's most recent refusal, via tools/lock_state.py's own CLI -- the
    // primary path this panel exists for: a refusal happens, the operator opens 詳細設定, and
    // the refused client is already sitting at the top with an Allow button, never a box to
    // type an address into.
    Dictionary<string, object> ReadRefusedClient()
    {
        string stdout = RunPyModule("tools.lock_state", "show", 15000);
        try
        {
            var d = _js.DeserializeObject(stdout) as Dictionary<string, object>;
            if (d != null) return d;
        }
        catch (Exception) { }
        return new Dictionary<string, object>();
    }

    void GrantClientIp(string ip)
    {
        RunPyModule("tools.security", "grant \"" + ip.Replace("\"", "") + "\"", 15000);
    }

    void RevokeClientIp(string ip)
    {
        RunPyModule("tools.security", "revoke \"" + ip.Replace("\"", "") + "\"", 15000);
    }

    // Grant and revoke shell out too, so a click on 許可 froze the window for as long as the
    // subprocess took -- the same fault as building the list, reached from a different button.
    // The refresh that follows is queued from the same background thread, so the list is only
    // re-read once the write has actually landed.
    void RunClientAdmin(string verb, string ip)
    {
        var th = new Thread(new ThreadStart(delegate
        {
            try
            {
                if (verb == "grant") GrantClientIp(ip);
                else RevokeClientIp(ip);
            }
            catch (Exception) { }
            if (Dispatcher.HasShutdownStarted) return;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                try { RefreshClientRows(); } catch (Exception) { }
            }));
        }));
        th.IsBackground = true;
        th.Start();
    }

    // TWO PYTHON SUBPROCESSES, SO NOT ON THE UI THREAD.
    //
    // Opening 詳細設定 built this list inline, and building it shells out to
    // `python -m tools.security list` and `python -m tools.lock_state show` -- measured at 2 and
    // 4 seconds on this machine because each one imports the package. That ran on the dispatcher,
    // so the whole window stopped: the panel could not be dismissed, no other setting could be
    // clicked, and a WPF popup that cannot be dismissed keeps painting above every other window.
    // All three of those are one frozen thread, not three faults.
    void RefreshClientRows()
    {
        if (_clientRowsPanel == null) return;
        _clientRowsPanel.Children.Clear();
        var loading = new TextBlock();
        loading.Text = L("読み込み中…", "Loading…");
        loading.Foreground = Muted; loading.FontSize = 11; loading.Margin = new Thickness(0, 2, 0, 2);
        _clientRowsPanel.Children.Add(loading);

        var th = new Thread(new ThreadStart(delegate
        {
            List<Dictionary<string, object>> g2;
            Dictionary<string, object> r2;
            try { g2 = ListUnlockGrants(); } catch (Exception) { g2 = new List<Dictionary<string, object>>(); }
            try { r2 = ReadRefusedClient(); } catch (Exception) { r2 = new Dictionary<string, object>(); }
            if (Dispatcher.HasShutdownStarted) return;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                try { RenderClientRows(g2, r2); } catch (Exception) { }
            }));
        }));
        th.IsBackground = true;
        th.Start();
    }

    void RenderClientRows(List<Dictionary<string, object>> grants, Dictionary<string, object> refused)
    {
        // The panel may have been closed and rebuilt while the subprocesses ran.
        if (_clientRowsPanel == null) return;
        _clientRowsPanel.Children.Clear();
        string refusedIp = "";
        object rv;
        if (refused.TryGetValue("client_ip", out rv) && rv != null) refusedIp = rv.ToString();

        // Surface the refused IP up top UNLESS it is already granted and not expired -- an
        // already-resolved refusal shows up in the ordinary list below, not as "pending" too.
        if (!string.IsNullOrEmpty(refusedIp))
        {
            bool covered = false;
            foreach (var g in grants)
            {
                object ipv, ev;
                if (g.TryGetValue("ip", out ipv) && ipv != null && ipv.ToString() == refusedIp)
                {
                    bool expired = false;
                    if (g.TryGetValue("expired", out ev) && ev != null)
                        try { expired = Convert.ToBoolean(ev); } catch (Exception) { }
                    covered = !expired;
                    break;
                }
            }
            if (!covered) _clientRowsPanel.Children.Add(BuildPendingClientRow(refusedIp));
        }

        if (grants.Count == 0)
        {
            var none = new TextBlock();
            none.Text = L("(接続履歴なし)", "(no clients yet)");
            none.Foreground = Muted; none.FontSize = 11; none.Margin = new Thickness(0, 2, 0, 2);
            _clientRowsPanel.Children.Add(none);
            return;
        }
        foreach (var g in grants) _clientRowsPanel.Children.Add(BuildClientRow(g));
    }

    UIElement BuildPendingClientRow(string ip)
    {
        var card = new Border();
        card.Background = Theme.Br(Theme.SurfaceSubtle(_dark));
        card.BorderBrush = Theme.Br(Theme.Danger(_dark));
        card.BorderThickness = new Thickness(1);
        card.CornerRadius = new CornerRadius(Theme.RadSmall);
        card.Padding = new Thickness(8, 6, 8, 6);
        card.Margin = new Thickness(0, 0, 0, 6);

        var col = new StackPanel();
        var badge = new TextBlock();
        badge.Text = L("直近で拒否された接続", "Most recently refused");
        badge.Foreground = Theme.Br(Theme.Danger(_dark)); badge.FontSize = 10.5; badge.FontWeight = FontWeights.SemiBold;
        col.Children.Add(badge);

        var row = new DockPanel(); row.Margin = new Thickness(0, 4, 0, 0); row.LastChildFill = false;
        var allow = new Button();
        allow.Content = L("許可", "Allow");
        allow.FontSize = 11.5; allow.FontWeight = FontWeights.SemiBold; allow.Cursor = Cursors.Hand;
        allow.BorderThickness = new Thickness(0); allow.Padding = new Thickness(12, 3, 12, 3);
        // Filled green, matching what green means everywhere else in this list: allowed.
        allow.Template = FlatButtonTemplate(); allow.Background = Theme.Br(Theme.SuccessFill(_dark)); allow.Foreground = AccentFg;
        string capturedIp = ip;
        allow.Click += delegate { RunClientAdmin("grant", capturedIp); };
        DockPanel.SetDock(allow, Dock.Right); row.Children.Add(allow);

        var ipTb = new TextBlock(); ipTb.Text = ip; ipTb.Foreground = Fg; ipTb.FontSize = 12;
        ipTb.VerticalAlignment = VerticalAlignment.Center; ipTb.Margin = new Thickness(0, 0, 8, 0);
        DockPanel.SetDock(ipTb, Dock.Left); row.Children.Add(ipTb);
        col.Children.Add(row);

        card.Child = col;
        return card;
    }

    UIElement BuildClientRow(Dictionary<string, object> g)
    {
        string ip = "";
        object v;
        if (g.TryGetValue("ip", out v) && v != null) ip = v.ToString();
        bool expired = false;
        if (g.TryGetValue("expired", out v) && v != null)
            try { expired = Convert.ToBoolean(v); } catch (Exception) { }
        double remainingSec = 0;
        if (g.TryGetValue("remaining_seconds", out v) && v != null)
            try { remainingSec = Convert.ToDouble(v); } catch (Exception) { }

        var row = new DockPanel(); row.Margin = new Thickness(0, 3, 0, 3); row.LastChildFill = false;

        // COLOUR CARRIES THE ROW'S STATE, NOT THE BUTTON'S SEVERITY.
        //
        // Every row used to show 取り消し in red whatever its state, and 期限切れ in red as well,
        // so a list of ordinary allowed clients read as a list of problems and the one state
        // that IS a problem -- a connection being refused right now -- had no colour of its own.
        // Green means this client is allowed, grey means it lapsed, red is kept for the refused
        // card above the list.
        //
        // 拒否, not 取り消し: the button denies the client. "取り消し" reads as undoing your own
        // last action, which is not what it does.
        var revoke = new Button();
        revoke.Content = L("拒否", "Deny");
        revoke.FontSize = 11; revoke.Cursor = Cursors.Hand; revoke.BorderThickness = new Thickness(1);
        revoke.Padding = new Thickness(8, 3, 8, 3); revoke.Template = FlatButtonTemplate();
        revoke.Background = Brushes.Transparent; revoke.Foreground = Muted;
        revoke.BorderBrush = Border;
        revoke.ToolTip = L("この接続元を拒否します。", "Deny this client.");
        string capturedIp1 = ip;
        revoke.Click += delegate { RunClientAdmin("revoke", capturedIp1); };
        DockPanel.SetDock(revoke, Dock.Right); row.Children.Add(revoke);

        var allow = new Button();
        allow.Content = L("許可", "Allow");
        allow.FontSize = 11; allow.Cursor = Cursors.Hand; allow.BorderThickness = new Thickness(1);
        allow.Padding = new Thickness(8, 3, 8, 3); allow.Template = FlatButtonTemplate();
        allow.Background = BtnBg;
        allow.Foreground = expired ? Muted : Theme.Br(Theme.Success(_dark));
        allow.BorderBrush = expired ? Border : Theme.Br(Theme.Success(_dark));
        allow.Margin = new Thickness(0, 0, 6, 0);
        allow.ToolTip = expired
            ? L("再付与します。", "Grant again.")
            : L("延長（同じTTLで再付与）", "Extend (re-grant with the same TTL rule)");
        string capturedIp2 = ip;
        allow.Click += delegate { RunClientAdmin("grant", capturedIp2); };
        DockPanel.SetDock(allow, Dock.Right); row.Children.Add(allow);

        var statusTb = new TextBlock();
        statusTb.FontSize = 11; statusTb.VerticalAlignment = VerticalAlignment.Center;
        statusTb.Margin = new Thickness(0, 0, 8, 0);
        if (expired)
        {
            // Grey, not red. A grant that ran out is not a fault, and painting it the same as a
            // live refusal left the one row that needs attention indistinguishable from the rest.
            statusTb.Text = L("期限切れ", "Expired");
            statusTb.Foreground = Muted;
        }
        else
        {
            double days = remainingSec / 86400.0;
            statusTb.Text = days.ToString("0.#", CultureInfo.InvariantCulture) + L("日", "d");
            statusTb.Foreground = Muted;
        }
        DockPanel.SetDock(statusTb, Dock.Right); row.Children.Add(statusTb);

        var ipTb = new TextBlock(); ipTb.Text = ip;
        ipTb.Foreground = expired ? Muted : Fg;      // a lapsed row recedes as a whole
        ipTb.FontSize = 12;
        ipTb.VerticalAlignment = VerticalAlignment.Center; ipTb.TextTrimming = TextTrimming.CharacterEllipsis;
        ipTb.ToolTip = ip;
        DockPanel.SetDock(ipTb, Dock.Left); row.Children.Add(ipTb);

        return row;
    }

    UIElement BuildConnectedClientsControls()
    {
        var wrap = new StackPanel();
        var subHead = new TextBlock(); subHead.Text = L("接続クライアント", "Connected clients");
        subHead.Foreground = Fg; subHead.FontSize = 12.5; subHead.FontWeight = FontWeights.SemiBold;
        subHead.Margin = new Thickness(0, 12, 0, 4);
        wrap.Children.Add(subHead);

        var hint = new TextBlock();
        hint.Text = L("拒否された接続先はここで許可できます。", "Refused connections can be allowed here.");
        hint.Foreground = Muted; hint.FontSize = 10.5; hint.TextWrapping = TextWrapping.Wrap;
        hint.Margin = new Thickness(0, 0, 0, 4);
        wrap.Children.Add(hint);

        _clientRowsPanel = new StackPanel();
        RefreshClientRows();
        var scroll = new ScrollViewer();
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        scroll.MaxHeight = 170;
        scroll.Content = _clientRowsPanel;
        wrap.Children.Add(scroll);

        return wrap;
    }
}
