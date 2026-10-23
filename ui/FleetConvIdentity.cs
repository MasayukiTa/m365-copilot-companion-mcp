// FleetConvIdentity.cs -- the identity-merge DECISION for a fleet conversation's Goal /
// Transcript / Source / Name fields, extracted out of ui/CopilotChat.cs (commit 3c93b59) so it
// can be run by a test instead of only read as text.
//
// WHY THIS IS ITS OWN FILE. Before 3c93b59, OpenFromFleet() and SyncRegistry() (both in
// ui/CopilotChat.cs, a WPF class that cannot be constructed without a dispatcher, a bridge and
// a desktop) resolved a fleet conversation's identity fields and then either copied them onto
// the Conversation object or silently didn't. The only thing ever checking that logic was
// ui/test_a_fleet_conversation_opened_from_the_cockpit_keeps_its_identity.py, a source-text
// test -- it can see the string "c.Goal = bestGoal" exists somewhere in the method, but it
// cannot see whether the guard around it is correct, or run the never-overwrite rule against
// real values. Pulling the decision out into a WPF-free static class lets a test compile just
// THIS file (see ui/harness/FleetConvIdentityHarness.cs) and execute it.
//
// KEPT FREE OF THE Conversation TYPE ON PURPOSE. Conversation lives in ui/ChatSend.cs, which is
// compiled into CopilotChat.exe only (ui/rebuild_ui.ps1's CopilotChat Build line) -- FleetCockpit
// has no such type. This file's own Build-line entry covers BOTH binaries (a WPF-free decision
// belongs in both, not just the one that happens to use it today), so it operates on plain
// strings and lets each caller apply the result to whatever field it owns, instead of taking a
// Conversation parameter that would only resolve in one of the two builds.
//
// TWO MERGE RULES, taken from the two call sites verbatim:
//
//   MergeForward     -- OpenFromFleet: the freshly-resolved value (a live status.json worker
//                        dict read, or a transcript re-read) wins whenever it is non-empty,
//                        because OpenFromFleet's own reads are always at least as current as
//                        whatever the row already carried from an earlier poll. Falls back to
//                        the existing value only when the fresh read came up empty (a transient
//                        miss -- no live worker, no meta line, e.g. a slow disk read -- must not
//                        blank out an identity the row already had).
//
//   MergeBackfillOnly -- SyncRegistry: the registry poll runs continuously while the window
//                        stays open, so a row it already added must be able to pick up a value
//                        that only became available later -- but must NEVER overwrite a value
//                        the row already has, full stop. Also used by OpenFromFleet itself for
//                        Source ("claim fleet only for an unclaimed row") and Name (worker name
//                        is a fallback identifier, never a correction).
//
// Legacy csc (Framework64 v4.0.30319, C# 5): NO string interpolation, NO null-conditional, NO
// expression-bodied members, NO nameof. No WPF types in this file -- that is what lets a test
// compile it standalone.
using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;

static class FleetConvIdentity
{
    /// The fresh value wins when present; otherwise keep whatever the row already had. Used by
    /// OpenFromFleet for Transcript and Goal: its own reads (the live worker dict / a transcript
    /// re-read) are always at least as current as an earlier poll, so they are allowed to move
    /// the row forward, but an empty read must never blank out a value the row already carried.
    public static string MergeForward(string existing, string freshValue)
    {
        if (!string.IsNullOrEmpty(freshValue)) return freshValue;
        return existing ?? "";
    }

    /// Fill in a value only when the row does not already have one; never replace a non-empty
    /// value. Used by SyncRegistry for its Goal/Transcript backfill (the ONLY feed that updates
    /// a fleet conversation already in the sidebar while the window stays open -- an in-flight
    /// worker's registry read must not stomp a correct value with a stale or empty one), and by
    /// OpenFromFleet for Source (claim "fleet" only for an unclaimed row -- a row already
    /// carrying a real, different source, e.g. a plain Copilot-side orphan sharing this exact
    /// ConvUrl, must not be silently reclassified) and Name (a worker name is a fallback
    /// identifier, filled in once and never overwritten).
    public static string MergeBackfillOnly(string existing, string freshValue)
    {
        if (string.IsNullOrEmpty(existing) && !string.IsNullOrEmpty(freshValue)) return freshValue;
        return existing ?? "";
    }

