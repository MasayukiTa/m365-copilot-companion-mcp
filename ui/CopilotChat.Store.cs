// CopilotChat.Store.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
    // ── persistence (manual base64 store) ───────────────────────────────────────
    string Path_(string id) { return Path.Combine(StoreDir, id + ".chat"); }
    static string B64(string s) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(s == null ? "" : s)); }
    static string UnB64(string s) { try { return Encoding.UTF8.GetString(Convert.FromBase64String(s)); } catch { return ""; } }

    // THE SAVE THAT TELLS YOU WHEN IT DID NOT SAVE. This was `catch { }`: a full disk, a locked or
    // read-only folder, or a path that could not be created lost the conversation and said
    // nothing, so a chat could be on screen and nowhere else. A failure is now counted, written to
    // chat_save_errors.log beside the chats, and shown once per reason as a line in the chat.
    int _chatUserTurnsSent;      // user lines sent from this window in this process
    int _chatSaveFailures;       // saves (file or bridge store) that did not land

    void NoteNotSaved(Conversation c, string reason)
    {
        _chatSaveFailures++;
        try
        {
            Directory.CreateDirectory(StoreDir);
            File.AppendAllText(Path.Combine(StoreDir, "chat_save_errors.log"),
                DateTime.Now.ToString("s") + "\t" + (c == null ? "" : c.Id) + "\t" + (reason ?? "") + "\tsent=" + _chatUserTurnsSent + " failures=" + _chatSaveFailures + "\n",
                new UTF8Encoding(false));
        }
        catch (Exception) { /* the log is the second place; the notice below is the first */ }
        if (reason == _lastNotSavedReason) return;     // once per reason, not once per send
        _lastNotSavedReason = reason;
        Action show = delegate { if (c == null || ReferenceEquals(c, _conv)) AddAssistant(T("not_saved") + reason); };
        if (Dispatcher.CheckAccess()) show(); else Dispatcher.BeginInvoke(show);
    }

    // The user's line is kept the moment it is sent, not when (if) the answer comes back. A fleet
    // conversation has no .chat file of its own: its durable record is the command file the fleet
    // consumes, whose write failure ChatSend already reports (fleet_send_failed).
    void PersistSentTurn()
    {
        _chatUserTurnsSent++;
        var c = _conv;
        if (c == null || c.Source == "fleet" || c.Messages.Count == 0) return;
        SaveConversation(c);
    }

    bool SaveConversation(Conversation c)
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
            File.WriteAllText(Path_(c.Id), sb.ToString(), new UTF8Encoding(false));
            _lastNotSavedReason = "";      // a good save re-arms the notice for the next failure
            return true;
        }
        catch (Exception ex)
        {
            NoteNotSaved(c, ex.GetType().Name + ": " + ex.Message);
            return false;
        }
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
                    catch (Exception ex) { c.Ts = 0; NoteTranscriptLineFailure(f, ex); }
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
        catch (Exception ex)
        {
            ConvListing.Diag(Path.GetDirectoryName(_convsPath), "saved chats not readable: " + ex);
            _historyNotice = ConvListing.UnreadableNotice(_lang == 0, ex.Message);
        }
        DiscoverTranscripts();   // surface every fleet worker's disk transcript as a past chat
        // Restore what was open last time. _all[0] is just the first record read off disk --
        // neither the newest nor the one being worked on -- so startup used to reopen whatever
        // happened to sit at the top of the file, leaving a months-old conversation resident.
        if (_all.Count > 0)
        {
            Conversation want = null;
            if (!string.IsNullOrEmpty(_lastOpenId))
                foreach (var c in _all) if (c.Id == _lastOpenId) { want = c; break; }
            if (want == null)
            {
                want = _all[0];                       // fall back to the most recent by activity
                foreach (var c in _all) if (c.Ts > want.Ts) want = c;
            }
            _conv = want;
            foreach (var m in _conv.Messages) { if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text); }
            // Resume live-follow for whatever was open at last exit, if it is still a
            // live-tracked fleet worker -- otherwise the operator would have to touch the
            // sidebar once just to re-arm updates for the conversation already on screen.
            MaybeFollowConversation(_conv, _conv.Transcript);
        }
        else { _conv = new Conversation(); _all.Add(_conv); }
        ShowEmptyState();        // no-op if the active conversation rendered any real message
        RefreshConvList();
    }

    // Surface every fleet worker's disk transcript as a sidebar conversation, so PAST chats are
    // browsable straight from disk. The old path re-scraped live via the bridge, which fails for any
    // conversation whose agent the bridge is not on -> not one past chat was retrievable. Newest
    // first, capped so a huge dir doesn't flood the list; dedup by transcript path; sub-agent
    // (research) child transcripts are skipped (they nest under their parent on open).
    //
    // PAGED, NOT CAPPED (2026-10-05). The scan used to stop at the newest 80 and never run again,
    // so 3,229 of 3,309 transcripts on disk were not in the sidebar at all. The directory is now
    // indexed once (names and mtimes only: no file is opened) and the sidebar takes it a page at
    // a time: the newest ConvListing.FirstPage at startup, then one local day per "older" click
    // (LoadOlderPage). Opening a row still reads the local transcript first.
    List<KeyValuePair<string, DateTime>> _transcriptIndex;
    int _transcriptPos;

    int TranscriptsPending()
    {
        return _transcriptIndex == null ? 0 : Math.Max(0, _transcriptIndex.Count - _transcriptPos);
    }

    void DiscoverTranscripts()
    {
        try
        {
            string tdir = Path.Combine(Path.GetDirectoryName(_convsPath), "transcripts");
            if (!Directory.Exists(tdir)) return;
            // Merged glob: relay/fleet_retention.py gzips anything older than
            // COMPRESS_AFTER_HOURS and deletes the plain file, so "*.jsonl" alone only ever
            // shows the last few hours. See ConvListing.ListTranscriptFiles for the dedupe rule.
            _transcriptIndex = ConvListing.ListMainTranscripts(tdir);
            _transcriptPos = 0;
            LoadTranscriptPage();
        }
        catch (Exception ex)
        {
            ConvListing.Diag(Path.GetDirectoryName(_convsPath), "transcript scan failed: " + ex);
            _historyNotice = ConvListing.UnreadableNotice(_lang == 0, ex.Message);
        }
    }

    // The "older" row's action: add the next page (one day) of transcripts to the sidebar and
    // open the Archived section so they are visible. A page whose rows were all already listed
    // (through the registry) is skipped over rather than reported as empty.
    void LoadOlderPage()
    {
        int added = 0;
        try
        {
            while (added == 0 && TranscriptsPending() > 0) added = LoadTranscriptPage();
        }
        catch (Exception ex)
        {
            ConvListing.Diag(Path.GetDirectoryName(_convsPath), "older page failed: " + ex);
            _historyNotice = ConvListing.UnreadableNotice(_lang == 0, ex.Message);
        }
        _sectionCollapsed["archived"] = false;
        _sectionExpanded.Add("archived");
        RefreshConvList();
    }

    // Adds the next page of the transcript index to _all; returns how many rows it added.
    int LoadTranscriptPage()
    {
        if (_transcriptIndex == null) return 0;
        int end = ConvListing.PageEnd(_transcriptIndex, _transcriptPos, ConvListing.MaxPage);
        var known = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var c in _all)
        {
            if (!string.IsNullOrEmpty(c.Transcript)) known.Add(StripGz(c.Transcript));
            foreach (string tx in c.Transcripts)
                if (!string.IsNullOrEmpty(tx)) known.Add(StripGz(tx));
        }
        int added = 0;
        for (int i = _transcriptPos; i < end; i++)
        {
            string f = _transcriptIndex[i].Key;
            if (known.Contains(StripGz(f))) continue;
            string goal = "", name = "", guid = "";
            try
            {
                using (var sr = OpenTranscriptReader(f))
                {
                    string first = sr.ReadLine();
                    if (!string.IsNullOrEmpty(first))
                    {
                        var meta = _cjs.DeserializeObject(first) as Dictionary<string, object>;
                        if (meta != null) { goal = SS(meta, "goal"); name = SS(meta, "name"); }
                    }
                    // THE CONVERSATION'S OWN IDENTITY, written by _tx.note_guid on the
                    // first poll after the worker's first turn. Without it ConvUrl stays
                    // empty and _activeFleetUrl never arms, so neither steer mode nor the
                    // live snapshot refresh can recognise the open conversation.
                    // Bounded: the guid line lands within the first turn or not at all,
                    // and this runs for up to a page of transcripts.
                    for (int li = 0; li < 40 && guid.Length == 0; li++)
                    {
                        string ln2 = sr.ReadLine();
                        if (ln2 == null) break;
                        if (ln2.IndexOf("\"guid\"", StringComparison.Ordinal) < 0) continue;
                        var gd = _cjs.DeserializeObject(ln2) as Dictionary<string, object>;
                        if (gd != null) guid = SS(gd, "guid");
                    }
                }
            }
            catch (Exception ex)
            {
                // The row is still listed (by file name) and still opens; only its title and
                // identity are missing. Logged, because a file that cannot be read here is
                // usually one that cannot be opened either.
                NoteTranscriptLineFailure(f, ex);
            }
            string title = goal.Length > 0 ? (goal.Length > 54 ? goal.Substring(0, 54) + "…" : goal)
                                           : Path.GetFileNameWithoutExtension(StripGz(f));
            double ts = (_transcriptIndex[i].Value - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            var discovered = new Conversation { Transcript = f, Name = name, Title = title, Source = "fleet",
                                        Ts = ts, Goal = goal,
                                        // "sess:<guid>" -- the bridge's own shape for a
                                        // conversation with no navigable URL. NOT a url:
                                        // calling it one is how a resume silently becomes
                                        // a fresh chat.
                                        ConvUrl = guid.Length > 0 ? "sess:" + guid : "" };
            discovered.Transcripts.Add(f);
            _all.Add(discovered);
            known.Add(StripGz(f));
            added++;
        }
        _transcriptPos = end;
        return added;
    }

    string BridgeCall(string path) { return BridgeCall(path, 60000); }

    // Every bridge endpoint except the reachability probe goes through here: an authenticated
    // POST (ui/BridgeClient.cs). A refusal the person has to act on -- no token file, a bridge
    // older than this window -- is ALSO put in the banner, because most call sites catch and
    // swallow exceptions, and a door that fails in silence is how "nothing happened" looks.
    string BridgeCall(string path, int timeoutMs)
    {
        if (WindowSelfTest.Active) throw new InvalidOperationException("--selftest touches no network: " + path);
        try { return BridgeClient.Call(_bridge, path, timeoutMs); }
        catch (BridgeClientException ex) { NoteBridgeProblem(ex); throw; }
    }

    void NoteBridgeProblem(Exception ex)
    {
        var msg = ex.Message;
        try
        {
            Dispatcher.BeginInvoke(new Action(delegate
            {
                // At most once every 30 s: a sidebar refresh can make several calls in a row.
                if ((DateTime.UtcNow - _bridgeProblemShown).TotalSeconds < 30) return;
                _bridgeProblemShown = DateTime.UtcNow;
                ShowRecoveryBanner(T("bridge_auth_problem") + "\n" + msg, T("close"), delegate { HideBanner(); });
            }));
        }
        catch { }
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
