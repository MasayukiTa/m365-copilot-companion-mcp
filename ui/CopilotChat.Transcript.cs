// CopilotChat.Transcript.cs -- part of `partial class ChatWindow` (see CopilotChat.cs). Moved, not changed.
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
    // ── live-follow of the OPEN fleet transcript ─────────────────────────────────────────
    //
    // "別のを開いて開きなおさないと現行のが見えない" -- the operator should never have to
    // leave and come back to see a running conversation's new turns. status.json's mtime-poll
    // (CheckFleetSnapshot, above) already re-renders a live snapshot, but it is keyed to the
    // WHOLE fleet's status file and a 20s "is this fresh" heuristic that can go stale while a
    // worker is genuinely still writing. This instead watches the ONE open transcript file
    // directly with a FileSystemWatcher and appends bytes as they land -- no tick, no re-read
    // of anything but the new tail, and it can never fight over what is "new" with the status
    // poll because every line it renders is folded into _fleetTurnSigs, which the status poll
    // treats as "already on screen".
    FileSystemWatcher _txWatcher;          // non-null while a transcript is being followed
    string _txWatchPath;                   // full path of the plain .jsonl being followed
    string _txWatchConvKey;                // Conversation.ConvUrl this follow belongs to -- a
                                            // stale callback for a conversation the user has
                                            // since left is dropped rather than misapplied
    long _txReadOffset;                    // bytes of _txWatchPath already consumed

    volatile bool _txRecheckQueued;                // another change arrived while one was running

    // Begin following 'path' for appends, starting from byte offset 'fromOffset' (the length
    // already rendered on screen, so nothing already shown is re-emitted). 'convKey' is the
    // Conversation.ConvUrl this follow belongs to; a callback only applies its result while
    // _conv still matches it. Any previous follow is torn down first -- exactly one transcript
    // is ever watched at a time. A '.gz' path (fleet_retention already archived it) or a
    // missing directory is a no-op: there is nothing left that could still grow.
    void StartFollowingTranscript(string path, long fromOffset, string convKey)
    {
        StopFollowingTranscript();
        try
        {
            if (string.IsNullOrEmpty(path)) return;
            if (path.EndsWith(".gz", StringComparison.OrdinalIgnoreCase)) return;
            string dir = Path.GetDirectoryName(path);
            string file = Path.GetFileName(path);
            if (string.IsNullOrEmpty(dir) || string.IsNullOrEmpty(file) || !Directory.Exists(dir)) return;
            _txWatchPath = path;
            _txWatchConvKey = convKey ?? "";
            _txReadOffset = fromOffset > 0 ? fromOffset : 0;
            _txPartialLine = new byte[0];
            var w = new FileSystemWatcher(dir, file);
            w.NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.FileName;
            w.Changed += OnTranscriptFileEvent;
            w.Created += OnTranscriptFileEvent;
            w.Renamed += OnTranscriptFileEvent;
            // fleet_retention deletes the plain file once it has gzipped it -- that is the normal,
            // quiet end of a follow, handled inside ConsumeFollowedTranscriptOnce (File.Exists check),
            // not treated as an error here.
            w.Deleted += OnTranscriptFileEvent;
            w.Error += delegate { Dispatcher.BeginInvoke(new Action(delegate { if (_txWatchPath == path) StopFollowingTranscript(); })); };
            w.EnableRaisingEvents = true;
            _txWatcher = w;
        }
        catch { StopFollowingTranscript(); }
    }

    // Tear down the current follow, if any. Safe to call when nothing is being followed.
    void StopFollowingTranscript()
    {
        var w = _txWatcher;
        _txWatcher = null; _txWatchPath = null; _txWatchConvKey = null;
        _txReadOffset = 0; _txPartialLine = new byte[0];
        if (w == null) return;
        try { w.EnableRaisingEvents = false; } catch { }
        try
        {
            w.Changed -= OnTranscriptFileEvent; w.Created -= OnTranscriptFileEvent;
            w.Renamed -= OnTranscriptFileEvent; w.Deleted -= OnTranscriptFileEvent;
        }
        catch { }
        try { w.Dispose(); } catch { }
    }

    // FileSystemWatcher fires on a threadpool thread, in bursts, and can fire before the
    // writer's flush lands -- so this never assumes the event means "a whole new line is ready".
    // It just means "go look again"; ConsumeFollowedTranscriptOnce does the actual, safe read.
    void OnTranscriptFileEvent(object sender, FileSystemEventArgs e)
    {
        // Serialize catch-up passes instead of running them concurrently: if one is already in
        // flight, mark that another look is needed and let IT pick up the new bytes when done,
        // rather than piling up threadpool threads racing the same file/offset.
        if (!Monitor.TryEnter(_txFollowLock)) { _txRecheckQueued = true; return; }
        try
        {
            do { _txRecheckQueued = false; ConsumeFollowedTranscriptOnce(); }
            while (_txRecheckQueued);
        }
        finally { Monitor.Exit(_txFollowLock); }
    }

    static byte[] Combine(byte[] a, byte[] b)
    {
        if (a == null || a.Length == 0) return b ?? new byte[0];
        if (b == null || b.Length == 0) return a;
        byte[] r = new byte[a.Length + b.Length];
        Buffer.BlockCopy(a, 0, r, 0, a.Length);
        Buffer.BlockCopy(b, 0, r, a.Length, b.Length);
        return r;
    }

    // Read exactly the bytes appended since the last pass, parse whatever complete JSONL lines
    // they contain, and append those as messages -- never re-reads or re-renders anything
    // already on screen. Runs on the watcher's threadpool thread; only the UI application at
    // the end is marshaled onto the dispatcher.
    void ConsumeFollowedTranscriptOnce()
    {
        string path = _txWatchPath;
        string convKey = _txWatchConvKey;
        if (string.IsNullOrEmpty(path)) return;
        var newMsgs = new List<Msg>();
        try
        {
            if (!File.Exists(path))
            {
                // The run finished and fleet_retention gzipped (and deleted) the plain file, or it
                // otherwise vanished. That is not an error -- just the quiet end of this follow.
                Dispatcher.BeginInvoke(new Action(delegate { if (_txWatchPath == path) StopFollowingTranscript(); }));
                return;
            }
            byte[] chunk;
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                long len = fs.Length;
                if (len < _txReadOffset) { _txReadOffset = 0; _txPartialLine = new byte[0]; }  // truncated/replaced -- resync
                long toRead = len - _txReadOffset;
                if (toRead <= 0) return;   // nothing new since last pass
                fs.Seek(_txReadOffset, SeekOrigin.Begin);
                chunk = new byte[toRead];
                int off = 0;
                while (off < chunk.Length)
                {
                    int n = fs.Read(chunk, off, chunk.Length - off);
                    if (n <= 0) break;   // read landed mid-flush -- whatever we got becomes the new partial tail
                    off += n;
                }
                if (off < chunk.Length) Array.Resize(ref chunk, off);
                _txReadOffset += off;
            }

            // Split at the BYTE level, not after decoding the whole chunk as UTF8: 0x0A never
            // appears as a continuation byte of a multi-byte UTF-8 sequence, so a segment between
            // two 0x0A bytes is always a complete, independently-decodable run of characters --
            // decoding a still-partial multi-byte tail is exactly the corruption this avoids.
            byte[] all = Combine(_txPartialLine, chunk);
            int start = 0;
            for (int i = 0; i < all.Length; i++)
            {
                if (all[i] != (byte)'\n') continue;
                int lineLen = i - start;
                if (lineLen > 0 && all[start + lineLen - 1] == (byte)'\r') lineLen--;   // CRLF -> LF
                string ln = Encoding.UTF8.GetString(all, start, lineLen);
                Msg m;
                if (TryParseTranscriptLine(ln, out m)) newMsgs.Add(m);
                start = i + 1;
            }
            // Whatever follows the last '\n' (possibly nothing) is an incomplete line -- buffered
            // verbatim and left UNPARSED until the rest of it arrives on a later pass.
            int remain = all.Length - start;
            var partial = new byte[remain];
            if (remain > 0) Array.Copy(all, start, partial, 0, remain);
            _txPartialLine = partial;
        }
        catch (IOException) { return; }   // writer holds the range mid-flush; the next event retries
        catch { return; }

        if (newMsgs.Count == 0) return;
        Dispatcher.BeginInvoke(new Action(delegate
        {
            // The open conversation may have changed since this pass started; apply the result
            // only to the SAME one this follow was started for.
            if (_txWatchPath != path || _conv == null || _conv.ConvUrl != convKey) return;
            foreach (var m in newMsgs)
            {
                if (m.Role == "U") AddUser(m.Text); else AddAssistant(m.Text);
                _conv.Messages.Add(m);
                // Keep the status-poll's own diff baseline in sync so its next tick sees these
                // lines as already-rendered, instead of re-appending or rebuilding over them.
                _fleetTurnSigs.Add(m.Role + "" + m.Text);
            }
        }));
    }

    // Resume/stop following the ONE fleet transcript that matches the just-opened conversation.
    // Called every time a conversation is put on screen (from OpenFromFleet, from re-opening an
    // already-cached fleet conversation, and from the startup restore) so a watcher never lingers
    // on a conversation the user has left, and one is (re)armed whenever the newly-opened one is
    // still a live-tracked fleet worker with an on-disk, not-yet-archived transcript.
    void MaybeFollowConversation(Conversation c, string transcriptHint)
    {
        try
        {
            string key = c != null ? (c.ConvUrl ?? "") : "";
            var wkr = ReadFleetWorker(key);
            if (wkr == null) { StopFollowingTranscript(); return; }
            string tp = SS(wkr, "transcript");
            if (string.IsNullOrEmpty(tp)) tp = transcriptHint;
            if (string.IsNullOrEmpty(tp) && c != null && !string.IsNullOrEmpty(c.Name))
                tp = NewestTranscriptForWorker(c.Name);
            if (string.IsNullOrEmpty(tp) || tp.EndsWith(".gz", StringComparison.OrdinalIgnoreCase))
            { StopFollowingTranscript(); return; }
            long startOff = 0;
            try { if (File.Exists(tp)) startOff = new FileInfo(tp).Length; } catch { }
            // Sync the status-poll's diff baseline to exactly what is already on screen, so its
            // next tick does not treat this fresh follow's starting point as something to append.
            _fleetTurnSigs = new List<string>();
            if (c != null) foreach (var m in c.Messages) _fleetTurnSigs.Add(m.Role + "" + m.Text);
            StartFollowingTranscript(tp, startOff, key);
        }
        catch { StopFollowingTranscript(); }
    }

    static string SS(Dictionary<string, object> d, string k) { return ChatSend.SS(d, k); }

    static List<string> TranscriptListField(Dictionary<string, object> d, string key)
    {
        var outp = new List<string>();
        if (d == null || !d.ContainsKey(key) || d[key] == null) return outp;
        var arr = d[key] as object[];
        if (arr == null) return outp;
        foreach (object o in arr)
        {
            string text = o == null ? "" : o.ToString().Trim();
            if (text.Length > 0) outp.Add(text);
        }
        return outp;
    }

    List<string> RegistryTranscriptLineage(Dictionary<string, object> d)
    {
        return FleetConvIdentity.MergeTranscriptLineage(
            null, TranscriptListField(d, "transcripts"), SS(d, "transcript"));
    }

    // Resolve one shared-conversation lineage from the registry. Real/sess URLs are the primary
    // identity. For legacy rows without a URL, mirror the registry writer's (source,name) key.
    // A latest hint is appended only when the registry has not seen it yet.
    List<string> RegistryTranscriptLineageFor(string url, string worker, string latestHint)
    {
        try
        {
            foreach (object o in ReadConvsRegistry())
            {
                var d = o as Dictionary<string, object>;
                if (d == null) continue;
                string du = SS(d, "url");
                string dn = SS(d, "name");
                string ds = SS(d, "source");
                string dt = SS(d, "transcript");
                bool hit = false;
                if (!string.IsNullOrEmpty(url)) hit = (du == url);
                else if (!string.IsNullOrEmpty(worker)) hit = (du.Length == 0 && ds == "fleet" && dn == worker);
                if (!hit && !string.IsNullOrEmpty(latestHint)) hit = (dt == latestHint);
                if (!hit) continue;
                return FleetConvIdentity.MergeTranscriptLineage(
                    RegistryTranscriptLineage(d), null, latestHint);
            }
        }
        catch (Exception ex) { NoteTranscriptLineFailure(url, ex); }
        return FleetConvIdentity.MergeTranscriptLineage(null, null, latestHint);
    }

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

    // relay/fleet_retention.py::compress() gzips any transcript older than COMPRESS_AFTER_HOURS
    // (default 6h) to "<name>.jsonl.gz" and DELETES the plain file -- introduced 2026-09-01. This
    // reader was never updated, so every one of the three lookups below used to see only the last
    // few hours of transcripts and silently drop the rest (no error, just an empty-looking history).
    // These three helpers are the single place that knows about both name shapes; every call site
    // routes through them so the bug class can't reappear one glob at a time.
    //
    // Strip a trailing ".gz" to get the logical (uncompressed) transcript identity, so a plain file
    // and its compressed successor are recognised as the SAME transcript.
    static string StripGz(string path) { return ConvListing.StripGz(path); }

    // True if the transcript exists on disk in EITHER form (plain or gzipped), regardless of
    // which spelling the caller has.
    static bool TranscriptFileExists(string path)
    {
        if (string.IsNullOrEmpty(path)) return false;
        if (File.Exists(path)) return true;
        return path.EndsWith(".gz", StringComparison.OrdinalIgnoreCase)
            ? File.Exists(StripGz(path)) : File.Exists(path + ".gz");
    }

    // Enumerate every transcript in tdir, merging "*.jsonl" and "*.jsonl.gz" into one list
    // deduped by logical name (StripGz) -- when both spellings exist for the same transcript
    // (a race with fleet_retention mid-compress), the plain file wins the slot.
    static List<string> ListTranscriptFiles(string tdir) { return ConvListing.ListTranscriptFiles(tdir); }

    // Open a transcript for reading regardless of compression: resolves to whichever of
    // path/path+".gz" actually exists on disk (a caller may hand either spelling), then wraps the
    // stream in GZipStream when needed. Every read of a transcript's bytes must go through this --
    // see relay/fleet_retention.py::open_maybe_gz(), whose docstring says readers must not have to
    // know about compression. This is that contract kept on the C# side.
    static StreamReader OpenTranscriptReader(string path)
    {
        string actual = path;
        if (!File.Exists(actual))
        {
            actual = actual.EndsWith(".gz", StringComparison.OrdinalIgnoreCase) ? StripGz(actual) : actual + ".gz";
        }
        var fsr = new FileStream(actual, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        if (actual.EndsWith(".gz", StringComparison.OrdinalIgnoreCase))
            return new StreamReader(new GZipStream(fsr, CompressionMode.Decompress), Encoding.UTF8);
        return new StreamReader(fsr, Encoding.UTF8);
    }

    // Locate a worker's on-disk transcript by NAME, independent of the live status.json worker
    // entry. Transcripts are named "<runid>_a<agent>_w<N>.jsonl" (or "...jsonl.gz" once
    // fleet_retention compresses it) and OUTLIVE the live worker dict, so this lets click-to-open
    // load the full conversation for a finished/restarted/history worker (when ReadFleetWorker
    // returns null) instead of falling back to the "not available" placeholder.
    // Returns the NEWEST matching file (the latest run for that worker), or "" if none.
    string NewestTranscriptForWorker(string worker)
    {
        try
        {
            if (string.IsNullOrEmpty(worker)) return "";
            string tdir = Path.Combine(Path.GetDirectoryName(_convsPath), "transcripts");
            if (!Directory.Exists(tdir)) return "";
            string suffix = "_" + worker + ".jsonl";   // exact suffix: "w1" must not match "w10"
            string gzSuffix = suffix + ".gz";
            string newest = null; DateTime best = DateTime.MinValue;
            var candidates = new List<string>(Directory.GetFiles(tdir, "*" + suffix));
            candidates.AddRange(Directory.GetFiles(tdir, "*" + gzSuffix));
            foreach (string f in candidates)
            {
                string name = Path.GetFileName(f);
                if (!(name.EndsWith(suffix, StringComparison.Ordinal) || name.EndsWith(gzSuffix, StringComparison.Ordinal))) continue;
                DateTime t = File.GetLastWriteTimeUtc(f);
                if (t > best) { best = t; newest = f; }
            }
            return newest ?? "";
        }
        catch { return ""; }
    }

    // Read a worker's persisted full-text transcript (.fleet/transcripts/<key>.jsonl, one
    // JSON object per line) into ordered messages. Untruncated. Returns an empty list if the
    // path is empty/missing/unreadable (caller falls back to /history or the snapshot).
    // Fully isolated: a parse hiccup on one line is skipped, never thrown.
    List<Msg> ReadTranscript(string path)
    {
        var msgs = new List<Msg>();
        if (string.IsNullOrEmpty(path) || !TranscriptFileExists(path)) return msgs;
        try
        {
            string[] lines;
            using (var sr = OpenTranscriptReader(path))
                lines = sr.ReadToEnd().Replace("\r", "").Split('\n');
            foreach (string ln in lines)
            {
                Msg m;
                if (TryParseTranscriptLine(ln, out m)) msgs.Add(m);
            }
        }
        catch { }
        return msgs;
    }

    List<Msg> ReadTranscriptLineage(IEnumerable<string> paths)
    {
        var msgs = new List<Msg>();
        var seen = new List<string>();
        if (paths == null) return msgs;
        foreach (string raw in paths)
        {
            string path = (raw ?? "").Trim();
            if (path.Length == 0 || seen.Contains(path)) continue;
            seen.Add(path);
            var part = ReadTranscript(path);
            if (part.Count > 0) msgs.AddRange(part);
            AppendSubAgentTranscripts(msgs, path);
        }
        return msgs;
    }

    // The goal text off a transcript's own first ("meta") line -- relay/relay_fleet.py's
    // _Transcript writes it once, at file creation, and it never changes afterward (a retry
    // opens a new attempt file with the SAME goal; see relay_fleet.py's fresh_replay). Used as
    // the last-resort source of identity for a fleet conversation opened by click: the live
    // status.json worker dict is the first choice (see OpenFromFleet) because it survives a
    // finished worker's slot being reused, but a restarted fleet or an old run has no worker
    // dict at all, only this file. Returns "" on any read/parse failure -- never throws.
    string TranscriptMetaGoal(string path)
    {
        if (string.IsNullOrEmpty(path) || !TranscriptFileExists(path)) return "";
        try
        {
            using (var sr = OpenTranscriptReader(path))
            {
                string first = sr.ReadLine();
                if (string.IsNullOrEmpty(first)) return "";
                var meta = _cjs.DeserializeObject(first) as Dictionary<string, object>;
                return meta != null ? SS(meta, "goal") : "";
            }
        }
        catch (Exception ex) { NoteTranscriptLineFailure(path, ex); return ""; }
    }

    // One JSONL transcript line -> a turn, or nothing. Shared by the full read above and the
    // incremental live-follow below so the two can never disagree about what counts as a turn.
    //
    // NOT EVERY RECORDED ROLE IS A TURN. The test used to be `role.StartsWith("user") ? "U" :
    // "A"`, so EVERYTHING that was not a user line became an assistant line -- including
    // `metric`, which the fleet writes with an empty text. Those rendered as a "Copilot" label
    // with nothing under it, and a reader looking for what the agent said mid-run found a blank
    // where the answer should be. Reported as "copilotの分が途中のが表示されないケースが".
    //
    // Asked as "is this a turn with something in it", because that is the question: a role
    // nobody has seen before should be shown if it carries text, and an empty record of any
    // role is bookkeeping, not speech.
    bool TryParseTranscriptLine(string ln, out Msg msg)
    {
        msg = null;
        if (string.IsNullOrEmpty(ln)) return false;
        Dictionary<string, object> o;
        try { o = _cjs.DeserializeObject(ln) as Dictionary<string, object>; }
        catch (Exception ex)
        {
            // A line that does not parse is a turn the reader could not show. The writer appends
            // while this runs, so a half-written LAST line is expected and harmless; anything
            // else is not, and used to vanish without a trace (a line over the serializer's
            // 2 MB default was dropped here, which is how a long answer went missing).
            NoteTranscriptLineFailure(ln, ex);
            return false;
        }
        if (o == null) return false;
        if (!o.ContainsKey("role")) return false;   // skip meta / guid marker lines
        string role = o["role"] != null ? o["role"].ToString() : "assistant";
        string text = (o.ContainsKey("text") && o["text"] != null) ? o["text"].ToString() : "";
        if (role == "metric" || role == "meta" || role == "guid" || role == "note") return false;
        if (text.Trim().Length == 0) return false;
        msg = new Msg(role.StartsWith("user") ? "U" : "A", text);
        return true;
    }

    int _lineFailures;
    void NoteTranscriptLineFailure(string ln, Exception ex)
    {
        if (++_lineFailures > 20) return;      // a damaged file must not fill the log
        ConvListing.Diag(Path.GetDirectoryName(_convsPath),
            "transcript line (" + (ln == null ? 0 : ln.Length) + " chars) not readable: "
            + ex.GetType().Name + ": " + ex.Message);
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
                // The magnifier and the test tube were doing the work of a word, in a face this UI does
        // not control and at a size it cannot set. The words are shorter to read anyway.
        string kind = sf.Contains("__sub_research_") ? "research" : "sub-agent";
                msgs.Add(new Msg("A", "──────── " + kind + (_lang == 0 ? "（サブエージェント）" : " (sub-agent)") + " ────────"));
                msgs.AddRange(sm);
            }
        }
        catch { }
    }
}