    /// Stable ordered union for one conversation's transcript segments. Existing order wins,
    /// then newly observed lineage entries, then the latest pointer if it was not listed yet.
    /// A stale registry poll carrying an older pointer therefore cannot move the conversation
    /// backwards: an already-known newer segment stays at the tail.
    public static List<string> MergeTranscriptLineage(IEnumerable<string> existing,
                                                      IEnumerable<string> fresh,
                                                      string latest)
    {
        var outp = new List<string>();
        AddTranscriptPaths(outp, existing);
        AddTranscriptPaths(outp, fresh);
        if (!string.IsNullOrEmpty(latest) && !outp.Contains(latest)) outp.Add(latest);
        return outp;
    }

    static void AddTranscriptPaths(List<string> dst, IEnumerable<string> src)
    {
        if (src == null) return;
        foreach (string raw in src)
        {
            string value = (raw ?? "").Trim();
            if (value.Length > 0 && !dst.Contains(value)) dst.Add(value);
        }
    }

    /// Latest display/send pointer for a lineage. Falls back to the pre-lineage pointer for old
    /// rows or a transient empty registry read.
    public static string LatestTranscript(IList<string> lineage, string fallback)
    {
        if (lineage != null && lineage.Count > 0) return lineage[lineage.Count - 1];
        return fallback ?? "";
    }

    /// OpenFromFleet's goal resolution: the live status.json worker dict's own "goal" field
    /// wins when non-empty; otherwise the transcript's first-line meta goal (already looked up
    /// by the caller via TranscriptMetaGoal -- this method takes the result, not the path, so it
    /// stays free of file I/O). Equivalent to MergeForward(transcriptMetaGoal, liveGoal), named
    /// separately because "which source wins" reads more plainly than a forward-merge here.
    public static string ResolveGoal(string liveGoal, string transcriptMetaGoal)
    {
        if (!string.IsNullOrEmpty(liveGoal)) return liveGoal;
        return transcriptMetaGoal ?? "";
    }

    // ---- WHICH CONVERSATION A TRANSCRIPT BELONGS TO (2026-10-23) -------------------------------
    //
    // MEASURED: opening the ADAMS conversation (sess:65c85c0a-..., worker w1) showed, after its own
    // turns, two 10-14 phone-ledger transcripts (r6acf1938_a0_w1, r6acf1938_a1_w1) and then the
    // current run's computer-use transcript (r6adac45a_a0_w1). None of them is that conversation.
    // They were joined by the WORKER NAME alone. A worker name (w0, w1, ...) is reused by every
    // run, so it says nothing about which conversation a transcript is. The conversation's
    // identity is its Copilot id (sess:<guid>, the transcript's own "guid" line); a run is the
    // file-name prefix before the worker ("r6acf1938_a0"). These functions are the only place
    // that decides a match, so the chat window cannot grow another name-only comparison.

    /// The run a transcript file belongs to: "r6acf1938_a0" for ".../r6acf1938_a0_w1.jsonl.gz"
    /// (and for its "__sub_" siblings). "" when the name has no worker part.
    public static string RunOf(string path)
    {
        if (string.IsNullOrEmpty(path)) return "";
        string name = Path.GetFileName(path.Trim());
        if (name.EndsWith(".gz", StringComparison.OrdinalIgnoreCase)) name = name.Substring(0, name.Length - 3);
        if (name.EndsWith(".jsonl", StringComparison.OrdinalIgnoreCase)) name = name.Substring(0, name.Length - 6);
        int sub = name.IndexOf("__sub_", StringComparison.Ordinal);
        if (sub >= 0) name = name.Substring(0, sub);
        int w = name.LastIndexOf("_w", StringComparison.Ordinal);
        return w > 0 ? name.Substring(0, w) : "";
    }

