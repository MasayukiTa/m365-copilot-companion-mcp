// SubmittedTasks.cs -- what the cockpit shows at the TOP of its list the instant a task is
// submitted, before any worker exists for it. No WPF here: FleetCockpit.cs renders the rows,
// this file decides WHICH rows, in WHAT order, with WHAT label.
//
// THE DEFECT THIS REPLACES. The card list was built from .fleet/status.json -- the runner's
// snapshot of WORKERS -- and a submitted job has no worker until a run picks it up. The queue
// (.fleet/tasks/pending, for_fleet) was read, but rendered only inside the EMPTY STATE, i.e.
// only when there was no run and no history. In the normal state -- a run live, or anything in
// history -- a submission was invisible until a worker existed, and even then a new worker in
// status "pending" sorted BELOW every active card. The owner's requirement, verbatim intent:
// "whatever the queue has or has not registered, show it on top at once."
//
// SOURCES, merged and de-duplicated by goal text (whitespace normalised):
//   (a) LOCAL: added by this window the moment it submits (AddLocal). On screen before any
//       file is read back.
//   (b) COMMAND: .fleet/commands.d/*.json carrying `add_goal`, written by ui/FleetCommands.cs
//       (both exes) or relay/task_router.write_command. relay/fleet_runner.read_commands
//       deletes each file as it consumes it, so a file on disk is a command no run has read.
//   (c) QUEUE: .fleet/tasks/pending/<id>.json (tools/fleet_intake.fleet_submit) and
//       .fleet/tasks/for_fleet/<id>.txt (relay/task_router._write_for_fleet; plain text, or
//       {"text":..,"priority":true} -- the same two shapes task_router.read_for_fleet reads).
//
// AN ENTRY LEAVES ONLY WHEN A WORKER FOR IT EXISTS: a status.json worker with the same goal
// that was NOT already there when the entry was first seen. The exclusion is what makes a
// RETRY work -- a retry re-submits the goal text of a worker that is still on the board
// (finished, failed), and matching that old worker would drop the new entry the instant it
// was added. Keys are "<status.started>#<worker name>", the cockpit's own WorkerKey shape.
//
// NEVER SILENTLY DROPPED. When the file behind an entry disappears (the run consumed the
// command; the router moved the job on) the entry STAYS, relabelled, until its worker appears.
// A local entry that no file and no worker ever confirmed stays too, labelled "not confirmed
// by the fleet" with its age; past UNCONFIRMED_AFTER_S it is marked stale and the person -- only
// the person -- may dismiss it. Nothing here deletes an entry on a timer.
//
// Compiled into FleetCockpit.exe (ui/rebuild_ui.ps1) AND standalone by
// ui/test_a_submitted_task_is_on_top_at_once.py, which runs it against real files.
// Legacy csc (Framework64 v4.0.30319, C# 5): no string interpolation, no ?., no nameof,
// no expression-bodied members.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

/// One file on disk that says "this goal was submitted". Plain data.
sealed class SubmittedFile
{
    public string Source;          // "command" | "pending" | "for_fleet"
    public string Id;              // file name without extension (+ "#i" for the i-th add_goal item)
    public string Goal;
    public double SubmittedUnix;   // when it was submitted, as well as the file can say
}

/// One row of the "submitted, not picked up yet" group, as rendered. Immutable once built, so
/// the health poll's thread can read the list the UI thread published without a lock.
sealed class SubmittedView
{
    public string Key;             // normalised goal -- the de-dup key and the dismiss key
    public string Goal;            // the goal as first seen (display trims it)
    public string Source;          // "local" | "command" | "pending" | "for_fleet" | "taken"
    public string Id;
    public double SubmittedUnix;
    public double AgeS;
    public bool Backed;            // a file on disk is behind it right now
    public bool Unconfirmed;       // only this window says so: no file and no worker ever did
    public bool Stale;             // older than UNCONFIRMED_AFTER_S and still no worker
    public bool Dismissable;       // stale with no file behind it: the person may clear it
}

/// A worker or a submitted entry, in display order.
sealed class SubmittedDisplayItem
{
    public SubmittedView Submitted;                // exactly one of these two is set
    public Dictionary<string, object> Worker;
    public int Rank;                                // -1 submitted; else SubmittedTasks.WorkerRank
}

