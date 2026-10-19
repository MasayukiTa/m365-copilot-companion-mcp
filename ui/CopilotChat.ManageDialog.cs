// CopilotChat.ManageDialog.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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

        var dock = new DockPanel { Margin = new Thickness(16, 16, 16, 16) };

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
        var footer = new DockPanel { Margin = new Thickness(0, 12, 0, 0) };
        var selAll = new Button { Content = T("select_all"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 6, 0), BorderThickness = new Thickness(1) };
        SetRef(selAll, BackgroundProperty, "PanelAlt"); SetRef(selAll, ForegroundProperty, "Fg"); SetRef(selAll, Control.BorderBrushProperty, "Border");
        var clrAll = new Button { Content = T("clear_all"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 12, 0), BorderThickness = new Thickness(1) };
        SetRef(clrAll, BackgroundProperty, "PanelAlt"); SetRef(clrAll, ForegroundProperty, "Fg"); SetRef(clrAll, Control.BorderBrushProperty, "Border");
        var fetchBtn = new Button { Content = T("fetch_copilot"), Cursor = Cursors.Hand, FontSize = 12, Padding = new Thickness(12, 5, 12, 6), Margin = new Thickness(0, 0, 12, 0), BorderThickness = new Thickness(1) };
        SetRef(fetchBtn, BackgroundProperty, "PanelAlt"); SetRef(fetchBtn, ForegroundProperty, "Fg"); SetRef(fetchBtn, Control.BorderBrushProperty, "Border");
        if (localOnly) fetchBtn.IsEnabled = false;   // a run is live -> don't touch the page
        var countLbl = new TextBlock { Text = "", FontSize = 12.5, VerticalAlignment = VerticalAlignment.Center };
        SetRef(countLbl, TextBlock.ForegroundProperty, "Muted");
        var closeBtn = new Button { Content = T("close"), Cursor = Cursors.Hand, FontSize = 12.5, Padding = new Thickness(16, 6, 16, 7), Margin = new Thickness(8, 0, 0, 0), BorderThickness = new Thickness(1), FontWeight = FontWeights.SemiBold };
        SetRef(closeBtn, BackgroundProperty, "PanelAlt"); SetRef(closeBtn, ForegroundProperty, "Fg"); SetRef(closeBtn, Control.BorderBrushProperty, "Border");
        closeBtn.Click += delegate { win.Close(); };
        var delBtn = new Button { Content = T("del_selected"), Cursor = Cursors.Hand, FontSize = 12.5, Padding = new Thickness(16, 6, 16, 7), BorderThickness = new Thickness(0), FontWeight = FontWeights.SemiBold };
        SetRef(delBtn, BackgroundProperty, "AccentFill"); SetRef(delBtn, ForegroundProperty, "AccentFg");
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
        var listBorder = new Border { Child = listScroll, CornerRadius = new CornerRadius(Theme.RadCard) };
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
                try { j = BridgeCall("/agent_conversations", 120000); }
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
                        if (_conv.Id == c.Id) { StopFollowingTranscript(); _conv = new Conversation(); _messages.Children.Clear(); }
                    }));
                    deleted++;
                    if (!localOnly && !string.IsNullOrEmpty(c.ConvUrl))
                    {
                        bool ok = false; string reason = null;
                        try
                        {
                            // a Copilot-side delete (goto + menu ops + GUID-disappearance verify) can take
                            // ~40s; give it 120s so the HTTP call doesn't time out mid-delete.
                            var j = BridgeCall("/delete?url=" + Uri.EscapeDataString(c.ConvUrl) + "&title=" + Uri.EscapeDataString(c.Title ?? ""), 120000);
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
                    if (_all.Count == 0) { StopFollowingTranscript(); _conv = new Conversation(); _all.Add(_conv); _messages.Children.Clear(); }
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
}
