// ChatSendHarness.cs -- test-only Main for ui/test_the_chat_window_sends_what_was_typed.py.
//
//   ChatSendHarness.exe <cases.json> <results.json> <work dir>
//
// Compiled with ui/ChatSend.cs (the extracted send path), ui/testdata/ChatDecisionsOriginal.cs
// (the pre-extraction code, the oracle) and ui/FleetCommands.cs (the real command writer).
// For every case it runs BOTH over a fresh RecordingWorld each and writes, keyed by the case's
// id, what each did: the ordered effect trace, the state left behind, and the three status
// decisions (fleet state, live worker, active count).
//
// The world records; it does not decide. Everything it answers comes from the case, and
// everything it is told is appended to Trace -- including reads of status.json, because WHEN
// the send path consults the fleet is part of what it does. A successful AppendCommand really
// writes the command with FleetCommands.Write, so the Python side can read it back through
// relay/fleet_runner's own reader.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Web.Script.Serialization;

class RecordingWorld : IChatSendEffects
{
    public readonly List<string> Trace = new List<string>();
    static readonly JavaScriptSerializer Js = new JavaScriptSerializer();

    public string Input = "";
    public bool Enabled = true;
    public Conversation Conv;
    public Conversation Page;
    public List<Conversation> All = new List<Conversation>();
    public bool Reachable = true;
    public bool RouterIsShown;
    public int LangV;
    public string StatusPath;               // a path that may or may not exist
    public List<string> HttpFail = new List<string>();   // path prefixes whose GET throws
    public bool AppendOk = true;
    public string CommandDir;               // FleetCommands.Write's state dir for this run
    int _newCount;

    void Rec(string s) { Trace.Add(s); }

    public string InputText() { return Input; }
    public bool SendEnabled() { return Enabled; }
    public Conversation CurrentConversation() { return Conv; }
    public Conversation PageConversation() { return Page; }
    public List<Conversation> AllConversations() { return All; }
    public bool BridgeReachable() { return Reachable; }
    public bool RouterShown() { return RouterIsShown; }
    public int Lang() { return LangV; }
    public string T(string key) { return "T:" + key; }
    public string CommandHelpText() { return "HELP"; }

    public string ReadFleetStatus() { Rec("ReadStatus"); return ChatSend.ReadStatusFile(StatusPath); }
    /// The oracle reads the file itself, with the original code; it asks for the path here so
    /// that read is noted in the same place.
    public string StatusPathForOracle() { Rec("ReadStatus"); return StatusPath; }

    public void ClearInput() { Rec("ClearInput"); Input = ""; }
    public void RestoreInput(string text) { Rec("RestoreInput|" + text); Input = text; }
    public void AddUser(string text) { Rec("AddUser|" + text); }
    public void AddAssistant(string text) { Rec("AddAssistant|" + text); }
    public void NewChat()
    {
        // The data half of ChatWindow.NewChat: a new conversation, current and first in the
        // list. (Its fire-and-forget GET /new on a thread is not modelled.)
        Rec("NewChat");
        _newCount++;
        var c = new Conversation();
        c.Id = "new" + _newCount;
        Conv = c;
        All.Insert(0, c);
    }
    public string HttpGet(string path, int timeoutMs)
    {
        Rec("HttpGet|" + path + "|" + timeoutMs);
        foreach (var f in HttpFail)
            if (path.StartsWith(f, StringComparison.Ordinal)) throw new WebException("fake failure: " + path);
        return "";
    }
    public void SetBridgeReachable(bool ok) { Rec("SetBridgeReachable|" + (ok ? "true" : "false")); Reachable = ok; }
    public void SetDot(string state) { Rec("SetDot|" + state); }
    public void ShowStartStackBanner(string message, string buttonLabel) { Rec("StartStackBanner|" + message + "|" + buttonLabel); }
    public void RefreshIdleDot() { Rec("RefreshIdleDot"); }
    public void HideRouter() { Rec("HideRouter"); RouterIsShown = false; }
    public void ShowRouter(string text) { Rec("ShowRouter|" + text); RouterIsShown = true; }
    public bool AppendCommand(string key, object item)
    {
        Rec("AppendCommand|" + key + "|" + Js.Serialize(item));
        if (!AppendOk) return false;
        var items = new List<object>(); items.Add(item);
        var patch = new Dictionary<string, object>(); patch[key] = items;
        return FleetCommands.Write(CommandDir, patch);
    }
    public void SetPageConversation(Conversation c) { Rec("SetPageConv|" + (c == null ? "null" : c.Id)); Page = c; }
    public void MarkSendInFlight() { Rec("SendInFlight"); }
    public void RefreshConvList() { Rec("RefreshConvList"); }
    public void BeginStream(string text, Conversation target) { Rec("BeginStream|" + text + "|" + target.Id); }
    public void StickToEnd() { Rec("StickToEnd"); }
}