    /// True when any transcript of side A and any of side B come from the same run.
    public static bool SharesRun(IEnumerable<string> aLineage, string aLatest,
                                 IEnumerable<string> bLineage, string bLatest)
    {
        var runs = new HashSet<string>(StringComparer.Ordinal);
        foreach (string p in MergeTranscriptLineage(aLineage, null, aLatest))
        { string r = RunOf(p); if (r.Length > 0) runs.Add(r); }
        foreach (string p in MergeTranscriptLineage(bLineage, null, bLatest))
        { string r = RunOf(p); if (r.Length > 0 && runs.Contains(r)) return true; }
        return false;
    }

    static bool SameTranscript(string a, string b)
    {
        return !string.IsNullOrEmpty(a) && !string.IsNullOrEmpty(b)
            && string.Equals(ConvListing.StripGz(a.Trim()), ConvListing.StripGz(b.Trim()),
                             StringComparison.OrdinalIgnoreCase);
    }

    /// OpenFromFleet may fall back to "the newest transcript of worker <name>" only when the
    /// conversation has no id of its own (a "fleet:<name>" key). With an id, the worker name is
    /// reused by other runs and would pick some other conversation's transcript.
    public static bool MayResolveByWorkerName(string url)
    {
        return string.IsNullOrEmpty(url);
    }

    /// A transcript resolved from outside the conversation may join its lineage only when it is
    /// not known to belong to another Copilot conversation. `hintGuid` is that transcript's own
    /// guid ("" when it has none yet, e.g. a run whose first turn has not been answered).
    public static bool HintMayJoin(string url, string hintGuid)
    {
        if (string.IsNullOrEmpty(url) || !url.StartsWith("sess:", StringComparison.Ordinal)) return true;
        if (string.IsNullOrEmpty(hintGuid)) return true;
        return string.Equals(url.Substring(5), hintGuid, StringComparison.OrdinalIgnoreCase);
    }

    /// Does this registry row describe the conversation being opened? (RegistryTranscriptLineageFor)
    ///   * with an id: the row's id must be the same. A latest-hint match is accepted only for a
    ///     row that has no id of its own (the same transcript file, not another conversation).
    ///   * without an id: the exact transcript file, or the same worker name AND the same run.
    ///     A name alone never matches -- the name is reused by every run. With no hint there is
    ///     nothing to anchor the run to, so nothing matches.
    public static bool RegistryRowMatches(string url, string worker, string latestHint,
                                          string rowUrl, string rowSource, string rowName,
                                          string rowTranscript, IEnumerable<string> rowLineage)
    {
        rowUrl = rowUrl ?? "";
        if (!string.IsNullOrEmpty(url))
        {
            if (rowUrl == url) return true;
            return rowUrl.Length == 0 && SameTranscript(rowTranscript, latestHint);
        }
        if (SameTranscript(rowTranscript, latestHint)) return true;
        return !string.IsNullOrEmpty(worker) && !string.IsNullOrEmpty(latestHint)
            && rowUrl.Length == 0 && rowSource == "fleet" && rowName == worker
            && SharesRun(rowLineage, rowTranscript, null, latestHint);
    }

    /// Is this registry row the sidebar conversation `c`? (SyncRegistry's existing-row lookup)
    /// A row with an id matches the conversation with that id. A row without one matches by
    /// transcript file, or by (source, name) AND a shared run -- never by the name alone.
    public static bool SyncRowMatches(string rowUrl, string rowSource, string rowName,
                                      string rowTranscript, IEnumerable<string> rowLineage,
                                      string cUrl, string cSource, string cName,
                                      string cTranscript, IEnumerable<string> cLineage)
    {
        if (!string.IsNullOrEmpty(rowUrl)) return cUrl == rowUrl;
        if (SameTranscript(rowTranscript, cTranscript)) return true;
        return !string.IsNullOrEmpty(rowName) && rowSource == cSource && rowName == cName
            && SharesRun(rowLineage, rowTranscript, cLineage, cTranscript);
    }
}