sealed class SubmittedTasks
{
    /// A local entry nothing has confirmed for this long is marked unconfirmed (and may be
    /// dismissed by the person). It is NOT removed: that is the whole point of the mark.
    public const double UNCONFIRMED_AFTER_S = 600.0;

    /// THE FRESH-PENDING RULE. A worker whose status is "pending" and whose FIRST phase event
    /// (relay_fleet appends one on construction, event "pending") is younger than this is the
    /// newest task the fleet has: it sorts ABOVE active workers, newest first. Older than this,
    /// a pending worker is one the admission gate has been holding, and it sorts below active
    /// ones as before. A pending worker with no phase events cannot prove it is new, so it is
    /// treated as old. Three minutes: long enough to be seen after a submission, short enough
    /// that a worker stuck behind the gate stops crowding the live ones.
    public const double FRESH_PENDING_S = 180.0;

    /// Files bigger than this are not commands or queue entries anyone wrote on purpose; they
    /// are skipped rather than parsed on a 700 ms tick.
    const long MAX_FILE_BYTES = 256 * 1024;

    sealed class Entry
    {
        public string Key, Goal, Source, Id;
        public double SubmittedUnix, FirstSeenUnix;
        public long Order;
        public bool Backed, EverBacked;
        public HashSet<string> PreexistingWorkers;
    }

    readonly List<Entry> _entries = new List<Entry>();
    long _order = 0;

    static readonly JavaScriptSerializer _js = new JavaScriptSerializer();

    // ── plain helpers ──────────────────────────────────────────────────────────────────────

    /// The de-dup key: whitespace runs collapsed to one space, trimmed. Case is kept -- two
    /// goals differing only in case are different instructions.
    public static string Normalize(string goal)
    {
        if (goal == null) return "";
        var sb = new StringBuilder(goal.Length);
        bool space = false;
        foreach (char c in goal)
        {
            if (char.IsWhiteSpace(c)) { space = true; continue; }
            if (space && sb.Length > 0) sb.Append(' ');
            space = false;
            sb.Append(c);
        }
        return sb.ToString();
    }

    static string Str(Dictionary<string, object> d, string key)
    {
        object v;
        if (d == null || !d.TryGetValue(key, out v) || v == null) return "";
        return Convert.ToString(v, CultureInfo.InvariantCulture);
    }

    static double Num(object v)
    {
        if (v == null) return 0.0;
        try { return Convert.ToDouble(v, CultureInfo.InvariantCulture); }
        catch (Exception) { return 0.0; }
    }

    static readonly DateTime _epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    static double MtimeUnix(string path)
    {
        try { return (File.GetLastWriteTimeUtc(path) - _epoch).TotalSeconds; }
        catch (Exception) { return 0.0; }
    }

    /// The goal text a SpawnFleet line carries: either the line itself, or -- for the callers
    /// that pass an already-serialised {"text":..,"resume_conv":..} object -- its "text".
    public static string GoalTextOf(string raw)
    {
        string t = (raw ?? "").Trim();
        if (t.StartsWith("{"))
        {
            try
            {
                var o = _js.DeserializeObject(t) as Dictionary<string, object>;
                if (o != null)
                {
                    object v;
                    if (o.TryGetValue("text", out v) && v is string) return (string)v;
                }
            }
            catch (Exception) { }
        }
        return raw ?? "";
    }

    // ── reading the disk ───────────────────────────────────────────────────────────────────

    /// Every submission on disk. NEVER THROWS: this runs on the cockpit's 700 ms tick, and a
    /// torn, half-written or unreadable file is skipped, never a crash. Cost: one directory
    /// listing per directory plus one small read per file.
    public static List<SubmittedFile> ReadFiles(string commandsDir, string pendingDir,
                                                string forFleetDir)
    {
        var outList = new List<SubmittedFile>();
        try { ReadCommands(commandsDir, outList); } catch (Exception) { }
        try { ReadPending(pendingDir, outList); } catch (Exception) { }
        try { ReadForFleet(forFleetDir, outList); } catch (Exception) { }
        return outList;
    }