static class Harness
{
    static readonly JavaScriptSerializer Js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    // Absent -> dflt; present null -> null; else the string. The difference is the point.
    static string Str(Dictionary<string, object> d, string k, string dflt)
    {
        if (d == null || !d.ContainsKey(k)) return dflt;
        return d[k] == null ? null : Convert.ToString(d[k]);
    }
    static bool Bool(Dictionary<string, object> d, string k, bool dflt)
    {
        return d != null && d.ContainsKey(k) && d[k] != null ? Convert.ToBoolean(d[k]) : dflt;
    }

    static Conversation MakeConv(Dictionary<string, object> s)
    {
        var c = new Conversation();
        c.Id = Str(s, "id", "c1");
        c.Source = Str(s, "source", c.Source);
        c.ConvUrl = Str(s, "conv_url", c.ConvUrl);
        c.Name = Str(s, "name", c.Name);
        c.Transcript = Str(s, "transcript", c.Transcript);
        c.Goal = Str(s, "goal", c.Goal);
        c.Title = Str(s, "title", c.Title);
        if (s.ContainsKey("messages") && s["messages"] is object[])
            foreach (object m in (object[])s["messages"])
            {
                var pair = (object[])m;
                c.Messages.Add(new Msg(Convert.ToString(pair[0]), Convert.ToString(pair[1])));
            }
        return c;
    }

    static RecordingWorld MakeWorld(Dictionary<string, object> k, string commandDir)
    {
        var w = new RecordingWorld();
        w.Input = Str(k, "input", "");
        w.Enabled = Bool(k, "send_enabled", true);
        w.Reachable = Bool(k, "bridge_reachable", true);
        w.RouterIsShown = Bool(k, "router_shown", false);
        w.LangV = k.ContainsKey("lang") ? Convert.ToInt32(k["lang"]) : 0;
        w.AppendOk = Bool(k, "append_ok", true);
        w.StatusPath = Str(k, "status_path", null);
        w.CommandDir = commandDir;
        if (k.ContainsKey("http_fail") && k["http_fail"] is object[])
            foreach (object o in (object[])k["http_fail"]) w.HttpFail.Add(Convert.ToString(o));
        var cs = k.ContainsKey("conv") ? k["conv"] as Dictionary<string, object> : null;
        if (cs != null)
        {
            w.Conv = MakeConv(cs);
            if (Bool(k, "listed", true)) w.All.Add(w.Conv);
            string page = Str(k, "page", "other");
            if (page == "same") w.Page = w.Conv;
            else if (page == "other") { var p = new Conversation(); p.Id = "page"; w.Page = p; }
            else w.Page = null;
        }
        return w;
    }

    static Dictionary<string, object> Snapshot(RecordingWorld w)
    {
        var s = new Dictionary<string, object>();
        s["current"] = w.Conv == null ? null : w.Conv.Id;
        s["page"] = w.Page == null ? null : w.Page.Id;
        var ids = new List<object>();
        foreach (var c in w.All) ids.Add(c.Id);
        s["all"] = ids;
        var convs = new Dictionary<string, object>();
        var seen = new List<Conversation>(w.All);
        if (w.Conv != null && !seen.Contains(w.Conv)) seen.Add(w.Conv);
        foreach (var c in seen)
        {
            var cd = new Dictionary<string, object>();
            cd["title"] = c.Title;
            var ms = new List<object>();
            foreach (var m in c.Messages) ms.Add(m.Role + ":" + m.Text);
            cd["messages"] = ms;
            convs[c.Id] = cd;
        }
        s["convs"] = convs;
        s["input"] = w.Input;
        s["reachable"] = w.Reachable;
        s["router_shown"] = w.RouterIsShown;
        return s;
    }