/// The conversation LIST, as plain functions: which transcripts exist, how they are paged, and
/// how the shared registry is read. Extracted from ui/CopilotChat.cs for the same reason
/// FleetConvIdentity is -- that class is WPF and cannot run in a test; this one can
/// (ui/harness/ConvListingHarness.cs).
///
/// WHY IT EXISTS (2026-10-05, "old conversations often cannot be opened from the main chat").
/// Three separate limits, each silent, together hid every conversation older than a day or so:
///   1. The sidebar kept the NEWEST 80 transcripts, scanned once at startup. 3,309 were on disk.
///   2. The registry (.fleet/conversations.json) grew past 2,097,152 characters, which is the
///      JavaScriptSerializer default MaxJsonLength; deserialising threw, a bare `catch { }`
///      swallowed it, and the registry contributed an empty list.
///   3. The registry pruned a row 24 h after it stopped being linked, even while its transcript
///      file was still on disk.
/// So: every transcript is listable (newest page first, older pages on request), the registry
/// is read without the 2 MB ceiling, and a read that fails says so instead of returning nothing.
static class ConvListing
{
    /// Rows shown at startup. Kept at the old figure: parsing a transcript's first lines costs
    /// a file open each, and startup speed is why the cap existed.
    public const int FirstPage = 80;

    /// Upper bound for one "older" page, so a day with hundreds of runs does not freeze the UI
    /// thread while every file is opened. A larger day is split across successive pages.
    public const int MaxPage = 300;

    public static string StripGz(string path)
    {
        return (path != null && path.EndsWith(".gz", StringComparison.OrdinalIgnoreCase))
            ? path.Substring(0, path.Length - 3) : path;
    }

