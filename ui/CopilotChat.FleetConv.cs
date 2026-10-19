// CopilotChat.FleetConv.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
}