    static Dictionary<string, object> RunOne(Dictionary<string, object> k, bool original, string workDir)
    {
        string id = Convert.ToString(k["id"]);
        string cmdDir = Path.Combine(workDir, original ? "original" : "extracted", id);
        Directory.CreateDirectory(cmdDir);
        var outp = new Dictionary<string, object>();

        // A locked status.json: held open with no sharing for the whole run, as a writer
        // mid-replace would. Both sides must treat it as "no fleet".
        FileStream hold = null;
        if (Bool(k, "status_locked", false))
            hold = new FileStream(Str(k, "status_path", null), FileMode.Open, FileAccess.ReadWrite, FileShare.None);
        try
        {
            // 1. the send itself
            var w = MakeWorld(k, cmdDir);
            string entry = Str(k, "entry", "DoSend");
            string error = null;
            try
            {
                if (original)
                {
                    var o = new OriginalChat(w, w.StatusPathForOracle, ConvsPathBeside(w.StatusPath));
                    if (entry == "SendText") o.SendText(Str(k, "send_text", "")); else o.DoSend();
                }
                else
                {
                    if (entry == "SendText") ChatSend.SendText(w, Str(k, "send_text", "")); else ChatSend.DoSend(w);
                }
            }
            catch (Exception ex) { error = ex.GetType().Name; }
            outp["trace"] = w.Trace;
            outp["state"] = Snapshot(w);
            outp["error"] = error;

            // 2. the three status decisions, each on a fresh world so their reads stay out of
            //    the send's trace
            var dw = MakeWorld(k, cmdDir);
            var dec = new Dictionary<string, object>();
            int[] fs; string live; int active;
            if (original)
            {
                var o = new OriginalChat(dw, dw.StatusPathForOracle, ConvsPathBeside(dw.StatusPath));
                fs = o.FleetState();
                live = o.LiveWorkerFor(dw.Conv);
                active = o.ReadActiveFleetWorkerCount();
            }
            else
            {
                fs = ChatSend.FleetState(dw);
                live = ChatSend.LiveWorkerFor(dw, dw.Conv);
                active = ChatSend.ActiveWorkerCount(Path.Combine(Path.GetDirectoryName(ConvsPathBeside(dw.StatusPath)), "status.json"));
            }
            dec["fleet_state"] = new List<object> { fs[0], fs[1], fs[2] };
            dec["live_worker"] = live;
            dec["active_count"] = active;
            dec["reads"] = dw.Trace.Count;
            outp["decisions"] = dec;
        }
        finally { if (hold != null) hold.Dispose(); }
        return outp;
    }

    // The window found status.json beside conversations.json for its chip; give both sides the
    // same arrangement. A case with no status file still gets a directory that has none.
    static string ConvsPathBeside(string statusPath)
    {
        string dir = string.IsNullOrEmpty(statusPath) ? Path.GetTempPath() : Path.GetDirectoryName(statusPath);
        return Path.Combine(dir, "conversations.json");
    }

    static int Main(string[] argv)
    {
        if (argv.Length != 3) { Console.Error.WriteLine("usage: harness <cases.json> <results.json> <workdir>"); return 2; }
        var cases = Js.DeserializeObject(File.ReadAllText(argv[0], new UTF8Encoding(false))) as object[];
        if (cases == null) { Console.Error.WriteLine("cases.json is not a JSON array"); return 2; }
        var results = new Dictionary<string, object>();
        foreach (object co in cases)
        {
            var k = (Dictionary<string, object>)co;
            string id = Convert.ToString(k["id"]);
            if (results.ContainsKey(id)) { Console.Error.WriteLine("duplicate case id " + id); return 2; }
            var r = new Dictionary<string, object>();
            r["original"] = RunOne(k, true, argv[2]);
            r["extracted"] = RunOne(k, false, argv[2]);
            results[id] = r;
        }
        File.WriteAllText(argv[1], Js.Serialize(results), new UTF8Encoding(false));
        Console.Out.WriteLine("ran " + results.Count + " case(s)");
        return 0;
    }
}