    /// The Copilot conversation id written into a transcript ({"guid": ...}, within its first
    /// 40 lines), or "" when there is none yet or the file cannot be read. Reads "x.jsonl" or
    /// "x.jsonl.gz", whichever exists.
    public static string TranscriptGuid(string path)
    {
        try
        {
            if (string.IsNullOrEmpty(path)) return "";
            string actual = path;
            if (!File.Exists(actual)) actual = actual.EndsWith(".gz", StringComparison.OrdinalIgnoreCase) ? StripGz(actual) : actual + ".gz";
            if (!File.Exists(actual)) return "";
            var js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            using (var fs = new FileStream(actual, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = actual.EndsWith(".gz", StringComparison.OrdinalIgnoreCase)
                       ? new StreamReader(new System.IO.Compression.GZipStream(fs, System.IO.Compression.CompressionMode.Decompress), System.Text.Encoding.UTF8)
                       : new StreamReader(fs, System.Text.Encoding.UTF8))
            {
                for (int i = 0; i < 40; i++)
                {
                    string ln = sr.ReadLine();
                    if (ln == null) break;
                    if (ln.IndexOf("\"guid\"", StringComparison.Ordinal) < 0 || ln.IndexOf("\"role\"", StringComparison.Ordinal) >= 0) continue;
                    var d = js.DeserializeObject(ln) as Dictionary<string, object>;
                    object g;
                    if (d != null && d.TryGetValue("guid", out g) && g != null) return g.ToString();
                }
            }
        }
        catch (Exception) { }
        return "";
    }

    public static bool IsSubTranscript(string path)
    {
        return path != null && path.IndexOf("__sub_", StringComparison.Ordinal) >= 0;
    }

    /// Every transcript in `tdir`, "*.jsonl" and "*.jsonl.gz" merged by logical name (the plain
    /// file wins the slot while a compression is mid-flight).
    public static List<string> ListTranscriptFiles(string tdir)
    {
        var byKey = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var raw = new List<string>(Directory.GetFiles(tdir, "*.jsonl"));
        raw.AddRange(Directory.GetFiles(tdir, "*.jsonl.gz"));
        foreach (var f in raw)
        {
            string key = StripGz(f);
            string existing;
            bool fIsGz = f.EndsWith(".gz", StringComparison.OrdinalIgnoreCase);
            if (!byKey.TryGetValue(key, out existing)
                || (!fIsGz && existing.EndsWith(".gz", StringComparison.OrdinalIgnoreCase)))
                byKey[key] = f;
        }
        return new List<string>(byKey.Values);
    }

    /// Top-level (non sub-agent) transcripts with their last-write time (UTC), newest first.
    /// One stat per file: the sort compares the cached value, not the disk.
    public static List<KeyValuePair<string, DateTime>> ListMainTranscripts(string tdir)
    {
        var outp = new List<KeyValuePair<string, DateTime>>();
        foreach (string f in ListTranscriptFiles(tdir))
        {
            if (IsSubTranscript(f)) continue;
            DateTime m;
            try { m = File.GetLastWriteTimeUtc(f); }
            catch (IOException) { continue; }          // vanished between listing and stat
            outp.Add(new KeyValuePair<string, DateTime>(f, m));
        }
        outp.Sort(delegate (KeyValuePair<string, DateTime> a, KeyValuePair<string, DateTime> b)
        {
            int c = b.Value.CompareTo(a.Value);
            return c != 0 ? c : string.CompareOrdinal(a.Key, b.Key);
        });
        return outp;
    }

    /// End index (exclusive) of the page starting at `start`: the first page is `FirstPage`
    /// rows; every later page is ONE LOCAL DAY (the day of items[start]), capped at `maxPage`.
    /// `items` is newest first, as ListMainTranscripts returns it.
    public static int PageEnd(IList<KeyValuePair<string, DateTime>> items, int start, int maxPage)
    {
        if (items == null || start >= items.Count) return start;
        if (start == 0) return Math.Min(items.Count, FirstPage);
        DateTime day = items[start].Value.ToLocalTime().Date;
        int end = start;
        while (end < items.Count && end - start < maxPage
               && items[end].Value.ToLocalTime().Date == day) end++;
        return end;
    }

    /// The shared registry as a list, WITHOUT the 2 MB serializer ceiling. `error` is "" when
    /// the file is absent or read fine, and a one-line reason when it could not be read -- the
    /// caller must show that, because an empty list from a failed read looks exactly like an
    /// empty registry.
    public static List<object> ReadRegistry(string path, out string error)
    {
        error = "";
        var empty = new List<object>();
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return empty;
        try
        {
            var js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            var a = js.DeserializeObject(File.ReadAllText(path, System.Text.Encoding.UTF8)) as object[];
            if (a == null) { error = "conversations.json is not a JSON array"; return empty; }
            return new List<object>(a);
        }
        catch (Exception ex)
        {
            error = ex.GetType().Name + ": " + ex.Message;
            return empty;
        }
    }

    /// The one-line notice for the sidebar when part of the history could not be read.
    public static string UnreadableNotice(bool ja, string reason)
    {
        string r = string.IsNullOrEmpty(reason) ? "?" : reason.Replace('\r', ' ').Replace('\n', ' ');
        if (r.Length > 160) r = r.Substring(0, 160) + "...";
        return (ja ? "履歴の一部を読めませんでした: " : "Part of the history could not be read: ") + r;
    }

    /// Append one line to <dir>/chat_listing.log. A diagnostic must never be the thing that
    /// breaks the list, so a failure to WRITE it is the one error ignored here (and only IO
    /// ones). Stops growing at 512 KB.
    public static void Diag(string dir, string message)
    {
        try
        {
            if (string.IsNullOrEmpty(dir)) return;
            string p = Path.Combine(dir, "chat_listing.log");
            if (File.Exists(p) && new FileInfo(p).Length > 512 * 1024) return;
            File.AppendAllText(p, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message
                + Environment.NewLine, new System.Text.UTF8Encoding(false));
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