    static string[] Files(string dir, string ext)
    {
        if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return new string[0];
        var keep = new List<string>();
        // EndsWith, not the pattern alone: Directory.GetFiles("*.txt") also matches "x.txtold".
        foreach (string f in Directory.GetFiles(dir, "*" + ext))
            if (f.EndsWith(ext, StringComparison.OrdinalIgnoreCase)) keep.Add(f);
        keep.Sort(StringComparer.Ordinal);
        return keep.ToArray();
    }

    static string ReadSmall(string path)
    {
        var fi = new FileInfo(path);
        if (!fi.Exists || fi.Length > MAX_FILE_BYTES) return null;
        // FileShare.ReadWrite|Delete: the runner may be deleting this very file as it consumes it.
        using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                                       FileShare.ReadWrite | FileShare.Delete))
        using (var sr = new StreamReader(fs, new UTF8Encoding(false), true))
            return sr.ReadToEnd();
    }

    /// commands.d/<ns>-<seq>-<rand>.json, consumed (deleted) by fleet_runner.read_commands.
    /// Only `add_goal` items that the reader itself would turn into goals count
    /// (relay/fleet_runner.goals_from_command): a single item or a list; a dict with a string
    /// "text", or a bare string. `.tmp` is a writer mid-flight and `.bad` a file the reader gave
    /// up on; neither ends in .json, so neither is read.
    static void ReadCommands(string dir, List<SubmittedFile> outList)
    {
        foreach (string f in Files(dir, ".json"))
        {
            try
            {
                string body = ReadSmall(f);
                if (string.IsNullOrEmpty(body)) continue;
                var cmd = _js.DeserializeObject(body) as Dictionary<string, object>;
                if (cmd == null) continue;
                object add;
                if (!cmd.TryGetValue("add_goal", out add) || add == null) continue;
                var items = new List<object>();
                if (add is object[]) items.AddRange((object[])add);
                else if (add is System.Collections.ArrayList)
                    foreach (object o in (System.Collections.ArrayList)add) items.Add(o);
                else items.Add(add);
                string name = Path.GetFileNameWithoutExtension(f);
                double when = CommandTimeUnix(name);
                if (when <= 0) when = MtimeUnix(f);
                for (int i = 0; i < items.Count; i++)
                {
                    // The reader's two shapes: {"text": ..} and a bare string.
                    string text = items[i] as string;
                    var it = items[i] as Dictionary<string, object>;
                    object t;
                    if (it != null && it.TryGetValue("text", out t)) text = t as string;
                    // A blank text is a goal the runner would start with nothing to say; there is
                    // no row to draw for it (and no key to de-duplicate on), so it is skipped.
                    if (text == null || Normalize(text).Length == 0) continue;
                    outList.Add(new SubmittedFile {
                        Source = "command", Goal = text, SubmittedUnix = when,
                        Id = items.Count == 1 ? name : name + "#" + i });
                }
            }
            catch (Exception) { }   // torn / malformed: skipped, the rest still read
        }
    }

    /// The writer's name starts with time_ns zero-padded to 19 digits (FleetCommands.Write and
    /// task_router.write_command agree on "%019d-%09d-%s"). 0 when the name is not that shape.
    static double CommandTimeUnix(string name)
    {
        if (name == null || name.Length < 19) return 0.0;
        long ns;
        if (!long.TryParse(name.Substring(0, 19), NumberStyles.None, CultureInfo.InvariantCulture,
                           out ns))
            return 0.0;
        return ns / 1e9;
    }

    /// tasks/pending/<id>.json from fleet_intake.fleet_submit: {"payload":{"goal":..},
    /// "created":<unix>}. A top-level "goal" is accepted for older writers.
    static void ReadPending(string dir, List<SubmittedFile> outList)
    {
        foreach (string f in Files(dir, ".json"))
        {
            try
            {
                string body = ReadSmall(f);
                if (string.IsNullOrEmpty(body)) continue;
                var o = _js.DeserializeObject(body) as Dictionary<string, object>;
                if (o == null) continue;
                string goal = "";
                object pay;
                if (o.TryGetValue("payload", out pay))
                    goal = Str(pay as Dictionary<string, object>, "goal");
                if (Normalize(goal).Length == 0) goal = Str(o, "goal");
                if (Normalize(goal).Length == 0) continue;
                object cr;
                double when = o.TryGetValue("created", out cr) ? Num(cr) : 0.0;
                if (when <= 0) when = MtimeUnix(f);
                outList.Add(new SubmittedFile {
                    Source = "pending", Goal = goal, SubmittedUnix = when,
                    Id = Path.GetFileNameWithoutExtension(f) });
            }
            catch (Exception) { }
        }
    }

    /// tasks/for_fleet/<id>.txt: the goal as plain text, or {"text":..,"priority":true} -- the
    /// inverse of task_router._write_for_fleet, as task_router.read_for_fleet reads it.
    static void ReadForFleet(string dir, List<SubmittedFile> outList)
    {
        foreach (string f in Files(dir, ".txt"))
        {
            try
            {
                string body = ReadSmall(f);
                if (body == null) continue;
                string goal = body;
                if (body.TrimStart().StartsWith("{"))
                {
                    try
                    {
                        var o = _js.DeserializeObject(body) as Dictionary<string, object>;
                        object t;
                        if (o != null && o.TryGetValue("text", out t) && t is string)
                            goal = (string)t;
                    }
                    catch (Exception) { }
                }
                if (Normalize(goal).Length == 0) continue;
                outList.Add(new SubmittedFile {
                    Source = "for_fleet", Goal = goal, SubmittedUnix = MtimeUnix(f),
                    Id = Path.GetFileNameWithoutExtension(f) });
            }
            catch (Exception) { }
        }
    }

    // ── the merge ──────────────────────────────────────────────────────────────────────────

    static Dictionary<string, List<string>> WorkerKeysByGoal(string started,
                                                              IList<Dictionary<string, object>> workers)
    {
        var map = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        if (workers == null) return map;
        foreach (Dictionary<string, object> w in workers)
        {
            if (w == null) continue;
            string k = Normalize(Str(w, "goal"));
            if (k.Length == 0) continue;
            List<string> l;
            if (!map.TryGetValue(k, out l)) { l = new List<string>(); map[k] = l; }
            l.Add((started ?? "") + "#" + Str(w, "name"));
        }
        return map;
    }

    Entry Find(string key)
    {
        foreach (Entry e in _entries) if (e.Key == key) return e;
        return null;
    }

    Entry NewEntry(string key, string goal, string source, string id, double submitted,
                   double now, Dictionary<string, List<string>> byGoal)
    {
        List<string> pre;
        var e = new Entry {
            Key = key, Goal = goal, Source = source, Id = id ?? "",
            SubmittedUnix = submitted > 0 ? submitted : now, FirstSeenUnix = now,
            Order = _order++,
            PreexistingWorkers = new HashSet<string>(
                byGoal.TryGetValue(key, out pre) ? pre : new List<string>(), StringComparer.Ordinal) };
        _entries.Add(e);
        return e;
    }

    /// The instant this window submits `goal`. `started`/`workers` are the status.json the
    /// window has right now: a worker already there with this goal (the one being retried)
    /// does not count as this submission's worker. Returns false for an empty goal.
    public bool AddLocal(string goal, double nowUnix, string started,
                         IList<Dictionary<string, object>> workers)
    {
        string key = Normalize(goal);
        if (key.Length == 0) return false;
        if (Find(key) != null) return true;          // already on screen
        NewEntry(key, goal, "local", "", nowUnix, nowUnix, WorkerKeysByGoal(started, workers));
        return true;
    }

    /// Merge what is on disk and what status.json says into the rows to show, newest first.
    public List<SubmittedView> Refresh(List<SubmittedFile> files, string started,
                                       IList<Dictionary<string, object>> workers, double nowUnix)
    {
        var byGoal = WorkerKeysByGoal(started, workers);
        foreach (Entry e in _entries) e.Backed = false;

        if (files != null)
        {
            foreach (SubmittedFile f in files)
            {
                if (f == null) continue;
                string key = Normalize(f.Goal);
                if (key.Length == 0) continue;
                Entry e = Find(key);
                if (e == null)
                    e = NewEntry(key, f.Goal, f.Source, f.Id, f.SubmittedUnix, nowUnix, byGoal);
                // The furthest stage a file shows wins the label: a command is at the run's
                // door, for_fleet is waiting for a run, pending is not yet routed.
                if (!e.Backed || StageOf(f.Source) > StageOf(e.Source))
                {
                    e.Source = f.Source;
                    e.Id = f.Id;
                }
                if (f.SubmittedUnix > 0 && f.SubmittedUnix < e.SubmittedUnix)
                    e.SubmittedUnix = f.SubmittedUnix;
                e.Backed = true;
                e.EverBacked = true;
            }
        }

        // Resolved: a worker for this goal exists that was not there when it was first seen.
        _entries.RemoveAll(delegate (Entry e)
        {
            List<string> keys;
            if (!byGoal.TryGetValue(e.Key, out keys)) return false;
            foreach (string wk in keys)
                if (!e.PreexistingWorkers.Contains(wk)) return true;
            return false;
        });

        foreach (Entry e in _entries)
            if (!e.Backed && e.EverBacked) e.Source = "taken";   // its file went; no worker yet

        var ordered = new List<Entry>(_entries);
        // Newest submission first; the order first seen breaks ties, so rows do not swap.
        ordered.Sort(delegate (Entry a, Entry b)
        {
            int c = b.SubmittedUnix.CompareTo(a.SubmittedUnix);
            return c != 0 ? c : a.Order.CompareTo(b.Order);
        });
        var views = new List<SubmittedView>();
        foreach (Entry e in ordered)
        {
            double age = Math.Max(0.0, nowUnix - e.SubmittedUnix);
            bool stale = age >= UNCONFIRMED_AFTER_S;
            views.Add(new SubmittedView {
                Key = e.Key, Goal = e.Goal, Source = e.Source, Id = e.Id,
                SubmittedUnix = e.SubmittedUnix, AgeS = age, Backed = e.Backed,
                Unconfirmed = !e.Backed && !e.EverBacked, Stale = stale,
                Dismissable = stale && !e.Backed });
        }
        return views;
    }

    static int StageOf(string source)
    {
        if (source == "command") return 3;
        if (source == "for_fleet") return 2;
        if (source == "pending") return 1;
        return 0;
    }

    /// The person's ✕. Only an entry with no file behind it and past the threshold can be
    /// dismissed -- a fresh one may still be picked up, and one a file still backs would come
    /// straight back on the next read. Returns whether anything was removed.
    public bool Dismiss(string key, double nowUnix)
    {
        Entry e = Find(key ?? "");
        if (e == null || e.Backed) return false;
        if (nowUnix - e.SubmittedUnix < UNCONFIRMED_AFTER_S) return false;
        _entries.Remove(e);
        return true;
    }

    // ── labels ─────────────────────────────────────────────────────────────────────────────

    /// Minute granularity on purpose: the row's signature carries this label, and a label that
    /// changed every second would re-template the row on every 700 ms tick.
    public static string Age(double ageS, bool ja)
    {
        if (ageS < 60) return ja ? "1分未満" : "<1m";
        long m = (long)(ageS / 60.0);
        if (m < 60) return ja ? m + "分" : m + "m";
        long h = m / 60, mm = m % 60;
        return ja ? h + "時間" + mm + "分" : h + "h" + mm + "m";
    }

    /// Where the entry is. A local entry nothing has confirmed says exactly that at any age --
    /// "sent" is this window's claim, not the fleet's -- and past the threshold it says so with
    /// the age in front of it (the age is part of the label, see Label).
    static string Where(SubmittedView v, bool ja)
    {
        if (v.Unconfirmed) return ja ? "フリート未確認" : "not confirmed by the fleet";
        switch (v.Source)
        {
            case "command":   return ja ? "実行中のランの読込待ち" : "waiting for the run to read it";
            case "pending":   return ja ? "キュー・未割当" : "in the queue, unclaimed";
            case "for_fleet": return ja ? "受け渡し済・フリート待ち" : "handed off, waiting for a fleet";
            case "taken":     return ja ? "フリートが受領・ワーカー待ち" : "taken by the fleet, no worker yet";
            default:          return ja ? "この画面から投入" : "sent from this window";
        }
    }

    /// "投入済み・未着手 3分 · キュー・未割当" / "submitted, not picked up yet (3m) · in the queue, unclaimed"
    public static string Label(SubmittedView v, bool ja)
    {
        if (v == null) return "";
        string age = Age(v.AgeS, ja);
        return (ja ? "投入済み・未着手 " + age : "submitted, not picked up yet (" + age + ")")
               + " · " + Where(v, ja);
    }

    /// Everything a row's rendering depends on, so the cockpit re-renders exactly when it changes.
    public static string Signature(List<SubmittedView> views, bool ja)
    {
        if (views == null || views.Count == 0) return "";
        var sb = new StringBuilder();
        foreach (SubmittedView v in views)
            sb.Append(v.Key.Length).Append(':').Append(v.Key.GetHashCode()).Append('|')
              .Append(Label(v, ja)).Append('|').Append(v.Dismissable ? "x" : "-").Append(';');
        return sb.ToString();
    }

    // ── order ──────────────────────────────────────────────────────────────────────────────

    static double CreatedUnix(Dictionary<string, object> w)
    {
        object pe;
        if (w == null || !w.TryGetValue("phase_events", out pe) || pe == null) return 0.0;
        object first = null;
        if (pe is object[] && ((object[])pe).Length > 0) first = ((object[])pe)[0];
        else if (pe is System.Collections.ArrayList && ((System.Collections.ArrayList)pe).Count > 0)
            first = ((System.Collections.ArrayList)pe)[0];
        var d = first as Dictionary<string, object>;
        if (d == null) return 0.0;
        object ts;
        return d.TryGetValue("ts", out ts) ? Num(ts) : 0.0;
    }

    /// 0 fresh pending (see FRESH_PENDING_S), 1 active, 2 older pending, 3 terminal.
    public static int WorkerRank(Dictionary<string, object> w, double nowUnix,
                                 Predicate<Dictionary<string, object>> isTerminal)
    {
        if (isTerminal != null && isTerminal(w)) return 3;
        if (Str(w, "status") == "pending")
        {
            double born = CreatedUnix(w);
            return (born > 0 && nowUnix - born < FRESH_PENDING_S) ? 0 : 2;
        }
        return 1;
    }

    /// THE ORDER OF THE LIST: every submitted entry first (they arrive newest first from
    /// Refresh), then fresh pending workers newest first, then active, then older pending,
    /// then terminal -- each worker bucket otherwise in status.json order, so a card keeps its
    /// place while unrelated workers tick.
    public static List<SubmittedDisplayItem> Compose(List<SubmittedView> submitted,
                                                     IList<Dictionary<string, object>> workers,
                                                     double nowUnix,
                                                     Predicate<Dictionary<string, object>> isTerminal)
    {
        var outList = new List<SubmittedDisplayItem>();
        if (submitted != null)
            foreach (SubmittedView v in submitted)
                outList.Add(new SubmittedDisplayItem { Submitted = v, Rank = -1 });
        if (workers == null) return outList;

        var ranked = new List<SubmittedDisplayItem>();
        foreach (Dictionary<string, object> w in workers)
            ranked.Add(new SubmittedDisplayItem { Worker = w, Rank = WorkerRank(w, nowUnix, isTerminal) });

        var fresh = ranked.FindAll(delegate (SubmittedDisplayItem x) { return x.Rank == 0; });
        // Newest first within the fresh bucket. Stable: List.Sort is not, so the index breaks ties.
        var idx = new Dictionary<SubmittedDisplayItem, int>();
        for (int i = 0; i < fresh.Count; i++) idx[fresh[i]] = i;
        fresh.Sort(delegate (SubmittedDisplayItem a, SubmittedDisplayItem b)
        {
            int c = CreatedUnix(b.Worker).CompareTo(CreatedUnix(a.Worker));
            return c != 0 ? c : idx[a].CompareTo(idx[b]);
        });
        outList.AddRange(fresh);
        for (int rank = 1; rank <= 3; rank++)
            foreach (SubmittedDisplayItem x in ranked)
                if (x.Rank == rank) outList.Add(x);
        return outList;
    }
}
